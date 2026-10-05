"""Ledger files: picks, calibration lines and the pending list survive a save/load round trip."""

from datetime import timedelta

from ev_engine.models import MONEYLINE, Edge
from ev_engine.tracking.ledger import Ledger, match_key

from conftest import NOW, event, market


def edge(ev_event=None, price=2.25, minutes_old=3.0):
    ev_event = ev_event or event(market("Bet365", 1.9, 1.9))
    return Edge(event=ev_event, market=MONEYLINE, line=None, outcome="Petrov A.", bookmaker="DraftKings",
                price=price, fair_prob=0.5, ev=price * 0.5 - 1, reference="Consensus of 2 books (median, power devig)",
                updated_at=NOW - timedelta(minutes=minutes_old))


def test_pick_round_trip(tmp_path):
    ledger = Ledger.load(tmp_path)
    row = ledger.add_pick(edge(), NOW)
    assert row["status"] == "pending" and row["ev_pct"] == "12.50" and row["price_age_min"] == "3.0"
    assert row["alert_n"] == "1" and row["line"] == ""
    assert match_key("betsapi_tt", "9001") in ledger.pending  # its match will get graded
    written = {p.relative_to(tmp_path).as_posix() for p in ledger.save()}
    assert written == {"picks/2026-10.csv", "state/pending.json", "state/meta.json"}

    again = Ledger.load(tmp_path)
    [loaded] = list(again.iter_picks())
    assert loaded["pick"] == "Petrov A." and loaded["reference"].startswith("Consensus of 2 books")
    assert again.add_pick(edge(price=2.30), NOW + timedelta(minutes=15))["alert_n"] == "2"  # a re-alert


def test_update_marks_only_that_month_dirty(tmp_path):
    ledger = Ledger.load(tmp_path)
    row = ledger.add_pick(edge(), NOW)
    ledger.save()
    assert not ledger.changed
    ledger.update_pick(row, status="won", profit="1.25")
    assert ledger.changed
    ledger.save()
    assert next(Ledger.load(tmp_path).iter_picks())["status"] == "won"


def test_lines_are_daily_files_without_duplicates(tmp_path):
    ledger = Ledger.load(tmp_path)
    row = {"start": "2026-10-02T21:30:00Z", "provider": "betsapi_tt", "event_id": "e1", "market": "moneyline",
           "line": "", "side": "Ivanov D.", "fair_pct": "61.00", "result": "1"}
    assert ledger.add_line(dict(row)) and not ledger.add_line(dict(row))
    ledger.save()
    assert (tmp_path / "lines" / "2026-10" / "2026-10-02.csv").exists()
    assert len(list(Ledger.load(tmp_path).iter_lines())) == 1


def test_pending_list_and_requeue(tmp_path):
    ledger = Ledger.load(tmp_path)
    ledger.add_pick(edge(), NOW)
    key = match_key("betsapi_tt", "9001")
    ledger.done(key)
    assert key not in ledger.pending
    assert ledger.requeue_unsettled_picks() == 1     # its pick is still ungraded -> back on the list
    assert ledger.pending[key]["home"] == "Ivanov D."
    ledger.save()
    assert key in Ledger.load(tmp_path).pending


def test_report_file_only_rewritten_when_it_changes(tmp_path):
    ledger = Ledger.load(tmp_path)
    assert ledger.write_report("# Report\n") is not None
    assert ledger.write_report("# Report\n") is None
