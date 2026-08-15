"""ConnorsRSI below 10 buys, above 70 exits."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D, _state_machine, _pct_rank, _streak


RULE = 'ConnorsRSI below 10 buys, above 70 exits.'
SOURCE = "Connors Research, 'ConnorsRSI'"
FAMILY = 'reversion'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    ConnorsRSI averages three things: RSI(3) of price, RSI(2) of the up/down STREAK
    length, and the percentile rank of the 1-day return over 100 days.

The claim
    Combining magnitude, persistence and rarity is meant to identify exhaustion more
    reliably than price alone — a move that is large, prolonged AND unusual.

What the position is really exposed to
    Short-term reversal again, with a streak term that makes it explicitly a
    counter-trend bet on consecutive moves.

How it fails
    Three components mean three ways to be fitted. The published thresholds (10/70) are
    the only ones that carry the original claim; every grid variant is a new trial.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'buy': 10.0, 'sell': 70.0},
        {'buy': 5.0, 'sell': 70.0},
        {'buy': 15.0, 'sell': 60.0},
        {'buy': 20.0, 'sell': 80.0},
)

def position(df, close, bpy, buy=10.0, sell=70.0):
    """ConnorsRSI = mean of RSI(3), RSI(streak,2) and the 100-bar percentile of ROC(1)."""
    a = talib.RSI(close, timeperiod=_bars(bpy, 3 * D))
    b = talib.RSI(_streak(close), timeperiod=_bars(bpy, 2 * D))
    roc = np.zeros(len(close))
    roc[1:] = close[1:] / close[:-1] - 1.0
    c = _pct_rank(roc, _bars(bpy, 100 * D))
    crsi = (a + b + c) / 3.0
    return _state_machine(np.nan_to_num(crsi < buy, nan=False),
                          np.nan_to_num(crsi > sell, nan=False))
