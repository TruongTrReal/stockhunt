"""FRAMA: a moving average whose smoothing comes from the price's fractal dimension."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (ema_var, expose, rmax, rmin, safe_div, shift, win)

RULE = "FRAMA: a moving average whose smoothing comes from the price's fractal dimension."
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'FractalAdaptiveMA'"
FAMILY = 'trend'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = 'RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet.'

LOGIC = """
What the source published
    Calculates the fractal dimension of recent prices to determine a smoothing factor
    (alpha) for a Fractal Adaptive Moving Average (FRAMA). The predicted price is the
    calculated FRAMA.

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
    {'days': 14.0, 'rebal': 7.0},
    {'days': 7.0, 'rebal': 7.0},
    {'days': 21.0, 'rebal': 7.0},
    {'days': 28.0, 'rebal': 7.0},
    {'days': 30.0, 'rebal': 30.0},
    {'days': 60.0, 'rebal': 30.0},
    {'days': 90.0, 'rebal': 30.0},
    {'days': 180.0, 'rebal': 30.0},
)


def position(df, close, bpy, days=14.0, rebal=7.0):
    """FRAMA: a moving average whose smoothing comes from the price's fractal dimension."""
    w = win(bpy, days)
    half = max(2, w // 2)
    hi_a, lo_a = rmax(close, half), rmin(close, half)
    hi_b, lo_b = shift(hi_a, half), shift(lo_a, half)
    hi_c, lo_c = rmax(close, w), rmin(close, w)
    n1 = safe_div(hi_a - lo_a, float(half), fill=np.nan)
    n2 = safe_div(hi_b - lo_b, float(half), fill=np.nan)
    n3 = safe_div(hi_c - lo_c, float(w), fill=np.nan)
    ok = (n1 > 0) & (n2 > 0) & (n3 > 0)
    dim = np.where(ok, (np.log(np.where(ok, n1 + n2, np.nan))
                        - np.log(np.where(ok, n3, np.nan))) / np.log(2.0), np.nan)
    alpha = np.clip(np.exp(-4.6 * (dim - 1.0)), 0.01, 1.0)
    pred = ema_var(close, alpha)
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
