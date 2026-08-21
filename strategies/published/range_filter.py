"""Long above a smoothed range filter that only moves when price clears its band."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D, _flip


RULE = 'Long above a smoothed range filter that only moves when price clears its band.'
SOURCE = "DonovanWall, 'Range Filter [DW]' (TradingView); this is the reduced 'B&S Signals' build"
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ('Converted from Pine v4. The source names the file `vumanchu` but the code is '
        "DonovanWall's Range Filter; credited accordingly. `range_filter_macd` is the "
        'same filter with a MACD histogram gate and shares this implementation.')

LOGIC = """
What it measures
    A price line that refuses to move until price has travelled a full band width. The
    band is `EMA(EMA(|dclose|, n), 2n-1) * mult` — an average bar move, smoothed twice
    and multiplied up. The filter then ratchets: it steps to `close - band` when price
    closes that far above it and to `close + band` when price closes that far below,
    and otherwise it does not move at all.

The claim
    A trend is real only once price has covered several average bar moves in one
    direction. Everything smaller is range noise and the filter is designed to sit
    still through it, so the direction of the filter IS the trend.

What the position is really exposed to
    Trend, both directions, with a deadband. The deadband is the whole mechanism: the
    rule is out of the market in name only — it holds its last side through the chop
    rather than trading it.

How it fails
    A ratchet that only steps on a full band move gives back exactly one band width at
    every turn, and with `mult = 3.5` on a doubly-smoothed average that is a large give-
    back. The entry also requires the previous state to have been the OPPOSITE side, so
    it takes the first signal of a leg and ignores every later one — which means a
    single mistimed flip costs the whole leg.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'per_days': 20.0, 'mult': 3.5, 'allow_short': 1},
        {'per_days': 20.0, 'mult': 3.5, 'allow_short': 0},
        {'per_days': 10.0, 'mult': 3.5, 'allow_short': 1},
        {'per_days': 50.0, 'mult': 2.0, 'allow_short': 1},
)


def _filter(src: np.ndarray, n: int, mult: float):
    """The band, the ratcheted filter line and its direction.

    Split out because `range_filter_macd` is the same filter with one extra gate, and a
    second copy of a ratchet loop is how two "identical" rules quietly stop being
    identical.
    """
    avrng = talib.EMA(np.ascontiguousarray(np.abs(np.diff(src, prepend=src[0]))), n)
    band = talib.EMA(np.ascontiguousarray(avrng), max(2, 2 * n - 1)) * mult

    filt = np.empty(len(src))
    # Pine seeds `var rfilt = array.new_float(2, x)` from bar 0 and leaves it there
    # while the band is still na, so the line is flat through the warmup rather than
    # tracking price. Reproduced: an implementation that tracks price during warmup
    # generates a burst of signals the published rule never had.
    cur = src[0]
    for i in range(len(src)):
        r = band[i]
        if np.isfinite(r):
            if src[i] - r > cur:
                cur = src[i] - r
            elif src[i] + r < cur:
                cur = src[i] + r
        filt[i] = cur

    direction = np.zeros(len(src))
    d = 0.0
    for i in range(1, len(src)):
        if filt[i] > filt[i - 1]:
            d = 1.0
        elif filt[i] < filt[i - 1]:
            d = -1.0
        direction[i] = d
    return band, filt, direction


def raw_signals(src: np.ndarray, n: int, mult: float):
    """`(longCondition, shortCondition)` — the first signal of each leg, as published.

    `longCond` in the source is written as two ORed branches that differ only in
    `src > src[1]` versus `src < src[1]`, which collapses to "src moved at all". The
    equality case is genuinely excluded and is kept out here too.
    """
    _, filt, direction = _filter(src, n, mult)
    moved = np.zeros(len(src), dtype=bool)
    moved[1:] = src[1:] != src[:-1]
    long_c = (src > filt) & (direction > 0) & moved
    short_c = (src < filt) & (direction < 0) & moved

    # CondIni is the LAST side that had a condition, and an entry needs the previous
    # value of it to be the opposite one — so only the first bar of a leg signals.
    n_bars = len(src)
    long_sig = np.zeros(n_bars, dtype=bool)
    short_sig = np.zeros(n_bars, dtype=bool)
    cond = 0
    for i in range(n_bars):
        prev = cond
        if long_c[i] and prev == -1:
            long_sig[i] = True
        if short_c[i] and prev == 1:
            short_sig[i] = True
        if long_c[i]:
            cond = 1
        elif short_c[i]:
            cond = -1
    return long_sig, short_sig


def position(df, close, bpy, per_days=20.0, mult=3.5, allow_short=1):
    """Reverse on the first long/short signal of each leg."""
    n = _bars(bpy, per_days * D)
    long_sig, short_sig = raw_signals(close, n, mult)
    pos = _flip(long_sig, short_sig)
    return pos if allow_short else np.maximum(pos, 0.0)
