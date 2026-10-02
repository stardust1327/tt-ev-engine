"""Rate-limit-aware HTTP client shared by every provider.

What it handles so providers don't have to:

* a timeout on every request;
* retries with exponential backoff + jitter on network errors and HTTP 5xx;
* HTTP 429: honours Retry-After (seconds or an HTTP date). If the server asks for a
  longer wait than we'll block a scheduled job for, it raises RateLimitError and the
  provider stops for this run - the next scheduled run simply picks up again;
* quota headers (BetsAPI `X-RateLimit-Remaining`, The Odds API `x-requests-remaining`):
  once the remaining allowance falls to a safety floor we stop calling, so the engine
  never burns the whole hourly/monthly quota;
* a hard per-run call budget and a minimum gap between calls (smooths bursts).

Error messages never include full URLs, because some providers (BetsAPI, The Odds API)
take the API key as a query parameter.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"ev-engine/{__version__}"
_NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)


class ApiError(RuntimeError):
    """A request failed. `fatal` marks errors retrying can't fix (bad token, no permission)."""

    def __init__(self, message: str, *, status: int | None = None, fatal: bool = False):
        super().__init__(message)
        self.status = status
        self.fatal = fatal


class RateLimitError(ApiError):
    """The provider told us to slow down for longer than we're willing to wait this run."""


class BudgetExhausted(ApiError):
    """Our own safety cap was reached (per-run call budget or remaining-quota floor)."""


@dataclass(frozen=True)
class RateLimitPolicy:
    max_retries: int = 3                # retries after the first attempt
    backoff_base: float = 1.0           # seconds; doubles every retry, with jitter
    backoff_cap: float = 20.0           # longest sleep between retries
    max_retry_after: float = 30.0       # longest server-requested wait we honour inline
    min_interval: float = 0.0           # minimum gap between consecutive calls (seconds)
    call_budget: int | None = None      # max HTTP calls per run
    remaining_header: str | None = None # header that reports remaining quota
    min_remaining: int = 0              # stop once remaining quota <= this


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After is either delta-seconds ('120') or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())


class ApiClient:
    """GET-and-decode-JSON with retries, backoff, quota tracking and a call budget."""

    RETRY_STATUSES = frozenset({500, 502, 503, 504})

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        timeout: float,
        policy: RateLimitPolicy,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.policy = policy
        self.session = session or requests.Session()
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_call: float | None = None
        self.calls = 0                       # HTTP responses received this run
        self.remaining: int | None = None    # last quota value reported by the provider

    def usage(self) -> str:
        parts = [f"calls={self.calls}"]
        if self.remaining is not None:
            parts.append(f"remaining={self.remaining}")
        return ", ".join(parts)

    # -- guards -------------------------------------------------------------------------
    def _check_budget(self) -> None:
        p = self.policy
        if p.call_budget is not None and self.calls >= p.call_budget:
            raise BudgetExhausted(f"{self.name}: per-run call budget of {p.call_budget} reached")
        if self.remaining is not None and self.remaining <= p.min_remaining:
            raise BudgetExhausted(
                f"{self.name}: provider reports {self.remaining} calls left (safety floor {p.min_remaining})"
            )

    def _space_calls(self) -> None:
        if self.policy.min_interval <= 0 or self._last_call is None:
            return
        wait = self.policy.min_interval - (self._monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)

    def _backoff(self, attempt: int) -> float:
        delay = min(self.policy.backoff_cap, self.policy.backoff_base * (2**attempt))
        return delay * (0.5 + random.random() / 2)  # "equal jitter" avoids synchronized retries

    def _track_quota(self, resp: requests.Response) -> None:
        header = self.policy.remaining_header
        raw = resp.headers.get(header) if header else None
        if raw is None:
            return
        try:
            self.remaining = int(float(raw))
        except ValueError:
            pass

    # -- request ------------------------------------------------------------------------
    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        attempts = self.policy.max_retries + 1

        for attempt in range(attempts):
            self._check_budget()
            self._space_calls()
            last_try = attempt + 1 >= attempts
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout, headers={"User-Agent": USER_AGENT})
            except _NETWORK_ERRORS as exc:
                self._last_call = self._monotonic()
                if last_try:
                    raise ApiError(f"{self.name} {path}: network error ({type(exc).__name__})") from None
                delay = self._backoff(attempt)
                log.warning("%s %s: %s - retrying in %.1fs", self.name, path, type(exc).__name__, delay)
                self._sleep(delay)
                continue

            self._last_call = self._monotonic()
            self.calls += 1
            self._track_quota(resp)
            status = resp.status_code

            if status == 429:
                wait = parse_retry_after(resp.headers.get("Retry-After"))
                wait = self._backoff(attempt) if wait is None else wait
                if last_try or wait > self.policy.max_retry_after:
                    raise RateLimitError(f"{self.name} {path}: HTTP 429 (retry after {wait:.0f}s)", status=429)
                log.warning("%s %s: HTTP 429 - waiting %.1fs before retrying", self.name, path, wait)
                self._sleep(wait)
                continue

            if status in self.RETRY_STATUSES:
                if last_try:
                    raise ApiError(f"{self.name} {path}: HTTP {status} after {attempts} attempts", status=status)
                delay = self._backoff(attempt)
                log.warning("%s %s: HTTP %d - retrying in %.1fs", self.name, path, status, delay)
                self._sleep(delay)
                continue

            if status in (401, 403):
                raise ApiError(
                    f"{self.name} {path}: HTTP {status} - check the API key/token and your plan",
                    status=status,
                    fatal=True,
                )
            if status >= 400:
                raise ApiError(f"{self.name} {path}: HTTP {status}", status=status)

            try:
                return resp.json()
            except ValueError:
                raise ApiError(f"{self.name} {path}: response was not valid JSON", status=status) from None

        raise ApiError(f"{self.name} {path}: retries exhausted")  # pragma: no cover - loop always returns/raises
