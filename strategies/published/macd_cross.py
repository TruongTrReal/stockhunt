"""Long while MACD(12,26) sits above its 9-period signal line."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D


RULE = 'Long while MACD(12,26) sits above its 9-period signal line.'
SOURCE = "QuantifiedStrategies, 'Bitcoin MACD Trading Strategy'"
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    MACD is the difference between a 12- and a 26-period EMA; the signal line is its own
    9-period EMA. Long while MACD is above signal.

The claim
    A momentum oscillator meant to catch acceleration earlier than a raw price cross
    does.

What the position is really exposed to
    Trend, with a shorter lag than the golden cross and correspondingly more turnover.

How it fails
    In a range it flips constantly and pays the spread each time. Its edge, if any, is
    in trending regimes, so its performance is a statement about the sample's regime
    mix.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'fast_days': 12.0, 'slow_days': 26.0, 'signal_days': 9.0},
        {'fast_days': 5.0, 'slow_days': 35.0, 'signal_days': 5.0},
        {'fast_days': 20.0, 'slow_days': 50.0, 'signal_days': 9.0},
        {'fast_days': 8.0, 'slow_days': 17.0, 'signal_days': 9.0},
)

def position(df, close, bpy, fast_days=12.0, slow_days=26.0, signal_days=9.0):
    macd, sig, _ = talib.MACD(close,
                              fastperiod=_bars(bpy, fast_days * D),
                              slowperiod=_bars(bpy, slow_days * D),
                              signalperiod=_bars(bpy, signal_days * D))
    return np.where(np.isfinite(macd) & np.isfinite(sig) & (macd > sig), 1.0, 0.0)
