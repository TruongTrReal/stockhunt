"""Long when price crosses above a decayed average of recent pivots; short below it."""

from __future__ import annotations

import numpy as np

from strategies._indicators import _flip


RULE = 'Long when price crosses above a decayed average of recent pivots; short below it.'
SOURCE = "LonesomeTheBlue, 'Support Resistance Channels'; packaged as 'SH-LH' (Pine v5)"
FAMILY = 'reversion'
ANCHOR = False
CLASSES = None
NOTE = ('Converted from Pine. The pivot is confirmed `period` bars after it prints, which '
        'is what makes the centre line causal — the plotted version is drawn back onto '
        'the pivot bar and looks `period` bars more prescient than it is.')

LOGIC = """
What it measures
    A centre line built only from confirmed swing pivots. A pivot high at bar t-p is
    only known at bar t (p bars of higher lows on each side), and each new pivot pulls
    the centre one third of the way toward it: `centre = (2*centre + pivot) / 3`.

The claim
    Swings alternate around a slowly-moving level of agreement, so the level is a
    support/resistance line and crossing it marks a change of side.

What the position is really exposed to
    A slow, price-anchored moving level, so this behaves like a long-lag moving-average
    cross with an irregular update schedule. It reverses rather than going flat, so it
    is always in the market once the first cross happens.

How it fails
    The centre only updates when a pivot confirms, so in a fast trend it lags badly and
    every bar of the move is spent on the wrong side of a stale line. The signal also
    tests `close[1]` against `centre[2]`, i.e. it fires one bar AFTER the cross — a
    deliberate confirmation in the source, and a full bar of the move given away.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'period': 2.0, 'allow_short': 1},
        {'period': 5.0, 'allow_short': 1},
        {'period': 10.0, 'allow_short': 1},
        {'period': 2.0, 'allow_short': 0},
)


def _pivots(high: np.ndarray, low: np.ndarray, p: int):
    """Pine's `ta.pivothigh(p, p)` / `ta.pivotlow(p, p)`, confirmed at bar t.

    A pivot high at t-p requires high[t-p] to be the strict maximum of the 2p+1 window
    centred on it. Pine publishes the value at bar t — p bars later — and this returns
    it on the same schedule, because that is when a trader could have known it.
    """
    n = len(high)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    for t in range(2 * p, n):
        c = t - p
        win_h = high[t - 2 * p:t + 1]
        win_l = low[t - 2 * p:t + 1]
        if high[c] == win_h.max() and (win_h >= high[c]).sum() == 1:
            ph[t] = high[c]
        if low[c] == win_l.min() and (win_l <= low[c]).sum() == 1:
            pl[t] = low[c]
    return ph, pl


def position(df, close, bpy, period=2.0, allow_short=1):
    """Cross of the close over the pivot centre line, taken one bar late.

    `period` is a bar count, not a calendar span: a swing pivot is defined by the shape
    of the bars around it, so a "2-bar pivot" means the same two bars on every sheet
    and rescaling it by `bpy` would change what a pivot IS rather than how long it is.
    """
    p = max(1, int(round(period)))
    high = df["High"].to_numpy("float64")
    low = df["Low"].to_numpy("float64")
    ph, pl = _pivots(high, low, p)

    # `float lastpp = pivotHigh ? pivotHigh : pivotLow ? ... : na` — Pine treats a float
    # as false when it is na OR zero, and the high takes priority on a bar that confirms
    # both. Reproduced rather than tidied: a zero pivot is a real possibility on a
    # quote that has printed zero, and the source skips it.
    n = len(close)
    centre = np.full(n, np.nan)
    cur = np.nan
    for t in range(n):
        last = ph[t] if np.isfinite(ph[t]) and ph[t] != 0 else (
            pl[t] if np.isfinite(pl[t]) and pl[t] != 0 else np.nan)
        if np.isfinite(last):
            cur = last if not np.isfinite(cur) else (2.0 * cur + last) / 3.0
        centre[t] = cur

    # bsignal = close > centre[1] and close[1] < centre[2]
    c1 = np.roll(centre, 1)
    c2 = np.roll(centre, 2)
    p1 = np.roll(close, 1)
    c1[:1] = np.nan
    c2[:2] = np.nan
    p1[:1] = np.nan
    long_ = (close > c1) & (p1 < c2)
    short = (close < c1) & (p1 > c2)
    pos = _flip(long_, short)
    return pos if allow_short else np.maximum(pos, 0.0)
