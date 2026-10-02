"""The adapter contract every odds source implements."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from typing import ClassVar

import requests

from ..config import Settings
from ..http_client import ApiClient
from ..models import Event, RunReport


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
