"""The Settings annotation: confirms from the run page which repository variables took effect."""

import responses

from ev_engine.runner import run, settings_summary

from conftest import BETSAPI, NOW, make_settings, upcoming


def test_defaults(tmp_path):
    assert settings_summary(make_settings(tmp_path)) == (
        "Picks from: any book (BET_BOOKS not set) | Everything else at the defaults")


def test_changed_values_are_listed(tmp_path):
    settings = make_settings(tmp_path, bet_books=("DraftKings", "Bet365"), min_consensus_books=2,
                             max_odds_age_min=30.0, ev_threshold=0.03)
    assert settings_summary(settings) == (
        "Picks from: DraftKings, Bet365 | Changed from the defaults: "
        "EV_THRESHOLD=0.03, MIN_CONSENSUS_BOOKS=2, MAX_ODDS_AGE_MIN=30")


def test_only_settings_of_enabled_providers(tmp_path):
    us = make_settings(tmp_path, providers=("the_odds_api",), the_odds_api_key="k" * 32,
                       odds_api_lookahead_min=4320, betsapi_lookahead_min=60, sharp_books=("pinnacle",))
    text = settings_summary(us)
    assert "ODDS_API_LOOKAHEAD_MIN=4320" in text and "SHARP_BOOKS=pinnacle" in text
    assert "BETSAPI" not in text


@responses.activate
def test_every_run_emits_the_settings(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    responses.add(responses.GET, f"{BETSAPI}/v3/events/upcoming", json=upcoming())
    assert run(make_settings(tmp_path, dry_run=True, bet_books=("DraftKings",)), now=NOW,
               sleep=lambda _s: None) == 0
    out = capsys.readouterr().out
    assert "::notice title=Settings::Picks from: DraftKings | Everything else at the defaults" in out
    assert "tok_live" not in out
