"""Long on an 8-over-21 EMA cross, short on the cross back."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D, _cross_over, _flip


RULE = 'Long on an 8-over-21 EMA cross, short on the cross back.'
SOURCE = "TradersPost Inc, 'Sniper short-term' (Pine v5)"
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ('Converted from Pine. The source plots two 30-minute EMAs fetched with '
        '`barmerge.lookahead_on` — real look-ahead, but they are only plotted and never '
        'reach a condition, so nothing had to be dropped to make this causal. Its stop '
        'and target inputs both default to 0, i.e. off, so the published rule genuinely '
        'has no exit other than the opposite cross.')

LOGIC = """
What it measures
    The oldest signal in the book: a fast EMA crossing a slow one. 8 and 21 bars.

The claim
    None that is specific to this script. It is a packaged, broker-ready wrapper around
    the textbook cross, and its value here is as a CALIBRATION point rather than a
    candidate — every other trend rule in this catalog should be read against what the
    plainest possible version of the same idea earns.

What the position is really exposed to
    Trend, both directions, always in the market. At 8/21 on a daily sheet it turns over
    roughly every few weeks, which puts it in the band where costs and signal are the
    same order of magnitude.

How it fails
    It has no filter of any kind, so it takes every cross in a range. This is the rule
    the whole moving-average literature exists to improve on, and the honest prior is
    that it does not beat buy-and-hold anywhere in this repo — `golden_cross` and
    `macd_cross` are already here and already null.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'fast_days': 8.0, 'slow_days': 21.0, 'allow_short': 1},
        {'fast_days': 8.0, 'slow_days': 21.0, 'allow_short': 0},
        {'fast_days': 5.0, 'slow_days': 13.0, 'allow_short': 1},
        {'fast_days': 20.0, 'slow_days': 50.0, 'allow_short': 1},
)


def position(df, close, bpy, fast_days=8.0, slow_days=21.0, allow_short=1):
    """Reverse on each EMA cross; `allow_short=0` gives the long/flat version."""
    src = np.ascontiguousarray(close)
    fast = talib.EMA(src, _bars(bpy, fast_days * D))
    slow = talib.EMA(src, _bars(bpy, slow_days * D))
    pos = _flip(_cross_over(fast, slow), _cross_over(slow, fast))
    return pos if allow_short else np.maximum(pos, 0.0)
