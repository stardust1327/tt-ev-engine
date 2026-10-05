"""BetsAPI results and closing prices for the tracker, against mocked responses."""

from datetime import timedelta

import responses
from responses import matchers

from ev_engine.models import Event, RunReport
from ev_engine.providers.betsapi import BetsApiTableTennisProvider, _closing_record, _parse_result

from conftest import BETSAPI, NOW, make_settings, summary

VIEW = f"{BETSAPI}/v1/event/view"
SUMMARY = f"{BETSAPI}/v2/event/odds/summary"
START = NOW - timedelta(hours=1)
TS = int(START.timestamp())


def provider(tmp_path, **overrides):
    return BetsApiTableTennisProvider.from_settings(make_settings(tmp_path, **overrides), RunReport(started_at=NOW),
                                                    sleep=lambda _s: None)


def view_result(event_id, *, time_status="3", ss="3-1", scores=None):
    return {"id": event_id, "sport_id": "92", "time": str(TS), "time_status": time_status,
            "league": {"id": "1", "name": "TT Cup"}, "home": {"name": "Ivanov D."}, "away": {"name": "Petrov A."},
            "ss": ss, "scores": scores if scores is not None else
            {"1": {"home": "11", "away": "7"}, "2": {"home": "9", "away": "11"},
             "3": {"home": "11", "away": "5"}, "10": {"home": "13", "away": "11"}}}


def rec(home_od, away_od, add_time, ss=None):
    return {"home_od": home_od, "away_od": away_od, "ss": ss, "add_time": str(add_time)}


def test_parse_result_reads_games_and_points_in_order():
    r = _parse_result(view_result("e1"))
    assert r.status == "ended" and (r.home_score, r.away_score) == (3, 1)
    assert r.periods == ((11, 7), (9, 11), (11, 5), (13, 11))  # game "10" sorts after "3"
    no_ss = _parse_result(view_result("e2", ss=None))
    assert (no_ss.home_score, no_ss.away_score) == (3, 1)        # counted from the games


def test_parse_result_void_and_pending():
    assert _parse_result(view_result("e1", time_status="9")).status == "void"     # retired
    assert _parse_result(view_result("e1", time_status="9")).detail == "Retired"
    assert _parse_result(view_result("e1", time_status="1", ss="1-1")).status == "pending"  # in play
    assert _parse_result(view_result("e1", ss="", scores={})).status == "pending"  # ended, no score yet
    assert _parse_result({"time_status": "3"}) is None


def test_closing_record_prefers_kickoff_and_never_falls_back_to_the_opener():
    opening = rec("1.90", "1.90", TS - 7200)
    kickoff = rec("1.80", "2.00", TS - 300)
    live = rec("1.20", "4.00", TS + 600, ss="1-0")
    pre = rec("1.85", "1.95", TS - 60)
    assert _closing_record({"start": {"92_1": opening}, "kickoff": {"92_1": kickoff}, "end": {"92_1": live}},
                           "92_1", TS) == (kickoff, "kickoff")
    assert _closing_record({"start": {"92_1": opening}, "end": {"92_1": pre}}, "92_1", TS) == (pre, "last pre-match")
    # in-play prices only and no kickoff snapshot: the closing price is unknown, not the opener
    assert _closing_record({"start": {"92_1": opening}, "end": {"92_1": live}}, "92_1", TS) == (None, "")
    assert _closing_record({"start": {"92_1": opening}}, "92_1", TS) == (opening, "opening (unchanged)")


@responses.activate
def test_fetch_results_batches_ids_in_one_call(tmp_path):
    responses.add(responses.GET, VIEW, json={"success": 1, "results": [view_result("e1"), view_result("e2", ss="0-3")]},
                  match=[matchers.query_param_matcher({"event_id": "e1,e2"}, strict_match=False)])
    results = provider(tmp_path).fetch_results(["e1", "e2"])
    assert results["e1"].home_score == 3 and results["e2"].away_score == 3
    assert len(responses.calls) == 1


@responses.activate
def test_closing_markets_swaps_reversed_books(tmp_path):
    responses.add(responses.GET, SUMMARY, json=summary(
        Bet365={"matching_dir": "1", "odds": {"kickoff": {"92_1": rec("1.80", "2.00", TS - 300)}}},
        BWin={"matching_dir": "-1", "odds": {"kickoff": {"92_1": rec("2.10", "1.75", TS - 300)}}},
        Suspended={"matching_dir": "1", "odds": {"kickoff": {"92_1": rec("0.00", "0.00", TS - 300)}}},
    ))
    event = Event("betsapi_tt", "BetsAPI", "e1", "", "TT Cup", "Ivanov D.", "Petrov A.", START)
    markets, sources = provider(tmp_path).closing_markets(event)
    prices = {m.bookmaker: [o.price for o in m.outcomes] for m in markets}
    assert prices == {"Bet365": [1.8, 2.0], "BWin": [1.75, 2.1]}
    assert sources["kickoff"] == 2
