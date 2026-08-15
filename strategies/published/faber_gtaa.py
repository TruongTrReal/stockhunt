"""Long while the close is above its 10-month moving average."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, M


RULE = 'Long while the close is above its 10-month moving average.'
SOURCE = "Faber (2007), 'A Quantitative Approach to Tactical Asset Allocation'"
FAMILY = 'trend'
ANCHOR = True
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    Close versus its 10-MONTH moving average, the canonical tactical-allocation filter.

The claim
    Faber (2007): a simple long-term trend filter avoids the worst drawdowns while
    giving up little upside, because large declines are persistent rather than
    instantaneous.

What the position is really exposed to
    Trend and market beta, at a very slow frequency — it trades a handful of times a
    year.

How it fails
    Its whole case is drawdown reduction, not return. Judge it on drawdown and on
    risk-matched terms; on raw CAGR it will usually lose to buy-and-hold and that is not
    a refutation of the claim.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'ma_months': 10.0},
        {'ma_months': 6.0},
        {'ma_months': 8.0},
        {'ma_months': 12.0},
)

def position(df, close, bpy, ma_months=10.0):
    ma = talib.SMA(close, timeperiod=_bars(bpy, ma_months * M))
    return np.nan_to_num((close > ma).astype("float64"), nan=0.0)
