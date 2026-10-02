"""Shared fixtures: a fixed clock, settings factory and BetsAPI payload builders."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ev_engine.config import Settings  # noqa: E402
from ev_engine.models import MONEYLINE, BookMarket, Event, Outcome  # noqa: E402

NOW = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)
WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/abcDEF_ghi-JKLmnoPQR"
BETSAPI = "https://api.b365api.com"


@pytest.fixture
def now() -> datetime:
    return NOW


def make_settings(tmp_path: Path | None = None, **overrides) -> Settings:
    base = Settings(
        betsapi_token="tok_live_1234567890",
        discord_webhook_url=WEBHOOK,
        state_path=(tmp_path or Path("/tmp")) / "state" / "alerts.json",
    )
    settings = replace(base, **overrides)
    settings.validate()
    return settings


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def market(book: str, home_price: float, away_price: float, *, minutes_old: float = 1.0,
           home: str = "Ivanov D.", away: str = "Petrov A.", now: datetime = NOW) -> BookMarket:
    from datetime import timedelta

    return BookMarket(
        bookmaker=book,
        market=MONEYLINE,
        line=None,
        outcomes=(Outcome(home, home_price), Outcome(away, away_price)),
        updated_at=now - timedelta(minutes=minutes_old),
        book_id=book,
    )


def event(*markets: BookMarket, minutes_to_start: float = 30.0, now: datetime = NOW) -> Event:
    from datetime import timedelta

    return Event(
        provider="betsapi_tt",
        source="BetsAPI",
        event_id="9001",
        sport="Table Tennis",
        league="TT Cup",
        home="Ivanov D.",
        away="Petrov A.",
        start_time=now + timedelta(minutes=minutes_to_start),
        markets=tuple(markets),
    )


# ---- BetsAPI payload builders ------------------------------------------------------
def fixture(event_id: str, minutes: float, *, league: str = "TT Cup", league_id: str = "22742",
            home: str = "Ivanov D.", away: str = "Petrov A.", time_status: str = "0", now: datetime = NOW) -> dict:
    return {
        "id": event_id,
        "sport_id": "92",
        "time": str(int(now.timestamp() + minutes * 60)),
        "time_status": time_status,
        "league": {"id": league_id, "name": league, "cc": None},
        "home": {"id": "1", "name": home, "image_id": "0", "cc": None},
        "away": {"id": "2", "name": away, "image_id": "0", "cc": None},
        "ss": None,
    }


def upcoming(*fixtures: dict, total: int | None = None) -> dict:
    return {"success": 1, "pager": {"page": 1, "per_page": 50, "total": total or len(fixtures)},
            "results": list(fixtures)}


def book_odds(home_od: str, away_od: str, *, checked_ago: float = 60, added_ago: float = 300,
              matching_dir: str = "1", now: datetime = NOW) -> dict:
    """One bookmaker entry of /v2/event/odds/summary with a single 92_1 'end' snapshot."""
    ts = int(now.timestamp())
    return {
        "matching_dir": matching_dir,
        "odds_update": {"92_1": ts - int(checked_ago)},
        "odds": {
            "start": {"92_1": {"id": "1", "home_od": "1.900", "away_od": "1.900", "ss": None,
                               "time_str": None, "add_time": str(ts - 7200)}},
            "end": {"92_1": {"id": "2", "home_od": home_od, "away_od": away_od, "ss": None,
                             "time_str": None, "add_time": str(ts - int(added_ago))}},
        },
    }


def summary(**books: dict) -> dict:
    return {"success": 1, "results": books}
