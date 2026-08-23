"""When a week-long autocorrelation is strong, predict the price from a week ago."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (autocorr, expose, shift, win)

RULE = 'When a week-long autocorrelation is strong, predict the price from a week ago.'
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'RollingWindowSeasonalityTest'"
FAMILY = 'calendar'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = "RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet. The lag is a BAR count, so on the 4h sheets it is five 4h bars rather than five sessions. The source's construction; a calendar-aware version is a different rule and would be a separate trial."

LOGIC = """
What the source published
    If a significant weekly seasonality (autocorrelation at lag 5 > 0.3) is detected, it
    predicts the price from 5 days ago. Otherwise, it predicts the current price.

How that becomes a position here
    The forecast is compared with the SAME bar's close: long above it, flat at or below,
    and the resulting position is then held until the next rebalance. Only the sign of
    (forecast - close) reaches the book, so whatever magnitude the formula produces is
    discarded - a rule that forecasts a 200% move and one that forecasts a tick score
    identically.

What the position is really exposed to
    A fixed periodicity measured in BARS, so what it means changes between the 1d and 4h
    sheets even though the parameter does not.

How it fails
    Whatever seasonality it found is the most likely thing in this repo to be an
    artefact of the sample: calendar effects are the classic in-sample survivor and this
    one was never pre-registered by its author.

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
    {'days': 28.0, 'season_len': 5.0, 'rebal': 7.0},
    {'days': 7.0, 'season_len': 5.0, 'rebal': 7.0},
    {'days': 14.0, 'season_len': 5.0, 'rebal': 7.0},
    {'days': 21.0, 'season_len': 5.0, 'rebal': 7.0},
    {'days': 30.0, 'season_len': 5.0, 'rebal': 30.0},
    {'days': 60.0, 'season_len': 5.0, 'rebal': 30.0},
    {'days': 90.0, 'season_len': 5.0, 'rebal': 30.0},
    {'days': 180.0, 'season_len': 5.0, 'rebal': 30.0},
)


def position(df, close, bpy, days=28.0, season_len=5.0, rebal=7.0):
    """When a week-long autocorrelation is strong, predict the price from a week ago."""
    w = win(bpy, days)
    lag = max(2, int(round(season_len)))
    rho = autocorr(close, w, lag)[:, lag]
    pred = np.where(rho > 0.3, shift(close, lag), close)
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
