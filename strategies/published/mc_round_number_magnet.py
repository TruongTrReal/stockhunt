"""Drift toward the nearest psychological round number, harder when it is close."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (expose, safe_div, win)

RULE = 'Drift toward the nearest psychological round number, harder when it is close.'
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'RoundNumberMagnet'"
FAMILY = 'reversion'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = 'RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet.'

LOGIC = """
What the source published
    Identifies the psychologically nearest round number price level (e.g., 10, 50, 100).
    Predicts a drift towards this nearest round number, with strength inversely related
    to the distance and capped.

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

# grid[0] IS the published parameter set. This rule has no lookback window at all, so
# the source's eight-cell sweep collapses to its two rebalance schedules here: the
# lookback axis has no expression in the rule and inventing one would not be a
# replication of it.
GRID = (
    {'strength': 0.001, 'cap': 0.3, 'rebal': 30.0},
    {'strength': 0.001, 'cap': 0.3, 'rebal': 7.0},
)


def position(df, close, bpy, strength=0.001, cap=0.3, rebal=30.0):
    """Drift toward the nearest psychological round number, harder when it is close."""
    step = 10.0 ** np.floor(np.log10(np.where(close > 0, close, np.nan)))
    nearest = np.round(close / step) * step
    dist = safe_div(np.abs(close - nearest), close, fill=np.nan)
    pull = np.clip(safe_div(strength, dist + 1e-6, fill=0.0), 0.0, cap)
    pred = close + pull * (nearest - close)
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
