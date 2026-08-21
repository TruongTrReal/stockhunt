"""Buy the dip on the bar an aligned 5/10/25/60 SMA fan breaks; exit on stochastic or CCI."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D


RULE = ('Buy the dip on the bar an aligned 5/10/25/60 SMA fan breaks; exit on a '
        'stochastic or CCI reading once in profit.')
SOURCE = "'EVA2' (freqtrade strategy, 15m)"
FAMILY = 'reversion'
ANCHOR = False
CLASSES = None
NOTE = ('Converted from freqtrade. Its exits are a `custom_exit` written against live '
        'trade state, so they are reproduced here by walking the bars and carrying the '
        'entry price — the same information, causally available. TWO deviations: the '
        'stop is measured on the CLOSE where freqtrade measures it intrabar, and the '
        '"5% profit within 10 minutes" rule is dropped as unreachable, since the '
        'coarsest bar this repo runs is a day and the rule can only fire before the '
        'first bar after entry has closed.')

LOGIC = """
What it measures
    A four-deep moving-average fan — 5 over 10 over 25 over 60 — with the extra
    condition that the previous close was BELOW the fastest of them. That combination
    marks a pullback inside an intact uptrend. The rule then fires on the bar the
    combination stops being true, and only if price is a further 2% under the 5-bar
    average.

The claim
    An uptrend that has just been interrupted by a sharp dip is a discount, not a
    breakdown. Buy the interruption.

What the position is really exposed to
    Short-horizon reversal inside a medium-horizon trend, long-only, in cash most of the
    time. The exits are almost all profit-conditional — six of the seven require
    `current_profit > 0` or a price back above entry — so the rule holds losers and
    releases winners. That asymmetry, not the entry, is what its equity curve is
    actually made of.

How it fails
    Cutting winners and running losers is the classic disposition-effect shape: a very
    high hit rate, a small median win, and the whole distribution's mass in a left tail
    held open by a -25% stop. Any summary that quotes the win rate on this rule is
    quoting the wrong number; look at the excess CAGR and the drawdown.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'dip_pct': 0.02, 'sell_fastk': 70.0, 'sell_cci': 90.0, 'loss_cci': 148.0,
         'loss_cci_profit': -0.04, 'hold_days': 8.0, 'stop': 0.25},
        {'dip_pct': 0.03, 'sell_fastk': 70.0, 'sell_cci': 90.0, 'loss_cci': 148.0,
         'loss_cci_profit': -0.04, 'hold_days': 8.0, 'stop': 0.25},
        {'dip_pct': 0.01, 'sell_fastk': 70.0, 'sell_cci': 90.0, 'loss_cci': 148.0,
         'loss_cci_profit': -0.04, 'hold_days': 8.0, 'stop': 0.25},
        {'dip_pct': 0.02, 'sell_fastk': 50.0, 'sell_cci': 90.0, 'loss_cci': 148.0,
         'loss_cci_profit': -0.04, 'hold_days': 8.0, 'stop': 0.25},
)


def position(df, close, bpy, dip_pct=0.02, sell_fastk=70.0, sell_cci=90.0,
             loss_cci=148.0, loss_cci_profit=-0.04, hold_days=8.0, stop=0.25):
    """Enter on the fan break, then walk the trade to find its exit.

    `hold_days` stands in for the source's "sell any open profit after two hours". Two
    hours is eight 15-minute candles and there is no honest calendar translation of that
    to a daily bar, so it is carried as a BAR COUNT in days and its published value is
    the eight candles the author used.
    """
    high = df["High"].to_numpy("float64")
    low = df["Low"].to_numpy("float64")
    src = np.ascontiguousarray(close)

    sma5 = talib.SMA(src, _bars(bpy, 5 * D))
    sma10 = talib.SMA(src, _bars(bpy, 10 * D))
    sma25 = talib.SMA(src, _bars(bpy, 25 * D))
    sma60 = talib.SMA(src, _bars(bpy, 60 * D))
    fastk, _ = talib.STOCHF(np.ascontiguousarray(high), np.ascontiguousarray(low), src,
                            fastk_period=_bars(bpy, 5 * D), fastd_period=3, fastd_matype=0)
    cci = talib.CCI(np.ascontiguousarray(high), np.ascontiguousarray(low), src,
                    timeperiod=_bars(bpy, 20 * D))

    n = len(close)
    prev_close = np.roll(close, 1)
    prev_sma5 = np.roll(sma5, 1)
    prev_close[0] = np.nan
    prev_sma5[0] = np.nan
    aligned = ((sma5 > sma10) & (sma10 > sma25) & (sma25 > sma60)
               & (prev_close < prev_sma5))
    switch = np.zeros(n, dtype=bool)
    switch[1:] = aligned[:-1] & ~aligned[1:]
    entry = switch & (close < sma5 * (1.0 - dip_pct))

    hold = _bars(bpy, hold_days * D)
    out = np.zeros(n)
    open_rate = np.nan
    held = 0
    for i in range(n):
        if np.isfinite(open_rate):
            profit = close[i] / open_rate - 1.0
            c = cci[i]
            leave = (
                profit <= -stop
                or (profit > 0 and np.isfinite(fastk[i]) and fastk[i] > sell_fastk)
                or (profit > 0 and np.isfinite(c) and c > sell_cci)
                or (held > hold and profit > 0)
                or (high[i] >= open_rate and np.isfinite(c) and c > sell_cci)
                or (profit > loss_cci_profit and np.isfinite(c) and c > loss_cci)
            )
            if leave:
                open_rate = np.nan
                held = 0
            else:
                held += 1
        if not np.isfinite(open_rate) and entry[i]:
            open_rate = close[i]
            held = 0
        out[i] = 1.0 if np.isfinite(open_rate) else 0.0
    return out
