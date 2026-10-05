"""Accuracy tracker: a ledger of every alert and every priced match, graded after the match.

See tracker.py for the flow, grading.py for the math and scorecard.py for the report card.
"""

from .tracker import Tracker

__all__ = ["Tracker"]
