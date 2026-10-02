"""TT Cup +EV engine: fetch odds, remove the vig, flag +EV prices, alert Discord.

Package layout
--------------
config.py        Environment-driven settings. Secrets come only from env vars.
models.py        Provider-agnostic data model (Event -> BookMarket -> Outcome, Edge).
http_client.py   Rate-limit-aware HTTP client shared by every provider.
quant.py         Pure math: implied probability, devig methods, expected value.
analyzer.py      Builds the fair line for each market and scans prices for +EV.
providers/       One adapter per odds source (BetsAPI TT Cup, The Odds API).
notifier.py      Discord webhook delivery (rich embeds, batching, 429 handling).
state.py         Alert de-duplication between scheduled runs.
runner.py        One scan end to end: providers -> analyzer -> dedupe -> notify.
__main__.py      CLI entry point:  python -m ev_engine [--dry-run] [--test-alert]
"""

__version__ = "1.0.0"
