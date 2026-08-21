"""Long when price is a fixed percentage under a VWMA, an EMA and a slower EMA at once."""

from __future__ import annotations

import numpy as np
import pandas as pd
import talib

from strategies._indicators import _bars, D


RULE = ('Long when price is a fixed percentage under a VWMA, a fast EMA and a slow EMA '
        'at once; exit on any of four EMA offset bands.')
SOURCE = "'Cenderawasih_3_kucoin' (freqtrade strategy, 5m, KuCoin)"
FAMILY = 'reversion'
ANCHOR = False
CLASSES = ('us_stocks', 'us_etfs')
NOTE = ('Converted from freqtrade. RESTRICTED TO THE TWO CLASSES THAT HAVE VOLUME: the '
        'vendor serves none for crypto or commodities, so a VWMA there is a silently '
        'unweighted mean and the rule would score as something it is not. Its 15-minute '
        'informative EMA is approximated on the native frame by multiplying the length '
        'by the timeframe ratio; that is an analogue, not the same series. The `-25%` '
        'trailing custom stoploss is a trade-level exit and is dropped, along with the '
        'exchange-listing age and liquidity filters, which are universe screens this '
        'repo already applies elsewhere.')

LOGIC = """
What it measures
    Three simultaneous distances below three averages: 1.1% under a 31-bar VWMA, 8.8%
    under a 19-bar EMA, and below a slower EMA standing in for a higher timeframe — plus
    RSI(14) under 52. All four say the same thing in different words: price has fallen
    a long way, fast.

The claim
    On a 5-minute crypto chart a drop of that size inside a few hours is liquidation
    flow rather than information, and it reverts within the hour.

What the position is really exposed to
    Deep short-horizon reversal, long-only, in cash almost all of the time. The exit is
    four ORed bands and one of them — `close < EMA(5) * 0.908` — fires when price keeps
    falling, so the rule has a stop written as a signal.

How it fails
    The offsets are absolute percentages fitted to 5-minute crypto bars, and they do not
    travel. 8.8% below a 19-bar EMA is a routine afternoon in a KuCoin altcoin and close
    to unheard of on a daily large-cap sheet, so the honest prior is that this rule
    almost never trades here and that its trade count, not its return, is the first
    number to look at. A rule that fires ten times in twenty-six years has no result to
    quote no matter what those ten trades did.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'vwma_days': 31.0, 'vwma_off': 0.989, 'ema_days': 19.0, 'ema_off': 0.912,
         'rsi_max': 52.0, 'htf_days': 90.0, 'sell1_days': 61.0, 'sell1_off': 0.942,
         'sell2_days': 5.0, 'sell2_off': 0.908, 'sell3_days': 7.0, 'sell3_off': 0.947,
         'sell4_days': 14.0, 'sell4_off': 1.088},
        {'vwma_days': 31.0, 'vwma_off': 0.995, 'ema_days': 19.0, 'ema_off': 0.96,
         'rsi_max': 52.0, 'htf_days': 90.0, 'sell1_days': 61.0, 'sell1_off': 0.942,
         'sell2_days': 5.0, 'sell2_off': 0.908, 'sell3_days': 7.0, 'sell3_off': 0.947,
         'sell4_days': 14.0, 'sell4_off': 1.088},
        {'vwma_days': 31.0, 'vwma_off': 0.989, 'ema_days': 19.0, 'ema_off': 0.912,
         'rsi_max': 70.0, 'htf_days': 90.0, 'sell1_days': 61.0, 'sell1_off': 0.942,
         'sell2_days': 5.0, 'sell2_off': 0.908, 'sell3_days': 7.0, 'sell3_off': 0.947,
         'sell4_days': 14.0, 'sell4_off': 1.088},
)


def _vwma(close: np.ndarray, volume: np.ndarray, n: int) -> np.ndarray:
    """Volume-weighted moving average, the `pandas_ta.vwma` the source calls."""
    c, v = pd.Series(close * volume), pd.Series(volume)
    num = c.rolling(n).sum().to_numpy()
    den = v.rolling(n).sum().to_numpy()
    return np.divide(num, den, out=np.full(len(close), np.nan), where=den > 0)


def position(df, close, bpy, vwma_days=31.0, vwma_off=0.989, ema_days=19.0,
             ema_off=0.912, rsi_max=52.0, htf_days=90.0, sell1_days=61.0,
             sell1_off=0.942, sell2_days=5.0, sell2_off=0.908, sell3_days=7.0,
             sell3_off=0.947, sell4_days=14.0, sell4_off=1.088):
    """Four entry conditions, four ORed exit bands, walked as a long/flat state."""
    src = np.ascontiguousarray(close)
    volume = df["Volume"].to_numpy("float64")

    ema = lambda d: talib.EMA(src, _bars(bpy, d * D))
    entry = ((close < _vwma(close, volume, _bars(bpy, vwma_days * D)) * vwma_off)
             & (close < ema(ema_days) * ema_off)
             & (talib.RSI(src, _bars(bpy, 14 * D)) < rsi_max)
             & (close < ema(htf_days)))

    below3 = close < ema(sell3_days) * sell3_off
    above4 = close > ema(sell4_days) * sell4_off
    # `.rolling(2).min() > 0` on a boolean is "true on this bar and the last one".
    two = lambda b: np.concatenate(([False], b[1:] & b[:-1]))
    exit_ = ((close > ema(sell1_days) * sell1_off)
             | (close < ema(sell2_days) * sell2_off)
             | two(below3) | two(above4))

    entry = np.nan_to_num(entry).astype(bool)
    exit_ = np.nan_to_num(exit_).astype(bool)

    # Exit wins the tie here, unlike `_state_machine`: freqtrade evaluates the exit of an
    # open trade before it considers a new entry, so a bar that satisfies both leaves
    # the position flat rather than re-entering it.
    out = np.zeros(len(close))
    pos = 0.0
    for i in range(len(close)):
        if pos > 0 and exit_[i]:
            pos = 0.0
        elif pos == 0.0 and entry[i] and not exit_[i]:
            pos = 1.0
        out[i] = pos
    return out
