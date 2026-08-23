"""Average a short rolling maximum over a longer window."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (expose, rmax, rmean, win)

RULE = 'Average a short rolling maximum over a longer window.'
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'RollingWindowSmoothedMax'"
FAMILY = 'trend'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = 'RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet. Ranked 2nd on the source leaderboard. A smoothed rolling maximum sits above the price on most bars by construction, so under this mapping it is long most of the time - which is the shape almost every top-ranked source rule has.'

LOGIC = """
What the source published
    First, calculates a series of rolling maximums over a short `max_period`. Then, it
    predicts the Simple Moving Average (SMA) of these rolling maximums over
    `smooth_period`.

How that becomes a position here
    The forecast is compared with the SAME bar's close: long above it, flat at or below,
    and the resulting position is then held until the next rebalance. Only the sign of
    (forecast - close) reaches the book, so whatever magnitude the formula produces is
    discarded - a rule that forecasts a 200% move and one that forecasts a tick score
    identically.

What the position is really exposed to
    Trend continuation, plus a long bias: the forecast sits above the close whenever the
    fitted level does, so time-in-market rises with the trend rather than with any edge.

How it fails
    Chop. The forecast crosses the close repeatedly in a sideways market and the rule
    pays the spread on both sides of every crossing, which is where a trend-following
    equity curve spends most of its life.

Provenance
    Re-implemented from the source's prose; no code was published. See
    `strategies/CONVERSIONS.md` for what that costs and what was assumed.
"""

# grid[0] IS the published parameter set, and for this batch that means THE CELL THE
# SOURCE'S OWN LEADERBOARD REPORTS -- its `timeframe` key is `<lookback-days>_<schedule>`
# and its headline figure per rule is the best of the eight below. Carrying all eight
# lets the walk-forward re-pick the lookback out of sample instead of inheriting that
# in-sample choice, and lets the no-fitting row be read directly against the claim.
GRID = (
    {'max_days': 10.0, 'smooth_days': 28.0, 'rebal': 7.0},
    {'max_days': 10.0, 'smooth_days': 7.0, 'rebal': 7.0},
    {'max_days': 10.0, 'smooth_days': 14.0, 'rebal': 7.0},
    {'max_days': 10.0, 'smooth_days': 21.0, 'rebal': 7.0},
    {'max_days': 10.0, 'smooth_days': 30.0, 'rebal': 30.0},
    {'max_days': 10.0, 'smooth_days': 60.0, 'rebal': 30.0},
    {'max_days': 10.0, 'smooth_days': 90.0, 'rebal': 30.0},
    {'max_days': 10.0, 'smooth_days': 180.0, 'rebal': 30.0},
)


def position(df, close, bpy, max_days=10.0, smooth_days=28.0, rebal=7.0):
    """Average a short rolling maximum over a longer window."""
    w = win(bpy, max_days)
    sm = max(2, win(bpy, smooth_days))
    pred = rmean(rmax(close, w), sm)
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
