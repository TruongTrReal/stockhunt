"""Follow a Bollinger break in its own direction; inside the bands, revert to the SMA."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (expose, rmean, rstd, win)

RULE = 'Follow a Bollinger break in its own direction; inside the bands, revert to the SMA.'
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'BollingerBandBreakout'"
FAMILY = 'trend'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = 'RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet.'

LOGIC = """
What the source published
    Calculates Bollinger Bands. If the price breaks out above the upper band or below
    the lower band, it predicts continuation of the breakout (scaled by breakout size).
    If within the bands, it predicts reversion to the SMA.

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
    {'days': 7.0, 'k': 2.0, 'rebal': 7.0},
    {'days': 14.0, 'k': 2.0, 'rebal': 7.0},
    {'days': 21.0, 'k': 2.0, 'rebal': 7.0},
    {'days': 28.0, 'k': 2.0, 'rebal': 7.0},
    {'days': 30.0, 'k': 2.0, 'rebal': 30.0},
    {'days': 60.0, 'k': 2.0, 'rebal': 30.0},
    {'days': 90.0, 'k': 2.0, 'rebal': 30.0},
    {'days': 180.0, 'k': 2.0, 'rebal': 30.0},
)


def position(df, close, bpy, days=7.0, k=2.0, rebal=7.0):
    """Follow a Bollinger break in its own direction; inside the bands, revert to the SMA."""
    w = win(bpy, days)
    mu, sd = rmean(close, w), rstd(close, w)
    up, dn = mu + k * sd, mu - k * sd
    pred = np.where(close > up, close + (close - up),
                    np.where(close < dn, close - (dn - close), mu))
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
