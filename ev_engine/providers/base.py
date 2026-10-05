"""The adapter contract every odds source implements."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import ClassVar

import requests

from ..config import Settings
from ..http_client import ApiClient
from ..models import MARKET_LABELS, BookMarket, Event, MatchResult, RunReport, format_line
from ..quant import overround


class OddsProvider(ABC):
    """Translate one odds source into normalized `Event` objects.

    Contract for implementations:
      * `fetch_events(now)` returns pre-match events inside the provider's look-ahead
        window, each carrying one BookMarket per bookmaker per market line.
      * Expected, recoverable problems (one bad event, a rate limit, a quota floor) are
        recorded with `self.report.skip(...)` and must not raise.
      * Problems retrying can't fix (bad API key) raise ApiError(fatal=True); the runner
        reports them and fails the run so the Actions UI flags it.
      * All HTTP goes through `self.client` (ApiClient) to get retries and rate limiting.
    """

    name: ClassVar[str] = "base"   # registry key used in ENABLED_PROVIDERS
    label: ClassVar[str] = "Base"  # display name in logs and alerts

    def __init__(self, settings: Settings, report: RunReport, client: ApiClient):
        self.settings = settings
        self.report = report
        self.client = client

    @classmethod
    @abstractmethod
    def from_settings(
        cls,
        settings: Settings,
        report: RunReport,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> OddsProvider:
        """Build the provider from settings; raise ConfigError if its credentials are missing."""

    @abstractmethod
    def fetch_events(self, now: datetime) -> list[Event]:
        """Return normalized upcoming events with their bookmaker markets."""

    def usage(self) -> str:
        return self.client.usage()

    # -- accuracy tracker (optional) ----------------------------------------------------
    # A provider that implements both of these can have its alerts graded and its fair
    # line calibrated (ev_engine/tracking). The others are simply skipped by the tracker.
    supports_grading: ClassVar[bool] = False
    results_per_call: ClassVar[int] = 1

    def fetch_results(self, event_ids: Sequence[str]) -> dict[str, MatchResult]:
        """Final status and score for up to `results_per_call` events, keyed by event id (one HTTP call)."""
        raise NotImplementedError

    def closing_markets(self, event: Event) -> tuple[list[BookMarket], Counter]:
        """Each book's last pre-match prices for a finished event (one HTTP call).

        Returns the markets and a count of where the closing prices came from (diagnostics).
        """
        raise NotImplementedError

    def inspect(self, now: datetime, matches: int = 3) -> list[tuple[str, str]]:
        """Diagnostics: (heading, one line per book) for the next few matches, as parsed.

        Providers can override this to show their raw payload instead (see BetsAPI).
        """
        events = sorted(self.fetch_events(now), key=lambda e: e.start_time)[:matches]
        samples = []
        for event in events:
            minutes = (event.start_time - now).total_seconds() / 60.0
            lines = []
            for bm in event.markets:
                market = MARKET_LABELS.get(bm.market, bm.market)
                if bm.line is not None:
                    market += f" {format_line(bm.line)}"
                prices = " / ".join(f"{o.name} {o.price:.2f}" for o in bm.outcomes)
                margin = overround([o.price for o in bm.outcomes]) - 1
                age = (
                    f"confirmed {(now - bm.updated_at).total_seconds() / 60:.0f}m ago"
                    if bm.updated_at else "no timestamp"
                )
                lines.append(f"{bm.bookmaker} · {market}: {prices} · margin {margin:.1%} · {age}")
            samples.append((f"{event.matchup} · {event.league} · starts in {minutes:.0f} min", "\n".join(lines)))
        return samples
