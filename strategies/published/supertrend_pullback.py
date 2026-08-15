"""Supertrend picks the trend; RSI(2) below 25 picks the entry."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D, _state_machine, _supertrend_trend


RULE = 'Supertrend picks the trend; RSI(2) below 25 picks the entry.'
SOURCE = 'Supertrend direction + the Connors RSI(2) dip entry'
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    Supertrend sets the direction; RSI(2) < 25 picks the entry within it.

The claim
    Buy dips inside an uptrend — a trend filter with a reversion trigger, combining the
    two families in this catalog that behave most differently.

What the position is really exposed to
    Trend for direction, short-term reversal for timing.

How it fails
    It waits for both conditions, so it is rarely invested and its per-trade statistics
    rest on relatively few events.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'atr_days': 10.0, 'mult': 3.0, 'rsi_days': 2.0, 'buy': 25.0},
        {'atr_days': 10.0, 'mult': 3.0, 'rsi_days': 2.0, 'buy': 10.0},
        {'atr_days': 10.0, 'mult': 3.0, 'rsi_days': 2.0, 'buy': 35.0},
        {'atr_days': 10.0, 'mult': 3.0, 'rsi_days': 3.0, 'buy': 25.0},
)

def position(df, close, bpy, atr_days=10.0, mult=3.0,
                        rsi_days=2.0, buy=25.0):
    """Supertrend sets the direction; entry waits for an RSI(2) dip inside the uptrend.

    Deliberately the meta-labelling shape — the trend rule decides *whether* the market is
    ownable and a second, unrelated signal decides *when* to pay for it. It can only cut
    time-in-market relative to plain Supertrend, so a lower turnover here is expected and
    an improvement in IR would not be.
    """
    up = _supertrend_trend(df, close, bpy, atr_days, mult) > 0
    rsi = talib.RSI(close, timeperiod=_bars(bpy, rsi_days * D))
    return _state_machine(up & np.isfinite(rsi) & (rsi < buy), ~up)
