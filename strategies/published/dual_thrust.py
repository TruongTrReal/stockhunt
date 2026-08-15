"""Long when the close clears the open by k x the recent 4-day range."""

from __future__ import annotations

import numpy as np

from strategies._indicators import _bars, D, _rolling_max, _rolling_min


RULE = 'Long when the close clears the open by k x the recent 4-day range.'
SOURCE = "QuantConnect Strategy Library, 'Dual Thrust Trading Algorithm'"
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    A band built from the recent 4-day high/low RANGE, projected from today's open. Long
    when the close clears the open by k times that range.

The claim
    An intraday breakout framework: a move that exceeds a fraction of recent range is a
    genuine directional push rather than noise.

What the position is really exposed to
    Trend / breakout, scaled by recent range, so it self-adjusts to volatility.

How it fails
    It is an intraday system being run on bar closes here, which is not how it was
    published. That mismatch matters more than any parameter choice.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'lookback_days': 4.0, 'k': 0.5},
        {'lookback_days': 4.0, 'k': 0.2},
        {'lookback_days': 10.0, 'k': 0.5},
        {'lookback_days': 20.0, 'k': 0.7},
)

def position(df, close, bpy, lookback_days=4.0, k=0.5):
    """QuantConnect's Dual Thrust: long when price clears the open by k x recent range."""
    n = _bars(bpy, lookback_days * D)
    hi, lo = df["High"].to_numpy("float64"), df["Low"].to_numpy("float64")
    op = df["Open"].to_numpy("float64")
    hh, ll = _rolling_max(hi, n), _rolling_min(lo, n)
    hc, lc = _rolling_max(close, n), _rolling_min(close, n)
    rng = np.maximum(hh - lc, hc - ll)
    return np.nan_to_num((close > op + k * rng).astype("float64"), nan=0.0)
