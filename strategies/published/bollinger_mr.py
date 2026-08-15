"""Long below the lower Bollinger band (20,2); exit at the midline."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D, _state_machine


RULE = 'Long below the lower Bollinger band (20,2); exit at the midline.'
SOURCE = 'Bollinger; QuantifiedStrategies mean-reversion write-ups'
FAMILY = 'reversion'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    Bollinger bands: a 20-day moving average plus/minus 2 standard deviations of price.
    The lower band is 'unusually cheap relative to recent noise'.

The claim
    Price that pierces the lower band has moved further than recent volatility
    justifies, and should revert to the mean. Exiting at the midline takes the reversion
    and not the trend.

What the position is really exposed to
    Short-term reversal, volatility-scaled. Because the band widens in turbulence, the
    trigger automatically demands a bigger move when the market is noisier.

How it fails
    Bands adapt to volatility with a lag, so in a regime shift the lower band chases
    price down and the rule buys the whole way. It cannot tell a mispricing from a
    repricing.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'period_days': 20.0, 'nbdev': 2.0},
        {'period_days': 20.0, 'nbdev': 2.5},
        {'period_days': 10.0, 'nbdev': 2.0},
        {'period_days': 50.0, 'nbdev': 2.0},
)

def position(df, close, bpy, period_days=20.0, nbdev=2.0):
    n = _bars(bpy, period_days * D)
    upper, mid, lower = talib.BBANDS(close, timeperiod=n, nbdevup=nbdev, nbdevdn=nbdev)
    return _state_machine(np.nan_to_num(close < lower, nan=False),
                          np.nan_to_num(close > mid, nan=False))
