"""Turns the ledger into the report card: alert results, CLV, calibration, book accuracy.

All of it is plain arithmetic on the ledger rows, so it is fully unit-tested and the same
numbers appear in REPORT.md, the weekly Discord post and the run annotation.

How to read the numbers
-----------------------
* Profit / ROI: every alert counted as a 1-unit bet at the alerted price.
* Luck band: one standard deviation of profit from luck alone, sqrt(sum p(1-p)d^2) with
  p the alert's fair probability. Profit inside the band says nothing either way.
* CLV: price * p_close - 1 (see grading.py). Average CLV and its standard error give the
  verdict: |t| >= 2 is a real signal, anything less is "not yet".
* Calibration: every graded match, folded to the favourite's side. If the fair line says
  60%, the favourite should win about 60% of those matches.
* Book accuracy: log loss of each book's no-vig closing line against the results, over the
  same matches. Lower = closer to what actually happened.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models import MARKET_LABELS
from .grading import LOST, PUSH, WON, book_probabilities
from .ledger import num, parse_iso

GRADED = (WON, LOST, PUSH)
MIN_CLV_FOR_VERDICT = 20
MIN_MATCHES_FOR_VERDICT = 100
BUCKETS = ((0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 0.80), (0.80, 1.00001))


# -- alerts -------------------------------------------------------------------------------
@dataclass
class PickStats:
    won: int = 0
    lost: int = 0
    push: int = 0
    profit: float = 0.0
    expected: float = 0.0   # sum of EV at alert time, in units
    variance: float = 0.0   # luck: sum of p(1-p)d^2
    clv: list[float] = field(default_factory=list)

    @property
    def bets(self) -> int:
        return self.won + self.lost + self.push

    @property
    def record(self) -> str:
        return f"{self.won}-{self.lost}" + (f"-{self.push}" if self.push else "")

    @property
    def roi(self) -> float | None:
        staked = self.won + self.lost
        return self.profit / staked if staked else None

    @property
    def luck(self) -> float:
        return math.sqrt(self.variance)

    @property
    def clv_mean(self) -> float | None:
        return statistics.fmean(self.clv) if self.clv else None

    @property
    def clv_se(self) -> float | None:
        return statistics.stdev(self.clv) / math.sqrt(len(self.clv)) if len(self.clv) >= 2 else None

    @property
    def clv_beat(self) -> int:
        return sum(1 for c in self.clv if c > 0)


def pick_stats(rows: Iterable[dict[str, str]]) -> PickStats:
    stats = PickStats()
    for row in rows:
        status = row.get("status")
        price, fair_odds = num(row.get("price")), num(row.get("fair_odds"))
        if status not in GRADED or price is None:
            continue
        stats.won += status == WON
        stats.lost += status == LOST
        stats.push += status == PUSH
        stats.profit += num(row.get("profit")) or 0.0
        stats.expected += (num(row.get("ev_pct")) or 0.0) / 100.0
        if fair_odds and fair_odds > 1.0:
            p = 1.0 / fair_odds
            stats.variance += p * (1.0 - p) * price * price
        if (clv := num(row.get("clv_pct"))) is not None:
            stats.clv.append(clv / 100.0)
    return stats


# Split labels sort by their leading number ("1 under 4%" -> "under 4%"), so bands read in order.
def _ev_band(row: dict[str, str]) -> str:
    ev = num(row.get("ev_pct")) or 0.0
    return "1 under 4%" if ev < 4 else ("2 4-6%" if ev < 6 else "3 6% or more")


def _age_band(row: dict[str, str]) -> str:
    age = num(row.get("price_age_min"))
    if age is None:
        return "5 unknown"
    if age <= 5:
        return "1 up to 5 min"
    return "2 5-10 min" if age <= 10 else ("3 10-20 min" if age <= 20 else "4 over 20 min")


def _fair_line_source(row: dict[str, str]) -> str:
    """'Bet365 no-vig line (power devig)' -> 'Bet365 alone'; 'Consensus of 2 books ...' -> 'consensus'."""
    reference = row.get("reference", "")
    if " no-vig line" in reference:
        return f"{reference.split(' no-vig line')[0]} alone"
    return "consensus of books" if reference.startswith("Consensus") else (reference or "unknown")


SPLITS: dict[str, Callable[[dict[str, str]], str]] = {
    "By book": lambda row: row.get("book") or "?",
    "By fair line": _fair_line_source,
    "By EV at alert": _ev_band,
    "By price age at alert": _age_band,
    "By market": lambda row: MARKET_LABELS.get(row.get("market", ""), row.get("market", "?")),
}


def split_stats(rows: list[dict[str, str]], key: Callable[[dict[str, str]], str]) -> dict[str, PickStats]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return {_unrank(name): pick_stats(group) for name, group in sorted(groups.items())}


def _unrank(label: str) -> str:
    rank, _, rest = label.partition(" ")
    return rest if rank.isdigit() and rest else label


def clv_verdict(stats: PickStats) -> str:
    n, mean, se = len(stats.clv), stats.clv_mean, stats.clv_se
    if not n or mean is None:
        return "No graded alerts with a closing price yet."
    detail = f"avg {mean:+.1%}" + (f" ± {se:.1%}" if se else "") + f" over {n} alert{'s' if n != 1 else ''}"
    if n < MIN_CLV_FOR_VERDICT or not se:
        return f"⏳ Too early to tell: {detail}. CLV starts to mean something after about {MIN_CLV_FOR_VERDICT}-50."
    t = mean / se
    if t >= 2:
        return f"✅ Beating the closing line: {detail}. The edges look real."
    if t <= -2:
        return f"❌ Losing to the closing line: {detail}. The edges look like noise or stale prices."
    return f"⏳ No clear signal yet: {detail}."


# -- calibration --------------------------------------------------------------------------
@dataclass
class Bucket:
    lo: float
    hi: float
    n: int = 0
    said: float = 0.0   # average favourite probability
    won: float = 0.0    # favourite's actual win rate

    @property
    def label(self) -> str:
        return f"{self.lo * 100:.0f}-{min(self.hi, 1.0) * 100:.0f}%"

    @property
    def z(self) -> float:
        if not self.n or not 0 < self.said < 1:
            return 0.0
        return (self.won - self.said) / math.sqrt(self.said * (1 - self.said) / self.n)


def _samples(rows: Iterable[dict[str, str]]) -> Iterable[tuple[dict[str, str], float, int]]:
    """(row, side A probability, 1/0 result) for every graded calibration row."""
    for row in rows:
        p, result = num(row.get("fair_pct")), row.get("result")
        if p is None or result not in ("0", "1") or not 0 < p < 100:
            continue
        yield row, p / 100.0, int(result)


def calibration(rows: Iterable[dict[str, str]]) -> tuple[list[Bucket], int]:
    buckets = [Bucket(lo, hi) for lo, hi in BUCKETS]
    sums: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    total = 0
    for _, p, won in _samples(rows):
        if p < 0.5:  # fold to the favourite so both sides of a match aren't double-counted
            p, won = 1.0 - p, 1 - won
        for i, b in enumerate(buckets):
            if b.lo <= p < b.hi:
                sums[i][0] += p
                sums[i][1] += won
                b.n += 1
                total += 1
                break
    for i, b in enumerate(buckets):
        if b.n:
            b.said, b.won = sums[i][0] / b.n, sums[i][1] / b.n
    return [b for b in buckets if b.n], total


def calibration_verdict(buckets: list[Bucket], total: int) -> str:
    if total < MIN_MATCHES_FOR_VERDICT:
        return f"⏳ Building: {total} graded match{'es' if total != 1 else ''} so far (readable from about 200)."
    solid = [b for b in buckets if b.n >= 30]
    worst = max(solid, key=lambda b: abs(b.z), default=None)
    if worst is None or abs(worst.z) < 2.5:
        return (f"✅ Well calibrated over {total} matches: when the fair line gives a player X%, "
                "they win about X% of the time.")
    return (f"⚠️ Off in the {worst.label} range: the fair line said {worst.said:.0%} but favourites won "
            f"{worst.won:.0%} of {worst.n} matches.")


def book_accuracy(rows: list[dict[str, str]], devig_method: str, max_overround: float,
                  min_n: int = 30) -> tuple[list[tuple[str, float]], int]:
    """[(book or 'consensus', log loss)] best first, over the matches every listed book priced."""
    samples = []
    for row, p_consensus, won in _samples(rows):
        prices = parse_prices(row.get("prices", ""))
        probs = book_probabilities(prices, devig_method, max_overround)
        probs["consensus"] = p_consensus
        samples.append((probs, won))

    counts: dict[str, int] = defaultdict(int)
    for probs, _ in samples:
        for book in probs:
            counts[book] += 1
    books = sorted((b for b, n in counts.items() if n >= min_n and b != "consensus"), key=lambda b: -counts[b])
    while len(books) >= 2:
        common = [(probs, won) for probs, won in samples if all(b in probs for b in books)]
        if len(common) >= min_n:
            scores = [(b, _log_loss(common, b)) for b in [*books, "consensus"]]
            return sorted(scores, key=lambda s: s[1]), len(common)
        books.pop()  # drop the book with the fewest matches and try again
    return [], 0


def _log_loss(samples: list[tuple[dict[str, float], int]], book: str) -> float:
    total = 0.0
    for probs, won in samples:
        p = min(max(probs[book], 1e-6), 1 - 1e-6)
        total -= math.log(p if won else 1 - p)
    return total / len(samples)


def parse_prices(text: str) -> dict[str, tuple[float, ...]]:
    """'Bet365=1.850/1.950;DraftKings=1.870/1.920' -> {'Bet365': (1.85, 1.95), ...}."""
    out: dict[str, tuple[float, ...]] = {}
    for part in (text or "").split(";"):
        book, _, quotes = part.partition("=")
        values = tuple(num(q) for q in quotes.split("/"))
        if book and len(values) >= 2 and all(v is not None and v > 1.0 for v in values):
            out[book.strip()] = values  # type: ignore[assignment]
    return out


def format_prices(prices: dict[str, tuple[float, ...]]) -> str:
    return ";".join(f"{book}={'/'.join(f'{p:.3f}' for p in quotes)}" for book, quotes in sorted(prices.items()))


# -- the report card ----------------------------------------------------------------------
@dataclass
class Scorecard:
    now: datetime
    all_time: PickStats
    week: PickStats
    splits: dict[str, dict[str, PickStats]]
    buckets: list[Bucket]
    matches: int
    books: list[tuple[str, float]]
    books_n: int
    recent: list[dict[str, str]]
    pending: int
    since: str

    @property
    def clv_verdict(self) -> str:
        return clv_verdict(self.all_time)

    @property
    def calibration_verdict(self) -> str:
        return calibration_verdict(self.buckets, self.matches)


def first_alerts(picks: list[dict[str, str]]) -> list[dict[str, str]]:
    """One row per pick: its first alert. A re-alert (the same player in the same match, posted again
    because its EV rose) is the same bet, so counting it again would inflate the record and the CLV."""
    first: dict[tuple, dict[str, str]] = {}
    for row in sorted(picks, key=lambda r: r.get("sent_at", "")):
        key = tuple(row.get(k, "") for k in ("provider", "event_id", "market", "line", "outcome"))
        first.setdefault(key, row)
    return list(first.values())


def build_scorecard(picks: list[dict[str, str]], lines: list[dict[str, str]], *, now: datetime,
                    devig_method: str, max_overround: float, pending: int = 0, since: str = "") -> Scorecard:
    recent = sorted(picks, key=lambda r: r.get("sent_at", ""), reverse=True)[:20]
    picks = first_alerts(picks)
    week_start = now - timedelta(days=7)
    week_rows = [r for r in picks if (start := parse_iso(r.get("start", ""))) and start >= week_start]
    graded = [r for r in picks if r.get("status") in GRADED]
    buckets, matches = calibration(lines)
    books, books_n = book_accuracy(lines, devig_method, max_overround)
    return Scorecard(
        now=now,
        all_time=pick_stats(picks),
        week=pick_stats(week_rows),
        splits={name: split_stats(graded, key) for name, key in SPLITS.items()},
        buckets=buckets,
        matches=matches,
        books=books,
        books_n=books_n,
        recent=recent,
        pending=pending,
        since=since,
    )


def _units(x: float) -> str:
    return f"{x:+.2f}u"


def _alerts_line(label: str, s: PickStats) -> str:
    if not s.bets:
        return f"{label}: no graded alerts"
    roi = f" (ROI {s.roi:+.1%})" if s.roi is not None else ""
    return f"{label}: {s.bets} bet{'s' if s.bets != 1 else ''} · {s.record} · {_units(s.profit)}{roi}"


def _books_line(card: Scorecard) -> str | None:
    if not card.books:
        return None
    ranked = " · ".join(f"{book} {loss:.3f}" for book, loss in card.books)
    return f"Closest to the results over the same {card.books_n} matches (log loss, lower is better): {ranked}"


def discord_embed(card: Scorecard, *, title: str, url: str = "") -> dict:
    s = card.all_time
    lines = ["**Alerts** (1 unit per pick, at its first alert's price)",
             _alerts_line("Last 7 days", card.week),
             _alerts_line("All time", s)]
    if s.bets:
        lines.append(f"Expected from EV: {_units(s.expected)} · luck alone: about ±{s.luck:.1f}u")
    lines += ["", "**Closing line value**", card.clv_verdict]
    lines += ["", f"**Fair line** ({card.matches} graded matches)", card.calibration_verdict]
    lines += [f"Favourite at {b.label}: won {b.won:.0%} ({b.n} matches)" for b in card.buckets if b.n >= 10]
    if card.books:
        best = card.books[0][0]
        best = "the consensus of all books" if best == "consensus" else best
        lines.append(f"Most accurate closing line: **{best}** (same {card.books_n} matches)")
    footer = f"Tracking since {card.since}" if card.since else "Accuracy tracker"
    if card.pending:
        footer += f" · {card.pending} match{'es' if card.pending != 1 else ''} waiting for a result"
    embed = {
        "title": title[:256],
        "description": "\n".join(lines)[:4096],
        "color": 3447003,  # 0x3498DB blue: reports, not picks
        "footer": {"text": footer[:2048]},
        "timestamp": card.now.replace(microsecond=0).isoformat(),
    }
    if url:
        embed["url"] = url
    return embed


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _stats_row(label: str, s: PickStats) -> str:
    roi = f"{s.roi:+.1%}" if s.roi is not None else "-"
    clv = f"{s.clv_mean:+.1%} ({s.clv_beat}/{len(s.clv)} beat)" if s.clv else "-"
    return (f"| {_cell(label)} | {s.bets} | {s.record} | {_units(s.profit)} | {roi} | ±{s.luck:.1f}u | "
            f"{_units(s.expected)} | {clv} |")


def markdown(card: Scorecard, *, title: str) -> str:
    out = [f"# {title}", "",
           f"Updated {card.now:%Y-%m-%d %H:%M} UTC" + (f" · tracking since {card.since}" if card.since else "")
           + f" · {card.pending} match{'es' if card.pending != 1 else ''} waiting for a result", "",
           "## Verdict", "",
           f"- **Alerts:** {card.clv_verdict}",
           f"- **Fair line:** {card.calibration_verdict}"]
    if (books := _books_line(card)):
        out.append(f"- **Books:** {books}")
    header = ["| | Bets | W-L | Profit | ROI | Luck (±1 SD) | Expected from EV | CLV |",
              "|---|---|---|---|---|---|---|---|"]
    out += ["", "## Alerts", "",
            "Each pick counts once, as a 1-unit bet at its first alert's price "
            "(re-alerts are listed below but not counted again).", "", *header,
            _stats_row("Last 7 days", card.week), _stats_row("All time", card.all_time)]
    for name, groups in card.splits.items():
        if len(groups) < 2 and name != "By book":
            continue
        if not any(s.bets for s in groups.values()):
            continue
        out += ["", f"### {name}", "", *header]
        out += [_stats_row(label, s) for label, s in groups.items() if s.bets]

    out += ["", "## Fair-line calibration", "",
            f"Every match the scanner priced ({card.matches} graded so far), from the favourite's side.", ""]
    if card.buckets:
        out += ["| Fair line said | Favourite won | Matches | Off by (SDs) |", "|---|---|---|---|"]
        out += [f"| {b.label} (avg {b.said:.1%}) | {b.won:.1%} | {b.n} | {b.z:+.1f} |" for b in card.buckets]
    else:
        out.append("No graded matches yet.")

    out += ["", "## Recent alerts", ""]
    if card.recent:
        out += ["| Sent (UTC) | Pick | Book | Odds | Fair | EV | Result | Profit | CLV |",
                "|---|---|---|---|---|---|---|---|---|"]
        for r in card.recent:
            sent = (r.get("sent_at") or "").replace("T", " ")[5:16]
            ev = num(r.get("ev_pct"))
            clv = num(r.get("clv_pct"))
            prof = num(r.get("profit"))
            out.append(
                f"| {sent} | {_cell(r.get('pick', ''))}{' (re-alert)' if r.get('alert_n', '1') != '1' else ''} "
                f"({_cell(r.get('home', ''))} vs {_cell(r.get('away', ''))}) | "
                f"{_cell(r.get('book', ''))} | {r.get('price', '')} | {r.get('fair_odds', '')} | "
                f"{'' if ev is None else f'{ev:+.1f}%'} | {_cell(r.get('status', ''))} {_cell(r.get('score', ''))} | "
                f"{'' if prof is None else _units(prof)} | {'' if clv is None else f'{clv:+.1f}%'} |"
            )
    else:
        out.append("No alerts logged yet.")

    out += ["", "## How to read this", "",
            "- **CLV (closing line value)**: the alerted price against the fair line at kickoff, built the same way "
            "as the alert's (other books, no-vig, median). Beating the close on average is the best early sign that "
            "the edges are real; results take thousands of bets to prove the same thing.",
            "- **Luck (±1 SD)**: how far profit can swing from luck alone at this sample size. "
            "Profit inside that range doesn't prove anything either way.",
            "- **Calibration**: when the fair line gives the favourite 60%, favourites should win about 60% of "
            "those matches. 'Off by' over ±2.5 SDs is a real miss, not noise.",
            "- **Log loss**: how close each book's no-vig closing line was to the results, over the same matches. "
            "Lower is better; the lowest is the sharpest reference for the fair line.",
            "- Files: `picks/` has every alert, `lines/` every priced match, both as CSV.", ""]
    return "\n".join(out)


def run_summary(card: Scorecard) -> str:
    s = card.all_time
    clv = f", CLV {s.clv_mean:+.1%} over {len(s.clv)}" if s.clv else ""
    return (f"all time: {s.bets} alert(s) graded ({s.record}, {_units(s.profit)}{clv}), "
            f"{card.matches} match(es) calibrated, {card.pending} waiting for a result")
