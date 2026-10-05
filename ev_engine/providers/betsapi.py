"""BetsAPI adapter: upcoming TT Cup table-tennis matches + multi-book odds.

Endpoints (docs: https://betsapi.com/docs/)
  GET /v3/events/upcoming?sport_id=92[&league_id=][&page=]   upcoming fixtures, 50 per page
  GET /v2/event/odds/summary?event_id=                       per-bookmaker odds snapshots
  GET /v1/event/view?event_id=1,2,3                          final scores, up to 10 events per call
                                                             (accuracy tracker only)

Odds-summary market keys for table tennis (sport_id 92)
  92_1  match winner  -> home_od / away_od
  92_2  handicap      -> handicap (home side's line) + home_od / away_od
  92_3  total         -> handicap (the total) + over_od / under_od

Details that matter for correctness
  * Every request counts against the hourly allowance (3,600/h by default). The remaining
    allowance comes back in X-RateLimit-Remaining and ApiClient stops at a safety floor.
  * Errors come back as {"success": 0, "error": "TOO_MANY_REQUESTS" | "AUTHORIZE_FAILED" | ...}.
  * matching_dir == -1 means that bookmaker lists the players the other way round; its
    home/away prices are swapped back here (and handicap lines flipped).
  * odds_update[market] is the last time BetsAPI confirmed the price - used for staleness.
  * BetsAPI's coverage table lists no Pinnacle odds, so the TT Cup fair line comes from a
    consensus of the books BetsAPI does carry (see analyzer.py).
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from ..config import ConfigError, Settings
from ..http_client import ApiClient, ApiError, BudgetExhausted, RateLimitError, RateLimitPolicy
from ..models import (
    MARKET_LABELS,
    MONEYLINE,
    SPREAD,
    TOTAL,
    BookMarket,
    Event,
    MatchResult,
    Outcome,
    RunReport,
)
from .base import OddsProvider

log = logging.getLogger(__name__)

SPORT_ID = "92"  # table tennis
MARKET_TYPES = {"92_1": MONEYLINE, "92_2": SPREAD, "92_3": TOTAL}
SNAPSHOTS = ("end", "kickoff", "start")  # odds-summary snapshots; we take the newest pre-match one
# time_status codes (BetsAPI glossary). 3 = Ended; these mean the match won't be played out,
# so bets on it are refunded: postponed, cancelled, walkover, interrupted, abandoned,
# retired, suspended, decided by FA, disqualified, removed.
ENDED_STATUS = "3"
VOID_STATUSES = {
    "4": "Postponed", "5": "Cancelled", "6": "Walkover", "7": "Interrupted", "8": "Abandoned",
    "9": "Retired", "10": "Suspended", "11": "Decided by FA", "12": "Disqualified", "99": "Removed",
}
RESULTS_PER_CALL = 10  # /v1/event/view takes up to 10 comma-separated event ids
# Error codes (BetsAPI glossary, R-Errors) that retrying won't fix -> fail the run loudly.
FATAL_ERRORS = frozenset(
    {"AUTHORIZE_FAILED", "PERMISSION_DENIED", "PARAM_REQUIRED", "PARAM_INVALID", "METHOD_NOT_ALLOWED"}
)
DEFAULT_PER_PAGE = 50


def parse_price(raw: Any) -> float:
    """BetsAPI prices are strings: '1.833', '2', 'EVS' (evens = 2.0). '0.00' means suspended."""
    if raw is None or str(raw).strip() == "":
        raise ValueError("missing price")
    text = str(raw).strip().upper()
    if text in {"EVS", "EVEN", "EVENS"}:
        return 2.0
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"unreadable price {raw!r}") from None
    if not math.isfinite(value) or value <= 1.0:
        raise ValueError(f"unusable price {raw!r} (suspended or invalid)")
    return value


def parse_line(raw: Any) -> float | None:
    """A single handicap/total -> float. Split Asian lines ('2.5,3.0', '0-0.5', '2.5/3') -> None."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or "," in text or "/" in text or "-" in text[1:]:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(raw: Any) -> int | None:
    try:
        return int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return None


def _ago(ts: int, now: datetime) -> str:
    minutes = (now.timestamp() - ts) / 60.0
    if minutes < 0:
        return f"{-minutes:.0f}m in the future"
    return f"{minutes:.0f}m ago" if minutes < 120 else f"{minutes / 60:.1f}h ago"


def _closing_record(snapshots: dict, key: str, start_ts: float) -> tuple[dict | None, str]:
    """(record, snapshot name) holding a book's closing price for one market, or (None, "")."""
    def record_in(name: str) -> dict | None:
        snapshot = snapshots.get(name)
        record = snapshot.get(key) if isinstance(snapshot, dict) else None
        return record if isinstance(record, dict) and _to_int(record.get("add_time")) else None

    if (kickoff := record_in("kickoff")) is not None:
        return kickoff, "kickoff"
    end = record_in("end")
    if end is not None:
        pre_match = _to_int(end.get("add_time")) <= start_ts and end.get("ss") in (None, "")
        return (end, "last pre-match") if pre_match else (None, "")
    start = record_in("start")  # no later record at all: the opening price never changed
    return (start, "opening (unchanged)") if start is not None else (None, "")


def _games(raw: Any) -> tuple[tuple[int, int], ...]:
    """{"1": {"home": "11", "away": "7"}, ...} -> ((11, 7), ...) in game order; () if unreadable."""
    if not isinstance(raw, dict):
        return ()
    games = []
    for number in sorted(raw, key=lambda k: _to_int(k) or 0):
        game = raw[number]
        home = _to_int(game.get("home")) if isinstance(game, dict) else None
        away = _to_int(game.get("away")) if isinstance(game, dict) else None
        if _to_int(number) is None or home is None or away is None:
            return ()
        games.append((home, away))
    return tuple(games)


def _parse_result(raw: Any) -> MatchResult | None:
    """One /v1/event/view result -> MatchResult (games won from "ss", points per game from "scores")."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    event_id = str(raw["id"])
    status = str(raw.get("time_status", ""))
    if status in VOID_STATUSES:
        return MatchResult(event_id, "void", detail=VOID_STATUSES[status])
    games = _games(raw.get("scores"))
    home = away = None
    ss = str(raw.get("ss") or "")
    if "-" in ss:
        left, _, right = ss.partition("-")
        home, away = _to_int(left.strip()), _to_int(right.strip())
    if (home is None or away is None) and games:  # no "ss": count the games each player won
        home = sum(1 for h, a in games if h > a)
        away = sum(1 for h, a in games if a > h)
    if status != ENDED_STATUS or home is None or away is None or home == away:
        return MatchResult(event_id, "pending", detail=f"time_status {status or '?'}")
    return MatchResult(event_id, "ended", home, away, games, detail="Ended")


def _price_key(record: dict) -> tuple:
    """A record's prices and line, comparable across '1.9' / '1.900' formatting."""
    def norm(value: Any) -> Any:
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            return value
    return tuple(norm(record.get(f)) for f in ("home_od", "away_od", "over_od", "under_od", "handicap"))


def _name(obj: Any) -> str | None:
    return str(obj["name"]).strip() if isinstance(obj, dict) and obj.get("name") else None


class BetsApiTableTennisProvider(OddsProvider):
    name = "betsapi_tt"
    label = "BetsAPI"
    supports_grading = True
    results_per_call = RESULTS_PER_CALL

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        report: RunReport,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> BetsApiTableTennisProvider:
        if not settings.betsapi_token:
            raise ConfigError("BETSAPI_TOKEN is not set (needed by provider 'betsapi_tt')")
        policy = RateLimitPolicy(
            max_retries=3,
            min_interval=0.2,  # at most ~5 calls/s - gentle on a 3,600/hour allowance
            call_budget=settings.betsapi_max_calls,
            remaining_header="X-RateLimit-Remaining",
            min_remaining=settings.betsapi_min_remaining,
        )
        client = ApiClient("BetsAPI", settings.betsapi_base_url, timeout=settings.request_timeout,
                           policy=policy, session=session, sleep=sleep)
        return cls(settings, report, client)

    # -- HTTP -------------------------------------------------------------------------
    def _call(self, path: str, **params: Any) -> dict:
        """GET a BetsAPI endpoint and unwrap its {"success": 1, ...} envelope."""
        params["token"] = self.settings.betsapi_token
        data = self.client.get_json(path, params)
        if not isinstance(data, dict):
            raise ApiError(f"BetsAPI {path}: unexpected response shape")
        if str(data.get("success")) != "1":
            code = str(data.get("error") or "UNKNOWN_ERROR")
            detail = data.get("error_detail")
            message = f"BetsAPI {path}: {code}" + (f" ({detail})" if detail else "")
            if code == "TOO_MANY_REQUESTS":
                raise RateLimitError(message)
            raise ApiError(message, fatal=code in FATAL_ERRORS)
        return data

    # -- pipeline ---------------------------------------------------------------------
    def fetch_events(self, now: datetime) -> list[Event]:
        try:
            fixtures = self._upcoming_fixtures(now)
        except (RateLimitError, BudgetExhausted) as exc:
            log.warning("%s - skipping BetsAPI this run; the next scheduled run will retry.", exc)
            self.report.skip("rate_limit", str(exc))
            return []

        limit = self.settings.betsapi_max_events
        if len(fixtures) > limit:
            log.info("BetsAPI: scanning the %d soonest of %d fixtures (BETSAPI_MAX_EVENTS).", limit, len(fixtures))

        events: list[Event] = []
        for index, fx in enumerate(fixtures[:limit]):
            label = f"{fx['home']} vs {fx['away']}"
            try:
                data = self._call("/v2/event/odds/summary", event_id=fx["id"])
            except (RateLimitError, BudgetExhausted) as exc:
                left = min(len(fixtures), limit) - index
                log.warning("%s - %d fixture(s) left unscanned; the next run continues.", exc, left)
                self.report.skip("rate_limit", f"{left} fixture(s) not scanned: {exc}")
                break
            except ApiError as exc:
                if exc.fatal:
                    raise
                self.report.skip("api_error", f"{label}: odds request failed ({exc})")
                continue

            markets = self._parse_summary(data.get("results"), fx)
            if not markets:
                self.report.skip("no_odds", f"{label}: no usable pre-match prices from any bookmaker")
                continue
            events.append(
                Event(
                    provider=self.name,
                    source=self.label,
                    event_id=fx["id"],
                    sport="Table Tennis",
                    league=fx["league"],
                    home=fx["home"],
                    away=fx["away"],
                    start_time=fx["start"],
                    markets=tuple(markets),
                )
            )
        return events

    # -- accuracy tracker ---------------------------------------------------------------
    def fetch_results(self, event_ids: Sequence[str]) -> dict[str, MatchResult]:
        """Final status and score for up to 10 events in one /v1/event/view call."""
        ids = [str(i) for i in event_ids][:RESULTS_PER_CALL]
        if not ids:
            return {}
        data = self._call("/v1/event/view", event_id=",".join(ids))
        results = data.get("results")
        parsed = (_parse_result(raw) for raw in (results if isinstance(results, list) else []))
        return {r.event_id: r for r in parsed if r is not None and r.event_id in ids}

    def closing_markets(self, event: Event) -> tuple[list[BookMarket], Counter]:
        """Each book's closing prices for a finished match, from the same odds summary the scan reads.

        The closing price is the "kickoff" snapshot (the price when the match went live). Books
        without one fall back to their newest record if it was posted before the start with no
        live score - i.e. that price was still up at kickoff - and are left out otherwise rather
        than graded against an opening price.
        """
        data = self._call("/v2/event/odds/summary", event_id=event.event_id)
        results = data.get("results")
        fx = {"home": event.home, "away": event.away, "start": event.start_time}
        start_ts = event.start_time.timestamp()
        markets: list[BookMarket] = []
        sources: Counter = Counter()
        for book, payload in (results.items() if isinstance(results, dict) else ()):
            if not isinstance(payload, dict) or not isinstance(payload.get("odds"), dict):
                continue
            reversed_dir = str(payload.get("matching_dir", "1")).strip() == "-1"
            for key in self.settings.betsapi_markets:
                record, source = _closing_record(payload["odds"], key, start_ts)
                if record is None:
                    continue
                try:
                    bm = self._book_market(str(book), MARKET_TYPES[key], record, fx, reversed_dir,
                                           _to_int(record.get("add_time")))
                except ValueError:
                    continue  # suspended / missing prices at the close
                if bm is not None:
                    markets.append(bm)
                    sources[source] += 1
        return markets, sources

    def inspect(self, now: datetime, matches: int = 3) -> list[tuple[str, str]]:
        """Diagnostics: BetsAPI's raw odds summary for the next few fixtures, one line per bookmaker.

        Shows exactly what the freshness and in-play checks see: the last-checked time
        (odds_update), every snapshot's prices, when each was posted, and any live score.
        """
        samples = []
        for fx in self._upcoming_fixtures(now)[:matches]:
            data = self._call("/v2/event/odds/summary", event_id=fx["id"])
            results = data.get("results") if isinstance(data.get("results"), dict) else {}
            lines = [f"{book}: {self._describe_raw(payload, now)}" for book, payload in sorted(results.items())]
            minutes = (fx["start"] - now).total_seconds() / 60.0
            heading = f"{fx['home']} vs {fx['away']} · {fx['league']} · event {fx['id']} · starts in {minutes:.0f} min"
            samples.append((heading, "\n".join(lines) or "no bookmakers in the odds summary"))
        return samples

    def _describe_raw(self, payload: Any, now: datetime) -> str:
        if not isinstance(payload, dict):
            return f"unexpected {type(payload).__name__}"
        checked = payload.get("odds_update") if isinstance(payload.get("odds_update"), dict) else {}
        snapshots = payload.get("odds") if isinstance(payload.get("odds"), dict) else {}
        parts = [f"matching_dir {payload.get('matching_dir')}"]
        for key in self.settings.betsapi_markets:
            ts = _to_int(checked.get(key))
            parts.append(f"{key} checked {_ago(ts, now)}" if ts else f"{key} no check time")
            for snap_name in ("start", "kickoff", "end"):
                snapshot = snapshots.get(snap_name)
                record = snapshot.get(key) if isinstance(snapshot, dict) else None
                if not isinstance(record, dict):
                    continue
                prices = "/".join(
                    str(record[f]) for f in ("home_od", "away_od", "over_od", "under_od") if record.get(f) is not None
                )
                line = f" line {record['handicap']}" if record.get("handicap") not in (None, "") else ""
                added = _to_int(record.get("add_time"))
                text = f"{snap_name} {prices or '-'}{line} posted {_ago(added, now) if added else '?'}"
                if record.get("ss") not in (None, ""):
                    text += f" ss={record['ss']!r}"
                parts.append(text)
        other = sorted({k for snap in snapshots.values() if isinstance(snap, dict) for k in snap}
                       - set(self.settings.betsapi_markets))
        if other:
            parts.append("also has " + ",".join(other[:8]))
        return " · ".join(parts)

    def _upcoming_fixtures(self, now: datetime) -> list[dict]:
        """Not-started TT Cup fixtures between now + MIN_MINUTES_TO_START and the look-ahead horizon."""
        s = self.settings
        earliest = now + timedelta(minutes=s.min_minutes_to_start)
        horizon = now + timedelta(minutes=s.betsapi_lookahead_min)
        wanted = [name.lower() for name in s.tt_leagues]
        found: dict[str, dict] = {}
        leagues_seen: dict[str, str] = {}

        for league_id in s.betsapi_league_ids or (None,):
            for page in range(1, s.betsapi_max_pages + 1):
                params: dict[str, Any] = {"sport_id": SPORT_ID, "page": page}
                if league_id:
                    params["league_id"] = league_id
                try:
                    data = self._call("/v3/events/upcoming", **params)
                except ApiError as exc:
                    # Page 1 failing is a real problem (raise). A later page failing still
                    # leaves us the soonest fixtures, which are the ones that matter most.
                    if page == 1 or exc.fatal or isinstance(exc, (RateLimitError, BudgetExhausted)):
                        raise
                    log.warning("%s - continuing with the fixtures from pages 1-%d.", exc, page - 1)
                    break
                results = data.get("results")
                if not isinstance(results, list) or not results:
                    break

                earliest_on_page: datetime | None = None
                for raw in results:
                    fx = self._parse_fixture(raw)
                    if fx is None:
                        continue
                    if earliest_on_page is None or fx["start"] < earliest_on_page:
                        earliest_on_page = fx["start"]
                    if league_id is None and not any(w in fx["league"].lower() for w in wanted):
                        continue  # a different table-tennis league
                    if fx["time_status"] != "0" or not earliest <= fx["start"] <= horizon:
                        continue  # already live, or outside the window
                    found[fx["id"]] = fx
                    leagues_seen[fx["league_id"]] = fx["league"]

                pager = data.get("pager") if isinstance(data.get("pager"), dict) else {}
                per_page = _to_int(pager.get("per_page")) or DEFAULT_PER_PAGE
                total = _to_int(pager.get("total"))
                if earliest_on_page is not None and earliest_on_page > horizon:
                    break  # results are time-ordered: everything further is beyond the horizon
                if total is not None and page * per_page >= total:
                    break

        if leagues_seen and not s.betsapi_league_ids:
            ids = ",".join(sorted(leagues_seen))
            names = ", ".join(f"{name} [{lid}]" for lid, name in sorted(leagues_seen.items()))
            log.info("BetsAPI: matched leagues %s. Tip: set BETSAPI_LEAGUE_IDS=%s to skip the sport-wide scan.",
                     names, ids)
        fixtures = sorted(found.values(), key=lambda f: f["start"])
        log.info("BetsAPI: %d fixture(s) starting %s-%s UTC", len(fixtures),
                 earliest.strftime("%H:%M"), horizon.strftime("%H:%M"))
        return fixtures

    @staticmethod
    def _parse_fixture(raw: Any) -> dict | None:
        if not isinstance(raw, dict):
            return None
        start = _to_int(raw.get("time"))
        league = raw.get("league") if isinstance(raw.get("league"), dict) else {}
        home, away = _name(raw.get("home")), _name(raw.get("away"))
        if not (raw.get("id") and start and home and away) or home == away:
            return None
        return {
            "id": str(raw["id"]),
            "start": datetime.fromtimestamp(start, tz=timezone.utc),
            "time_status": str(raw.get("time_status", "")),
            "league": str(league.get("name") or ""),
            "league_id": str(league.get("id") or ""),
            "home": home,
            "away": away,
        }

    def _parse_summary(self, results: Any, fx: dict) -> list[BookMarket]:
        """One BookMarket per bookmaker per configured market, from the newest pre-match snapshot."""
        if not isinstance(results, dict):
            return []
        start_ts = fx["start"].timestamp()
        label = f"{fx['home']} vs {fx['away']}"
        markets: list[BookMarket] = []

        for book, payload in results.items():
            if not isinstance(payload, dict):
                continue
            reversed_dir = str(payload.get("matching_dir", "1")).strip() == "-1"
            checked = payload.get("odds_update") if isinstance(payload.get("odds_update"), dict) else {}
            snapshots = payload.get("odds")
            if not isinstance(snapshots, dict):
                continue

            for key in self.settings.betsapi_markets:
                record, superseded = self._latest_pre_match(snapshots, key, start_ts)
                if record is None:
                    continue
                # Freshness = the later of "price last changed" (add_time) and "BetsAPI last checked
                # the market" (odds_update). The check time only vouches for the newest record, so
                # it is ignored when a newer (in-play) record was skipped.
                last_check = None if superseded else _to_int(checked.get(key))
                confirmed = max(filter(None, (last_check, _to_int(record.get("add_time")))), default=None)
                where = f"{label} · {book} · {MARKET_LABELS[MARKET_TYPES[key]]}"
                try:
                    bm = self._book_market(str(book), MARKET_TYPES[key], record, fx, reversed_dir, confirmed)
                except ValueError as exc:
                    self.report.skip("bad_price", f"{where}: {exc}")
                    continue
                if bm is None:
                    self.report.skip("split_line", f"{where}: split line {record.get('handicap')!r} ignored")
                    continue
                markets.append(bm)
                # Diagnostics: a book whose prices never move off the opener while others do is suspect.
                opening = snapshots.get("start")
                opening = opening.get(key) if isinstance(opening, dict) else None
                if isinstance(opening, dict) and _price_key(opening) == _price_key(record):
                    self.report.book_unmoved[str(book)] += 1
        return markets

    @staticmethod
    def _latest_pre_match(snapshots: dict, key: str, start_ts: float) -> tuple[dict | None, bool]:
        """Newest record for `key` posted before the start with no live score.

        Returns (record, superseded) where superseded=True means a newer in-play record
        exists, so BetsAPI's "last checked" time doesn't apply to the returned record.
        """
        best: dict | None = None
        best_added = -1
        newest_seen = -1
        for snap_name in SNAPSHOTS:
            snapshot = snapshots.get(snap_name)
            record = snapshot.get(key) if isinstance(snapshot, dict) else None
            if not isinstance(record, dict):
                continue
            added = _to_int(record.get("add_time"))
            if added is None:
                continue
            newest_seen = max(newest_seen, added)
            if added > start_ts or record.get("ss") not in (None, ""):
                continue  # posted after the start / carries a live score -> in-play price
            if added > best_added:
                best, best_added = record, added
        return best, newest_seen > best_added

    @staticmethod
    def _book_market(book: str, market: str, record: dict, fx: dict, reversed_dir: bool,
                     confirmed: int | None) -> BookMarket | None:
        """Raises ValueError for bad prices; returns None for split lines we can't compare."""
        line: float | None = None
        if market == TOTAL:
            line = parse_line(record.get("handicap"))
            if line is None:
                return None
            outcomes = (
                Outcome("Over", parse_price(record.get("over_od"))),
                Outcome("Under", parse_price(record.get("under_od"))),
            )
        else:
            home_price = parse_price(record.get("home_od"))
            away_price = parse_price(record.get("away_od"))
            if market == SPREAD:
                line = parse_line(record.get("handicap"))
                if line is None:
                    return None
            if reversed_dir:
                # This book lists the players the other way round: swap back and flip the line.
                home_price, away_price = away_price, home_price
                if line is not None:
                    line = -line or 0.0
            outcomes = (Outcome(fx["home"], home_price), Outcome(fx["away"], away_price))

        updated = datetime.fromtimestamp(confirmed, tz=timezone.utc) if confirmed else None
        return BookMarket(bookmaker=book, market=market, line=line, outcomes=outcomes,
                          updated_at=updated, book_id=book)
