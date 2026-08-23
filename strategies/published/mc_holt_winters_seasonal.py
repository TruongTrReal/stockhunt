"""Holt-Winters with additive trend and a multiplicative week-long season."""

from __future__ import annotations

import numpy as np

from strategies._forecast import (expose, win)

RULE = 'Holt-Winters with additive trend and a multiplicative week-long season.'
SOURCE = "megacellar '164 Trading Strategy Browser' (megacellar-browser.onrender.com), 'HoltWintersSeasonal'"
FAMILY = 'calendar'
DRAFT = False
ANCHOR = False
CLASSES = None
NOTE = "RE-IMPLEMENTED FROM A DESCRIPTION, not from source code - the site publishes a one-paragraph account of each rule and no code, so this is a plausible rewrite and not a verified conversion of the original. It is also scored against a different benchmark: the source compares a book that rebalances weekly or monthly against one buy-and-hold number that never rebalances at all, which is the unmatched-schedule failure the root CLAUDE.md lists first. Its published result is not comparable to anything on this sheet. The season is a fixed BAR count, not a calendar week, so on the 4h sheets '5' is five 4h bars rather than five sessions. That is the source's construction; making it calendar-aware would be a different rule."

LOGIC = """
What the source published
    Uses Holt-Winters triple exponential smoothing with additive trend and
    multiplicative seasonality (daily, e.g., 5-day season) to forecast prices. It
    updates level, trend, and seasonal components iteratively.

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
    {'season_len': 60.0, 'a': 0.3, 'b': 0.1, 'g': 0.1, 'rebal': 30.0},
    {'season_len': 7.0, 'a': 0.3, 'b': 0.1, 'g': 0.1, 'rebal': 7.0},
    {'season_len': 14.0, 'a': 0.3, 'b': 0.1, 'g': 0.1, 'rebal': 7.0},
    {'season_len': 21.0, 'a': 0.3, 'b': 0.1, 'g': 0.1, 'rebal': 7.0},
    {'season_len': 28.0, 'a': 0.3, 'b': 0.1, 'g': 0.1, 'rebal': 7.0},
    {'season_len': 30.0, 'a': 0.3, 'b': 0.1, 'g': 0.1, 'rebal': 30.0},
    {'season_len': 90.0, 'a': 0.3, 'b': 0.1, 'g': 0.1, 'rebal': 30.0},
    {'season_len': 180.0, 'a': 0.3, 'b': 0.1, 'g': 0.1, 'rebal': 30.0},
)


def position(df, close, bpy, season_len=60.0, a=0.3, b=0.1, g=0.1, rebal=30.0):
    """Holt-Winters with additive trend and a multiplicative week-long season."""
    n = len(close)
    season = max(2, int(round(season_len)))
    lvl = np.empty(n); tr = np.empty(n); pred = np.full(n, np.nan)
    seas = np.ones(season)
    lvl[0], tr[0] = close[0], 0.0
    for t in range(1, n):
        s_old = seas[t % season]
        prev = lvl[t - 1] + tr[t - 1]
        lvl[t] = a * (close[t] / s_old if s_old else prev) + (1.0 - a) * prev
        tr[t] = b * (lvl[t] - lvl[t - 1]) + (1.0 - b) * tr[t - 1]
        seas[t % season] = (g * (close[t] / lvl[t] if lvl[t] else s_old)
                            + (1.0 - g) * s_old)
        pred[t] = (lvl[t] + tr[t]) * seas[(t + 1) % season]
    return expose(pred, close, win(bpy, rebal, 1) if rebal > 0 else 0)
