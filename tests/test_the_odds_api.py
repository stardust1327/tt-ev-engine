"""The Odds API adapter (MLB/NFL path) against a mocked v4 /odds response."""

from dataclasses import replace

import responses

from ev_engine.analyzer import find_edges
from ev_engine.models import MONEYLINE, SPREAD, TOTAL, RunReport
from ev_engine.providers.the_odds_api import TheOddsApiProvider

from conftest import NOW, make_settings

URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
TS = int(NOW.timestamp())


def book(key, title, home_ml, away_ml, *, spread=None, total=None, age=60):
    markets = [{"key": "h2h", "last_update": TS - age, "outcomes": [
        {"name": "New York Yankees", "price": home_ml}, {"name": "Boston Red Sox", "price": away_ml}]}]
    if spread:
        markets.append({"key": "spreads", "last_update": TS - age, "outcomes": [
            {"name": "New York Yankees", "price": spread[0], "point": -1.5},
            {"name": "Boston Red Sox", "price": spread[1], "point": 1.5}]})
    if total:
        markets.append({"key": "totals", "last_update": TS - age, "outcomes": [
            {"name": "Over", "price": total[0], "point": 8.5}, {"name": "Under", "price": total[1], "point": 8.5}]})
    return {"key": key, "title": title, "markets": markets}


def payload():
    return [{
        "id": "abc123", "sport_key": "baseball_mlb", "sport_title": "MLB",
        "commence_time": TS + 3 * 3600, "home_team": "New York Yankees", "away_team": "Boston Red Sox",
        "bookmakers": [
            book("pinnacle", "Pinnacle", 1.80, 2.08, spread=(2.30, 1.65), total=(1.95, 1.90)),
            book("draftkings", "DraftKings", 1.77, 2.20, spread=(2.25, 1.66), total=(1.91, 1.91)),
            {"key": "betfair_ex_eu", "title": "Betfair", "markets": [  # exchange lay prices -> ignored
                {"key": "h2h_lay", "last_update": TS, "outcomes": [
                    {"name": "New York Yankees", "price": 1.82}, {"name": "Boston Red Sox", "price": 2.12}]}]},
        ],
    }]


def make_provider(tmp_path, **overrides):
    settings = make_settings(tmp_path, providers=("the_odds_api",), the_odds_api_key="odds_key_123456",
                             odds_api_sports=("baseball_mlb",), odds_api_markets=("h2h", "spreads", "totals"),
                             **overrides)
    report = RunReport(started_at=NOW)
    return TheOddsApiProvider.from_settings(settings, report, sleep=lambda _s: None), settings, report


@responses.activate
def test_parses_markets_and_skips_lay_prices(tmp_path):
    responses.add(responses.GET, URL, json=payload(), headers={"x-requests-remaining": "19500"})
    p, _, _ = make_provider(tmp_path)
    events = p.fetch_events(NOW)
    assert len(events) == 1
    by_key = {(m.book_id, m.market): m for m in events[0].markets}
    assert set(by_key) == {(b, m) for b in ("pinnacle", "draftkings") for m in (MONEYLINE, SPREAD, TOTAL)}
    assert by_key[("pinnacle", SPREAD)].line == -1.5          # home team's line
    assert by_key[("pinnacle", TOTAL)].line == 8.5
    query = responses.calls[0].request.url
    assert "oddsFormat=decimal" in query and "regions=us%2Ceu" in query
    assert "commenceTimeFrom=2026-10-02T21%3A02%3A00Z" in query  # now + MIN_MINUTES_TO_START
    assert p.usage() == "calls=1, remaining=19500"


@responses.activate
def test_sharp_reference_finds_mlb_edge(tmp_path):
    responses.add(responses.GET, URL, json=payload())
    p, settings, report = make_provider(tmp_path, sharp_books=("pinnacle",), devig_method="multiplicative")
    event = p.fetch_events(NOW)[0]
    edges = find_edges(event, settings, NOW, report)
    # DraftKings' Red Sox 2.20 vs Pinnacle's no-vig line is the only +EV price.
    assert [(e.bookmaker, e.selection, e.price) for e in edges] == [("DraftKings", "Boston Red Sox", 2.20)]
    assert edges[0].reference.startswith("Pinnacle no-vig line")


@responses.activate
def test_bad_sport_key_is_reported_not_fatal(tmp_path):
    responses.add(responses.GET, URL, status=422, json={"message": "Unknown sport"})
    p, _, report = make_provider(tmp_path)
    assert p.fetch_events(NOW) == []
    assert report.errors and "422" in report.errors[0]


@responses.activate
def test_quota_floor(tmp_path):
    other = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
    responses.add(responses.GET, URL, json=payload(), headers={"x-requests-remaining": "10"})
    p, settings, report = make_provider(tmp_path)
    p.settings = replace(settings, odds_api_sports=("baseball_mlb", "americanfootball_nfl"))
    assert len(p.fetch_events(NOW)) == 1
    assert report.skip_counts["rate_limit"] == 1
    assert not any(c.request.url.startswith(other) for c in responses.calls)
