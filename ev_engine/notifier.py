"""Discord delivery: rich embeds through an incoming webhook.

* POSTs a JSON payload with `requests` to DISCORD_WEBHOOK_URL (read from the environment).
* One embed per edge; up to 10 embeds and 6,000 embed characters per message, per
  Discord's documented limits (title 256, description 4096, 25 fields, field name 256,
  field value 1024, footer 2048).
* Rate limits: on HTTP 429 waits `retry_after` from the JSON body (or the Retry-After
  header) and retries; when X-RateLimit-Remaining hits 0 it waits
  X-RateLimit-Reset-After before the next POST, as Discord recommends.
* HTTP 404 means the webhook was deleted - Discord asks clients to stop using it, so we stop.
* allowed_mentions is empty, so nothing in a player name can ever ping anyone.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from . import quant
from .config import Settings
from .http_client import parse_retry_after
from .models import MARKET_LABELS, Edge

log = logging.getLogger(__name__)

GREEN = 5763719        # 0x57F287, Discord green: a qualifying +EV edge
DARK_GREEN = 2067276   # 0x1F8B4C, deeper green: strong edge (EV >= STRONG_EV_THRESHOLD)
BLURPLE = 5793266      # 0x5865F2, test alerts only

MAX_EMBEDS_PER_MESSAGE = 10
MAX_CHARS_PER_MESSAGE = 6000
LIMIT_TITLE, LIMIT_DESCRIPTION, LIMIT_FIELD_NAME, LIMIT_FIELD_VALUE, LIMIT_FOOTER = 256, 4096, 256, 1024, 2048


class DiscordError(RuntimeError):
    """Delivery failed in a way retrying this run won't fix."""


@dataclass
class DeliveryResult:
    delivered: list[Edge]
    error: str | None = None


def _escape(text: str) -> str:
    """Escape Discord markdown so a name like 'Li_Wei' renders literally."""
    return re.sub(r"([\\*_~`|>])", r"\\\1", text)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _field(name: str, value: str, inline: bool = True) -> dict:
    return {"name": _clip(name, LIMIT_FIELD_NAME), "value": _clip(value or "-", LIMIT_FIELD_VALUE), "inline": inline}


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds}s" if seconds < 90 else f"{round(seconds / 60)} min"


def embed_chars(embed: dict) -> int:
    """Characters Discord counts toward the 6,000-per-message limit."""
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len((embed.get("footer") or {}).get("text", ""))
    total += len((embed.get("author") or {}).get("name", ""))
    total += sum(len(f["name"]) + len(f["value"]) for f in embed.get("fields", []))
    return total


def build_embed(edge: Edge, now: datetime, strong_threshold: float, *, test: bool = False) -> dict:
    """One rich embed per edge: players, +EV %, recommended book and the raw odds."""
    strong = edge.ev >= strong_threshold
    if test:
        icon, color, prefix = "🧪", BLURPLE, "TEST · "
    else:
        icon, color, prefix = ("🔥", DARK_GREEN, "") if strong else ("🟢", GREEN, "")

    ev_pct = f"+{edge.ev:.1%}"
    players = _escape(f"{edge.event.home} vs {edge.event.away}")
    pick = _escape(edge.selection)
    book = _escape(edge.bookmaker)
    odds = f"{edge.price:.2f} ({quant.decimal_to_american(edge.price)})"
    start = int(edge.event.start_time.timestamp())

    fields = [
        _field("Players", players, inline=False),
        _field("League", _escape(edge.event.league)),
        _field("Starts", f"<t:{start}:t> · <t:{start}:R>"),  # rendered in each viewer's timezone
        _field("Market", MARKET_LABELS.get(edge.market, edge.market)),
        _field("Pick", pick),
        _field("Sportsbook", f"**{book}**"),
        _field("Odds (raw)", odds),
        _field("Expected value", f"**{ev_pct}** per unit staked"),
        _field("True probability", f"{edge.fair_prob:.1%} · fair odds {edge.fair_odds:.2f}"),
        _field("Fair line from", _escape(edge.reference), inline=False),
    ]
    if edge.alternatives:
        others = "\n".join(f"{_escape(b)} {p:.2f} ({ev:+.1%})" for b, p, ev in edge.alternatives[:5])
        fields.append(_field("Also +EV at", others, inline=False))

    footer = edge.event.source
    if edge.updated_at:
        footer += f" · price confirmed {_ago((now - edge.updated_at).total_seconds())} ago"

    return {
        "title": _clip(f"{icon} {prefix}{ev_pct} EV · {players}", LIMIT_TITLE),
        "description": _clip(f"Bet **{pick}** @ **{edge.price:.2f}** on **{book}**", LIMIT_DESCRIPTION),
        "color": color,
        "fields": fields,
        "footer": {"text": _clip(footer, LIMIT_FOOTER)},
        "timestamp": now.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
    }


def batch_embeds(
    items: list, max_embeds: int = MAX_EMBEDS_PER_MESSAGE, max_chars: int = MAX_CHARS_PER_MESSAGE
) -> list[list]:
    """Group (edge, embed) pairs into messages that respect Discord's per-message limits."""
    batches: list[list] = []
    current: list = []
    size = 0
    for item in items:
        chars = embed_chars(item[1])
        if current and (len(current) >= max_embeds or size + chars > max_chars):
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += chars
    if current:
        batches.append(current)
    return batches


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str | None,
        *,
        username: str,
        strong_threshold: float,
        dry_run: bool = False,
        timeout: float = 15.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        max_attempts: int = 5,
        max_wait: float = 60.0,
    ):
        if not dry_run and not webhook_url:
            raise DiscordError("DISCORD_WEBHOOK_URL is not set")
        self.webhook_url = webhook_url
        self.username = username
        self.strong_threshold = strong_threshold
        self.dry_run = dry_run
        self.timeout = timeout
        self.session = session or requests.Session()
        self._sleep = sleep
        self._monotonic = monotonic
        self.max_attempts = max_attempts
        self.max_wait = max_wait
        self._not_before = 0.0  # monotonic time before which the bucket is exhausted

    @classmethod
    def from_settings(cls, settings: Settings, *, session: requests.Session | None = None,
                      sleep: Callable[[float], None] = time.sleep) -> DiscordNotifier:
        return cls(
            settings.discord_webhook_url,
            username=settings.discord_username,
            strong_threshold=settings.strong_ev_threshold,
            dry_run=settings.dry_run,
            timeout=settings.request_timeout,
            session=session,
            sleep=sleep,
        )

    def send(self, edges: list[Edge], now: datetime, *, test: bool = False) -> DeliveryResult:
        """Post edges (best first). Stops at the first unrecoverable failure."""
        items = [(edge, build_embed(edge, now, self.strong_threshold, test=test)) for edge in edges]
        delivered: list[Edge] = []
        for batch in batch_embeds(items):
            payload = {
                "username": self.username,
                "allowed_mentions": {"parse": []},
                "embeds": [embed for _, embed in batch],
            }
            if self.dry_run:
                log.info("[dry run] Discord message (%d embed(s)):\n%s", len(batch),
                         json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                try:
                    self._post(payload)
                except DiscordError as exc:
                    return DeliveryResult(delivered, str(exc))
            delivered.extend(edge for edge, _ in batch)
        return DeliveryResult(delivered)

    def post_embed(self, embed: dict) -> str | None:
        """Post one standalone embed (the tracker's report card). Returns an error message, or None."""
        payload = {"username": self.username, "allowed_mentions": {"parse": []}, "embeds": [embed]}
        if self.dry_run:
            log.info("[dry run] Discord message:\n%s", json.dumps(payload, indent=2, ensure_ascii=False))
            return None
        try:
            self._post(payload)
        except DiscordError as exc:
            return str(exc)
        return None

    # -- transport --------------------------------------------------------------------
    def _wait_for_bucket(self) -> None:
        wait = self._not_before - self._monotonic()
        if wait > 0:
            log.info("Discord: rate-limit bucket empty, waiting %.2fs", wait)
            self._sleep(wait)

    def _track_bucket(self, resp: requests.Response) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset_after = resp.headers.get("X-RateLimit-Reset-After")
        if remaining is None or reset_after is None:
            return
        try:
            if int(float(remaining)) <= 0:
                self._not_before = self._monotonic() + float(reset_after)
        except ValueError:
            pass

    @staticmethod
    def _retry_after(resp: requests.Response) -> tuple[float, bool]:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        wait = body.get("retry_after") if isinstance(body, dict) else None
        if not isinstance(wait, (int, float)):
            wait = parse_retry_after(resp.headers.get("Retry-After"))
        is_global = bool(body.get("global")) if isinstance(body, dict) else False
        return (1.0 if wait is None else float(wait)), is_global

    def _post(self, payload: dict) -> None:
        assert self.webhook_url  # guaranteed by __init__ unless dry_run
        for attempt in range(self.max_attempts):
            self._wait_for_bucket()
            try:
                resp = self.session.post(self.webhook_url, json=payload, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                delay = min(30.0, 2.0**attempt)
                log.warning("Discord: %s - retrying in %.0fs", type(exc).__name__, delay)
                self._sleep(delay)
                continue

            self._track_bucket(resp)
            status = resp.status_code
            if status in (200, 204):
                return
            if status == 429:
                wait, is_global = self._retry_after(resp)
                if wait > self.max_wait:
                    raise DiscordError(f"Discord asked us to wait {wait:.0f}s - giving up for this run")
                log.warning("Discord rate limit (%s): waiting %.2fs", "global" if is_global else "route", wait)
                self._sleep(wait)
                continue
            if status == 404:
                raise DiscordError("webhook not found (HTTP 404) - it was deleted or the URL is wrong; "
                                   "update the DISCORD_WEBHOOK_URL secret")
            if status in (401, 403):
                raise DiscordError(f"webhook rejected (HTTP {status}) - check the DISCORD_WEBHOOK_URL secret")
            if status >= 500:
                delay = min(30.0, 2.0**attempt)
                log.warning("Discord: HTTP %d - retrying in %.0fs", status, delay)
                self._sleep(delay)
                continue
            raise DiscordError(f"message rejected (HTTP {status}): {resp.text[:300]}")
        raise DiscordError(f"delivery failed after {self.max_attempts} attempts")
