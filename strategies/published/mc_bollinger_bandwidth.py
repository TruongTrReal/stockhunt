"""Scale the price up by the Bollinger bandwidth, so volatility itself is the forecast."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (expose, rmean, rstd, safe_div, win)

RULE = 'Scale the price up by the Bollinger bandwidth, so volatility itself is the forecast.'
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'BollingerBandwidth'"
FAMILY = 'volatility'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = 'RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet. STRUCTURALLY ALWAYS LONG once warmed up. Bandwidth is non-negative, so the forecast can never fall below the close and the mapping can never produce a flat bar. Kept because a rule that is buy-and-hold in disguise is worth having on the sheet by name rather than discovered later.'

LOGIC = """
What the source published
    Calculates Bollinger Bandwidth ((Upper - Lower) / SMA). The predicted price is the
    current price scaled by a factor of (1 + Bandwidth), anticipating price movement
    proportional to volatility.

How that becomes a position here
    The forecast is compared with the SAME bar's close: long above it, flat at or below,
    and the resulting position is then held until the next rebalance. Only the sign of
    (forecast - close) reaches the book, so whatever magnitude the formula produces is
    discarded - a rule that forecasts a 200% move and one that forecasts a tick score
    identically.

What the position is really exposed to
    Realised range, not direction. A range is non-negative, so this family leans
    structurally long and what it actually varies is how often it is NOT long.

How it fails
    It has no view. If the forecast can only sit above the close the rule degrades to
    buy-and-hold, and the exposure controls in `strat_wf` will say so.

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
    """Scale the price up by the Bollinger bandwidth, so volatility itself is the forecast."""
    w = win(bpy, days)
    mu, sd = rmean(close, w), rstd(close, w)
    bw = safe_div(2.0 * k * sd, mu, fill=np.nan)
    pred = close * (1.0 + bw)
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
