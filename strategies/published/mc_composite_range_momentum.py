"""Scale the price up by the average range across three sub-windows."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (expose, rmax, rmin, safe_div, win)

RULE = 'Scale the price up by the average range across three sub-windows.'
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'CompositeRangeMomentum'"
FAMILY = 'volatility'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = 'RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet. STRUCTURALLY ALWAYS LONG: a range is non-negative, so the forecast never falls below the close. Same disguised buy-and-hold as `mc_bollinger_bandwidth`.'

LOGIC = """
What the source published
    Calculates the average price range over multiple sub-window lengths (5, 10, full
    window). Predicts price movement scaled by `(1 + (avg_range / current_price) *
    0.5)`, implying momentum with larger ranges.

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
    """Scale the price up by the average range across three sub-windows."""
    w = win(bpy, days)
    parts = [rmax(close, n) - rmin(close, n)
             for n in (max(2, w // 12), max(2, w // 6), w)]
    stack = np.vstack(parts)
    avg = np.where(np.isfinite(stack).all(axis=0), np.nansum(stack, axis=0) / 3.0, np.nan)
    pred = close * (1.0 + safe_div(avg, close, fill=np.nan) * 0.5)
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
