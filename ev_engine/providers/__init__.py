"""Odds-provider adapters and their registry.

Adding a source = subclass OddsProvider (see base.py), then add the class to REGISTRY.
Users switch sources on with ENABLED_PROVIDERS (comma-separated registry names).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import requests

from ..config import ConfigError, Settings
from ..models import RunReport
from .base import OddsProvider
from .betsapi import BetsApiTableTennisProvider
from .the_odds_api import TheOddsApiProvider

REGISTRY: dict[str, type[OddsProvider]] = {
    BetsApiTableTennisProvider.name: BetsApiTableTennisProvider,  # "betsapi_tt"
    TheOddsApiProvider.name: TheOddsApiProvider,                  # "the_odds_api"
}


def build_providers(
    settings: Settings,
    report: RunReport,
    *,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[OddsProvider]:
    """Instantiate every enabled provider. Raises ConfigError for unknown names or missing keys."""
    providers: list[OddsProvider] = []
    for name in settings.providers:
        cls = REGISTRY.get(name)
        if cls is None:
            raise ConfigError(f"Unknown provider {name!r} in ENABLED_PROVIDERS. Available: {', '.join(REGISTRY)}")
        providers.append(cls.from_settings(settings, report, session=session, sleep=sleep))
    return providers


__all__ = ["REGISTRY", "OddsProvider", "build_providers"]
