"""Pure grading math for the accuracy tracker: results, profit, closing line, CLV.

Closing line value (CLV) is the main accuracy signal. For a pick alerted at price `d`:

    CLV = d * p_close - 1

where p_close is the fair probability of the pick at kickoff, built exactly like the alert's
fair line (each book devigged with DEVIG_METHOD, per-outcome median of the OTHER books,
renormalized). In words: "what the EV of that price was by the time the market closed".
Prices that keep beating the close are real edges; results only confirm it much later.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .. import quant
from ..analyzer import consensus
from ..models import MONEYLINE, SPREAD, TOTAL, BookMarket, MatchResult, book_key

WON, LOST, PUSH = "won", "lost", "push"
_MARGIN_TOLERANCE = 1e-9


def grade(market: str, line: float | None, outcome: str, home: str, away: str,
          result: MatchResult) -> str | None:
    """'won' / 'lost' / 'push' for one selection, or None if this result can't grade it.

    line is the HOME side's line for handicaps (as stored on the pick) and the total itself
    for over/unders. Table-tennis handicaps and totals count points across all games.
    """
    if result.status != "ended" or result.home_score is None or result.away_score is None:
        return None
    if market == MONEYLINE:
        if result.home_score == result.away_score or outcome not in (home, away):
            return None
        winner = home if result.home_score > result.away_score else away
        return WON if outcome == winner else LOST
    if not result.periods or line is None:
        return None  # handicaps and totals need the points of every game
    if market == SPREAD:
        if outcome == home:
            margin = result.home_points + line - result.away_points
        elif outcome == away:
            margin = result.away_points - line - result.home_points
        else:
            return None
    elif market == TOTAL:
        total = result.home_points + result.away_points
        if outcome == "Over":
            margin = total - line
        elif outcome == "Under":
            margin = line - total
        else:
            return None
    else:
        return None
    if abs(margin) < 1e-9:
        return PUSH
    return WON if margin > 0 else LOST


def profit(status: str, price: float) -> float:
    """Result of a 1-unit bet: price - 1 when it wins, -1 when it loses, 0 otherwise."""
    if status == WON:
        return price - 1.0
    if status == LOST:
        return -1.0
    return 0.0


@dataclass
class ClosingLine:
    """Every book's closing prices for one market line of one match."""

    market: str
    line: float | None
    names: tuple[str, ...]                                  # outcome names; names[0] is "side A"
    prices: dict[str, tuple[float, ...]] = field(default_factory=dict)  # book -> prices, `names` order
    no_vig: dict[str, dict[str, float]] = field(default_factory=dict)   # sane-margin books only

    def fair(self, exclude: str | None = None) -> tuple[dict[str, float] | None, list[str]]:
        """Consensus fair probabilities without `exclude`'s own prices, and the books used."""
        books = [b for b in self.no_vig if exclude is None or book_key(b) != book_key(exclude)]
        if not books:
            return None, []
        return consensus([self.no_vig[b] for b in books], self.names), books

    def price_at(self, book: str, outcome: str) -> float | None:
        for name, prices in self.prices.items():
            if book_key(name) == book_key(book) and outcome in self.names:
                return prices[self.names.index(outcome)]
        return None


def closing_lines(markets: list[BookMarket], devig_method: str, max_overround: float) -> list[ClosingLine]:
    """Group closing BookMarkets by market line, devigging each book with a sane margin."""
    groups: dict[tuple, ClosingLine] = {}
    for bm in markets:
        prices = tuple(o.price for o in bm.outcomes)
        if len(prices) < 2 or any(not math.isfinite(p) or p <= 1.0 for p in prices):
            continue
        cl = groups.get(bm.signature)
        if cl is None:
            cl = groups[bm.signature] = ClosingLine(bm.market, bm.line, tuple(o.name for o in bm.outcomes))
        by_name = {o.name: o.price for o in bm.outcomes}
        ordered = tuple(by_name[n] for n in cl.names)
        cl.prices[bm.bookmaker] = ordered
        if not 1.0 - _MARGIN_TOLERANCE <= quant.overround(ordered) <= max_overround:
            continue  # junk margin: shown in prices, kept out of the fair line (as in the analyzer)
        try:
            cl.no_vig[bm.bookmaker] = dict(zip(cl.names, quant.devig(ordered, devig_method), strict=True))
        except ValueError:
            continue
    return list(groups.values())


@dataclass(frozen=True)
class ClosingValue:
    clv: float | None = None            # price * p_close - 1
    close_fair_odds: float | None = None
    close_price: float | None = None    # the alerted book's own closing price
    close_books: tuple[str, ...] = ()   # books behind the closing fair line


def closing_value(market: str, line: float | None, outcome: str, book: str, price: float,
                  lines: list[ClosingLine]) -> ClosingValue:
    """CLV of one pick against the closing line of the same market line (blank if the line moved)."""
    for cl in lines:
        if cl.market != market or outcome not in cl.names:
            continue
        if (cl.line is None) != (line is None) or (line is not None and abs(cl.line - line) > 1e-9):
            continue
        fair, books = cl.fair(exclude=book)
        close_price = cl.price_at(book, outcome)
        if fair is None:
            return ClosingValue(close_price=close_price)
        p = fair[outcome]
        return ClosingValue(clv=price * p - 1.0, close_fair_odds=1.0 / p, close_price=close_price,
                            close_books=tuple(books))
    return ClosingValue()


def calibration_rows(lines: list[ClosingLine], result: MatchResult, home: str,
                     away: str) -> list[dict[str, object]]:
    """One calibration sample per closing market line: side A's fair probability and whether it won."""
    rows = []
    for cl in lines:
        fair, books = cl.fair()
        if fair is None:
            continue
        side = cl.names[0]
        outcome = grade(cl.market, cl.line, side, home, away, result)
        rows.append({
            "market": cl.market,
            "line": cl.line,
            "side": side,
            "fair": fair[side],
            "n_books": len(books),
            "prices": cl.prices,
            "names": cl.names,
            "result": {WON: 1, LOST: 0}.get(outcome or ""),
        })
    return rows


def book_probabilities(prices: dict[str, tuple[float, ...]], devig_method: str,
                       max_overround: float) -> dict[str, float]:
    """Side A's no-vig probability at each book with a sane margin (for comparing books)."""
    out: dict[str, float] = {}
    for book, quotes in prices.items():
        try:
            if 1.0 - _MARGIN_TOLERANCE <= quant.overround(quotes) <= max_overround:
                out[book] = quant.devig(quotes, devig_method)[0]
        except ValueError:
            continue
    return out

