"""Long when ADXR is rising: trend strength is extrapolated as price movement."""

from __future__ import annotations

import numpy as np
import talib

from strategies._forecast import (expose, shift, win)

RULE = 'Long when ADXR is rising: trend strength is extrapolated as price movement.'
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'ADXRPersistence'"
FAMILY = 'trend'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = 'RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet. ADXR is unsigned, so its rate of change carries trend STRENGTH and no direction. Reproduced as published: the source reads a rising ADXR as a rising price, which means the rule is long in strengthening downtrends too.'

LOGIC = """
What the source published
    Calculates the ADXR (Average Directional Index Rating), a smoothed ADX. Predicts
    price movement proportional to the rate of change of ADXR over the smoothing period
    (14 days).

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
    {'days': 30.0, 'smooth': 14.0, 'rebal': 30.0},
    {'days': 7.0, 'smooth': 14.0, 'rebal': 7.0},
    {'days': 14.0, 'smooth': 14.0, 'rebal': 7.0},
    {'days': 21.0, 'smooth': 14.0, 'rebal': 7.0},
    {'days': 28.0, 'smooth': 14.0, 'rebal': 7.0},
    {'days': 60.0, 'smooth': 14.0, 'rebal': 30.0},
    {'days': 90.0, 'smooth': 14.0, 'rebal': 30.0},
    {'days': 180.0, 'smooth': 14.0, 'rebal': 30.0},
)


def position(df, close, bpy, days=30.0, smooth=14.0, rebal=30.0):
    """Long when ADXR is rising: trend strength is extrapolated as price movement."""
    w = win(bpy, days)
    n = win(bpy, smooth)
    hi, lo = df["High"].to_numpy("float64"), df["Low"].to_numpy("float64")
    adxr = talib.ADXR(hi, lo, close, timeperiod=n)
    pred = close * (1.0 + (adxr - shift(adxr, n)) / 100.0)
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
