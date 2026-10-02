"""End-to-end: BetsAPI -> analyzer -> Discord -> alert state, plus config and secret handling."""

import json
import logging

import pytest
import responses

from ev_engine.__main__ import RedactingFormatter, main
from ev_engine.config import ConfigError, Settings
from ev_engine.runner import run, send_test_alert

from conftest import BETSAPI, NOW, WEBHOOK, book_odds, fixture, make_settings, summary, upcoming

UPCOMING = f"{BETSAPI}/v3/events/upcoming"
SUMMARY = f"{BETSAPI}/v2/event/odds/summary"


def mock_betsapi(soft_price="2.25"):
    responses.add(responses.GET, UPCOMING, json=upcoming(fixture("e1", 25)))
    responses.add(responses.GET, SUMMARY, json=summary(
        Bet365=book_odds("1.900", "1.900"),
        BWin=book_odds("1.870", "1.930"),
        Betway=book_odds("1.930", "1.870"),
        Unibet=book_odds("1.700", soft_price),   # off-market away price -> the edge
    ))
    responses.add(responses.POST, WEBHOOK, status=204)


def discord_posts():
    return [c for c in responses.calls if c.request.method == "POST"]


@responses.activate
def test_full_run_alerts_once_then_dedupes(tmp_path):
    mock_betsapi()
    settings = make_settings(tmp_path)
    summary_file = tmp_path / "summary.md"

    assert run(settings, now=NOW, sleep=lambda _s: None, summary_path=str(summary_file)) == 0
    posts = discord_posts()
    assert len(posts) == 1
    embed = json.loads(posts[0].request.body)["embeds"][0]
    assert "Petrov A." in embed["description"] and "Unibet" in embed["description"]
    assert embed["title"].startswith(("🟢", "🔥"))
    assert "+EV edges" in summary_file.read_text()
    assert json.loads(settings.state_path.read_text())["alerts"]  # remembered

    # Same prices 15 minutes later: no duplicate alert.
    assert run(settings, now=NOW, sleep=lambda _s: None, summary_path=str(summary_file)) == 0
    assert len(discord_posts()) == 1


@responses.activate
def test_dry_run_posts_nothing_and_remembers_nothing(tmp_path, caplog):
    mock_betsapi()
    settings = make_settings(tmp_path, dry_run=True)
    with caplog.at_level(logging.INFO):
        assert run(settings, now=NOW, sleep=lambda _s: None) == 0
    assert discord_posts() == []
    assert "[dry run] Discord message" in caplog.text
    assert json.loads(settings.state_path.read_text())["alerts"] == {}


@responses.activate
def test_no_edges_means_no_post(tmp_path):
    mock_betsapi(soft_price="1.950")
    assert run(make_settings(tmp_path), now=NOW, sleep=lambda _s: None) == 0
    assert discord_posts() == []


@responses.activate
def test_bad_token_fails_the_run_without_posting(tmp_path):
    responses.add(responses.GET, UPCOMING, json={"success": 0, "error": "AUTHORIZE_FAILED"})
    assert run(make_settings(tmp_path), now=NOW, sleep=lambda _s: None) == 1
    assert discord_posts() == []


@responses.activate
def test_discord_failure_fails_the_run_and_alert_retries_next_time(tmp_path):
    responses.add(responses.GET, UPCOMING, json=upcoming(fixture("e1", 25)))
    responses.add(responses.GET, SUMMARY, json=summary(
        Bet365=book_odds("1.9", "1.9"), BWin=book_odds("1.87", "1.93"),
        Betway=book_odds("1.93", "1.87"), Unibet=book_odds("1.70", "2.25")))
    responses.add(responses.POST, WEBHOOK, status=404)
    settings = make_settings(tmp_path)
    assert run(settings, now=NOW, sleep=lambda _s: None) == 1
    assert json.loads(settings.state_path.read_text())["alerts"] == {}  # not recorded -> retried


@responses.activate
def test_test_alert_posts_one_labelled_embed(tmp_path):
    responses.add(responses.POST, WEBHOOK, status=204)
    assert send_test_alert(make_settings(tmp_path), now=NOW) == 0
    embed = json.loads(discord_posts()[0].request.body)["embeds"][0]
    assert embed["title"].startswith("🧪 TEST")


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BETSAPI_TOKEN", "tok_live_1234567890")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setenv("EV_THRESHOLD", "0.03")
    monkeypatch.setenv("BET_BOOKS", "Bet365, Betway ,")
    monkeypatch.setenv("SHARP_BOOKS", "")          # empty GitHub variable -> default
    monkeypatch.setenv("DEVIG_METHOD", "SHIN")
    s = Settings.from_env()
    assert s.ev_threshold == 0.03 and s.bet_books == ("Bet365", "Betway")
    assert s.sharp_books == () and s.devig_method == "shin"
    assert "tok_live" not in repr(s) and "webhooks" not in repr(s)


@pytest.mark.parametrize("name,value", [
    ("EV_THRESHOLD", "two percent"),
    ("DEVIG_METHOD", "magic"),
    ("DISCORD_WEBHOOK_URL", "https://example.com/hook"),
    ("BETSAPI_MARKETS", "92_9"),
    ("DRY_RUN", "maybe"),
])
def test_bad_config_is_rejected(monkeypatch, name, value):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_missing_webhook_is_a_config_error_unless_dry_run(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("DRY_RUN", "true")
    assert Settings.from_env().dry_run


def test_main_returns_2_on_config_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("EV_THRESHOLD", "abc")
    assert main([]) == 2


def test_missing_betsapi_token_is_reported(monkeypatch, tmp_path):
    settings = make_settings(tmp_path, betsapi_token=None)
    assert run(settings, now=NOW) == 2


def test_secrets_never_reach_the_logs():
    formatter = RedactingFormatter("%(message)s", ("tok_live_1234567890", WEBHOOK))
    record = logging.LogRecord("x", logging.ERROR, __file__, 1,
                               "GET https://api.b365api.com/v3/events/upcoming?token=%s failed, hook %s",
                               ("tok_live_1234567890", WEBHOOK), None)
    line = formatter.format(record)
    assert "tok_live" not in line and "webhooks" not in line and line.count("***") == 2
