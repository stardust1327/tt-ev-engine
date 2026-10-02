"""Discord embeds, batching and webhook error handling."""

import json
from datetime import timedelta

import pytest
import responses

from ev_engine.models import MONEYLINE, SPREAD, Edge
from ev_engine.notifier import (
    DARK_GREEN,
    GREEN,
    DiscordNotifier,
    batch_embeds,
    build_embed,
    embed_chars,
)

from conftest import NOW, WEBHOOK, event, market


def edge(ev=0.034, price=2.10, outcome="Ivanov D.", market_type=MONEYLINE, line=None, alternatives=()):
    return Edge(event=event(market("A", 1.9, 1.9)), market=market_type, line=line, outcome=outcome,
                bookmaker="Bet365", price=price, fair_prob=(1 + ev) / price, ev=ev,
                reference="Consensus of 4 books (median, power devig)",
                updated_at=NOW - timedelta(minutes=2), alternatives=alternatives)


def notifier(sleeps=None, **kw):
    sleep = sleeps.append if sleeps is not None else (lambda _s: None)
    return DiscordNotifier(WEBHOOK, username="EV Engine", strong_threshold=0.05, sleep=sleep, **kw)


def test_embed_has_players_ev_book_and_raw_odds():
    embed = build_embed(edge(alternatives=(("Betway", 2.08, 0.024),)), NOW, 0.05)
    text = json.dumps(embed, ensure_ascii=False)
    assert embed["color"] == GREEN == 5763719
    assert embed["title"] == "🟢 +3.4% EV · Ivanov D. vs Petrov A."
    assert "Bet **Ivanov D.** @ **2.10** on **Bet365**" == embed["description"]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Players"] == "Ivanov D. vs Petrov A."
    assert fields["Sportsbook"] == "**Bet365**"
    assert fields["Odds (raw)"] == "2.10 (+110)"
    assert fields["Expected value"].startswith("**+3.4%**")
    assert fields["Also +EV at"] == "Betway 2.08 (+2.4%)"
    assert "<t:" in fields["Starts"]
    assert "confirmed 2 min ago" in embed["footer"]["text"]
    assert "@everyone" not in text


def test_strong_edge_gets_darker_green_and_fire():
    embed = build_embed(edge(ev=0.071), NOW, 0.05)
    assert embed["color"] == DARK_GREEN and embed["title"].startswith("🔥 +7.1% EV")


def test_handicap_pick_shows_the_line_for_the_right_side():
    away = edge(market_type=SPREAD, line=-1.5, outcome="Petrov A.")
    assert away.selection == "Petrov A. +1.5"
    assert edge(market_type=SPREAD, line=-1.5).selection == "Ivanov D. -1.5"


def test_markdown_in_names_is_escaped():
    e = edge()
    e = Edge(**{**e.__dict__, "bookmaker": "Book_*X*"})
    assert "Book\\_\\*X\\*" in build_embed(e, NOW, 0.05)["description"]


def test_batches_respect_10_embeds_and_6000_chars():
    items = [(i, build_embed(edge(), NOW, 0.05)) for i in range(23)]
    batches = batch_embeds(items)
    assert [len(b) for b in batches] == [10, 10, 3]
    assert all(sum(embed_chars(e) for _, e in b) <= 6000 for b in batches)
    big = [(i, {"title": "x" * 256, "description": "y" * 2000, "fields": []}) for i in range(5)]
    assert [len(b) for b in batch_embeds(big)] == [2, 2, 1]


@responses.activate
def test_send_posts_payload_without_mentions():
    responses.add(responses.POST, WEBHOOK, status=204)
    result = notifier().send([edge()], NOW)
    assert result.error is None and len(result.delivered) == 1
    body = json.loads(responses.calls[0].request.body)
    assert body["allowed_mentions"] == {"parse": []}
    assert body["username"] == "EV Engine" and len(body["embeds"]) == 1


@responses.activate
def test_429_waits_retry_after_then_succeeds():
    responses.add(responses.POST, WEBHOOK, status=429, json={"message": "You are being rate limited.",
                                                              "retry_after": 1.5, "global": False})
    responses.add(responses.POST, WEBHOOK, status=204)
    sleeps = []
    result = notifier(sleeps).send([edge()], NOW)
    assert result.error is None and sleeps == [1.5] and len(responses.calls) == 2


@responses.activate
def test_exhausted_bucket_waits_before_next_message():
    responses.add(responses.POST, WEBHOOK, status=204,
                  headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2.0"})
    responses.add(responses.POST, WEBHOOK, status=204)
    sleeps = []
    result = notifier(sleeps).send([edge() for _ in range(12)], NOW)  # 2 messages
    assert len(result.delivered) == 12
    assert len(sleeps) == 1 and 1.5 < sleeps[0] <= 2.0


@responses.activate
def test_deleted_webhook_stops_and_reports():
    responses.add(responses.POST, WEBHOOK, status=404, json={"message": "Unknown Webhook", "code": 10015})
    result = notifier().send([edge() for _ in range(12)], NOW)
    assert result.delivered == [] and "404" in result.error
    assert len(responses.calls) == 1  # never retries a 404


@responses.activate
def test_dry_run_makes_no_http_calls():
    result = DiscordNotifier(None, username="x", strong_threshold=0.05, dry_run=True).send([edge()], NOW)
    assert len(result.delivered) == 1 and len(responses.calls) == 0


def test_missing_webhook_outside_dry_run_is_an_error():
    from ev_engine.notifier import DiscordError

    with pytest.raises(DiscordError):
        DiscordNotifier(None, username="x", strong_threshold=0.05)
