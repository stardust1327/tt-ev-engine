"""Inspect mode: the raw odds behind the skip reasons, shown as run annotations."""

import responses

from ev_engine.__main__ import main
from ev_engine.models import RunReport
from ev_engine.providers.betsapi import BetsApiTableTennisProvider
from ev_engine.runner import inspect_odds

from conftest import BETSAPI, NOW, book_odds, fixture, make_settings, summary, upcoming

TS = int(NOW.timestamp())


def odds_summary():
    draftkings = {  # no check time; the newest record carries a score -> the interesting case
        "matching_dir": "-1",
        "odds": {
            "start": {"92_1": {"home_od": "1.83", "away_od": "1.95", "ss": None, "add_time": str(TS - 4 * 3600)}},
            "end": {"92_1": {"home_od": "1.80", "away_od": "2.00", "ss": "0-0", "add_time": str(TS - 50 * 60)},
                    "92_3": {"over_od": "1.9", "under_od": "1.9", "handicap": "74.5", "add_time": str(TS - 60)}},
        },
    }
    return summary(Bet365=book_odds("1.9", "1.9", checked_ago=120, added_ago=600), DraftKings=draftkings)


def provider(tmp_path, **overrides):
    settings = make_settings(tmp_path, dry_run=True, **overrides)
    return BetsApiTableTennisProvider.from_settings(settings, RunReport(started_at=NOW), sleep=lambda _s: None)


@responses.activate
def test_betsapi_inspect_shows_raw_records(tmp_path):
    responses.add(responses.GET, f"{BETSAPI}/v3/events/upcoming", json=upcoming(fixture("e1", 25), fixture("e2", 40)))
    responses.add(responses.GET, f"{BETSAPI}/v2/event/odds/summary", json=odds_summary())
    samples = provider(tmp_path).inspect(NOW, matches=1)
    assert len(samples) == 1
    heading, body = samples[0]
    assert heading == "Ivanov D. vs Petrov A. · TT Cup · event e1 · starts in 25 min"
    bet365, draftkings = body.split("\n")
    assert bet365 == ("Bet365: matching_dir 1 · 92_1 checked 2m ago · start 1.900/1.900 posted 2.0h ago"
                      " · end 1.9/1.9 posted 10m ago")
    assert draftkings == ("DraftKings: matching_dir -1 · 92_1 no check time · start 1.83/1.95 posted 4.0h ago"
                          " · end 1.80/2.00 posted 50m ago ss='0-0' · also has 92_3")


@responses.activate
def test_inspect_odds_emits_one_notice_per_match(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    responses.add(responses.GET, f"{BETSAPI}/v3/events/upcoming", json=upcoming(fixture("e1", 25), fixture("e2", 40)))
    responses.add(responses.GET, f"{BETSAPI}/v2/event/odds/summary", json=odds_summary())
    settings = make_settings(tmp_path, discord_webhook_url=None, inspect_odds=True)
    assert inspect_odds(settings, now=NOW, sleep=lambda _s: None) == 0
    out = capsys.readouterr().out
    notices = [line for line in out.splitlines() if line.startswith("::notice title=Odds sample · BetsAPI::")]
    assert len(notices) == 2
    assert "starts in 25 min%0ABet365: matching_dir 1" in notices[0]
    assert "tok_live" not in out
    assert not any(call.request.method == "POST" for call in responses.calls)  # never alerts


@responses.activate
def test_inspect_odds_reports_api_errors(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    responses.add(responses.GET, f"{BETSAPI}/v3/events/upcoming", json={"success": 0, "error": "AUTHORIZE_FAILED"})
    assert inspect_odds(make_settings(tmp_path, inspect_odds=True), now=NOW, sleep=lambda _s: None) == 1
    out = capsys.readouterr().out
    assert "::error title=Odds sample · BetsAPI::BetsAPI /v3/events/upcoming: AUTHORIZE_FAILED" in out


@responses.activate
def test_base_inspect_shows_parsed_prices(tmp_path):
    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
    responses.add(responses.GET, url, json=[{
        "id": "abc", "sport_title": "MLB", "commence_time": TS + 3600,
        "home_team": "New York Yankees", "away_team": "Boston Red Sox",
        "bookmakers": [{"key": "draftkings", "title": "DraftKings", "markets": [
            {"key": "h2h", "last_update": TS - 180, "outcomes": [
                {"name": "New York Yankees", "price": 1.91}, {"name": "Boston Red Sox", "price": 1.91}]}]}],
    }])
    from ev_engine.providers.the_odds_api import TheOddsApiProvider

    settings = make_settings(tmp_path, providers=("the_odds_api",), the_odds_api_key="odds_key_123456",
                             odds_api_sports=("baseball_mlb",))
    p = TheOddsApiProvider.from_settings(settings, RunReport(started_at=NOW), sleep=lambda _s: None)
    [(heading, body)] = p.inspect(NOW)
    assert heading == "New York Yankees vs Boston Red Sox · MLB · starts in 60 min"
    assert body == ("DraftKings · Match Winner: New York Yankees 1.91 / Boston Red Sox 1.91 · margin 4.7%"
                    " · confirmed 3m ago")


@responses.activate
def test_cli_inspect_needs_no_webhook(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("BETSAPI_TOKEN", "tok_live_1234567890")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("INSPECT_ODDS", "false")  # main() sets it to true; monkeypatch removes it afterwards
    responses.add(responses.GET, f"{BETSAPI}/v3/events/upcoming", json=upcoming())
    assert main(["--inspect"]) == 0
