"""Accuracy tracker: log alerts, grade them after the match, keep the report card current.

Runs at the end of every scan (never in dry runs), against the ledger folder the workflow
checked out (LEDGER_DIR):

1. log_alerts(): every alert Discord accepted becomes a 1-unit pick at the alerted price.
2. watch():      every match the scan priced goes on the pending list, alert or not, so the
                 fair line can be calibrated on all of them.
3. settle():     about SETTLE_AFTER_MIN after a match starts, fetch its result and every book's
                 closing price (2 BetsAPI calls per ~10 matches + 1 per match, capped by
                 TRACKER_MAX_CALLS), grade its picks (result, profit, CLV) and write one
                 calibration row per market line. Cancelled / retired matches void their picks.
4. finish():     rebuild REPORT.md and, once a week (REPORT_DAY / REPORT_HOUR_UTC) or when
                 asked (REPORT_NOW), post the report card to Discord.

Nothing here can stop the scan or its alerts: the runner calls it last and contains errors.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from ..config import REPORT_DAYS, Settings
from ..http_client import ApiError, BudgetExhausted, RateLimitError
from ..models import Edge, Event, MatchResult
from ..providers.base import OddsProvider
from .grading import calibration_rows, closing_lines, closing_value, grade, profit
from .ledger import PENDING, Ledger, iso, match_key, num, parse_iso
from .scorecard import Scorecard, build_scorecard, discord_embed, format_prices, markdown, run_summary

log = logging.getLogger(__name__)

TITLE = "TT Cup accuracy tracker"


def _fmt(value: float | None, digits: int) -> str:
    return "" if value is None else f"{value:.{digits}f}"


class Tracker:
    def __init__(self, settings: Settings, ledger: Ledger, now: datetime):
        self.settings = settings
        self.ledger = ledger
        self.now = now
        self.calls = 0
        self.logged = 0
        self.picks_graded = 0
        self.matches_graded = 0
        self.voided = 0
        self.gave_up = 0
        self.sources: Counter = Counter()
        self.notes: list[str] = []
        self.card: Scorecard | None = None
        self.reported = False

    @classmethod
    def open(cls, settings: Settings, now: datetime) -> Tracker | None:
        """The tracker for this run, or None when it's off (no ledger, dry run, diagnostics)."""
        if settings.ledger_dir is None or settings.dry_run or settings.inspect_odds or settings.send_test_alert:
            return None
        if not settings.ledger_dir.is_dir():
            log.warning("Tracker off: LEDGER_DIR %s is not a folder.", settings.ledger_dir)
            return None
        ledger = Ledger.load(settings.ledger_dir)
        if "started_at" not in ledger.meta:
            ledger.touch_meta(started_at=iso(now))
        return cls(settings, ledger, now)

    # -- 1 + 2 ----------------------------------------------------------------------------
    def log_alerts(self, edges: Iterable[Edge]) -> None:
        for edge in edges:
            self.ledger.add_pick(edge, self.now)
            self.logged += 1

    def watch(self, events: Iterable[Event]) -> None:
        for event in events:
            self.ledger.watch(event)

    # -- 3 --------------------------------------------------------------------------------
    def settle(self, providers: Iterable[OddsProvider]) -> None:
        s = self.settings
        if requeued := self.ledger.requeue_unsettled_picks():
            self.notes.append(f"re-queued {requeued} ungraded match(es)")
        graders = {p.name: p for p in providers if p.supports_grading}
        ready_at = self.now - timedelta(minutes=s.settle_after_min)
        ready = sorted((m for m in self.ledger.pending.values() if m.get("start", 0) <= ready_at.timestamp()),
                       key=lambda m: m.get("start", 0))

        for provider_name in dict.fromkeys(m.get("provider", "") for m in ready):
            matches = [m for m in ready if m.get("provider") == provider_name]
            provider = graders.get(provider_name)
            if provider is None:  # e.g. a feed that can't grade: give up on time, never block the rest
                for match in matches:
                    if self._overdue(match):
                        self._give_up(match)
                continue
            for i in range(0, len(matches), provider.results_per_call):
                if self.calls >= s.tracker_max_calls:
                    self.notes.append(f"call cap reached (TRACKER_MAX_CALLS={s.tracker_max_calls}); "
                                      "the rest waits for the next run")
                    return
                chunk = matches[i:i + provider.results_per_call]
                try:
                    self.calls += 1
                    results = provider.fetch_results([m["event_id"] for m in chunk])
                except (RateLimitError, BudgetExhausted) as exc:
                    self.notes.append(f"stopped early: {exc}")
                    return
                except ApiError as exc:
                    self.notes.append(f"results request failed: {exc}")
                    return
                for match in chunk:
                    if not self._resolve(provider, match, results.get(str(match["event_id"]))):
                        return  # out of calls or rate-limited: the next run carries on

    def _overdue(self, match: dict) -> bool:
        start = datetime.fromtimestamp(match.get("start", 0), tz=timezone.utc)
        return self.now - start > timedelta(hours=self.settings.settle_give_up_hours)

    def _resolve(self, provider: OddsProvider, match: dict, result: MatchResult | None) -> bool:
        """Grade one match if its result is in. False = stop settling for this run."""
        key = match_key(match["provider"], str(match["event_id"]))
        if result is not None and result.status == "void":
            for row in self._open_picks(key):
                self.ledger.update_pick(row, status="void", score=result.detail, profit="0.00",
                                        settled_at=iso(self.now))
            self.ledger.done(key)
            self.voided += 1
            return True
        if result is None or result.status != "ended":
            if self._overdue(match):
                self._give_up(match)
            return True

        event = Event(provider=match["provider"], source=provider.label, event_id=str(match["event_id"]),
                      sport="", league=match.get("league", ""), home=match.get("home", ""),
                      away=match.get("away", ""),
                      start_time=datetime.fromtimestamp(match.get("start", 0), tz=timezone.utc))
        lines = []
        if self.calls >= self.settings.tracker_max_calls:
            if not self._overdue(match):
                return False
        else:
            try:
                self.calls += 1
                markets, sources = provider.closing_markets(event)
                self.sources.update(sources)
                lines = closing_lines(markets, self.settings.devig_method, self.settings.max_overround)
            except (RateLimitError, BudgetExhausted) as exc:
                self.notes.append(f"stopped early: {exc}")
                if not self._overdue(match):
                    return False
            except ApiError as exc:
                self.notes.append(f"closing odds failed for {event.matchup}: {exc}")
                if not self._overdue(match):
                    return True  # retry this match next run, keep going with the others

        for row in self._open_picks(key):
            self._grade_pick(row, result, lines)
        for sample in calibration_rows(lines, result, event.home, event.away):
            self.ledger.add_line({
                "start": iso(event.start_time), "provider": event.provider, "event_id": event.event_id,
                "league": event.league, "home": event.home, "away": event.away, "market": sample["market"],
                "line": "" if sample["line"] is None else f"{sample['line']:g}", "side": sample["side"],
                "fair_pct": f"{sample['fair'] * 100:.2f}", "n_books": str(sample["n_books"]),
                "prices": format_prices(sample["prices"]),
                "result": "" if sample["result"] is None else str(sample["result"]),
                "score": result.score, "settled_at": iso(self.now),
            })
        self.ledger.done(key)
        self.matches_graded += 1
        return True

    def _open_picks(self, key: str) -> list[dict[str, str]]:
        return [row for row in self.ledger.picks_for(key) if row.get("status") == PENDING]

    def _grade_pick(self, row: dict[str, str], result: MatchResult, lines: list) -> None:
        line = num(row.get("line")) if row.get("line") else None
        price = num(row.get("price")) or 0.0
        status = grade(row.get("market", ""), line, row.get("outcome", ""), row.get("home", ""),
                       row.get("away", ""), result) or "unknown"
        cv = closing_value(row.get("market", ""), line, row.get("outcome", ""), row.get("book", ""), price, lines)
        self.ledger.update_pick(
            row, status=status, score=result.score, profit=f"{profit(status, price):.2f}",
            close_fair_odds=_fmt(cv.close_fair_odds, 3), clv_pct=_fmt(None if cv.clv is None else cv.clv * 100, 2),
            close_price=_fmt(cv.close_price, 3), close_books="+".join(cv.close_books), settled_at=iso(self.now),
        )
        self.picks_graded += 1

    def _give_up(self, match: dict) -> None:
        key = match_key(match["provider"], str(match["event_id"]))
        for row in self._open_picks(key):
            self.ledger.update_pick(row, status="unknown", score="no result", settled_at=iso(self.now))
        self.ledger.done(key)
        self.gave_up += 1

    # -- 4 --------------------------------------------------------------------------------
    def report_due(self) -> bool:
        s = self.settings
        if s.report_now:
            return True
        if s.report_day == "off":
            return False
        weekday = REPORT_DAYS.index(s.report_day)
        slot = self.now.replace(hour=s.report_hour_utc, minute=0, second=0, microsecond=0)
        slot -= timedelta(days=(self.now.weekday() - weekday) % 7)
        if slot > self.now:
            slot -= timedelta(days=7)
        meta = self.ledger.meta
        last = parse_iso(meta.get("last_report_at", "")) or parse_iso(meta.get("started_at", ""))
        return last is not None and slot > last

    def finish(self, post_embed=None) -> list[str]:
        """Save the ledger, refresh REPORT.md and post the report card when it's due.

        post_embed(embed) -> error message or None. Returns the ledger files written.
        """
        meta = self.ledger.meta
        self.card = build_scorecard(
            list(self.ledger.iter_picks()), list(self.ledger.iter_lines()), now=self.now,
            devig_method=self.settings.devig_method, max_overround=self.settings.max_overround,
            pending=len(self.ledger.pending), since=(meta.get("started_at") or "")[:10],
        )
        if post_embed is not None and self.report_due():
            url = f"{self.settings.ledger_url.rstrip('/')}/blob/main/REPORT.md" if self.settings.ledger_url else ""
            error = post_embed(discord_embed(self.card, title=f"📊 {TITLE}", url=url))
            if error:
                self.notes.append(f"report card not posted: {error}")
            else:
                self.reported = True
                self.ledger.touch_meta(last_report_at=iso(self.now))
        report_missing = not (self.ledger.root / "REPORT.md").exists()
        written = [str(p) for p in self.ledger.save()]
        if written or report_missing:
            path = self.ledger.write_report(markdown(self.card, title=TITLE))
            if path is not None:
                written.append(str(path))
        return written

    def summary(self) -> str:
        parts = [f"logged {self.logged} alert(s)",
                 f"graded {self.picks_graded} alert(s) and {self.matches_graded} match(es)"]
        if self.voided:
            parts.append(f"{self.voided} voided")
        if self.gave_up:
            parts.append(f"{self.gave_up} without a result after {self.settings.settle_give_up_hours:g}h")
        if self.sources:
            parts.append("closing prices from " + ", ".join(f"{k} ×{v}" for k, v in self.sources.most_common()))
        parts.append(f"{self.calls} BetsAPI call(s)")
        if self.reported:
            parts.append("report card posted to Discord")
        if self.card is not None:
            parts.append(run_summary(self.card))
        parts += self.notes
        return " · ".join(parts)
