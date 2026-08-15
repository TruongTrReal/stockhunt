"""Supertrend long, gated by price above its 200-day average."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D, _supertrend_trend


RULE = 'Supertrend long, gated by price above its 200-day average.'
SOURCE = 'Supertrend + the Faber/GTAA regime filter'
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    Supertrend long, gated by price above its 200-day average.

The claim
    Same idea as the ADX version with a simpler, slower regime test.

What the position is really exposed to
    Trend, conditioned on long-run trend — two trend filters stacked.

How it fails
    Stacking correlated filters cuts exposure sharply without adding much independent
    information. Check `exposure`: much of any apparent improvement is simply being out
    of the market.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'atr_days': 10.0, 'mult': 3.0, 'ma_days': 200.0},
        {'atr_days': 10.0, 'mult': 3.0, 'ma_days': 100.0},
        {'atr_days': 10.0, 'mult': 3.0, 'ma_days': 50.0},
        {'atr_days': 7.0, 'mult': 3.0, 'ma_days': 200.0},
)

def position(df, close, bpy, atr_days=10.0, mult=3.0, ma_days=200.0):
    """Supertrend long, gated by a long-horizon moving-average regime filter."""
    t = np.maximum(_supertrend_trend(df, close, bpy, atr_days, mult), 0.0)
    ma = talib.SMA(close, timeperiod=_bars(bpy, ma_days * D))
    return np.where(np.isfinite(ma) & (close > ma), t, 0.0)
