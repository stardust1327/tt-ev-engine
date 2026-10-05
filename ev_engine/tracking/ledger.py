"""The ledger: plain CSV/JSON files in a private Git repo that the workflow checks out.

    picks/2026-10.csv             every Discord alert (one row each), graded in place later
    lines/2026-10/2026-10-05.csv  every priced match's closing fair line + result (calibration)
    state/pending.json            matches seen by the scanner and not graded yet
    state/meta.json               report bookkeeping
    REPORT.md                     the live report card

Everything is human-readable, so the repo doubles as a spreadsheet you can open anywhere.
The workflow commits the folder after each run; a run whose push fails is simply redone
by the next one, because the pending list is part of the same commit as the graded rows.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from ..models import Edge, Event, format_line

log = logging.getLogger(__name__)

PICK_FIELDS = (
    "sent_at", "start", "provider", "event_id", "league", "home", "away", "market", "line", "pick",
    "outcome", "book", "price", "fair_odds", "ev_pct", "price_age_min", "alert_n", "reference",
    "status", "score", "profit", "close_fair_odds", "clv_pct", "close_price", "close_books", "settled_at",
)
LINE_FIELDS = (
    "start", "provider", "event_id", "league", "home", "away", "market", "line", "side",
    "fair_pct", "n_books", "prices", "result", "score", "settled_at",
)
PENDING = "pending"


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def num(text: object) -> float | None:
    """CSV cell -> float, or None for blanks and junk."""
    try:
        value = float(str(text))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _line_text(line: float | None) -> str:
    return "" if line is None else format_line(line)


def match_key(provider: str, event_id: str) -> str:
    return f"{provider}:{event_id}"


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    except FileNotFoundError:
        return []


def _write_text(path: Path, text: str) -> None:
    """Atomic write (temp file + rename): a killed run never leaves half a file to commit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _csv_text(fields: tuple[str, ...], rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fields})
    return buf.getvalue()


class Ledger:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.picks: dict[str, list[dict[str, str]]] = {}   # month ("2026-10") -> rows
        self.lines: dict[str, list[dict[str, str]]] = {}   # day ("2026-10-05") -> rows
        self.pending: dict[str, dict] = {}
        self.meta: dict = {}
        self._dirty_picks: set[str] = set()
        self._dirty_lines: set[str] = set()
        self._dirty_state = False

    # -- loading ------------------------------------------------------------------------
    @classmethod
    def load(cls, root: Path) -> Ledger:
        ledger = cls(root)
        for path in sorted((ledger.root / "picks").glob("*.csv")):
            ledger.picks[path.stem] = _read_csv(path)
        for path in sorted((ledger.root / "lines").glob("*/*.csv")):
            ledger.lines[path.stem] = _read_csv(path)
        ledger.pending = ledger._read_json("pending.json").get("matches", {})
        ledger.meta = ledger._read_json("meta.json")
        return ledger

    def _read_json(self, name: str) -> dict:
        path = self.root / "state" / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("Ledger: ignoring unreadable %s (%s).", path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    # -- picks --------------------------------------------------------------------------
    def add_pick(self, edge: Edge, now: datetime) -> dict[str, str]:
        """Record one delivered alert as a 1-unit bet at the alerted price."""
        alert_n = 1 + sum(1 for row in self.iter_picks() if _pick_key(row) == edge.key)
        age = "" if edge.updated_at is None else f"{max(0.0, (now - edge.updated_at).total_seconds() / 60):.1f}"
        row = {
            "sent_at": iso(now),
            "start": iso(edge.event.start_time),
            "provider": edge.event.provider,
            "event_id": edge.event.event_id,
            "league": edge.event.league,
            "home": edge.event.home,
            "away": edge.event.away,
            "market": edge.market,
            "line": _line_text(edge.line),
            "pick": edge.selection,
            "outcome": edge.outcome,
            "book": edge.bookmaker,
            "price": f"{edge.price:.3f}",
            "fair_odds": f"{edge.fair_odds:.3f}",
            "ev_pct": f"{edge.ev * 100:.2f}",
            "price_age_min": age,
            "alert_n": str(alert_n),
            "reference": edge.reference,
            "status": PENDING,
        }
        month = now.astimezone(timezone.utc).strftime("%Y-%m")
        self.picks.setdefault(month, []).append(row)
        self._dirty_picks.add(month)
        self.watch(edge.event)  # its match must get graded even if the scan never lists it again
        return row

    def iter_picks(self) -> Iterator[dict[str, str]]:
        for month in sorted(self.picks):
            yield from self.picks[month]

    def picks_for(self, key: str) -> list[dict[str, str]]:
        return [row for row in self.iter_picks() if match_key(row.get("provider", ""), row.get("event_id", "")) == key]

    def update_pick(self, row: dict[str, str], **values: str) -> None:
        row.update(values)
        for month, rows in self.picks.items():
            if any(r is row for r in rows):
                self._dirty_picks.add(month)
                return

    # -- matches waiting for a result ---------------------------------------------------
    def watch(self, event: Event, books: list[str] | None = None) -> bool:
        """Remember a match so it gets graded after it ends. True if it was new.

        books: the books with fresh, usable prices in this scan. The latest scan before the
        start wins; only those books count toward the match's closing fair line.
        """
        key = match_key(event.provider, event.event_id)
        start = int(event.start_time.timestamp())
        if key in self.pending:
            entry = self.pending[key]
            if entry.get("start") != start:  # rescheduled: wait for the new start time
                entry["start"] = start
                self._dirty_state = True
            if books is not None and entry.get("books") != books:
                entry["books"] = books
                self._dirty_state = True
            return False
        self.pending[key] = {
            "provider": event.provider,
            "event_id": event.event_id,
            "start": start,
            "league": event.league,
            "home": event.home,
            "away": event.away,
        }
        if books is not None:
            self.pending[key]["books"] = books
        self._dirty_state = True
        return True

    def done(self, key: str) -> None:
        if self.pending.pop(key, None) is not None:
            self._dirty_state = True

    def requeue_unsettled_picks(self) -> int:
        """Put back any pick whose match fell out of the pending list (e.g. a lost commit)."""
        added = 0
        for row in self.iter_picks():
            if row.get("status") != PENDING:
                continue
            key = match_key(row.get("provider", ""), row.get("event_id", ""))
            start = parse_iso(row.get("start", ""))
            if key in self.pending or start is None:
                continue
            self.pending[key] = {"provider": row.get("provider", ""), "event_id": row.get("event_id", ""),
                                 "start": int(start.timestamp()), "league": row.get("league", ""),
                                 "home": row.get("home", ""), "away": row.get("away", "")}
            self._dirty_state = True
            added += 1
        return added

    # -- calibration lines ----------------------------------------------------------------
    def add_line(self, row: dict[str, str]) -> bool:
        """Append one graded closing line; ignores a repeat of the same match + market line."""
        start = parse_iso(row.get("start", ""))
        day = (start or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        rows = self.lines.setdefault(day, [])
        ident = (row.get("provider"), row.get("event_id"), row.get("market"), row.get("line"))
        if any((r.get("provider"), r.get("event_id"), r.get("market"), r.get("line")) == ident for r in rows):
            return False
        rows.append(row)
        self._dirty_lines.add(day)
        return True

    def iter_lines(self) -> Iterator[dict[str, str]]:
        for day in sorted(self.lines):
            yield from self.lines[day]

    # -- saving ---------------------------------------------------------------------------
    @property
    def changed(self) -> bool:
        return bool(self._dirty_picks or self._dirty_lines or self._dirty_state)

    def touch_meta(self, **values: object) -> None:
        self.meta.update(values)
        self._dirty_state = True

    def save(self) -> list[Path]:
        """Write every changed file. Returns the paths written."""
        written: list[Path] = []
        for month in sorted(self._dirty_picks):
            path = self.root / "picks" / f"{month}.csv"
            _write_text(path, _csv_text(PICK_FIELDS, self.picks[month]))
            written.append(path)
        for day in sorted(self._dirty_lines):
            path = self.root / "lines" / day[:7] / f"{day}.csv"
            _write_text(path, _csv_text(LINE_FIELDS, self.lines[day]))
            written.append(path)
        if self._dirty_state:
            pending = dict(sorted(self.pending.items(), key=lambda kv: (kv[1].get("start", 0), kv[0])))
            for name, payload in (("pending.json", {"version": 1, "matches": pending}), ("meta.json", self.meta)):
                path = self.root / "state" / name
                _write_text(path, json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
                written.append(path)
        self._dirty_picks.clear()
        self._dirty_lines.clear()
        self._dirty_state = False
        return written

    def write_report(self, markdown: str) -> Path | None:
        path = self.root / "REPORT.md"
        try:
            if path.read_text(encoding="utf-8") == markdown:
                return None
        except FileNotFoundError:
            pass
        _write_text(path, markdown)
        return path


def _pick_key(row: dict[str, str]) -> str:
    """The same identity as Edge.key, rebuilt from a ledger row (counts re-alerts)."""
    line = row.get("line") or "-"
    return f"{row.get('provider')}:{row.get('event_id')}:{row.get('market')}:{line}:{row.get('outcome')}"
