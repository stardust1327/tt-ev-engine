"""GitHub Actions annotations: run results and errors visible without opening the logs."""

import responses

from ev_engine.__main__ import main
from ev_engine.runner import emit_annotation, run

from conftest import BETSAPI, NOW, WEBHOOK, book_odds, fixture, make_settings, summary, upcoming


def test_annotations_only_inside_github_actions(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    emit_annotation("notice", "EV scan", "hello")
    assert capsys.readouterr().out == ""


def test_annotation_escaping_and_redaction(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    emit_annotation("error", "Scan: odds, MLB", "line one\nkey=sk_secret_123 100%", ("sk_secret_123",))
    assert capsys.readouterr().out.strip() == "::error title=Scan%3A odds%2C MLB::line one%0Akey=*** 100%25"


@responses.activate
def test_run_emits_a_notice_with_the_result(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    responses.add(responses.GET, f"{BETSAPI}/v3/events/upcoming", json=upcoming(fixture("e1", 25)))
    responses.add(responses.GET, f"{BETSAPI}/v2/event/odds/summary", json=summary(
        Bet365=book_odds("1.9", "1.9"), BWin=book_odds("1.87", "1.93"),
        Betway=book_odds("1.93", "1.87"), Unibet=book_odds("1.70", "2.25")))
    responses.add(responses.POST, WEBHOOK, status=204)
    assert run(make_settings(tmp_path), now=NOW, sleep=lambda _s: None) == 0
    notice = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::notice")]
    assert len(notice) == 1
    assert "1 events scanned%2C" not in notice[0]  # commas are only escaped in the title
    assert "1 events scanned, 1 +EV edges, 1 alerts posted" in notice[0]
    assert "BetsAPI calls=2" in notice[0]
    assert "tok_live" not in notice[0]


@responses.activate
def test_provider_error_becomes_an_error_annotation(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    responses.add(responses.GET, f"{BETSAPI}/v3/events/upcoming", json={"success": 0, "error": "AUTHORIZE_FAILED"})
    assert run(make_settings(tmp_path), now=NOW, sleep=lambda _s: None) == 1
    out = capsys.readouterr().out
    assert "::error title=EV scan error::BetsAPI" in out and "AUTHORIZE_FAILED" in out


def test_config_error_becomes_an_error_annotation(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("EV_THRESHOLD", "abc")
    assert main([]) == 2
    assert "::error title=Configuration error::EV_THRESHOLD must be a float" in capsys.readouterr().out


@responses.activate
def test_us_test_alert_uses_us_sample(monkeypatch, capsys, tmp_path):
    import json

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    responses.add(responses.POST, WEBHOOK, status=204)
    from ev_engine.runner import send_test_alert

    settings = make_settings(tmp_path, providers=("the_odds_api",), the_odds_api_key="odds_key_123456")
    assert send_test_alert(settings, now=NOW) == 0
    embed = json.loads(responses.calls[0].request.body)["embeds"][0]
    assert "Home Team vs Away Team" in embed["title"]
    assert "::notice title=Test alert::Sample alert posted to Discord." in capsys.readouterr().out
