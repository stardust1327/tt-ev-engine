"""Command-line entry point.

    python -m ev_engine              one scan (what GitHub Actions runs every 15 minutes)
    python -m ev_engine --dry-run    scan, but log the Discord payloads instead of posting
    python -m ev_engine --test-alert post one sample embed to verify the webhook
    python -m ev_engine --inspect    show the raw odds for the next few matches (diagnostics)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from .config import ConfigError, Settings, load_dotenv
from .runner import emit_annotation, inspect_odds, run, send_test_alert

log = logging.getLogger("ev_engine")


class RedactingFormatter(logging.Formatter):
    """Scrubs secret values from every rendered log line, tracebacks included."""

    converter = time.gmtime  # UTC timestamps

    def __init__(self, fmt: str, secrets: tuple[str, ...]):
        super().__init__(fmt=fmt, datefmt="%Y-%m-%dT%H:%M:%SZ")
        self._secrets = sorted(set(secrets), key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


def configure_logging(level: str, secrets: tuple[str, ...]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # urllib3 logs full request URLs at DEBUG, and BetsAPI/The Odds API carry keys in the query string.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ev_engine", description="Scan odds for +EV edges and alert Discord."
    )
    parser.add_argument("--dry-run", action="store_true", help="log alerts instead of posting to Discord")
    parser.add_argument("--test-alert", action="store_true", help="post one sample alert to verify the webhook")
    parser.add_argument("--inspect", action="store_true", help="show the raw odds for the next few matches")
    args = parser.parse_args(argv)

    load_dotenv()  # local convenience; real environment variables always win
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    if args.test_alert:
        os.environ["SEND_TEST_ALERT"] = "true"
    if args.inspect:
        os.environ["INSPECT_ODDS"] = "true"

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        configure_logging("INFO", ())
        log.error("Configuration error: %s", exc)
        emit_annotation("error", "Configuration error", str(exc))
        return 2

    configure_logging(settings.log_level, settings.secrets())
    try:
        if settings.send_test_alert:
            return send_test_alert(settings)
        return inspect_odds(settings) if settings.inspect_odds else run(settings)
    except Exception:  # last-resort guard: log (redacted) instead of a raw traceback
        log.exception("Unhandled error - aborting this run")
        return 1


if __name__ == "__main__":
    sys.exit(main())
