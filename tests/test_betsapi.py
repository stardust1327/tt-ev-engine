"""BetsAPI adapter against mocked HTTP responses shaped like the documented samples."""

import pytest
import responses
from responses import matchers

from ev_engine.http_client import ApiError
from ev_engine.models import RunReport
from ev_engine.providers.betsapi import BetsApiTableTennisProvider, parse_line, parse_price

from conftest import BETSAPI, NOW, book_odds, fixture, make_settings, summary, upcoming

UPCOMING = f"{BETSAPI}/v3/events/upcoming"
SUMMARY = f"{BETSAPI}/v2/event/odds/summary"


def provider(tmp_path, sleeps=None, **overrides):
    settings = make_settings(tmp_path, **overrides)
    report = RunReport(started_at=NOW)
    sleep = sleeps.append if sleeps is not None else (lambda _s: None)
    return BetsApiTableTennisProvider.from_settings(settings, report, sleep=sleep), report


def add_summary(event_id, payload, **kw):
    responses.add(responses.GET, SUMMARY, json=payload,
                  match=[matchers.query_param_matcher({"event_id": event_id}, strict_match=False)], **kw)


def test_price_and_line_parsing():
    assert parse_price("1.833") == 1.833
    assert parse_price("EVS") == 2.0
    for bad in ("0.00", "", None, "-", "1.00"):
        with pytest.raises(ValueError):
            parse_price(bad)
    assert parse_line("-1.5") == -1.5 and parse_line("74.5") == 74.5
    assert parse_line("2.5,3.0") is None and parse_line("0-0.5") is None and parse_line("2.5/3") is None


@responses.activate
def test_fetch_filters_leagues_window_and_status(tmp_path):
    responses.add(responses.GET, UPCOMING, json=upcoming(
        fixture("e1", 20),                                   # TT Cup, in window -> keep
        fixture("e2", 25, league="Setka Cup", league_id="1"),  # other league -> drop
        fixture("e3", 400),                                  # beyond 180-min look-ahead -> drop
        fixture("e4", 30, time_status="1"),                  # already in play -> drop
        fixture("e5", 1),                                    # starts within 2 min -> drop
    ))
    add_summary("e1", summary(
        Bet365=book_odds("1.800", "2.000"),
        BWin=book_odds("2.050", "1.750", matching_dir="-1"),  # reversed listing -> swapped back
        Betway=book_odds("EVS", "1.800"),
        **{"188Bet": book_odds("0.00", "0.00")},             # suspended -> skipped with reason
        CloudBet={"matching_dir": "1", "odds_update": [], "odds": {"start": {"92_1": None}}},
    ))
    p, report = provider(tmp_path)
    events = p.fetch_events(NOW)

    assert [e.event_id for e in events] == ["e1"]
    books = {m.bookmaker: m for m in events[0].markets}
    assert set(books) == {"Bet365", "BWin", "Betway"}
    assert [o.price for o in books["Bet365"].outcomes] == [1.8, 2.0]   # newest 'end' snapshot, not 'start'
    assert [o.price for o in books["BWin"].outcomes] == [1.75, 2.05]   # swapped back
    assert [o.price for o in books["Betway"].outcomes] == [2.0, 1.8]   # EVS = 2.0
    assert books["Bet365"].updated_at.timestamp() == NOW.timestamp() - 60  # odds_update beats add_time
    assert report.skip_counts["bad_price"] == 1
    # the token goes in the query string, as BetsAPI requires
    assert "token=tok_live_1234567890" in responses.calls[0].request.url
    assert p.usage().startswith("calls=2")


@responses.activate
def test_in_play_snapshot_is_ignored(tmp_path):
    fx = fixture("e1", 20)
    start = int(fx["time"])
    entry = book_odds("1.800", "2.000")
    entry["odds"]["end"]["92_1"].update(add_time=str(start + 60), ss="0-0")  # posted after start
    entry["odds"]["start"]["92_1"].update(home_od="1.700", away_od="2.150")
    responses.add(responses.GET, UPCOMING, json=upcoming(fx))
    add_summary("e1", summary(Bet365=entry))
    events = provider(tmp_path)[0].fetch_events(NOW)
    bm = events[0].markets[0]
    assert [o.price for o in bm.outcomes] == [1.7, 2.15]
    # BetsAPI's "last checked" time vouches for the in-play record, not this one -> use add_time.
    assert bm.updated_at.timestamp() == NOW.timestamp() - 7200


@responses.activate
def test_too_many_requests_stops_gracefully(tmp_path):
    responses.add(responses.GET, UPCOMING, json=upcoming(fixture("e1", 20), fixture("e2", 25)))
    add_summary("e1", summary(Bet365=book_odds("1.8", "2.0")))
    add_summary("e2", {"success": 0, "error": "TOO_MANY_REQUESTS"})
    p, report = provider(tmp_path)
    events = p.fetch_events(NOW)
    assert [e.event_id for e in events] == ["e1"]
    assert report.skip_counts["rate_limit"] == 1


@responses.activate
def test_http_429_is_retried_after_waiting(tmp_path):
    responses.add(responses.GET, UPCOMING, status=429, headers={"Retry-After": "2"})
    responses.add(responses.GET, UPCOMING, json=upcoming())
    sleeps = []
    p, _ = provider(tmp_path, sleeps)
    assert p.fetch_events(NOW) == []
    assert 2.0 in sleeps
    assert len(responses.calls) == 2


@responses.activate
def test_quota_floor_stops_before_burning_the_hourly_allowance(tmp_path):
    responses.add(responses.GET, UPCOMING, json=upcoming(fixture("e1", 20)),
                  headers={"X-RateLimit-Remaining": "40", "X-RateLimit-Limit": "3600"})
    p, report = provider(tmp_path)  # default floor: 100 calls left
    assert p.fetch_events(NOW) == []
    assert report.skip_counts["rate_limit"] == 1
    assert len(responses.calls) == 1  # no odds call was made


@responses.activate
def test_bad_token_is_fatal(tmp_path):
    responses.add(responses.GET, UPCOMING, json={"success": 0, "error": "AUTHORIZE_FAILED"})
    p, _ = provider(tmp_path)
    with pytest.raises(ApiError) as err:
        p.fetch_events(NOW)
    assert err.value.fatal


@responses.activate
def test_league_ids_skip_name_filtering(tmp_path):
    responses.add(responses.GET, UPCOMING, json=upcoming(fixture("e1", 20, league="Renamed League")),
                  match=[matchers.query_param_matcher({"sport_id": "92", "league_id": "22742", "page": "1"},
                                                      strict_match=False)])
    add_summary("e1", summary(Bet365=book_odds("1.8", "2.0")))
    p, _ = provider(tmp_path, betsapi_league_ids=("22742",))
    assert [e.event_id for e in p.fetch_events(NOW)] == ["e1"]


@responses.activate
def test_pagination_stops_past_the_horizon(tmp_path):
    page1 = upcoming(*[fixture(f"x{i}", 10 + i, league="Other") for i in range(50)], total=500)
    page2 = upcoming(*[fixture(f"y{i}", 300 + i, league="TT Cup") for i in range(50)], total=500)
    responses.add(responses.GET, UPCOMING, json=page1,
                  match=[matchers.query_param_matcher({"page": "1"}, strict_match=False)])
    responses.add(responses.GET, UPCOMING, json=page2,
                  match=[matchers.query_param_matcher({"page": "2"}, strict_match=False)])
    p, _ = provider(tmp_path)
    assert p.fetch_events(NOW) == []
    assert len(responses.calls) == 2  # stopped at page 2 instead of walking all 10 pages


@responses.activate
def test_later_page_failure_keeps_the_fixtures_already_found(tmp_path):
    page1 = upcoming(fixture("e1", 20), *[fixture(f"x{i}", 30 + i, league="Other") for i in range(49)], total=500)
    responses.add(responses.GET, UPCOMING, json=page1,
                  match=[matchers.query_param_matcher({"page": "1"}, strict_match=False)])
    responses.add(responses.GET, UPCOMING, status=503,
                  match=[matchers.query_param_matcher({"page": "2"}, strict_match=False)])
    add_summary("e1", summary(Bet365=book_odds("1.8", "2.0")))
    p, _ = provider(tmp_path)
    assert [e.event_id for e in p.fetch_events(NOW)] == ["e1"]  # 503 retried, then page 1 still used
