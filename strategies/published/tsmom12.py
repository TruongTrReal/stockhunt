"""Long if the trailing 12-month return is positive, else flat."""

from __future__ import annotations

import numpy as np

from strategies._indicators import _bars, M


RULE = 'Long if the trailing 12-month return is positive, else flat.'
SOURCE = "Moskowitz, Ooi & Pedersen (2012), 'Time Series Momentum', JFE"
FAMILY = 'trend'
ANCHOR = True
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    The sign of the trailing 12-month return.

The claim
    Time-series momentum (Moskowitz, Ooi & Pedersen 2012): an asset that has risen over
    the past year tends to keep rising over the next month. Distinct from
    cross-sectional momentum — it compares an asset to ITSELF, not to peers.

What the position is really exposed to
    Trend, and heavy market beta. On a rising index it is long most of the time, so much
    of its return is simply the market's.

How it fails
    It gets whipsawed at turning points, and by construction it is late: the 12-month
    window means it cannot exit a crash until the crash is a year old. Read `exposure`
    before crediting it with anything.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'lookback_months': 12.0},
        {'lookback_months': 3.0},
        {'lookback_months': 6.0},
        {'lookback_months': 9.0},
)

def position(df, close, bpy, lookback_months=12.0):
    n = _bars(bpy, lookback_months * M)
    past = np.full(len(close), np.nan)
    past[n:] = close[n:] / close[:-n] - 1.0
    return np.nan_to_num((past > 0).astype("float64"), nan=0.0)
