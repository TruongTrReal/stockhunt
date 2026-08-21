"""Long on an up bar that opened above the close two bars ago; short on the mirror."""

from __future__ import annotations

import numpy as np

from strategies._indicators import _flip


RULE = 'Long on an up bar that opened above the close two bars ago; short on the mirror.'
SOURCE = "TradingView built-in, 'BarUpDn Strategy' (Pine v5)"
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ('Converted from Pine. The source also sets `strategy.risk.max_intraday_loss(1%)`, '
        'which is an equity-curve circuit breaker rather than a signal and has no '
        'expression in a per-bar exposure series; it is dropped, so this is the rule '
        'without its risk stop.')

LOGIC = """
What it measures
    Two bars of a three-bar pattern. Long when the current bar closes up (close > open)
    AND its open is above the close of two bars ago; short on the exact mirror. Nothing
    is averaged and nothing has a lookback beyond three bars.

The claim
    A bar that gaps its open above a recent close and then closes higher still is
    short-horizon continuation — the buyers who marked the open up did not give it back.

What the position is really exposed to
    Very short-horizon momentum, always in the market once the first signal fires. It
    reverses rather than going flat, so it carries a short leg through every downtrend
    and its exposure is +/-1 essentially all the time.

How it fails
    It is one of the cheapest possible pattern rules and therefore one of the most
    heavily rediscovered. On a daily sheet it flips constantly, so turnover — not
    signal — is the thing to look at first: an edge of a few basis points per trade
    cannot survive a rule that trades this often.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
# The source has no tunable signal parameter at all, so the only variant worth a trial
# is the long/flat one, which prices the short leg the way `supertrend_lf` does.
GRID = (
        {'lookback': 2, 'allow_short': 1},
        {'lookback': 2, 'allow_short': 0},
        {'lookback': 1, 'allow_short': 1},
        {'lookback': 3, 'allow_short': 1},
)


def position(df, close, bpy, lookback=2, allow_short=1):
    """`close > open and open > close[2]` long, `close < open and open < close[2]` short."""
    n = int(lookback)
    open_ = df["Open"].to_numpy("float64")
    prev = np.full(len(close), np.nan)
    prev[n:] = close[:-n]
    long_ = (close > open_) & (open_ > prev)
    short = (close < open_) & (open_ < prev)
    pos = _flip(long_, short)
    return pos if allow_short else np.maximum(pos, 0.0)
