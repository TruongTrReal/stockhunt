"""Long on the first green Heikin-Ashi bar after a smoothed bearish stretch."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D, _state_machine


RULE = 'Long on the first green Heikin-Ashi bar after a smoothed bearish stretch.'
SOURCE = "Marwo, 'Marwo_heiken_pure' (freqtrade strategy, 1h)"
FAMILY = 'reversion'
ANCHOR = False
CLASSES = None
NOTE = ('Converted from freqtrade. Its `minimal_roi = {"0": 0.04}` — take profit at 4% — '
        'is a trade-level exit with no expression in a per-bar exposure series and is '
        'dropped, so this holds every winner longer than the published strategy did. Its '
        'stoploss is -99%, i.e. off, so nothing was lost there. The author\'s own header '
        'says "do not use this strategy in live mode".')

LOGIC = """
What it measures
    A near-Heikin-Ashi candle, smoothed. The close is the bar's own OHLC average; the
    "open" is deliberately NOT the running HA open but the midpoint of the bar two back,
    which the author says he found worked better. Both are then run through a 6-bar SMA,
    and the rule watches when the smoothed open sits above the smoothed close.

The claim
    A stretch of red synthetic candles under a bearish smoothed pair is a washout, and
    the first green candle after it is the turn.

What the position is really exposed to
    Short-horizon reversal with a trend-state latch. It is long-only and spends real
    time in cash, so its information ratio against a fully-invested benchmark carries
    the usual exposure penalty before any skill is measured — read `ir_vs_random`, not
    `ir`.

How it fails
    The "open" is a two-bar-old midpoint, which means the candle's body is a lagged
    comparison rather than a same-bar one, and the sign of that body flips on noise as
    often as on turns. Stripped of the 4% take-profit the original leans on, what is
    left is a plain buy-the-dip latch whose exit — a red candle outside the bearish
    state — is much weaker than its entry.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'ma_days': 6.0, 'open_lag': 2},
        {'ma_days': 12.0, 'open_lag': 2},
        {'ma_days': 3.0, 'open_lag': 2},
        {'ma_days': 6.0, 'open_lag': 1},
)


def position(df, close, bpy, ma_days=6.0, open_lag=2):
    """The author's synthetic-candle state machine, walked bar by bar.

    `open_lag` is a bar count and stays one: it defines the SHAPE of the synthetic
    candle, so rescaling it by `bpy` would change what the candle is rather than how
    long a window it covers.
    """
    open_ = df["Open"].to_numpy("float64")
    high = df["High"].to_numpy("float64")
    low = df["Low"].to_numpy("float64")
    n = len(close)
    k = max(1, int(open_lag))

    hclose = (open_ + high + low + close) / 4.0
    hopen = np.full(n, np.nan)
    hopen[k:] = (open_[:-k] + close[:-k]) / 2.0

    # The leading NaNs of `hopen` are left in place: TA-Lib starts at the first finite
    # bar exactly as pandas would, so the smoothed pair is undefined for the first
    # `open_lag + ma` bars and the state machine stays flat through them. Back-filling
    # them with a constant instead would manufacture a signal out of the warmup.
    ma = _bars(bpy, ma_days * D)
    emac = talib.SMA(np.ascontiguousarray(hclose), ma)
    emao = talib.SMA(np.ascontiguousarray(hopen), ma)

    red = hclose < hopen
    green = hopen < hclose

    # `signal` starts as "the smoothed open is above the smoothed close" and then
    # propagates forward through red candles. It has to be walked because the loop in
    # the source reads the value it wrote on the previous iteration.
    signal = (emao > emac)
    enter = np.zeros(n, dtype=bool)
    exit_ = np.zeros(n, dtype=bool)
    for i in range(1, n):
        if red[i] and signal[i - 1]:
            signal[i] = True
        if green[i] and signal[i - 1]:
            enter[i] = True
        elif (not signal[i - 1]) and red[i]:
            exit_[i] = True
    return _state_machine(enter, exit_)
