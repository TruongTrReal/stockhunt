"""Predict a bounce of half the window's largest peak-to-trough fall."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (expose, nan_mask, roll, rowmax, win)

RULE = "Predict a bounce of half the window's largest peak-to-trough fall."
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'RollingMaxDrawdownReversion'"
FAMILY = 'reversion'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = 'RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet. STRUCTURALLY ALWAYS LONG once the window holds any decline: a drawdown is non-negative, so the forecast never falls below the close.'

LOGIC = """
What the source published
    Identifies the maximum drawdown (peak - subsequent price) within the recent window.
    Predicts a price bounce equal to half of this maximum drawdown value (current_price
    + max_drawdown / 2).

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
    {'days': 180.0, 'rebal': 30.0},
    {'days': 7.0, 'rebal': 7.0},
    {'days': 14.0, 'rebal': 7.0},
    {'days': 21.0, 'rebal': 7.0},
    {'days': 28.0, 'rebal': 7.0},
    {'days': 30.0, 'rebal': 30.0},
    {'days': 60.0, 'rebal': 30.0},
    {'days': 90.0, 'rebal': 30.0},
)


def position(df, close, bpy, days=180.0, rebal=30.0):
    """Predict a bounce of half the window's largest peak-to-trough fall."""
    w = win(bpy, days)
    m = roll(close, w)
    bad = nan_mask(m)
    peak = np.maximum.accumulate(np.nan_to_num(m, nan=-np.inf), axis=1)
    mdd = rowmax(peak - m)
    pred = close + mdd / 2.0
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
