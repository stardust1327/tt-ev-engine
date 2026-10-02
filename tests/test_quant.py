"""Unit tests for the pure math in ev_engine.quant."""

import math

import pytest

from ev_engine import quant

METHODS = ["multiplicative", "additive", "power", "shin"]
MARKETS = [[1.90, 1.90], [1.50, 2.60], [1.25, 4.00], [2.10, 3.40, 3.60], [1.08, 9.00]]


def test_implied_probability_and_overround():
    assert quant.implied_probability(2.0) == 0.5
    assert quant.overround([1.90, 1.90]) == pytest.approx(1.052632, rel=1e-6)


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("odds", MARKETS)
def test_devig_returns_probabilities_that_sum_to_one(method, odds):
    probs = quant.devig(odds, method)
    assert sum(probs) == pytest.approx(1.0, abs=1e-9)
    assert all(0 < p < 1 for p in probs)
    # Ordering is preserved: the shorter price is always the likelier outcome.
    assert sorted(range(len(odds)), key=lambda i: odds[i]) == sorted(range(len(odds)), key=lambda i: -probs[i])


@pytest.mark.parametrize("method", METHODS)
def test_symmetric_market_is_fifty_fifty(method):
    assert quant.devig([1.91, 1.91], method) == pytest.approx([0.5, 0.5])


def test_multiplicative_is_proportional():
    q = [1 / 1.5, 1 / 2.6]
    assert quant.devig([1.5, 2.6], "multiplicative") == pytest.approx([x / sum(q) for x in q])


def test_power_takes_more_margin_from_the_longshot():
    mult = quant.devig([1.25, 4.00], "multiplicative")
    power = quant.devig([1.25, 4.00], "power")
    assert power[1] < mult[1] and power[0] > mult[0]


def test_shin_equals_additive_on_two_way_markets():
    # Known result: for two outcomes, Shin's model reduces to the additive method.
    assert quant.devig([1.5, 2.6], "shin") == pytest.approx(quant.devig([1.5, 2.6], "additive"), abs=1e-9)


def test_additive_rejects_extreme_longshots():
    # Margin share per outcome (0.034) exceeds the longshot's implied 2%: additive breaks down.
    with pytest.raises(ValueError):
        quant.devig([1.20, 4.00, 50.0], "additive")
    assert sum(quant.devig([1.20, 4.00, 50.0], "power")) == pytest.approx(1.0)


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("odds", [[1.90, 1.90], [1.50, 2.60], [1.25, 4.00], [2.10, 3.40, 3.60]])
def test_devigging_the_same_book_never_shows_positive_ev(method, odds):
    """Why the fair line must come from OTHER books: against its own no-vig line,
    every price at a book with a margin has negative EV (multiplicative: exactly 1/S - 1)."""
    probs = quant.devig(odds, method)
    evs = [quant.expected_value(o, p) for o, p in zip(odds, probs, strict=True)]
    assert all(ev < 0 for ev in evs)
    if method == "multiplicative":
        assert evs == pytest.approx([1 / quant.overround(odds) - 1] * len(odds))


def test_expected_value_matches_the_spec_formula():
    # EV = (decimal_odds x true_probability) - 1
    assert quant.expected_value(2.10, 0.50) == pytest.approx(0.05)
    assert quant.expected_value(1.80, 0.50) == pytest.approx(-0.10)


@pytest.mark.parametrize("bad", [[2.0], [1.0, 2.0], [0.0, 3.0], [math.nan, 2.0], [math.inf, 2.0]])
def test_devig_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        quant.devig(bad, "power")


def test_unknown_method_and_bad_probability():
    with pytest.raises(ValueError):
        quant.devig([1.9, 1.9], "magic")
    with pytest.raises(ValueError):
        quant.expected_value(2.0, 1.2)


def test_underround_market_still_devigs():
    probs = quant.devig([2.05, 2.05], "power")  # sums to 0.976 (stale / arbitrage-like)
    assert probs == pytest.approx([0.5, 0.5])


def test_fair_and_american_odds():
    assert quant.fair_odds(0.4) == pytest.approx(2.5)
    assert quant.decimal_to_american(2.10) == "+110"
    assert quant.decimal_to_american(1.50) == "-200"
    assert quant.decimal_to_american(2.00) == "+100"
