"""The Odds API (v4) adapter for US leagues - MLB, NFL, NBA, NHL and more.

Off by default. Turn it on with ENABLED_PROVIDERS=betsapi_tt,the_odds_api and the
THE_ODDS_API_KEY secret; pick leagues with ODDS_API_SPORTS (e.g. baseball_mlb,
americanfootball_nfl).

Endpoint (docs: https://the-odds-api.com/liveapi/guides/v4/)
  GET /v4/sports/{sport}/odds?apiKey&regions&markets&oddsFormat=decimal&dateFormat=unix
      &commenceTimeFrom&commenceTimeTo

Quota: each call costs (number of markets) x (number of regions) credits. Remaining
credits come back in x-requests-remaining; ApiClient stops at ODDS_API_MIN_REMAINING.
Bursts trigger HTTP 429, so calls are spaced out and retried with backoff.

Tip: include region 'eu' so Pinnacle is in the feed, then set SHARP_BOOKS=pinnacle to
price edges against the sharpest line instead of a soft-book consensus.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from ..config import ConfigError, Settings
from ..http_client import ApiClient, ApiError, BudgetExhausted, RateLimitError, RateLimitPolicy
from ..models import MARKET_LABELS, MONEYLINE, SPREAD, TOTAL, BookMarket, Event, Outcome, RunReport
from .base import OddsProvider

log = logging.getLogger(__name__)

# Featured markets only; anything else (h2h_lay from exchanges, alternates, props) is ignored.
MARKET_TYPES = {"h2h": MONEYLINE, "spreads": SPREAD, "totals": TOTAL}


def _iso(dt: datetime) -> str:
    """The Odds API wants e.g. 2026-10-02T18:00:00Z (no fractional seconds)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class TheOddsApiProvider(OddsProvider):
    name = "the_odds_api"
    label = "The Odds API"

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        report: RunReport,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> TheOddsApiProvider:
        if not settings.the_odds_api_key:
            raise ConfigError("THE_ODDS_API_KEY is not set (needed by provider 'the_odds_api')")
        policy = RateLimitPolicy(
            max_retries=3,
            min_interval=1.0,  # 429s come from bursts, so space calls out
            call_budget=4 * len(settings.odds_api_sports) + 4,
            remaining_header="x-requests-remaining",
            min_remaining=settings.odds_api_min_remaining,
        )
        client = ApiClient("The Odds API", settings.odds_api_base_url, timeout=settings.request_timeout,
                           policy=policy, session=session, sleep=sleep)
        return cls(settings, report, client)

    def fetch_events(self, now: datetime) -> list[Event]:
        s = self.settings
        base_params = {
            "apiKey": s.the_odds_api_key,
            "regions": ",".join(s.odds_api_regions),
            "markets": ",".join(s.odds_api_markets),
            "oddsFormat": "decimal",
            "dateFormat": "unix",
            "commenceTimeFrom": _iso(now + timedelta(minutes=s.min_minutes_to_start)),
            "commenceTimeTo": _iso(now + timedelta(minutes=s.odds_api_lookahead_min)),
        }
        events: list[Event] = []
        for sport in s.odds_api_sports:
            try:
                data = self.client.get_json(f"/v4/sports/{sport}/odds", dict(base_params))
            except (RateLimitError, BudgetExhausted) as exc:
                log.warning("%s - stopping The Odds API for this run.", exc)
                self.report.skip("rate_limit", str(exc))
                break
            except ApiError as exc:
                if exc.fatal:
                    raise
                # e.g. HTTP 404/422 for a sport key that doesn't exist: surface it, keep going.
                self.report.errors.append(f"The Odds API {sport}: {exc}")
                continue
            if not isinstance(data, list):
                self.report.errors.append(f"The Odds API {sport}: unexpected response shape")
                continue
            parsed = [ev for ev in (self._parse_event(raw, sport) for raw in data) if ev is not None]
            log.info("The Odds API: %s - %d event(s) with prices", sport, len(parsed))
            events.extend(parsed)
        return events

    def _parse_event(self, raw: Any, sport: str) -> Event | None:
        if not isinstance(raw, dict):
            return None
        home, away = raw.get("home_team"), raw.get("away_team")
        start = _parse_time(raw.get("commence_time"))
        if not (raw.get("id") and home and away and start):
            return None
        home, away = str(home), str(away)

        markets: list[BookMarket] = []
        for bk in raw.get("bookmakers") or []:
            if not isinstance(bk, dict):
                continue
            key = str(bk.get("key") or "")
            title = str(bk.get("title") or key)
            for mk in bk.get("markets") or []:
                if not isinstance(mk, dict):
                    continue
                market = MARKET_TYPES.get(mk.get("key"))
                if market is None:
                    continue
                updated = _parse_time(mk.get("last_update")) or _parse_time(bk.get("last_update"))
                try:
                    markets.append(self._book_market(title, key, market, mk.get("outcomes"), home, away, updated))
                except ValueError as exc:
                    self.report.skip("bad_price", f"{home} vs {away} · {title} · {MARKET_LABELS[market]}: {exc}")

        if not markets:
            self.report.skip("no_odds", f"{home} vs {away}: no usable prices")
            return None
        title = str(raw.get("sport_title") or sport)
        return Event(provider=self.name, source=self.label, event_id=str(raw["id"]), sport=title, league=title,
                     home=home, away=away, start_time=start, markets=tuple(markets))

    @staticmethod
    def _book_market(title: str, key: str, market: str, raw_outcomes: Any, home: str, away: str,
                     updated: datetime | None) -> BookMarket:
        if not isinstance(raw_outcomes, list) or len(raw_outcomes) < 2:
            raise ValueError("incomplete market")
        outcomes: list[Outcome] = []
        points: dict[str, float | None] = {}
        for raw in raw_outcomes:
            name = str(raw.get("name") or "") if isinstance(raw, dict) else ""
            price = _num(raw.get("price")) if isinstance(raw, dict) else None
            if not name or price is None or price <= 1.0:
                raise ValueError(f"invalid price for {name or '?'}")
            outcomes.append(Outcome(name, price))
            points[name] = _num(raw.get("point"))
        names = [o.name for o in outcomes]
        if len(set(names)) != len(names):
            raise ValueError("duplicate outcome names")

        line: float | None = None
        if market == SPREAD:
            if set(names) != {home, away}:
                raise ValueError("spread outcomes don't match the two teams")
            home_pt, away_pt = points[home], points[away]
            if home_pt is None or away_pt is None or abs(home_pt + away_pt) > 1e-9:
                raise ValueError(f"mismatched spread points {home_pt}/{away_pt}")
            line = home_pt
        elif market == TOTAL:
            if {n.lower() for n in names} != {"over", "under"}:
                raise ValueError("totals need exactly Over and Under")
            totals = set(points.values())
            if len(totals) != 1 or None in totals:
                raise ValueError("Over and Under quote different totals")
            line = totals.pop()
            outcomes = [Outcome(o.name.title(), o.price) for o in outcomes]
        return BookMarket(bookmaker=title, market=market, line=line, outcomes=tuple(outcomes),
                          updated_at=updated, book_id=key)
