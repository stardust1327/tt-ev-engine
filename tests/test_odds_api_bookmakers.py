"""ODDS_API_BOOKMAKERS: fetch named books (half the credits of us,eu) instead of whole regions."""

from urllib.parse import parse_qs, urlparse

import responses

from ev_engine.config import Settings
from ev_engine.models import RunReport
from ev_engine.providers.the_odds_api import TheOddsApiProvider

from conftest import NOW, make_settings

URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"


def fetch_params(tmp_path, **overrides) -> dict:
    responses.add(responses.GET, URL, json=[])
    settings = make_settings(tmp_path, providers=("the_odds_api",), the_odds_api_key="odds_key_123456",
                             odds_api_sports=("baseball_mlb",), **overrides)
    TheOddsApiProvider.from_settings(settings, RunReport(started_at=NOW), sleep=lambda _s: None).fetch_events(NOW)
    return parse_qs(urlparse(responses.calls[0].request.url).query)


@responses.activate
def test_named_books_replace_regions(tmp_path):
    params = fetch_params(tmp_path, odds_api_bookmakers=("pinnacle", "draftkings", "hardrockbet_az"))
    assert params["bookmakers"] == ["pinnacle,draftkings,hardrockbet_az"]
    assert "regions" not in params


@responses.activate
def test_regions_by_default(tmp_path):
    params = fetch_params(tmp_path)
    assert params["regions"] == ["us,eu"] and "bookmakers" not in params


def test_env_keys_are_lowercased(monkeypatch):
    monkeypatch.setenv("ODDS_API_BOOKMAKERS", "Pinnacle, DraftKings ,williamhill_us")
    monkeypatch.setenv("DRY_RUN", "true")
    assert Settings.from_env().odds_api_bookmakers == ("pinnacle", "draftkings", "williamhill_us")
