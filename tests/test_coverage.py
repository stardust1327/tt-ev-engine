"""Book coverage: how many books priced each market line, and which ones could anchor a fair line."""

import responses

from ev_engine.analyzer import find_edges
from ev_engine.models import RunReport
from ev_engine.runner import coverage_summary, run, write_step_summary

from conftest import BETSAPI, NOW, book_odds, event, fixture, make_settings, market, summary, upcoming


def quotes(*books: str, minutes_old: float = 1.0):
    return [market(book, 1.9, 1.9, minutes_old=minutes_old) for book in books]


def test_analyzer_records_quoting_and_usable_books(settings):
    report = RunReport(started_at=NOW)
    ev = event(
        market("Bet365", 1.90, 1.90, minutes_old=2),
        market("1XBet", 1.85, 1.95, minutes_old=4),
        market("BetFair", 1.95, 1.85, minutes_old=45),  # stale: quoted, not usable
        market("Marathon", 1.50, 2.00),                 # 1.167 overround: quoted, not usable
    )
    find_edges(ev, settings, NOW, report)
    assert report.books_quoting == {4: 1}
    assert report.books_usable == {2: 1}
    assert report.book_quotes == {"Bet365": 1, "1XBet": 1, "BetFair": 1, "Marathon": 1}
    assert report.book_usable == {"Bet365": 1, "1XBet": 1}
    assert report.median_age("BetFair") == 45 and report.median_age("Bet365") == 2


def test_median_age():
    report = RunReport(started_at=NOW)
    for minutes in (3, 30, 9, 12):
        report.record_coverage(quotes("DraftKings", minutes_old=minutes), [], NOW)
    assert report.median_age("DraftKings") == 10.5
    assert report.median_age("Unknown") is None


def test_coverage_summary_text(settings):
    report = RunReport(started_at=NOW)
    for books in (("Bet365", "1XBet"), ("Bet365", "1XBet"), ("Bet365",)):
        report.record_coverage(quotes(*books), quotes(books[0]), NOW)
    report.record_coverage(quotes("DraftKings", minutes_old=150), [], NOW)
    text = coverage_summary(report, settings)
    assert text.startswith("4 market line(s). Books quoting each: 1 book ×2, 2 books ×2.")
    assert "Usable for the fair line (fresh, sane margin): 0 books ×1, 1 book ×3." in text
    assert "By book (lines quoted/usable, median price age): Bet365 3/3 (1m), 1XBet 2/0 (1m), " \
           "DraftKings 1/0 (2.5h)." in text
    assert text.endswith("A consensus needs 3 books besides the one being bet (MIN_CONSENSUS_BOOKS).")


def test_coverage_summary_mentions_sharp_books(tmp_path):
    report = RunReport(started_at=NOW)
    report.record_coverage(quotes("Bet365", "BetFair"), quotes("Bet365", "BetFair"), NOW)
    auto = make_settings(tmp_path, sharp_books=("BetFair",), min_consensus_books=1)
    assert coverage_summary(report, auto).endswith(
        "A consensus needs 1 book besides the one being bet (MIN_CONSENSUS_BOOKS), or a fresh price from BetFair.")
    sharp = make_settings(tmp_path, sharp_books=("BetFair",), fair_line_mode="sharp")
    assert coverage_summary(report, sharp).endswith("Sharp mode needs a fresh price from BetFair.")


def test_no_coverage_line_without_markets(settings):
    assert coverage_summary(RunReport(started_at=NOW), settings) is None


def test_many_books_are_capped(settings):
    report = RunReport(started_at=NOW)
    report.record_coverage(quotes(*(f"Book{i:02d}" for i in range(15))), [], NOW)
    text = coverage_summary(report, settings, max_books=12)
    assert text.count("1/0 (1m)") == 12 and "+3 more" in text


def test_step_summary_lists_books(settings, tmp_path):
    report = RunReport(started_at=NOW)
    report.record_coverage(quotes("Bet365", "1XBet"), quotes("Bet365"), NOW)
    path = tmp_path / "summary.md"
    write_step_summary(report, settings, path=str(path))
    text = path.read_text()
    assert "**Book coverage**" in text
    assert "| Bet365 | 1 | 1 | 1m |" in text and "| 1XBet | 1 | 0 | 1m |" in text


@responses.activate
def test_run_emits_a_book_coverage_notice(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    responses.add(responses.GET, f"{BETSAPI}/v3/events/upcoming", json=upcoming(fixture("e1", 25)))
    responses.add(responses.GET, f"{BETSAPI}/v2/event/odds/summary",
                  json=summary(Bet365=book_odds("1.9", "1.9"), BWin=book_odds("1.87", "1.93")))
    assert run(make_settings(tmp_path, dry_run=True), now=NOW, sleep=lambda _s: None) == 0
    out = capsys.readouterr().out
    coverage = [line for line in out.splitlines() if line.startswith("::notice title=Book coverage::")]
    assert len(coverage) == 1
    assert "Books quoting each: 2 books ×1" in coverage[0]
    assert "BWin 1/1 (1m)" in coverage[0] and "Bet365 1/1 (1m)" in coverage[0]
    assert "tok_live" not in out
