"""End to end: an alert is logged, graded after the match, and lands in the report card."""

import csv
import json
import statistics
from datetime import timedelta

import pytest
import responses

from ev_engine import quant
from ev_engine.runner import run
from ev_engine.tracking import Tracker

from conftest import BETSAPI, NOW, WEBHOOK, book_odds, fixture, make_settings, summary, upcoming

UPCOMING = f"{BETSAPI}/v3/events/upcoming"
SUMMARY = f"{BETSAPI}/v2/event/odds/summary"
VIEW = f"{BETSAPI}/v1/event/view"
START = NOW + timedelta(minutes=25)
TS = int(START.timestamp())
CLOSING = {"Bet365": ("2.000", "1.800"), "BWin": ("1.980", "1.820"), "Betway": ("2.020", "1.780"),
           "Unibet": ("1.950", "1.850")}


def scan_mocks(rsps):
    """Run 1: one TT Cup match; Unibet's away price is the edge."""
    rsps.add(responses.GET, UPCOMING, json=upcoming(fixture("e1", 25)))
    rsps.add(responses.GET, SUMMARY, json=summary(
        Bet365=book_odds("1.900", "1.900"), BWin=book_odds("1.870", "1.930"),
        Betway=book_odds("1.930", "1.870"), Unibet=book_odds("1.700", "2.250")))
    rsps.add(responses.POST, WEBHOOK, status=204)


def closing_entry(home_od, away_od):
    return {"matching_dir": "1", "odds": {
        "start": {"92_1": {"home_od": "1.900", "away_od": "1.900", "ss": None, "add_time": str(TS - 7200)}},
        "kickoff": {"92_1": {"home_od": home_od, "away_od": away_od, "ss": None, "add_time": str(TS - 120)}},
        "end": {"92_1": {"home_od": "1.100", "away_od": "6.000", "ss": "0-2", "add_time": str(TS + 900)}}}}


def settle_mocks(rsps, *, ss="1-3", time_status="3"):
    """Run 2: nothing upcoming; the match has ended (away won) and closed shorter on the away side."""
    rsps.add(responses.GET, UPCOMING, json=upcoming())
    rsps.add(responses.GET, VIEW, json={"success": 1, "results": [{
        "id": "e1", "time": str(TS), "time_status": time_status, "ss": ss,
        "scores": {"1": {"home": "11", "away": "8"}, "2": {"home": "7", "away": "11"},
                   "3": {"home": "9", "away": "11"}, "4": {"home": "8", "away": "11"}}}]})
    rsps.add(responses.GET, SUMMARY, json=summary(**{b: closing_entry(*p) for b, p in CLOSING.items()}))
    rsps.add(responses.POST, WEBHOOK, status=204)


def picks(ledger):
    with (ledger / "picks" / "2026-10.csv").open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger"
    path.mkdir()
    return path


def test_alert_is_logged_then_graded_with_clv(tmp_path, ledger):
    settings = make_settings(tmp_path, ledger_dir=ledger, ledger_url="https://github.com/me/tt-ev-ledger")
    with responses.RequestsMock() as rsps:
        scan_mocks(rsps)
        assert run(settings, now=NOW, sleep=lambda _s: None) == 0
    [row] = picks(ledger)
    assert (row["book"], row["pick"], row["price"], row["status"]) == ("Unibet", "Petrov A.", "2.250", "pending")
    assert "betsapi_tt:e1" in json.loads((ledger / "state" / "pending.json").read_text())["matches"]
    assert "1 match waiting for a result" in (ledger / "REPORT.md").read_text()

    later = NOW + timedelta(hours=2)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        settle_mocks(rsps)
        assert run(settings, now=later, sleep=lambda _s: None) == 0
        assert not [c for c in rsps.calls if c.request.method == "POST"]  # no report due yet

    [row] = picks(ledger)
    away = [quant.devig([float(h), float(a)], "power")[1] for b, (h, a) in CLOSING.items() if b != "Unibet"]
    home = [quant.devig([float(h), float(a)], "power")[0] for b, (h, a) in CLOSING.items() if b != "Unibet"]
    p_close = statistics.median(away) / (statistics.median(away) + statistics.median(home))
    assert row["status"] == "won" and row["profit"] == "1.25" and row["score"].startswith("1-3")
    assert float(row["clv_pct"]) == pytest.approx((2.25 * p_close - 1) * 100, abs=0.01)
    assert row["close_price"] == "1.850" and row["close_books"] == "Bet365+BWin+Betway"
    assert json.loads((ledger / "state" / "pending.json").read_text())["matches"] == {}

    [line] = list(csv.DictReader((ledger / "lines" / "2026-10" / "2026-10-02.csv").open(encoding="utf-8")))
    assert line["side"] == "Ivanov D." and line["result"] == "0" and line["n_books"] == "4"
    report = (ledger / "REPORT.md").read_text()
    assert "| All time | 1 | 1-0 | +1.25u |" in report

    # On demand: the report card goes to Discord.
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, UPCOMING, json=upcoming())
        rsps.add(responses.POST, WEBHOOK, status=204)
        assert run(make_settings(tmp_path, ledger_dir=ledger, report_now=True),
                   now=later + timedelta(minutes=15), sleep=lambda _s: None) == 0
        [post] = [c for c in rsps.calls if c.request.method == "POST"]
    embed = json.loads(post.request.body)["embeds"][0]
    assert embed["title"].startswith("📊") and "All time: 1 bet" in embed["description"]
    assert "last_report_at" in json.loads((ledger / "state" / "meta.json").read_text())


def test_cancelled_match_voids_the_pick(tmp_path, ledger):
    settings = make_settings(tmp_path, ledger_dir=ledger)
    with responses.RequestsMock() as rsps:
        scan_mocks(rsps)
        run(settings, now=NOW, sleep=lambda _s: None)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        settle_mocks(rsps, ss="", time_status="5")
        assert run(settings, now=NOW + timedelta(hours=2), sleep=lambda _s: None) == 0
        assert not [c for c in rsps.calls if c.request.url.startswith(SUMMARY)]  # no odds call for a void
    [row] = picks(ledger)
    assert (row["status"], row["score"], row["profit"]) == ("void", "Cancelled", "0.00")


def test_weekly_report_waits_for_its_slot(tmp_path, ledger):
    settings = make_settings(tmp_path, ledger_dir=ledger)          # Mondays 16:00 UTC
    tracker = Tracker.open(settings, NOW)                           # NOW is Friday 21:00
    assert not tracker.report_due()
    tracker.ledger.save()
    monday = NOW.replace(day=5, hour=16, minute=5)
    assert Tracker.open(settings, monday).report_due()
    assert not Tracker.open(settings, monday.replace(hour=15)).report_due()


def test_tracker_is_off_for_dry_runs_and_without_a_ledger(tmp_path, ledger):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        scan_mocks(rsps)
        run(make_settings(tmp_path, ledger_dir=ledger, dry_run=True), now=NOW, sleep=lambda _s: None)
        run(make_settings(tmp_path), now=NOW, sleep=lambda _s: None)
    assert list(ledger.iterdir()) == []


def test_a_tracker_crash_never_fails_the_scan(tmp_path, ledger, monkeypatch):
    def boom(self, providers):
        raise RuntimeError("grading bug")

    monkeypatch.setattr(Tracker, "settle", boom)
    with responses.RequestsMock() as rsps:
        scan_mocks(rsps)
        assert run(make_settings(tmp_path, ledger_dir=ledger), now=NOW, sleep=lambda _s: None) == 0
    assert picks(ledger)[0]["status"] == "pending"  # the alert itself was still logged
