"""Provider-agnostic data model.

Every odds source is translated into the same few objects, so the math (quant.py),
the edge finder (analyzer.py) and the alerting (notifier.py) never need to know
whether prices came from BetsAPI, The Odds API or a feed you add later:

    Event
      └── BookMarket   one bookmaker's complete price set for one market line
            └── Outcome  selection name + decimal price
    Edge      a +EV price found by the analyzer, ready to alert on
    RunReport counters + skip reasons for one run (logs and the GitHub summary)
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger(__name__)

# Normalized market types shared by every provider.
MONEYLINE = "moneyline"
SPREAD = "spread"
TOTAL = "total"
MARKET_LABELS = {MONEYLINE: "Match Winner", SPREAD: "Handicap", TOTAL: "Total"}

# Human labels for skip categories (used in logs and the run summary).
SKIP_LABELS = {
    "stale": "Stale prices",
    "bad_price": "Missing / invalid prices",
    "overround": "Margin out of bounds (kept out of fair line)",
    "devig_failed": "Devig failed",
    "no_fair_line": "No fair line (too few books)",
    "odds_range": "Edge outside MIN_ODDS-MAX_ODDS",
    "suspect_ev": "EV above MAX_EV (likely bad data)",
    "starting": "Too close to start",
    "no_odds": "No usable pre-match odds",
    "split_line": "Split (Asian) line ignored",
    "api_error": "API error",
    "rate_limit": "Rate limit / call budget reached",
}


def book_key(name: str) -> str:
    """Normalize a bookmaker name/key for matching: 'Bet 365' -> 'bet365'."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def format_line(value: float) -> str:
    """Render a handicap/total line without float noise: 1.5 -> '1.5', 2.0 -> '2'."""
    return f"{value:g}"


@dataclass(frozen=True)
class Outcome:
    name: str     # player/team name, or "Over"/"Under"
    price: float  # decimal odds


@dataclass(frozen=True)
class BookMarket:
    """One bookmaker's complete, mutually exclusive prices for one market line.

    line:        None for moneylines; the HOME side's line for handicaps (away = -line);
                 the total itself for over/unders.
    updated_at:  when the provider last confirmed the price as current (UTC).
    book_id:     the provider's machine key (e.g. 'betfair_ex_eu'); defaults to bookmaker.
    """

    bookmaker: str
    market: str
    line: float | None
    outcomes: tuple[Outcome, ...]
    updated_at: datetime | None
    book_id: str = ""

    @property
    def signature(self) -> tuple:
        """Two BookMarkets with the same signature are the same bet at different books."""
        return (self.market, self.line, frozenset(o.name for o in self.outcomes))

    def matches_book(self, name: str) -> bool:
        wanted = book_key(name)
        return wanted in {book_key(self.bookmaker), book_key(self.book_id or self.bookmaker)}


@dataclass(frozen=True)
class Event:
    provider: str          # registry name, e.g. 'betsapi_tt'
    source: str            # display name, e.g. 'BetsAPI'
    event_id: str
    sport: str
    league: str
    home: str
    away: str
    start_time: datetime   # UTC
    markets: tuple[BookMarket, ...] = ()

    @property
    def matchup(self) -> str:
        return f"{self.home} vs {self.away}"


@dataclass(frozen=True)
class Edge:
    """A price whose expected value clears EV_THRESHOLD against the fair line."""

    event: Event
    market: str
    line: float | None
    outcome: str
    bookmaker: str             # the recommended book: best qualifying price
    price: float               # decimal odds at that book
    fair_prob: float           # the "true probability" used for EV
    ev: float                  # decimal_odds * fair_prob - 1
    reference: str             # how the fair line was built
    updated_at: datetime | None
    alternatives: tuple[tuple[str, float, float], ...] = ()  # (book, price, ev) also +EV

    @property
    def fair_odds(self) -> float:
        return 1.0 / self.fair_prob

    @property
    def selection(self) -> str:
        """Human-readable pick: 'Ivanov D.', 'Ivanov D. -1.5', 'Over 74.5'."""
        if self.market == SPREAD and self.line is not None:
            line = self.line if self.outcome == self.event.home else -self.line
            return f"{self.outcome} {line:+g}"
        if self.market == TOTAL and self.line is not None:
            return f"{self.outcome} {format_line(self.line)}"
        return self.outcome

    @property
    def key(self) -> str:
        """Stable identity of the pick (not the book) - used for alert de-duplication."""
        line = "-" if self.line is None else format_line(self.line)
        return f"{self.event.provider}:{self.event.event_id}:{self.market}:{line}:{self.outcome}"


@dataclass(frozen=True)
class Skip:
    category: str
    detail: str


@dataclass
class RunReport:
    """Everything that happened in one run - feeds the logs and the GitHub step summary."""

    started_at: datetime
    events_scanned: int = 0
    markets_evaluated: int = 0
    edges: list[Edge] = field(default_factory=list)
    alerts_sent: int = 0
    suppressed: int = 0
    deferred: int = 0
    skips: list[Skip] = field(default_factory=list)
    skip_counts: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    api_usage: dict[str, str] = field(default_factory=dict)

    MAX_SKIP_DETAILS = 300  # keep memory bounded; counts stay exact

    def skip(self, category: str, detail: str) -> None:
        """Record exactly why something was not evaluated (or not alerted)."""
        self.skip_counts[category] += 1
        if len(self.skips) < self.MAX_SKIP_DETAILS:
            self.skips.append(Skip(category, detail))
        log.debug("skip [%s] %s", category, detail)
