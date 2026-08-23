"""Average the midpoints of nested sub-windows, weighted by each one's range."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (expose, rmax, rmin, safe_div, win)

RULE = "Average the midpoints of nested sub-windows, weighted by each one's range."
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'RollingWindowWeightedMidpoints'"
FAMILY = 'reversion'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = "RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet. The source says 'various sub-window lengths' without naming them. Four nested lengths at 1/8, 1/4, 1/2 and the full window are used; the source's own set is unknown, so this is a re-implementation choice."

LOGIC = """
What the source published
    For various sub-window lengths ending at the current time, calculates their high-low
    midpoints and range sizes. Predicts a weighted average of these midpoints, where
    each midpoint is weighted by its corresponding sub-window's range size.

How that becomes a position here
    The forecast is compared with the SAME bar's close: long above it, flat at or below,
    and the resulting position is then held until the next rebalance. Only the sign of
    (forecast - close) reaches the book, so whatever magnitude the formula produces is
    discarded - a rule that forecasts a 200% move and one that forecasts a tick score
    identically.

What the position is really exposed to
    Short-term reversal, and short volatility with it: the rule buys weakness and is
    paid for supplying liquidity to whoever had to sell. Because it holds only while its
    condition is true it sits in cash for much of the sample, so its IR against a fully-
    invested benchmark is docked for time out of the market whether or not the signal is
    worth anything.

How it fails
    It sells insurance - small frequent gains against rare large losses - so its returns
    are negatively skewed and fat-tailed, and the same trade is the Fama-French short-
    term reversal factor, which earns a great deal gross and approximately nothing after
    real costs. Cost sensitivity, not signal decay, is what kills it.

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
    {'days': 7.0, 'rebal': 7.0},
    {'days': 14.0, 'rebal': 7.0},
    {'days': 21.0, 'rebal': 7.0},
    {'days': 28.0, 'rebal': 7.0},
    {'days': 30.0, 'rebal': 30.0},
    {'days': 60.0, 'rebal': 30.0},
    {'days': 90.0, 'rebal': 30.0},
    {'days': 180.0, 'rebal': 30.0},
)


def position(df, close, bpy, days=7.0, rebal=7.0):
    """Average the midpoints of nested sub-windows, weighted by each one's range."""
    w = win(bpy, days)
    lens = [n for n in (max(2, w // 8), max(2, w // 4), max(2, w // 2), w)]
    mids, wts = [], []
    for n in lens:
        hi_n, lo_n = rmax(close, n), rmin(close, n)
        mids.append((hi_n + lo_n) / 2.0)
        wts.append(hi_n - lo_n)
    mid_a, wt_a = np.vstack(mids), np.vstack(wts)
    pred = safe_div(np.nansum(mid_a * wt_a, axis=0), np.nansum(wt_a, axis=0), fill=np.nan)
    pred = np.where(np.isfinite(wt_a).all(axis=0), pred, np.nan)
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
