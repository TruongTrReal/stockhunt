"""Extend the window's dominant Fourier component, blended with its mean and trend."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (expose, lin, nan_mask, roll, rowmean, win)

RULE = "Extend the window's dominant Fourier component, blended with its mean and trend."
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'FourierTrendProjection'"
FAMILY = 'trend'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = 'RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet. The one-step extension of a periodic component is its value at phase zero, so the dominant cycle contributes its own window-start level. That is what a periodic extrapolation means and it is not an approximation.'

LOGIC = """
What the source published
    Applies Fast Fourier Transform (FFT) to recent prices to identify the dominant
    frequency. Extrapolates this dominant frequency component and combines it with the
    mean and recent trend (weighted) for prediction.

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
    {'days': 7.0, 'trend_weight': 0.5, 'rebal': 7.0},
    {'days': 14.0, 'trend_weight': 0.5, 'rebal': 7.0},
    {'days': 21.0, 'trend_weight': 0.5, 'rebal': 7.0},
    {'days': 28.0, 'trend_weight': 0.5, 'rebal': 7.0},
    {'days': 30.0, 'trend_weight': 0.5, 'rebal': 30.0},
    {'days': 60.0, 'trend_weight': 0.5, 'rebal': 30.0},
    {'days': 90.0, 'trend_weight': 0.5, 'rebal': 30.0},
    {'days': 180.0, 'trend_weight': 0.5, 'rebal': 30.0},
)


def position(df, close, bpy, days=7.0, trend_weight=0.5, rebal=7.0):
    """Extend the window's dominant Fourier component, blended with its mean and trend."""
    w = win(bpy, days)
    m = roll(close, w)
    bad = nan_mask(m)
    mu = rowmean(m)
    spec = np.fft.rfft(np.nan_to_num(m - mu[:, None], nan=0.0), axis=1)
    k = np.argmax(np.abs(spec[:, 1:]), axis=1) + 1
    comp = spec[np.arange(len(m)), k]
    amp = 2.0 * np.abs(comp) / float(w)
    nxt = amp * np.cos(np.angle(comp))
    slope, _ = lin(close, w)
    pred = np.where(bad, np.nan, mu + nxt + trend_weight * slope)
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
