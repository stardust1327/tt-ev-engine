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
    for error in report.errors[:8]:
        emit_annotation("error", "EV scan error", error, settings.secrets())
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
