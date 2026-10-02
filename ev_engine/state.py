"""Alert de-duplication between scheduled runs.

GitHub Actions runners are fresh VMs, so the "what did we already alert?" memory is a
small JSON file that the workflow restores from / saves to the Actions cache around
each run. Without it, every 15-minute run would re-post the same edges.

A pick (event + market + line + outcome; deliberately not the book) is alerted once,
and again only if its EV improves by at least REALERT_EV_DELTA. Entries expire one
hour after the match starts, or STATE_TTL_HOURS after they were sent.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from .models import Edge

log = logging.getLogger(__name__)


class AlertState:
    VERSION = 1

    def __init__(self, path: str | os.PathLike, *, ttl_hours: float, realert_ev_delta: float):
        self.path = Path(path)
        self.ttl = timedelta(hours=ttl_hours)
        self.realert_ev_delta = realert_ev_delta
        self._alerts: dict[str, dict] = {}

    def __len__(self) -> int:
        return len(self._alerts)

    @classmethod
    def load(cls, path: str | os.PathLike, *, ttl_hours: float, realert_ev_delta: float) -> AlertState:
        """Load saved state; a missing or corrupt file just means starting fresh."""
        state = cls(path, ttl_hours=ttl_hours, realert_ev_delta=realert_ev_delta)
        try:
            raw = json.loads(state.path.read_text(encoding="utf-8"))
            alerts = raw.get("alerts", {})
            if isinstance(alerts, dict):
                state._alerts = {str(k): v for k, v in alerts.items() if isinstance(v, dict)}
            log.info("Loaded alert state: %d remembered pick(s).", len(state))
        except FileNotFoundError:
            log.info("No alert state at %s yet - starting fresh.", state.path)
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("Ignoring unreadable alert state %s (%s).", state.path, exc)
        return state

    def should_alert(self, edge: Edge) -> bool:
        previous = self._alerts.get(edge.key)
        if previous is None:
            return True
        try:
            previous_ev = float(previous.get("ev", 0.0))
        except (TypeError, ValueError):
            return True
        return edge.ev >= previous_ev + self.realert_ev_delta

    def record(self, edge: Edge, now: datetime) -> None:
        self._alerts[edge.key] = {
            "ev": round(edge.ev, 6),
            "price": edge.price,
            "book": edge.bookmaker,
            "sent_at": int(now.timestamp()),
            "start": int(edge.event.start_time.timestamp()),
        }

    def prune(self, now: datetime) -> int:
        """Forget picks whose match started over an hour ago, or that are older than the TTL."""
        started_cutoff = (now - timedelta(hours=1)).timestamp()
        sent_cutoff = (now - self.ttl).timestamp()
        stale = [
            key
            for key, entry in self._alerts.items()
            if _num(entry.get("start")) < started_cutoff or _num(entry.get("sent_at")) < sent_cutoff
        ]
        for key in stale:
            del self._alerts[key]
        return len(stale)

    def save(self) -> None:
        """Atomic write (temp file + rename) so a killed run can't leave half a file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {"version": self.VERSION, "alerts": self._alerts}
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


def _num(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
