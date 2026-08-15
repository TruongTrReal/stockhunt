"""12-month momentum, taken only while price is above the 200-day SMA."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D

# Built ON another published strategy. In the old flat module this was a bare
# name; split across files it must be an import, and omitting it fails SILENTLY:
# `registry.build` catches the NameError and returns None, so the cell vanishes
# from the sheet instead of erroring. Caught by hashing positions before and after.
from strategies.published.tsmom12 import position as tsmom


RULE = '12-month momentum, taken only while price is above the 200-day SMA.'
SOURCE = "QuantConnect, 'Momentum and State of Market Filters'"
FAMILY = 'regime'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    12-month momentum, taken only while price is above its 200-day average.

The claim
    Momentum works better when the market itself is healthy; the regime filter is meant
    to remove momentum's worst episodes, which cluster in bear markets.

What the position is really exposed to
    Trend on trend — the two filters are highly correlated by construction.

How it fails
    Because both legs are long-run trend measures, the filter mostly removes exposure
    rather than adding information.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'lookback_months': 12.0, 'ma_days': 200.0},
        {'lookback_months': 6.0, 'ma_days': 200.0},
        {'lookback_months': 12.0, 'ma_days': 100.0},
        {'lookback_months': 3.0, 'ma_days': 50.0},
)

def position(df, close, bpy, lookback_months=12.0, ma_days=200.0):
    ma = talib.SMA(close, timeperiod=_bars(bpy, ma_days * D))
    regime = np.nan_to_num((close > ma).astype("float64"), nan=0.0)
    return tsmom(df, close, bpy, lookback_months) * regime
