"""A constant-velocity Kalman filter's one-step-ahead price estimate."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (expose, win)

RULE = "A constant-velocity Kalman filter's one-step-ahead price estimate."
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'KalmanFilterPrice'"
FAMILY = 'trend'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = 'RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet.'

LOGIC = """
What the source published
    Employs a Kalman Filter with a constant velocity model (state includes price and
    price change) to make a one-step ahead price prediction based on the estimated
    state.

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

# grid[0] IS the published parameter set. This rule has no lookback window at all, so
# the source's eight-cell sweep collapses to its two rebalance schedules here: the
# lookback axis has no expression in the rule and inventing one would not be a
# replication of it.
GRID = (
    {'q': 0.001, 'r': 1.0, 'rebal': 30.0},
    {'q': 0.001, 'r': 1.0, 'rebal': 7.0},
)


def position(df, close, bpy, q=0.001, r=1.0, rebal=30.0):
    """A constant-velocity Kalman filter's one-step-ahead price estimate."""
    n = len(close)
    pred = np.full(n, np.nan)
    state = np.array([close[0], 0.0])
    cov = np.eye(2)
    trans = np.array([[1.0, 1.0], [0.0, 1.0]])
    proc = np.array([[q, 0.0], [0.0, q]])
    obs = np.array([1.0, 0.0])
    for t in range(1, n):
        state = trans @ state
        cov = trans @ cov @ trans.T + proc
        innov = close[t] - obs @ state
        s = obs @ cov @ obs + r
        gain = (cov @ obs) / s if s > 0 else np.zeros(2)
        state = state + gain * innov
        cov = cov - np.outer(gain, obs @ cov)
        pred[t] = state[0] + state[1]
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
