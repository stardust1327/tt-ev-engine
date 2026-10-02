"""Runtime configuration for the EV engine.

Every setting is an environment variable, so the same code runs on a laptop, in
GitHub Actions or in any container without edits. Defaults live in exactly one
place: the field defaults of `Settings` below.

Secrets - BETSAPI_TOKEN, THE_ODDS_API_KEY and DISCORD_WEBHOOK_URL - are read ONLY
from the environment via os.getenv(). They are never hard-coded, they are scrubbed
from every log line (see __main__.RedactingFormatter), and they must never be
committed. For local runs put them in a git-ignored `.env` file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEVIG_METHODS = ("multiplicative", "additive", "power", "shin")
FAIR_LINE_MODES = ("auto", "sharp", "consensus")
BETSAPI_MARKET_KEYS = ("92_1", "92_2", "92_3")  # table tennis: winner, handicap, total
ODDS_API_MARKET_KEYS = ("h2h", "spreads", "totals")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[\w-]+/?(?:\?.*)?$"
)


class ConfigError(ValueError):
    """A required setting is missing, or a value is malformed / out of range."""


# ---------------------------------------------------------------------------------
# Typed readers. An empty string counts as "unset", so a GitHub Actions expression
# like `${{ vars.EV_THRESHOLD }}` that resolves to "" falls back to the default.
# ---------------------------------------------------------------------------------
def _raw(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _str(name: str, default: str) -> str:
    return _raw(name) or default


def _bool(name: str, default: bool) -> bool:
    raw = _raw(name)
    if raw is None:
        return default
    if raw.lower() in _TRUE:
        return True
    if raw.lower() in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _number(name: str, default, cast, lo, hi):
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a {cast.__name__}, got {raw!r}") from None
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ConfigError(f"{name}={raw} is out of range (allowed: {lo} to {hi})")
    return value


def _float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    return _number(name, default, float, lo, hi)


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    return _number(name, default, int, lo, hi)


def _list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _raw(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader for local runs (KEY=VALUE lines, # comments).

    Variables already present in the environment always win, so CI secrets are
    never overridden by a stray file.
    """
    env_file = Path(path)
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    # --- secrets: repr=False keeps them out of any accidental repr()/log ------------
    betsapi_token: str | None = field(default=None, repr=False)
    the_odds_api_key: str | None = field(default=None, repr=False)
    discord_webhook_url: str | None = field(default=None, repr=False)

    # --- pipeline ------------------------------------------------------------------
    providers: tuple[str, ...] = ("betsapi_tt",)  # ENABLED_PROVIDERS
    dry_run: bool = False                        # log alerts instead of posting
    send_test_alert: bool = False                # post one sample embed and exit
    log_level: str = "INFO"
    request_timeout: float = 15.0                # seconds, every HTTP call

    # --- quant ---------------------------------------------------------------------
    ev_threshold: float = 0.02         # alert when EV >= 2%
    strong_ev_threshold: float = 0.05  # darker green + fire icon at >= 5%
    max_ev: float = 0.15               # EV above this is treated as bad data, not an edge
    devig_method: str = "power"        # multiplicative | additive | power | shin
    fair_line_mode: str = "auto"       # auto (sharp if present, else consensus) | sharp | consensus
    sharp_books: tuple[str, ...] = ()  # e.g. pinnacle (The Odds API); BetsAPI has no Pinnacle
    min_consensus_books: int = 3       # other books needed to build a consensus fair line
    max_overround: float = 1.12        # books with a bigger margin are left out of the fair line
    min_odds: float = 1.10             # only alert prices inside this range
    max_odds: float = 5.00
    max_odds_age_min: float = 10.0     # ignore prices not confirmed within this many minutes
    min_minutes_to_start: float = 2.0  # skip matches about to start
    bet_books: tuple[str, ...] = ()    # only recommend these books (empty = any book)

    # --- alerting ------------------------------------------------------------------
    max_alerts_per_run: int = 20
    state_path: Path = Path(".state/alert_state.json")
    realert_ev_delta: float = 0.01     # re-alert a pick only if its EV rises by 1+ point
    state_ttl_hours: float = 24.0
    discord_username: str = "EV Engine"

    # --- BetsAPI (table tennis) ----------------------------------------------------
    betsapi_base_url: str = "https://api.b365api.com"
    tt_leagues: tuple[str, ...] = ("TT Cup",)  # league-name substrings to keep
    betsapi_league_ids: tuple[str, ...] = ()   # skip the sport-wide scan once known
    betsapi_markets: tuple[str, ...] = ("92_1",)
    betsapi_lookahead_min: int = 180
    betsapi_max_pages: int = 10
    betsapi_max_events: int = 60
    betsapi_max_calls: int = 150               # hard cap per run
    betsapi_min_remaining: int = 100           # stop when the hourly allowance gets this low

    # --- The Odds API (MLB / NFL / ...) ---------------------------------------------
    odds_api_base_url: str = "https://api.the-odds-api.com"
    odds_api_sports: tuple[str, ...] = ("baseball_mlb", "americanfootball_nfl")
    odds_api_regions: tuple[str, ...] = ("us", "eu")  # 'eu' brings in Pinnacle
    odds_api_markets: tuple[str, ...] = ("h2h",)
    odds_api_lookahead_min: int = 1440
    odds_api_min_remaining: int = 50                  # monthly credits floor

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from environment variables, then validate them."""
        d = cls()  # defaults, so each default is written exactly once (above)
        settings = cls(
            betsapi_token=_raw("BETSAPI_TOKEN"),
            the_odds_api_key=_raw("THE_ODDS_API_KEY"),
            discord_webhook_url=_raw("DISCORD_WEBHOOK_URL"),
            providers=tuple(p.lower() for p in _list("ENABLED_PROVIDERS", d.providers)),
            dry_run=_bool("DRY_RUN", d.dry_run),
            send_test_alert=_bool("SEND_TEST_ALERT", d.send_test_alert),
            log_level=_str("LOG_LEVEL", d.log_level).upper(),
            request_timeout=_float("REQUEST_TIMEOUT", d.request_timeout, 1, 120),
            ev_threshold=_float("EV_THRESHOLD", d.ev_threshold, 0.0, 0.99),
            strong_ev_threshold=_float("STRONG_EV_THRESHOLD", d.strong_ev_threshold, 0.0, 0.99),
            max_ev=_float("MAX_EV", d.max_ev, 0.0, 10.0),
            devig_method=_str("DEVIG_METHOD", d.devig_method).lower(),
            fair_line_mode=_str("FAIR_LINE_MODE", d.fair_line_mode).lower(),
            sharp_books=_list("SHARP_BOOKS", d.sharp_books),
            min_consensus_books=_int("MIN_CONSENSUS_BOOKS", d.min_consensus_books, 1, 50),
            max_overround=_float("MAX_OVERROUND", d.max_overround, 1.0, 2.0),
            min_odds=_float("MIN_ODDS", d.min_odds, 1.01, 1000),
            max_odds=_float("MAX_ODDS", d.max_odds, 1.01, 1000),
            max_odds_age_min=_float("MAX_ODDS_AGE_MIN", d.max_odds_age_min, 0.5, 1440),
            min_minutes_to_start=_float("MIN_MINUTES_TO_START", d.min_minutes_to_start, 0, 1440),
            bet_books=_list("BET_BOOKS", d.bet_books),
            max_alerts_per_run=_int("MAX_ALERTS_PER_RUN", d.max_alerts_per_run, 1, 200),
            state_path=Path(_str("STATE_PATH", str(d.state_path))),
            realert_ev_delta=_float("REALERT_EV_DELTA", d.realert_ev_delta, 0.0, 1.0),
            state_ttl_hours=_float("STATE_TTL_HOURS", d.state_ttl_hours, 1, 24 * 14),
            discord_username=_str("DISCORD_USERNAME", d.discord_username)[:80],
            betsapi_base_url=_str("BETSAPI_BASE_URL", d.betsapi_base_url),
            tt_leagues=_list("TT_LEAGUES", d.tt_leagues),
            betsapi_league_ids=_list("BETSAPI_LEAGUE_IDS", d.betsapi_league_ids),
            betsapi_markets=_list("BETSAPI_MARKETS", d.betsapi_markets),
            betsapi_lookahead_min=_int("BETSAPI_LOOKAHEAD_MIN", d.betsapi_lookahead_min, 5, 7 * 1440),
            betsapi_max_pages=_int("BETSAPI_MAX_PAGES", d.betsapi_max_pages, 1, 100),
            betsapi_max_events=_int("BETSAPI_MAX_EVENTS", d.betsapi_max_events, 1, 1000),
            betsapi_max_calls=_int("BETSAPI_MAX_CALLS", d.betsapi_max_calls, 1, 100_000),
            betsapi_min_remaining=_int("BETSAPI_MIN_REMAINING", d.betsapi_min_remaining, 0, 1_000_000),
            odds_api_base_url=_str("ODDS_API_BASE_URL", d.odds_api_base_url),
            odds_api_sports=_list("ODDS_API_SPORTS", d.odds_api_sports),
            odds_api_regions=_list("ODDS_API_REGIONS", d.odds_api_regions),
            odds_api_markets=_list("ODDS_API_MARKETS", d.odds_api_markets),
            odds_api_lookahead_min=_int("ODDS_API_LOOKAHEAD_MIN", d.odds_api_lookahead_min, 5, 30 * 1440),
            odds_api_min_remaining=_int("ODDS_API_MIN_REMAINING", d.odds_api_min_remaining, 0, 10_000_000),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Cross-field checks. Raises ConfigError with a message that names the variable."""
        if not self.providers:
            raise ConfigError("ENABLED_PROVIDERS is empty - enable at least one provider")
        if self.log_level not in LOG_LEVELS:
            raise ConfigError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
        if self.devig_method not in DEVIG_METHODS:
            raise ConfigError(f"DEVIG_METHOD must be one of {', '.join(DEVIG_METHODS)}")
        if self.fair_line_mode not in FAIR_LINE_MODES:
            raise ConfigError(f"FAIR_LINE_MODE must be one of {', '.join(FAIR_LINE_MODES)}")
        if self.strong_ev_threshold < self.ev_threshold:
            raise ConfigError("STRONG_EV_THRESHOLD must be >= EV_THRESHOLD")
        if self.max_ev <= self.ev_threshold:
            raise ConfigError("MAX_EV must be greater than EV_THRESHOLD")
        if self.min_odds >= self.max_odds:
            raise ConfigError("MIN_ODDS must be lower than MAX_ODDS")
        unknown = set(self.betsapi_markets) - set(BETSAPI_MARKET_KEYS)
        if unknown or not self.betsapi_markets:
            raise ConfigError(f"BETSAPI_MARKETS must be a subset of {', '.join(BETSAPI_MARKET_KEYS)}")
        unknown = set(self.odds_api_markets) - set(ODDS_API_MARKET_KEYS)
        if unknown or not self.odds_api_markets:
            raise ConfigError(f"ODDS_API_MARKETS must be a subset of {', '.join(ODDS_API_MARKET_KEYS)}")
        if not self.tt_leagues and not self.betsapi_league_ids:
            raise ConfigError("Set TT_LEAGUES (league-name filter) or BETSAPI_LEAGUE_IDS")
        if not self.dry_run and not self.discord_webhook_url:
            raise ConfigError("DISCORD_WEBHOOK_URL is not set (or set DRY_RUN=true to run without Discord)")
        if self.discord_webhook_url and not _WEBHOOK_RE.match(self.discord_webhook_url):
            raise ConfigError(
                "DISCORD_WEBHOOK_URL does not look like a Discord webhook "
                "(expected https://discord.com/api/webhooks/<id>/<token>)"
            )

    def secrets(self) -> tuple[str, ...]:
        """Every secret value (and the bare webhook token) for log redaction."""
        values = [v for v in (self.betsapi_token, self.the_odds_api_key, self.discord_webhook_url) if v]
        if self.discord_webhook_url:
            values.append(self.discord_webhook_url.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
        return tuple(v for v in values if len(v) >= 6)
