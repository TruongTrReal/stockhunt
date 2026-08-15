"""Buy when RSI(2) < 10 while above the 200-day SMA; exit above the 5-day SMA."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D, _state_machine


RULE = 'Buy when RSI(2) < 10 while above the 200-day SMA; exit above the 5-day SMA.'
SOURCE = "Connors & Alvarez, 'Short Term Trading Strategies That Work'; QuantifiedStrategies 'RSI-2'"
FAMILY = 'reversion'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    RSI(2) — a 2-period relative strength index, so it is dominated by the last two bars
    and swings to its extremes constantly, unlike the usual RSI(14).

The claim
    Connors' claim is that in an uptrend, very short-term oversold readings are noise
    and mean-revert. The 200-day filter is what makes it a dip-buy rather than a
    knife-catch: it only buys weakness inside strength.

What the position is really exposed to
    Short-term reversal conditioned on trend. The 200-day gate means it inherits the
    market's long-run drift as well as any reversal edge.

How it fails
    The trend filter is what stops it from buying assets that are genuinely dying —
    remove it (`rsi2_raw`) and it will buy every step of a terminal decline. Compare the
    two rows to price exactly what the filter is worth.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'buy': 10.0, 'exit_ma_days': 5.0, 'trend_ma_days': 200.0},
        {'buy': 5.0, 'exit_ma_days': 5.0, 'trend_ma_days': 200.0},
        {'buy': 15.0, 'exit_ma_days': 5.0, 'trend_ma_days': 200.0},
        {'buy': 10.0, 'exit_ma_days': 10.0, 'trend_ma_days': 200.0},
        {'buy': 10.0, 'exit_ma_days': 2.0, 'trend_ma_days': 100.0},
)

def position(df, close, bpy, buy=10.0, exit_ma_days=5.0, trend_ma_days=200.0):
    """Connors' RSI(2): buy deep oversold, exit on a short MA. Long/flat only.

    `trend_ma_days=0` removes the regime filter, which is `rsi2_raw`.
    """
    r = talib.RSI(close, timeperiod=_bars(bpy, 2 * D))
    entry = np.nan_to_num(r < buy, nan=False)
    if trend_ma_days:
        trend = talib.SMA(close, timeperiod=_bars(bpy, trend_ma_days * D))
        entry &= np.nan_to_num(close > trend, nan=False)
    ex = talib.SMA(close, timeperiod=_bars(bpy, exit_ma_days * D))
    return _state_machine(entry, np.nan_to_num(close > ex, nan=False))
