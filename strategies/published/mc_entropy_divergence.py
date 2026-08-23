"""Low return entropy means trend and is extended; high entropy means noise and reverts."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (entropy, expose, lin, rets, rmean, rmedian, win)

RULE = 'Low return entropy means trend and is extended; high entropy means noise and reverts.'
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'EntropyDivergence'"
FAMILY = 'regime'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = 'NAMED BY THE SOURCE AS A TOP-3 STRATEGY, advertised as EntropyDivergence at a 90-day lookback rebalanced monthly, claiming 20.95% CAGR after fees (171.9x total against a 9.75x benchmark). RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet. The entropy threshold is the rolling MEDIAN of the entropy itself, not a fixed level: a fixed one is not comparable across assets or timeframes, and a whole-series median would be look-ahead.'

LOGIC = """
What the source published
    Calculates the Shannon entropy of an asset's returns. Low entropy suggests a trend,
    leading to a momentum-based prediction. High entropy suggests noise, leading to a
    mean-reversion prediction towards the moving average.

How that becomes a position here
    The forecast is compared with the SAME bar's close: long above it, flat at or below,
    and the resulting position is then held until the next rebalance. Only the sign of
    (forecast - close) reaches the book, so whatever magnitude the formula produces is
    discarded - a rule that forecasts a 200% move and one that forecasts a tick score
    identically.

What the position is really exposed to
    Whichever of its two branches the sample happens to favour: a trend forecast in one
    state and a reversion forecast in the other.

How it fails
    The switch is the whole rule, so a result here is a statement about how the regime
    split fell in this sample as much as about either branch. Move the threshold and it
    becomes a different strategy.

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
    {'days': 90.0, 'rebal': 30.0},
    {'days': 7.0, 'rebal': 7.0},
    {'days': 14.0, 'rebal': 7.0},
    {'days': 21.0, 'rebal': 7.0},
    {'days': 28.0, 'rebal': 7.0},
    {'days': 30.0, 'rebal': 30.0},
    {'days': 60.0, 'rebal': 30.0},
    {'days': 180.0, 'rebal': 30.0},
)


def position(df, close, bpy, days=90.0, rebal=30.0):
    """Low return entropy means trend and is extended; high entropy means noise and reverts."""
    w = win(bpy, days)
    h = entropy(rets(close), w, bins=10)
    slope, _ = lin(close, w)
    cut = rmedian(h, w)
    pred = np.where(h <= cut, close + slope, rmean(close, w))
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
