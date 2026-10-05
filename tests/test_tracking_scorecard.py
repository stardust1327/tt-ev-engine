"""Report-card math: results, luck band, CLV verdict, calibration and book accuracy."""

import pytest

from ev_engine.tracking.scorecard import (
    SPLITS,
    PickStats,
    book_accuracy,
    build_scorecard,
    calibration,
    calibration_verdict,
    clv_verdict,
    discord_embed,
    format_prices,
    markdown,
    parse_prices,
    pick_stats,
    split_stats,
)

from conftest import NOW


def pick(status, price, fair_odds, ev_pct, profit, clv_pct="", **extra):
    return {"status": status, "price": str(price), "fair_odds": str(fair_odds), "ev_pct": str(ev_pct),
            "profit": str(profit), "clv_pct": str(clv_pct), "start": "2026-10-02T20:00:00Z",
            "sent_at": "2026-10-02T19:40:00Z", "book": "DraftKings", "market": "moneyline", **extra}


def test_pick_stats():
    s = pick_stats([
        pick("won", 2.0, 1.9, 5.26, 1.0, 2.0),
        pick("lost", 2.2, 2.0, 10.0, -1.0, -1.0),
        pick("push", 1.9, 1.85, 2.7, 0.0, 0.5),
        pick("pending", 2.0, 1.9, 5.0, ""),
        pick("void", 2.0, 1.9, 5.0, 0.0),
    ])
    assert (s.bets, s.record, s.profit, s.roi) == (3, "1-1-1", 0.0, 0.0)
    assert s.expected == pytest.approx(0.1796)
    p1, p3 = 1 / 1.9, 1 / 1.85
    assert s.luck == pytest.approx((p1 * (1 - p1) * 4 + 0.25 * 4.84 + p3 * (1 - p3) * 3.61) ** 0.5)
    assert s.clv_mean == pytest.approx(0.005) and s.clv_se == pytest.approx(0.015 / 3**0.5)
    assert s.clv_beat == 2


@pytest.mark.parametrize("values,start", [
    ([0.03 + 0.002 * (i % 5) for i in range(25)], "✅"),
    ([-0.03 - 0.002 * (i % 5) for i in range(25)], "❌"),
    ([0.05, -0.05] * 15, "⏳ No clear"),
    ([0.03] * 3, "⏳ Too early"),
])
def test_clv_verdict(values, start):
    assert clv_verdict(PickStats(clv=values)).startswith(start)
    assert clv_verdict(PickStats()).startswith("No graded alerts")


def line(fair_pct, result, prices=""):
    return {"fair_pct": str(fair_pct), "result": str(result), "prices": prices}


def test_calibration_folds_to_the_favourite():
    buckets, total = calibration([line(40, 0), line(62, 1), line(61, 0), line(50, ""), line(90, 1)])
    assert total == 4  # the ungraded row is skipped
    by_label = {b.label: b for b in buckets}
    assert by_label["60-65%"].n == 3                       # 40% underdog lost = 60% favourite won
    assert by_label["60-65%"].won == pytest.approx(2 / 3)
    assert by_label["80-100%"].said == pytest.approx(0.9)


def test_calibration_verdicts():
    assert calibration_verdict(*calibration([line(60, 1)] * 10)).startswith("⏳ Building")
    honest = [line(60, 1)] * 120 + [line(60, 0)] * 80          # said 60%, won 60%
    assert calibration_verdict(*calibration(honest)).startswith("✅")
    biased = [line(60, 1)] * 70 + [line(60, 0)] * 130          # said 60%, won 35%
    assert "Off in the 60-65% range" in calibration_verdict(*calibration(biased))


def test_book_accuracy_ranks_books_on_the_same_matches():
    # side A wins every time: Bet365 (~69% no-vig) was closest, DraftKings (~55%) furthest
    prices = format_prices({"Bet365": (1.40, 3.10), "DraftKings": (1.75, 2.10)})
    rows = [line(62, 1, prices) for _ in range(40)]
    ranked, n = book_accuracy(rows, "multiplicative", 1.12)
    assert [b for b, _ in ranked] == ["Bet365", "consensus", "DraftKings"] and n == 40
    assert book_accuracy(rows[:10], "multiplicative", 1.12) == ([], 0)  # too few matches to compare


def test_prices_round_trip():
    text = format_prices({"Bet365": (1.85, 1.95), "DraftKings": (1.87, 1.92)})
    assert text == "Bet365=1.850/1.950;DraftKings=1.870/1.920"
    assert parse_prices(text) == {"Bet365": (1.85, 1.95), "DraftKings": (1.87, 1.92)}
    assert parse_prices("Broken=0.00/1.9;=1.9/1.9") == {}


def test_report_renders():
    picks = [pick("won", 2.0, 1.9, 5.26, 1.0, 2.0, pick_name="x", home="A", away="B", score="3-1")]
    card = build_scorecard(picks, [line(60, 1)] * 5, now=NOW, devig_method="power", max_overround=1.12,
                           pending=3, since="2026-10-01")
    embed = discord_embed(card, title="📊 Tracker", url="https://github.com/me/ledger/blob/main/REPORT.md")
    assert "All time: 1 bet · 1-0 · +1.00u" in embed["description"]
    assert embed["url"].endswith("REPORT.md") and "3 matches waiting" in embed["footer"]["text"]
    text = markdown(card, title="Tracker")
    assert "## Verdict" in text and "| All time | 1 | 1-0 | +1.00u |" in text


def test_results_split_by_price_age_in_order():
    rows = [pick("won", 2.0, 1.9, 5.0, 1.0, price_age_min=age) for age in ("3.0", "8.5", "14.0", "19.9", "25.0", "")]
    bands = split_stats(rows, SPLITS["By price age at alert"])
    assert list(bands) == ["up to 5 min", "5-10 min", "10-20 min", "over 20 min", "unknown"]
    assert bands["10-20 min"].bets == 2
