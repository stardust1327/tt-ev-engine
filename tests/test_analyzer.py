"""Edge finding: fair-line construction, quality gates and best-book selection."""

from dataclasses import replace

import pytest

from ev_engine.analyzer import find_edges
from ev_engine.models import RunReport

from conftest import NOW, event, make_settings, market


def scan(ev, settings):
    report = RunReport(started_at=NOW)
    return find_edges(ev, settings, NOW, report), report


def test_consensus_edge_exact_numbers(tmp_path):
    """Three books at 1.90/1.90 make a 50/50 fair line; 2.10 elsewhere is exactly +5% EV."""
    settings = make_settings(tmp_path, devig_method="multiplicative")
    ev = event(market("BookA", 1.90, 1.90), market("BookB", 1.90, 1.90), market("BookC", 1.90, 1.90),
               market("SoftBook", 2.10, 1.75))
    edges, report = scan(ev, settings)
    assert len(edges) == 1
    edge = edges[0]
    assert (edge.bookmaker, edge.outcome, edge.price) == ("SoftBook", "Ivanov D.", 2.10)
    assert edge.fair_prob == pytest.approx(0.5)
    assert edge.ev == pytest.approx(0.05)
    assert edge.reference.startswith("Consensus of 3 books")  # leave-one-out: target excluded
    assert report.markets_evaluated == 4


def test_best_price_wins_and_others_become_alternatives(tmp_path):
    settings = make_settings(tmp_path, devig_method="multiplicative", min_consensus_books=3)
    ev = event(market("A", 1.90, 1.90), market("B", 1.90, 1.90), market("C", 1.90, 1.90),
               market("D", 1.90, 1.90), market("Soft1", 2.06, 1.76), market("Soft2", 2.12, 1.72))
    edges, _ = scan(ev, settings)
    assert len(edges) == 1
    assert edges[0].bookmaker == "Soft2" and edges[0].price == 2.12
    assert [alt[0] for alt in edges[0].alternatives] == ["Soft1"]


def test_sharp_book_is_used_when_present(tmp_path):
    settings = make_settings(tmp_path, sharp_books=("pinnacle",), devig_method="multiplicative")
    ev = event(market("Pinnacle", 1.95, 1.95), market("SoftBook", 2.08, 1.80))
    edges, _ = scan(ev, settings)
    assert len(edges) == 1
    assert edges[0].reference.startswith("Pinnacle no-vig line")
    assert edges[0].ev == pytest.approx(0.04)


def test_sharp_mode_without_sharp_book_records_the_gap(tmp_path):
    settings = make_settings(tmp_path, fair_line_mode="sharp", sharp_books=("pinnacle",))
    ev = event(market("A", 1.90, 1.90), market("B", 1.90, 1.90), market("C", 1.90, 1.90), market("Soft", 2.2, 1.7))
    edges, report = scan(ev, settings)
    assert edges == [] and report.skip_counts["no_fair_line"] == 1
    assert "pinnacle" in report.skips[0].detail


def test_thin_market_makes_no_prediction(tmp_path):
    """Only two books and no sharp: no fair line, so no alert - and the reason is recorded."""
    settings = make_settings(tmp_path)
    ev = event(market("Bet365", 1.90, 1.90), market("Betway", 2.20, 1.70))
    edges, report = scan(ev, settings)
    assert edges == []
    assert report.skip_counts["no_fair_line"] == 1
    assert "MIN_CONSENSUS_BOOKS=3" in report.skips[0].detail


def test_stale_book_is_excluded(tmp_path):
    settings = make_settings(tmp_path, devig_method="multiplicative")
    ev = event(market("A", 1.90, 1.90), market("B", 1.90, 1.90), market("C", 1.90, 1.90),
               market("Stale", 2.30, 1.65, minutes_old=45))
    edges, report = scan(ev, settings)
    assert edges == []
    assert report.skip_counts["stale"] == 1


def test_junk_margin_book_is_kept_out_of_the_fair_line_but_can_still_be_a_target(tmp_path):
    settings = make_settings(tmp_path, devig_method="multiplicative")
    # 'Wide' has a 25% margin: never part of the fair line. 'Generous' is underround (0.976):
    # also not a reference, but its 2.05 price can still be a +EV bet against A/B/C.
    ev = event(market("A", 1.90, 1.90), market("B", 1.90, 1.90), market("C", 1.90, 1.90),
               market("Wide", 1.60, 1.60), market("Generous", 2.05, 2.05))
    edges, report = scan(ev, settings)
    assert report.skip_counts["overround"] == 2
    assert {e.bookmaker for e in edges} == {"Generous"}
    assert all(e.reference == "Consensus of 3 books (median, multiplicative devig)" for e in edges)


def test_ev_above_sanity_cap_is_not_alerted(tmp_path):
    settings = make_settings(tmp_path, devig_method="multiplicative")
    ev = event(market("A", 1.90, 1.90), market("B", 1.90, 1.90), market("C", 1.90, 1.90),
               market("Reversed", 1.25, 3.60))  # a book with the players flipped
    edges, report = scan(ev, settings)
    assert edges == []
    assert report.skip_counts["suspect_ev"] == 1


def test_odds_range_filter(tmp_path):
    settings = make_settings(tmp_path, devig_method="multiplicative", max_odds=2.0)
    ev = event(market("A", 1.90, 1.90), market("B", 1.90, 1.90), market("C", 1.90, 1.90), market("Soft", 2.10, 1.75))
    edges, report = scan(ev, settings)
    assert edges == [] and report.skip_counts["odds_range"] == 1


def test_bet_books_allowlist(tmp_path):
    base = make_settings(tmp_path, devig_method="multiplicative")
    ev = event(market("A", 1.90, 1.90), market("B", 1.90, 1.90), market("C", 1.90, 1.90), market("Soft", 2.10, 1.75))
    assert scan(ev, replace(base, bet_books=("a", "b")))[0] == []
    assert len(scan(ev, replace(base, bet_books=("soft",)))[0]) == 1


def test_match_about_to_start_is_skipped(tmp_path):
    settings = make_settings(tmp_path)
    ev = event(market("A", 1.90, 1.90), minutes_to_start=1)
    edges, report = scan(ev, settings)
    assert edges == [] and report.skip_counts["starting"] == 1


def test_threshold_respected(tmp_path):
    settings = make_settings(tmp_path, devig_method="multiplicative", ev_threshold=0.06, strong_ev_threshold=0.08)
    ev = event(market("A", 1.90, 1.90), market("B", 1.90, 1.90), market("C", 1.90, 1.90), market("Soft", 2.10, 1.75))
    assert scan(ev, settings)[0] == []  # +5% is below a 6% threshold
