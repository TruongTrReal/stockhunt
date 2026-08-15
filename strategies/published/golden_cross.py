"""Long while the 50-day SMA is above the 200-day SMA."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D


RULE = 'Long while the 50-day SMA is above the 200-day SMA.'
SOURCE = 'Folk / StockCharts ChartSchool; the retail default'
FAMILY = 'trend'
ANCHOR = True
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    The 50-day moving average crossing the 200-day.

The claim
    The retail default trend signal. The claim is that a fast average above a slow one
    identifies a regime worth being long in.

What the position is really exposed to
    Trend and market beta, with a long lag on both entry and exit.

How it fails
    Included as folklore, not as literature — it is the most widely known and therefore
    most heavily mined rule here. Treat any edge with matching suspicion.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'fast_days': 50.0, 'slow_days': 200.0},
        {'fast_days': 20.0, 'slow_days': 100.0},
        {'fast_days': 10.0, 'slow_days': 50.0},
        {'fast_days': 50.0, 'slow_days': 150.0},
)

def position(df, close, bpy, fast_days=50.0, slow_days=200.0):
    fast = talib.SMA(close, timeperiod=_bars(bpy, fast_days * D))
    slow = talib.SMA(close, timeperiod=_bars(bpy, slow_days * D))
    return np.where(np.isfinite(fast) & np.isfinite(slow) & (fast > slow), 1.0, 0.0)
