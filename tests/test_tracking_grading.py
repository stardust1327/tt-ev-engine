"""Tracker math: grading, profit, closing line and CLV."""

import pytest

from ev_engine.models import MONEYLINE, SPREAD, TOTAL, BookMarket, MatchResult, Outcome
from ev_engine.tracking.grading import (
    LOST,
    PUSH,
    WON,
    calibration_rows,
    closing_lines,
    closing_value,
    grade,
    profit,
)

HOME, AWAY = "Ivanov D.", "Petrov A."
RESULT = MatchResult("e1", "ended", 3, 1, ((11, 7), (9, 11), (11, 5), (11, 8)))  # 42-31 points


def ml(book, home_price, away_price):
    return BookMarket(book, MONEYLINE, None, (Outcome(HOME, home_price), Outcome(AWAY, away_price)), None)


@pytest.mark.parametrize("market,line,outcome,expected", [
    (MONEYLINE, None, HOME, WON),
    (MONEYLINE, None, AWAY, LOST),
    (SPREAD, -10.5, HOME, WON),    # 42 - 10.5 > 31
    (SPREAD, -11.5, HOME, LOST),
    (SPREAD, -11.0, HOME, PUSH),
    (SPREAD, -11.5, AWAY, WON),    # away gets +11.5: 31 + 11.5 > 42
    (TOTAL, 72.5, "Over", WON),    # 73 points
    (TOTAL, 72.5, "Under", LOST),
    (TOTAL, 73.0, "Over", PUSH),
])
def test_grade(market, line, outcome, expected):
    assert grade(market, line, outcome, HOME, AWAY, RESULT) == expected


def test_grade_refuses_what_it_cannot_know():
    assert grade(MONEYLINE, None, HOME, HOME, AWAY, MatchResult("e1", "void", detail="Retired")) is None
    assert grade(MONEYLINE, None, "Someone Else", HOME, AWAY, RESULT) is None
    no_points = MatchResult("e1", "ended", 3, 0)
    assert grade(MONEYLINE, None, HOME, HOME, AWAY, no_points) == WON
    assert grade(TOTAL, 70.5, "Over", HOME, AWAY, no_points) is None  # totals need the points


def test_profit_is_one_unit_at_the_alerted_price():
    assert profit(WON, 2.10) == pytest.approx(1.10)
    assert profit(LOST, 2.10) == -1.0
    assert profit(PUSH, 2.10) == 0.0 and profit("void", 2.10) == 0.0


def test_score_text():
    assert RESULT.score == "3-1 (11-7 9-11 11-5 11-8)"
    assert MatchResult("e1", "void", detail="Walkover").score == "Walkover"


def test_clv_uses_the_other_books_closing_line():
    lines = closing_lines([
        ml("Bet365", 1.90, 1.90),
        ml("BWin", 1.87, 1.93),
        ml("Betway", 1.93, 1.87),
        ml("DraftKings", 1.80, 2.05),   # the alerted book: never part of its own fair line
    ], "power", 1.12)
    cv = closing_value(MONEYLINE, None, AWAY, "DraftKings", 2.25, lines)
    assert cv.clv == pytest.approx(0.125)            # 2.25 * 0.5 - 1
    assert cv.close_fair_odds == pytest.approx(2.0)
    assert cv.close_price == 2.05
    assert set(cv.close_books) == {"Bet365", "BWin", "Betway"}


def test_clv_blank_when_the_line_moved_or_no_other_book_closed():
    lines = closing_lines([BookMarket("Bet365", SPREAD, -2.5, (Outcome(HOME, 1.9), Outcome(AWAY, 1.9)), None)],
                          "power", 1.12)
    assert closing_value(SPREAD, -3.5, HOME, "DraftKings", 2.0, lines).clv is None   # line moved
    alone = closing_lines([ml("DraftKings", 1.8, 2.05)], "power", 1.12)
    cv = closing_value(MONEYLINE, None, AWAY, "DraftKings", 2.25, alone)
    assert cv.clv is None and cv.close_price == 2.05


def test_junk_margin_books_are_kept_out_of_the_closing_fair_line():
    lines = closing_lines([ml("Bet365", 1.90, 1.90), ml("Junk", 1.20, 1.20)], "multiplicative", 1.12)
    assert set(lines[0].no_vig) == {"Bet365"} and set(lines[0].prices) == {"Bet365", "Junk"}


def test_calibration_rows_record_side_a_and_its_result():
    lines = closing_lines([ml("Bet365", 1.50, 2.70), ml("FonBet", 1.55, 2.55)], "multiplicative", 1.12)
    [row] = calibration_rows(lines, RESULT, HOME, AWAY)
    assert row["side"] == HOME and row["result"] == 1 and row["n_books"] == 2
    assert 0.6 < row["fair"] < 0.65
    assert row["prices"]["Bet365"] == (1.50, 2.70)
