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
from collections.abc import Iterable
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
class MatchResult:
    """How an event finished, as its provider reports it - used to grade picks.

    status:  "ended" (a winner is known), "void" (cancelled, walkover, retired, ...: bets are
             refunded) or "pending" (not finished, or no final score yet).
    home_score / away_score: games won in table tennis.
    periods: points per game as (home, away), needed for handicaps and totals.
    """

    event_id: str
    status: str
    home_score: int | None = None
    away_score: int | None = None
    periods: tuple[tuple[int, int], ...] = ()
    detail: str = ""  # the provider's own status, e.g. "Retired"

    @property
    def home_points(self) -> int:
        return sum(h for h, _ in self.periods)

    @property
    def away_points(self) -> int:
        return sum(a for _, a in self.periods)

    @property
    def score(self) -> str:
        """'3-1 (11-7 9-11 11-5 11-8)'."""
        if self.home_score is None or self.away_score is None:
            return self.detail
        games = " ".join(f"{h}-{a}" for h, a in self.periods)
        return f"{self.home_score}-{self.away_score}" + (f" ({games})" if games else "")


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
    # Book depth per market line - answers "is there enough market to build a fair line?"
    books_quoting: Counter = field(default_factory=Counter)  # n books quoting a line -> number of lines
    books_usable: Counter = field(default_factory=Counter)   # n fresh, sane-margin books -> number of lines
    book_quotes: Counter = field(default_factory=Counter)    # bookmaker -> lines it quoted
    book_usable: Counter = field(default_factory=Counter)    # bookmaker -> lines where it could inform the fair line
    book_ages: dict[str, list[float]] = field(default_factory=dict)  # bookmaker -> minutes since each confirmation
    book_unmoved: Counter = field(default_factory=Counter)   # bookmaker -> lines still at the opening price

    MAX_SKIP_DETAILS = 300  # keep memory bounded; counts stay exact

    def skip(self, category: str, detail: str) -> None:
        """Record exactly why something was not evaluated (or not alerted)."""
        self.skip_counts[category] += 1
        if len(self.skips) < self.MAX_SKIP_DETAILS:
            self.skips.append(Skip(category, detail))
        log.debug("skip [%s] %s", category, detail)

    def record_coverage(self, quoting: Iterable[BookMarket], usable: Iterable[BookMarket], now: datetime) -> None:
        """Note which books priced one market line, and which of them were fit for the fair line."""
        quoting = list(quoting)
        quoted_by = {bm.bookmaker for bm in quoting}
        usable_by = {bm.bookmaker for bm in usable}
        self.books_quoting[len(quoted_by)] += 1
        self.books_usable[len(usable_by)] += 1
        self.book_quotes.update(quoted_by)
        self.book_usable.update(usable_by)
        for bm in quoting:
            if bm.updated_at is not None:
                self.book_ages.setdefault(bm.bookmaker, []).append((now - bm.updated_at).total_seconds() / 60.0)

    def median_age(self, book: str) -> float | None:
        """Median minutes since `book`'s prices were last confirmed, across this run's lines."""
        ages = sorted(self.book_ages.get(book, ()))
        if not ages:
            return None
        mid = len(ages) // 2
        return ages[mid] if len(ages) % 2 else (ages[mid - 1] + ages[mid]) / 2
