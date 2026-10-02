"""Pure betting math: no I/O, fully unit-tested.

Notation
--------
o_i  decimal odds of outcome i
q_i  raw implied probability = 1 / o_i
S    overround = sum(q_i). S > 1 is the bookmaker's margin (vig / juice).

"Devigging" maps q -> p with sum(p_i) = 1, i.e. it estimates the fair probabilities
hidden underneath the margin.

Methods
-------
multiplicative  p_i = q_i / S                     proportional; the textbook default
additive        p_i = q_i - (S - 1) / n           removes the margin equally from every outcome
power           p_i = q_i ** k with sum = 1       takes more margin from longshots, matching the
                                                  favourite-longshot bias of soft books (default)
shin            Shin (1993) insider-trading model; solves for z with sum(p_i) = 1

Expected value per 1 unit staked:   EV = decimal_odds * true_probability - 1

Key property (tested): devigging a book and pricing EV against THAT SAME book is
always negative for every outcome - with multiplicative devig it is exactly
1/S - 1. The fair line has to come from somewhere else (a sharp book or a
consensus of other books), which is what analyzer.py does.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

_TOL = 1e-12
_MAX_ITER = 200


def _validate(odds: Sequence[float]) -> list[float]:
    prices = [float(o) for o in odds]
    if len(prices) < 2:
        raise ValueError("a market needs at least two outcomes to devig")
    for price in prices:
        if not math.isfinite(price) or price <= 1.0:
            raise ValueError(f"decimal odds must be finite and > 1.0, got {price!r}")
    return prices


def implied_probability(decimal_odds: float) -> float:
    """Raw (vig-inclusive) probability implied by a decimal price."""
    if not math.isfinite(decimal_odds) or decimal_odds <= 1.0:
        raise ValueError(f"decimal odds must be finite and > 1.0, got {decimal_odds!r}")
    return 1.0 / decimal_odds


def overround(odds: Sequence[float]) -> float:
    """Sum of implied probabilities. 1.05 means a 5% margin."""
    return sum(1.0 / o for o in _validate(odds))


def _bisect(f: Callable[[float], float], lo: float, hi: float) -> float:
    """Root of a continuous f on [lo, hi] where f(lo) and f(hi) have opposite signs."""
    f_lo = f(lo)
    for _ in range(_MAX_ITER):
        mid = 0.5 * (lo + hi)
        f_mid = f(mid)
        if abs(f_mid) < _TOL or (hi - lo) < _TOL:
            return mid
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _normalize(values: list[float]) -> list[float]:
    total = sum(values)
    return [v / total for v in values]


def devig(odds: Sequence[float], method: str = "power") -> list[float]:
    """Remove the bookmaker margin and return fair probabilities that sum to 1.

    Args:
        odds:   decimal odds for every mutually exclusive outcome of one market.
        method: 'multiplicative' | 'additive' | 'power' | 'shin'.

    Raises:
        ValueError: invalid odds, unknown method, or a method that can't handle the market.
    """
    prices = _validate(odds)
    q = [1.0 / o for o in prices]
    s = sum(q)
    n = len(q)

    if method == "multiplicative":
        return [x / s for x in q]

    if method == "additive":
        p = [x - (s - 1.0) / n for x in q]
        if min(p) <= 0.0:
            raise ValueError("additive devig gave a non-positive probability (longshot too long for this margin)")
        return p

    if method == "power":
        if abs(s - 1.0) < _TOL:
            return q

        def excess(k: float) -> float:
            return sum(x**k for x in q) - 1.0

        # excess(k) falls as k grows (every q_i < 1). With a margin (S > 1) the root
        # sits above k = 1; for an underround book (S < 1) it sits below.
        lo, hi = (1.0, 2.0) if s > 1.0 else (1e-9, 1.0)
        while excess(hi) > 0.0:
            lo, hi = hi, hi * 2.0
            if hi > 1e6:
                raise ValueError("power devig did not converge")
        k = _bisect(excess, lo, hi)
        return _normalize([x**k for x in q])

    if method == "shin":
        if s <= 1.0 + _TOL:
            # No margin for Shin's insider fraction z to explain: z = 0, i.e. proportional.
            return [x / s for x in q]

        def shin_p(x: float, z: float) -> float:
            return (math.sqrt(z * z + 4.0 * (1.0 - z) * x * x / s) - z) / (2.0 * (1.0 - z))

        # sum(p) = sqrt(S) > 1 at z = 0 and falls below 1 as z -> 1, so the root is bracketed.
        z = _bisect(lambda zz: sum(shin_p(x, zz) for x in q) - 1.0, 0.0, 1.0 - 1e-9)
        return _normalize([shin_p(x, z) for x in q])

    raise ValueError(f"unknown devig method {method!r}")


def expected_value(decimal_odds: float, true_probability: float) -> float:
    """EV per unit staked: (decimal_odds * true_probability) - 1.

    0.034 means +3.4%: over many bets at this price you'd expect to win 3.4 cents per dollar.
    """
    if not math.isfinite(decimal_odds) or decimal_odds <= 1.0:
        raise ValueError(f"decimal odds must be finite and > 1.0, got {decimal_odds!r}")
    if not 0.0 < true_probability < 1.0:
        raise ValueError(f"probability must be between 0 and 1, got {true_probability!r}")
    return decimal_odds * true_probability - 1.0


def fair_odds(probability: float) -> float:
    """Decimal odds with zero margin for a given probability."""
    if not 0.0 < probability < 1.0:
        raise ValueError(f"probability must be between 0 and 1, got {probability!r}")
    return 1.0 / probability


def decimal_to_american(decimal_odds: float) -> str:
    """2.10 -> '+110', 1.50 -> '-200'."""
    if not math.isfinite(decimal_odds) or decimal_odds <= 1.0:
        raise ValueError(f"decimal odds must be finite and > 1.0, got {decimal_odds!r}")
    if decimal_odds >= 2.0:
        return f"+{round((decimal_odds - 1.0) * 100)}"
    return f"-{round(100.0 / (decimal_odds - 1.0))}"
