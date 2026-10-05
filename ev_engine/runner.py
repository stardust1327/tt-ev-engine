"""One scan, end to end:

    providers -> normalized events -> analyzer (fair line + EV) -> de-dupe -> Discord
              -> save alert state -> log summary + GitHub Actions step summary

Exit codes: 0 = ok (including "no edges"), 1 = a provider or Discord failed (the Actions
run turns red so you notice), 2 = configuration error.
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import requests

from .analyzer import find_edges
from .config import ConfigError, Settings
from .http_client import ApiError
from .models import MONEYLINE, SKIP_LABELS, Edge, Event, RunReport
from .notifier import DiscordError, DiscordNotifier
from .providers import build_providers
from .state import AlertState

log = logging.getLogger(__name__)


def run(
    settings: Settings,
    *,
    now: datetime | None = None,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    summary_path: str | None = None,
) -> int:
    now = now or datetime.now(timezone.utc)
    report = RunReport(started_at=now)
    log.info(
        "EV scan: providers=%s threshold=%.1f%% devig=%s fair_line=%s%s",
        ",".join(settings.providers), settings.ev_threshold * 100, settings.devig_method,
        settings.fair_line_mode, " [DRY RUN]" if settings.dry_run else "",
    )

    try:
        providers = build_providers(settings, report, session=session, sleep=sleep)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        emit_annotation("error", "Configuration error", str(exc), settings.secrets())
        return 2

    state = AlertState.load(settings.state_path, ttl_hours=settings.state_ttl_hours,
                            realert_ev_delta=settings.realert_ev_delta)

    # 1) Fetch + analyze. A failing provider never stops the others.
    for provider in providers:
        events: list[Event] = []
        try:
            events = provider.fetch_events(now)
        except ApiError as exc:
            log.error("%s: %s", provider.label, exc)
            report.errors.append(f"{provider.label}: {exc}")
        except Exception as exc:  # unexpected payload or a bug: log it, keep the run alive
            log.exception("%s crashed", provider.label)
            report.errors.append(f"{provider.label}: {type(exc).__name__}: {exc}")
        finally:
            report.api_usage[provider.label] = provider.usage()

        report.events_scanned += len(events)
        for event in events:
            try:
                report.edges.extend(find_edges(event, settings, now, report))
            except Exception as exc:  # one malformed event must not sink the run
                log.exception("Analyzer failed on %s", event.matchup)
                report.errors.append(f"analyzer: {event.matchup}: {type(exc).__name__}: {exc}")

    # 2) De-duplicate against earlier runs and cap the burst.
    edges = sorted(report.edges, key=lambda e: e.ev, reverse=True)
    fresh = [e for e in edges if state.should_alert(e)]
    report.suppressed = len(edges) - len(fresh)
    to_send = fresh[: settings.max_alerts_per_run]
    report.deferred = len(fresh) - len(to_send)
    if report.deferred:
        log.warning("%d edge(s) held back by MAX_ALERTS_PER_RUN=%d; they alert next run if still +EV.",
                    report.deferred, settings.max_alerts_per_run)

    # 3) Deliver, then remember what was delivered (never in dry-run mode).
    if to_send:
        try:
            notifier = DiscordNotifier.from_settings(settings, session=session, sleep=sleep)
            result = notifier.send(to_send, now)
        except DiscordError as exc:
            result = None
            report.errors.append(f"Discord: {exc}")
            log.error("Discord delivery failed: %s", exc)
        if result is not None:
            report.alerts_sent = len(result.delivered)
            if result.error:
                log.error("Discord delivery failed: %s", result.error)
                report.errors.append(f"Discord: {result.error}")
            if not settings.dry_run:
                for edge in result.delivered:
                    state.record(edge, now)
    else:
        log.info("No new +EV edges this run.")

    state.prune(now)
    try:
        state.save()
    except OSError as exc:
        log.warning("Could not save alert state (%s); the next run may repeat alerts.", exc)

    _log_summary(report, settings)
    write_step_summary(report, settings, path=summary_path)
    _annotate(report, settings)
    return 1 if report.errors else 0


def send_test_alert(
    settings: Settings,
    *,
    now: datetime | None = None,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Post one clearly-labelled sample embed to check the webhook and the formatting."""
    now = now or datetime.now(timezone.utc)
    table_tennis = "betsapi_tt" in settings.providers
    event = Event(provider="test", source="Test alert", event_id="sample",
                  sport="Table Tennis" if table_tennis else "MLB / NFL",
                  league="TT Cup (sample)" if table_tennis else "MLB / NFL (sample)",
                  home="Player A" if table_tennis else "Home Team",
                  away="Player B" if table_tennis else "Away Team",
                  start_time=now + timedelta(minutes=30))
    edge = Edge(event=event, market=MONEYLINE, line=None, outcome=event.home, bookmaker="Sample Book",
                price=2.10, fair_prob=0.495, ev=2.10 * 0.495 - 1,
                reference="Consensus of 4 books (median, power devig) - sample data", updated_at=now,
                alternatives=(("Other Book", 2.08, 2.08 * 0.495 - 1),))
    try:
        error = DiscordNotifier.from_settings(settings, session=session, sleep=sleep).send([edge], now, test=True).error
    except DiscordError as exc:
        error = str(exc)
    if error:
        log.error("Test alert failed: %s", error)
        emit_annotation("error", "Test alert failed", error, settings.secrets())
        return 1
    outcome = "logged (dry run)" if settings.dry_run else "posted to Discord"
    log.info("Test alert %s.", outcome)
    emit_annotation("notice", "Test alert", f"Sample alert {outcome}.", settings.secrets())
    return 0


def inspect_odds(
    settings: Settings,
    *,
    now: datetime | None = None,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    matches: int = 3,
) -> int:
    """Diagnostics: show what each provider returns for the next few matches. No analysis, no alerts.

    Each match becomes one annotation, so the raw prices, timestamps and flags behind a
    skip reason ("Stale prices", "No fair line") can be read straight off the run page.
    """
    now = now or datetime.now(timezone.utc)
    report = RunReport(started_at=now)
    try:
        providers = build_providers(settings, report, session=session, sleep=sleep)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        emit_annotation("error", "Configuration error", str(exc), settings.secrets())
        return 2
    status = 0
    for provider in providers:
        title = f"Odds sample · {provider.label}"
        try:
            samples = provider.inspect(now, matches)
        except ApiError as exc:
            log.error("%s: %s", provider.label, exc)
            emit_annotation("error", title, str(exc), settings.secrets())
            status = 1
            continue
        if not samples:
            samples = [("No upcoming matches with odds in the look-ahead window", "")]
        for heading, body in samples:
            log.info("%s\n%s", heading, body)
            emit_annotation("notice", title, f"{heading}\n{body}".rstrip(), settings.secrets())
        log.info("API usage - %s: %s", provider.label, provider.usage())
    return status


# -- reporting ------------------------------------------------------------------------
def _escape_command(text: str, *, prop: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def emit_annotation(level: str, title: str, message: str, secrets: tuple[str, ...] = ()) -> None:
    """Print a GitHub Actions workflow command, which shows as an annotation on the run page.

    Annotations are visible in the Actions UI (and API) without opening the logs, so each
    run's outcome - or the exact reason it failed - is one glance away. No-op outside Actions.
    """
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    for secret in sorted(set(secrets), key=len, reverse=True):
        message = message.replace(secret, "***")
    print(f"::{level} title={_escape_command(title, prop=True)}::{_escape_command(message)}", flush=True)


# Settings a user is likely to tune, by environment variable. Shown on every run so a
# changed repository variable can be confirmed from the run page.
_TUNABLES: dict[str, str] = {
    "ev_threshold": "EV_THRESHOLD",
    "strong_ev_threshold": "STRONG_EV_THRESHOLD",
    "max_ev": "MAX_EV",
    "devig_method": "DEVIG_METHOD",
    "fair_line_mode": "FAIR_LINE_MODE",
    "sharp_books": "SHARP_BOOKS",
    "min_consensus_books": "MIN_CONSENSUS_BOOKS",
    "max_overround": "MAX_OVERROUND",
    "min_odds": "MIN_ODDS",
    "max_odds": "MAX_ODDS",
    "max_odds_age_min": "MAX_ODDS_AGE_MIN",
    "min_minutes_to_start": "MIN_MINUTES_TO_START",
    "max_alerts_per_run": "MAX_ALERTS_PER_RUN",
    "realert_ev_delta": "REALERT_EV_DELTA",
    "tt_leagues": "TT_LEAGUES",
    "betsapi_league_ids": "BETSAPI_LEAGUE_IDS",
    "betsapi_markets": "BETSAPI_MARKETS",
    "betsapi_lookahead_min": "BETSAPI_LOOKAHEAD_MIN",
    "odds_api_sports": "ODDS_API_SPORTS",
    "odds_api_regions": "ODDS_API_REGIONS",
    "odds_api_markets": "ODDS_API_MARKETS",
    "odds_api_lookahead_min": "ODDS_API_LOOKAHEAD_MIN",
}


def _setting_text(value: object) -> str:
    if isinstance(value, tuple):
        return ",".join(value) or "(empty)"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def settings_summary(settings: Settings) -> str:
    """'Picks from: DraftKings, FanDuel | Changed from the defaults: MIN_CONSENSUS_BOOKS=2, ...'."""
    defaults = Settings()
    skip_prefix = []
    if "betsapi_tt" not in settings.providers:
        skip_prefix += ["tt_", "betsapi_"]
    if "the_odds_api" not in settings.providers:
        skip_prefix += ["odds_api_"]
    changed = [
        f"{env}={_setting_text(getattr(settings, field))}"
        for field, env in _TUNABLES.items()
        if not field.startswith(tuple(skip_prefix)) and getattr(settings, field) != getattr(defaults, field)
    ]
    picks = ", ".join(settings.bet_books) if settings.bet_books else "any book (BET_BOOKS not set)"
    rest = "Changed from the defaults: " + ", ".join(changed) if changed else "Everything else at the defaults"
    return f"Picks from: {picks} | {rest}"


def _annotate(report: RunReport, settings: Settings) -> None:
    mode = "logged (dry run)" if settings.dry_run else "posted"
    parts = [f"{report.events_scanned} events scanned, {len(report.edges)} +EV edges, "
             f"{report.alerts_sent} alerts {mode}"]
    if report.suppressed:
        parts.append(f"{report.suppressed} already alerted")
    parts += [f"{provider} {usage}" for provider, usage in report.api_usage.items()]
    if report.skip_counts:
        parts.append("skipped: " + ", ".join(
            f"{n}x {SKIP_LABELS.get(c, c)}" for c, n in report.skip_counts.most_common(4)))
    emit_annotation("notice", "EV scan", " | ".join(parts), settings.secrets())
    if report.edges:
        top = sorted(report.edges, key=lambda e: e.ev, reverse=True)
        lines = [_edge_line(e) for e in top[:5]] + ([f"+{len(top) - 5} more"] if len(top) > 5 else [])
        emit_annotation("notice", "Edges", "\n".join(lines), settings.secrets())
    coverage = coverage_summary(report, settings)
    if coverage:
        emit_annotation("notice", "Book coverage", coverage, settings.secrets())
    emit_annotation("notice", "Settings", settings_summary(settings), settings.secrets())
    for error in report.errors[:8]:
        emit_annotation("error", "EV scan error", error, settings.secrets())


def _edge_line(e: Edge) -> str:
    """e.g. '+3.1% · Petrov A. @ 2.10 on DraftKings (fair 2.04; <reference>) · Ivanov D. vs Petrov A. · 21:30 UTC'."""
    return (f"{e.ev:+.1%} · {e.selection} @ {e.price:.2f} on {e.bookmaker} (fair {e.fair_odds:.2f}; {e.reference}) · "
            f"{e.event.matchup} · {e.event.start_time:%H:%M} UTC")


def _books(n: int) -> str:
    return f"{n} book" if n == 1 else f"{n} books"


def _depth(counter: Counter) -> str:
    """{2: 9, 3: 4} -> '2 books ×9, 3 books ×4'."""
    return ", ".join(f"{_books(n)} ×{lines}" for n, lines in sorted(counter.items()))


def _fair_line_needs(settings: Settings) -> str:
    sharp = ", ".join(settings.sharp_books)
    if settings.fair_line_mode == "sharp":
        return f"Sharp mode needs a fresh price from {sharp or 'SHARP_BOOKS (empty!)'}"
    needs = f"A consensus needs {_books(settings.min_consensus_books)} besides the one being bet (MIN_CONSENSUS_BOOKS)"
    if sharp and settings.fair_line_mode == "auto":
        needs += f", or a fresh price from {sharp}"
    return needs


def coverage_summary(report: RunReport, settings: Settings, *, max_books: int = 12) -> str | None:
    """Book depth in one line: how many books priced each market line, and which books they were.

    This is what decides whether a fair line can be built at all, so it is the first thing
    to look at when every market is skipped with "No fair line (too few books)".
    """
    if not report.books_quoting:
        return None
    lines = sum(report.books_quoting.values())
    ranked = sorted(report.book_quotes.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    books = ", ".join(
        f"{book} {quoted}/{report.book_usable[book]}{_book_note(report, book)}"
        for book, quoted in ranked[:max_books]
    )
    if len(ranked) > max_books:
        books += f", +{len(ranked) - max_books} more"
    return (
        f"{lines} market line(s). Books quoting each: {_depth(report.books_quoting)}. "
        f"Usable for the fair line (fresh, sane margin): {_depth(report.books_usable)}. "
        f"By book (lines quoted/usable, median age since last check): {books}. {_fair_line_needs(settings)}."
    )


def _age(minutes: float) -> str:
    minutes = max(0.0, minutes)  # a provider clock a little ahead of ours is not "in the future" for a reader
    return f"{minutes:.0f}m" if minutes < 120 else f"{minutes / 60:.1f}h"


def _book_note(report: RunReport, book: str) -> str:
    notes = []
    if (age := report.median_age(book)) is not None:
        notes.append(_age(age))
    if report.book_unmoved[book]:
        notes.append(f"{report.book_unmoved[book]} still at opening price")
    return f" ({'; '.join(notes)})" if notes else ""


def _log_summary(report: RunReport, settings: Settings) -> None:
    log.info(
        "Done: %d event(s), %d book-market(s) evaluated, %d +EV edge(s) -> %d alert(s) %s, "
        "%d already alerted, %d held back.",
        report.events_scanned, report.markets_evaluated, len(report.edges), report.alerts_sent,
        "logged (dry run)" if settings.dry_run else "posted", report.suppressed, report.deferred,
    )
    if report.skip_counts:
        log.info("Skipped: %s", "; ".join(f"{n}x {SKIP_LABELS.get(c, c)}" for c, n in report.skip_counts.most_common()))
    for provider, usage in report.api_usage.items():
        log.info("API usage - %s: %s", provider, usage)
    coverage = coverage_summary(report, settings)
    if coverage:
        log.info("Book coverage: %s", coverage)
    log.info("Settings: %s", settings_summary(settings))
    for error in report.errors:
        log.error("Run error: %s", error)


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def write_step_summary(report: RunReport, settings: Settings, path: str | None = None) -> None:
    """Append a Markdown run report to the GitHub Actions job summary (no-op elsewhere)."""
    path = path or os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    mode = " (dry run)" if settings.dry_run else ""
    lines = [
        f"### EV scan · {report.started_at:%Y-%m-%d %H:%M} UTC{mode}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Providers | {_cell(', '.join(settings.providers))} |",
        f"| Events scanned | {report.events_scanned} |",
        f"| Book-markets evaluated | {report.markets_evaluated} |",
        f"| +EV edges (EV ≥ {settings.ev_threshold:.1%}) | {len(report.edges)} |",
        f"| Alerts {'logged' if settings.dry_run else 'posted'} | {report.alerts_sent} |",
        f"| Already alerted (suppressed) | {report.suppressed} |",
        f"| Held back (MAX_ALERTS_PER_RUN) | {report.deferred} |",
    ]
    lines += [f"| API usage · {_cell(p)} | {_cell(u)} |" for p, u in report.api_usage.items()]

    if report.edges:
        lines += ["", "**Edges**", "", "| EV | Pick | Book | Odds | Fair odds | Match | Starts (UTC) |",
                  "|---|---|---|---|---|---|---|"]
        for e in sorted(report.edges, key=lambda e: e.ev, reverse=True)[:25]:
            lines.append(
                f"| {e.ev:+.1%} | {_cell(e.selection)} | {_cell(e.bookmaker)} | {e.price:.2f} | "
                f"{e.fair_odds:.2f} | {_cell(e.event.matchup)} | {e.event.start_time:%H:%M} |"
            )
    if report.books_quoting:
        lines += ["", "**Book coverage**", "",
                  f"- Books quoting each market line: {_depth(report.books_quoting)}",
                  f"- Usable for the fair line (fresh, sane margin): {_depth(report.books_usable)}",
                  f"- {_fair_line_needs(settings)}",
                  "",
                  "| Book | Lines quoted | Usable for fair line | Median age since check | At opening price |",
                  "|---|---|---|---|---|"]
        ranked = sorted(report.book_quotes.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        lines += [
            f"| {_cell(book)} | {quoted} | {report.book_usable[book]} | "
            f"{_age(age) if (age := report.median_age(book)) is not None else '-'} | {report.book_unmoved[book]} |"
            for book, quoted in ranked[:40]
        ]
    if report.skip_counts:
        lines += ["", "**Skipped**", ""]
        lines += [f"- {n}× {SKIP_LABELS.get(c, c)}" for c, n in report.skip_counts.most_common()]
        examples = report.skips[:15]
        lines += ["", "<details><summary>Examples</summary>", ""]
        lines += [f"- `{s.category}` {_cell(s.detail)}" for s in examples]
        lines += ["", "</details>"]
    if report.errors:
        lines += ["", "**Errors**", ""] + [f"- {_cell(err)}" for err in report.errors]

    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        log.warning("Could not write the step summary (%s).", exc)
