"""Supertrend long, only while ADX(14) is at or above 25."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D, _supertrend_trend


RULE = 'Supertrend long, only while ADX(14) is at or above 25.'
SOURCE = 'The conventional Supertrend + ADX range filter'
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    Supertrend, gated by ADX(14) >= 25. ADX measures trend STRENGTH without direction.

The claim
    Trend rules lose in ranges, so only trade when the market is measurably trending.

What the position is really exposed to
    Trend, conditioned on a trend-strength regime.

How it fails
    ADX is itself lagging, so the gate opens after the trend is established and closes
    after it is over. The filter reduces turnover more reliably than it improves
    returns.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'atr_days': 10.0, 'mult': 3.0, 'adx_days': 14.0, 'adx_min': 25.0},
        {'atr_days': 10.0, 'mult': 3.0, 'adx_days': 14.0, 'adx_min': 20.0},
        {'atr_days': 10.0, 'mult': 3.0, 'adx_days': 14.0, 'adx_min': 30.0},
        {'atr_days': 10.0, 'mult': 3.0, 'adx_days': 20.0, 'adx_min': 25.0},
)

def position(df, close, bpy, atr_days=10.0, mult=3.0,
                   adx_days=14.0, adx_min=25.0):
    """Supertrend long, taken only while ADX says there is a trend worth following.

    The standard pairing, and the one with an actual thesis behind it: an ATR trailing
    stop whipsaws in a range, and ADX is the conventional measure of whether price is
    ranging. ADX is direction-blind by construction, so this is a pure filter — it can
    only remove exposure, never add or reverse it.
    """
    t = np.maximum(_supertrend_trend(df, close, bpy, atr_days, mult), 0.0)
    adx = talib.ADX(df["High"].to_numpy("float64"), df["Low"].to_numpy("float64"),
                    close, timeperiod=_bars(bpy, adx_days * D))
    return np.where(np.isfinite(adx) & (adx >= adx_min), t, 0.0)
