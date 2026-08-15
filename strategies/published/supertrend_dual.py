"""A fast Supertrend for entry, a wider slow one as the gate."""

from __future__ import annotations

import numpy as np

from strategies._indicators import _supertrend_trend


RULE = 'A fast Supertrend for entry, a wider slow one as the gate.'
SOURCE = 'The multi-timeframe Supertrend, expressed by band width on one series'
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    Two Supertrends — a fast one to enter, a wider slow one that must agree.

The claim
    Multi-timeframe confirmation: the slow line defines the regime, the fast line times
    the entry.

What the position is really exposed to
    Trend, with an entry/regime split.

How it fails
    Four parameters across two indicators is the widest search surface in the catalog.
    Every grid cell is another trial and the deflated Sharpe charges for all of them.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'atr_days': 10.0, 'mult': 3.0, 'slow_days': 20.0, 'slow_mult': 6.0},
        {'atr_days': 7.0, 'mult': 2.0, 'slow_days': 20.0, 'slow_mult': 6.0},
        {'atr_days': 10.0, 'mult': 3.0, 'slow_days': 30.0, 'slow_mult': 8.0},
        {'atr_days': 10.0, 'mult': 3.0, 'slow_days': 14.0, 'slow_mult': 4.0},
)

def position(df, close, bpy, atr_days=10.0, mult=3.0,
                    slow_days=20.0, slow_mult=6.0):
    """A fast Supertrend for the entry, a slow one as the regime gate.

    The multi-timeframe Supertrend that trading forums reach for, expressed on one series
    by widening the band instead of resampling the bars. Same information, no resampling
    seam, and it keeps the 1d and 4h sheets measuring the same thing.
    """
    fast = np.maximum(_supertrend_trend(df, close, bpy, atr_days, mult), 0.0)
    slow = np.maximum(_supertrend_trend(df, close, bpy, slow_days, slow_mult), 0.0)
    return fast * slow
