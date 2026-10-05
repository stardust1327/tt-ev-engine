"""Turns raw bookmaker prices into +EV edges.

For every market line of an event (e.g. the match-winner market of one TT Cup match):

1. Quality gates. A book's prices are dropped if they are stale, incomplete or invalid.
   Books whose margin is outside 1.00-MAX_OVERROUND are still allowed as *bet targets*
   but are kept out of the *fair line* (a junk margin makes a junk reference).
2. Fair line ("true probability"), built per target book so a book is never measured
   against its own prices:
      sharp mode      devig a trusted sharp book (SHARP_BOOKS, e.g. Pinnacle on The Odds API);
      consensus mode  devig every OTHER book and take the per-outcome median (leave-one-out),
                      then renormalize. Needs MIN_CONSENSUS_BOOKS other books.
      auto (default)  sharp if one is present, otherwise consensus.
3. EV = decimal_odds * true_probability - 1 for every outcome at every book.
4. Keep the best qualifying price per outcome (the recommended book) and list any other
   +EV books as alternatives. Prices outside MIN_ODDS-MAX_ODDS and EVs above MAX_EV
   (almost always stale, reversed or mismatched data) are recorded as skips, not alerts.

Why not devig the book you're betting into? Because EV against your own book's no-vig
line is guaranteed negative - with multiplicative devig it is exactly 1/overround - 1.
An edge only exists when one book's price disagrees with the market's fair price.

Plugging in your own model: `_fair_line()` is the single place where "true probability"
is decided. Return a FairLine built from model probabilities there to price edges
against a model instead of the market.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from . import quant
from .config import Settings
from .models import MARKET_LABELS, BookMarket, Edge, Event, RunReport, format_line

log = logging.getLogger(__name__)

_MARGIN_TOLERANCE = 1e-9


@dataclass(frozen=True)
class FairLine:
    probs: dict[str, float]  # outcome name -> fair probability (sums to 1)
    label: str               # shown in the alert, e.g. "Consensus of 4 books (median, power devig)"


def describe(event: Event, bm: BookMarket, *, book: bool = True) -> str:
    """Short location string used in skip reasons and logs."""
    market = MARKET_LABELS.get(bm.market, bm.market)
    if bm.line is not None:
        market += f" {format_line(bm.line)}"
    parts = [event.matchup, bm.bookmaker, market] if book else [event.matchup, market]
    return " · ".join(parts)


def find_edges(event: Event, settings: Settings, now: datetime, report: RunReport) -> list[Edge]:
    """Every +EV edge in one event (at most one per outcome per market line)."""
    minutes_to_start = (event.start_time - now).total_seconds() / 60.0
    if minutes_to_start < settings.min_minutes_to_start:
        report.skip(
            "starting",
            f"{event.matchup}: starts in {minutes_to_start:.0f} min "
            f"(MIN_MINUTES_TO_START={settings.min_minutes_to_start:g})",
        )
        return []

    # Group the same bet across books: same market type, same line, same outcome names.
    groups: dict[tuple, list[BookMarket]] = defaultdict(list)
    for bm in event.markets:
        groups[bm.signature].append(bm)

    edges: list[Edge] = []
    for books in groups.values():
        report.markets_evaluated += len(books)
        edges.extend(_scan_market(event, books, settings, now, report))
    return edges


def _price_problem(bm: BookMarket, settings: Settings, now: datetime) -> tuple[str, str] | None:
    """(category, reason) if this book's prices can't be trusted right now, else None."""
    if len(bm.outcomes) < 2:
        return "bad_price", "incomplete market (fewer than two prices)"
    if any(not math.isfinite(o.price) or o.price <= 1.0 for o in bm.outcomes):
        return "bad_price", "a price is at or below 1.00"
    if bm.updated_at is None:
        return "stale", "no timestamp for when the price was last confirmed"
    age_min = (now - bm.updated_at).total_seconds() / 60.0
    if age_min > settings.max_odds_age_min:
        return "stale", f"last confirmed {age_min:.0f} min ago (MAX_ODDS_AGE_MIN={settings.max_odds_age_min:g})"
    return None


def _scan_market(
    event: Event, books: list[BookMarket], settings: Settings, now: datetime, report: RunReport
) -> list[Edge]:
    targets: list[BookMarket] = []            # fresh, valid prices: can be bet into
    references: list[BookMarket] = []         # ...and a sane margin: can inform the fair line
    no_vig: dict[int, dict[str, float]] = {}  # id(book market) -> devigged probabilities

    # 1) Quality gates + devig every eligible reference once.
    for bm in books:
        problem = _price_problem(bm, settings, now)
        if problem:
            report.skip(problem[0], f"{describe(event, bm)}: {problem[1]}")
            continue
        targets.append(bm)
        prices = [o.price for o in bm.outcomes]
        margin = quant.overround(prices)
        if not (1.0 - _MARGIN_TOLERANCE <= margin <= settings.max_overround):
            report.skip(
                "overround",
                f"{describe(event, bm)}: overround {margin:.3f} outside 1.000-{settings.max_overround:.3f}, "
                "excluded from the fair line",
            )
            continue
        try:
            probs = quant.devig(prices, settings.devig_method)
        except ValueError as exc:
            report.skip("devig_failed", f"{describe(event, bm)}: {exc}")
            continue
        references.append(bm)
        no_vig[id(bm)] = {o.name: p for o, p in zip(bm.outcomes, probs, strict=True)}
    report.record_coverage(books, references, now)

    # 2-3) Fair line per target (leave-one-out) and EV for each of its outcomes.
    candidates: dict[str, list[tuple[float, float, BookMarket, float, str]]] = defaultdict(list)
    first_gap: str | None = None
    priced_targets = 0
    for target in targets:
        if settings.bet_books and not any(target.matches_book(b) for b in settings.bet_books):
            continue  # not a book you can bet at
        fair = _fair_line(target, references, no_vig, settings)
        if isinstance(fair, str):
            first_gap = first_gap or fair
            continue
        priced_targets += 1
        for outcome in target.outcomes:
            p = fair.probs[outcome.name]
            ev = quant.expected_value(outcome.price, p)
            if ev < settings.ev_threshold:
                continue
            where = f"{describe(event, target)} · {outcome.name} @ {outcome.price:.2f}"
            if not settings.min_odds <= outcome.price <= settings.max_odds:
                report.skip(
                    "odds_range",
                    f"{where}: EV {ev:+.1%} but price outside MIN_ODDS-MAX_ODDS "
                    f"({settings.min_odds:g}-{settings.max_odds:g})",
                )
                continue
            if ev > settings.max_ev:
                report.skip(
                    "suspect_ev",
                    f"{where}: EV {ev:+.1%} exceeds MAX_EV={settings.max_ev:.0%} - "
                    "likely stale, reversed or mismatched data",
                )
                continue
            candidates[outcome.name].append((ev, outcome.price, target, p, fair.label))

    if targets and priced_targets == 0 and first_gap:
        report.skip("no_fair_line", f"{describe(event, targets[0], book=False)}: {first_gap}")

    # 4) Best book per outcome; the rest become "also +EV at".
    edges: list[Edge] = []
    for outcome_name, options in candidates.items():
        options.sort(key=lambda c: (c[0], c[1]), reverse=True)
        ev, price, target, prob, label = options[0]
        edges.append(
            Edge(
                event=event,
                market=target.market,
                line=target.line,
                outcome=outcome_name,
                bookmaker=target.bookmaker,
                price=price,
                fair_prob=prob,
                ev=ev,
                reference=label,
                updated_at=target.updated_at,
                alternatives=tuple((o[2].bookmaker, o[1], o[0]) for o in options[1:]),
            )
        )
    return edges


def _fair_line(
    target: BookMarket,
    references: list[BookMarket],
    no_vig: dict[int, dict[str, float]],
    settings: Settings,
) -> FairLine | str:
    """Fair probabilities for `target`'s outcomes, or a string saying why there are none."""
    others = [bm for bm in references if bm is not target]
    method = settings.devig_method

    if settings.fair_line_mode in ("auto", "sharp"):
        for sharp in settings.sharp_books:  # priority order
            for bm in others:
                if bm.matches_book(sharp):
                    return FairLine(dict(no_vig[id(bm)]), f"{bm.bookmaker} no-vig line ({method} devig)")
        if settings.fair_line_mode == "sharp":
            if not settings.sharp_books:
                return "FAIR_LINE_MODE=sharp but SHARP_BOOKS is empty"
            return f"no fresh price from a sharp book ({', '.join(settings.sharp_books)})"

    if len(others) < settings.min_consensus_books:
        return (
            f"only {len(others)} other book(s) with fresh, sane prices "
            f"(MIN_CONSENSUS_BOOKS={settings.min_consensus_books})"
        )
    names = [o.name for o in target.outcomes]
    return FairLine(
        consensus([no_vig[id(bm)] for bm in others], names),
        f"Consensus of {len(others)} books (median, {method} devig)",
    )


def consensus(no_vig: Sequence[dict[str, float]], names: Sequence[str]) -> dict[str, float]:
    """Per-outcome median of several books' no-vig probabilities, renormalized to sum to 1.

    The one definition of "the market's fair line", shared by the alerts and by the
    accuracy tracker's closing line, so both measure the same thing.
    """
    medians = {n: statistics.median(probs[n] for probs in no_vig) for n in names}
    total = sum(medians.values())
    return {n: v / total for n, v in medians.items()}
