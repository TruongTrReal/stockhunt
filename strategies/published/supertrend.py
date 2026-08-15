"""Long above the ATR trailing stop, short below it (ATR 10, 3x)."""

from __future__ import annotations

import numpy as np

from strategies._indicators import _supertrend_trend


RULE = 'Long above the ATR trailing stop, short below it (ATR 10, 3x).'
SOURCE = "Seban, 'SuperTrend' (2007); the TradingView/MT4 default"
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = 'Absent from the 231-rule sweep only because TA-Lib has no Supertrend function. This is its first appearance in the project.'

LOGIC = """
What it measures
    An ATR trailing stop that flips side: the stop trails price by 3 x ATR(10), and the
    position is long above it and short below.

The claim
    A volatility-scaled trend follower — the stop distance widens in turbulence so
    normal noise does not stop you out.

What the position is really exposed to
    Trend, both directions. The short leg means it is NOT simply long-biased, which
    makes it one of the few rules here whose IR is not dominated by exposure.

How it fails
    Flipping side on every touch is expensive and whipsaw-prone in ranges.
    `supertrend_lf` is the long/flat version and the difference between them prices the
    short leg.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'atr_days': 10.0, 'mult': 3.0, 'allow_short': 1},
        {'atr_days': 7.0, 'mult': 3.0, 'allow_short': 1},
        {'atr_days': 14.0, 'mult': 2.0, 'allow_short': 1},
        {'atr_days': 20.0, 'mult': 4.0, 'allow_short': 1},
)

def position(df, close, bpy, atr_days=10.0, mult=3.0, allow_short=1):
    """The textbook rule: long in an uptrend, short in a downtrend.

    `allow_short=0` gives the long/flat variant. That switch is not cosmetic on this
    project's metric — IR is measured against buy-and-hold, and against a rising
    benchmark a short leg is charged the benchmark's drift twice over. The two versions
    are different strategies and both are scored.
    """
    t = _supertrend_trend(df, close, bpy, atr_days, mult)
    return t if allow_short else np.maximum(t, 0.0)
