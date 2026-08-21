"""Long on the first close back over the Bollinger midline after piercing the lower band."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D, _cross_over, _flip


RULE = ('Long on the first close back over the Bollinger midline after piercing the '
        'lower band; short on the mirror.')
SOURCE = "JustUncleL / 'BO Swing'; packaged as 'SH-BOSS' (Pine v5)"
FAMILY = 'reversion'
ANCHOR = False
CLASSES = None
NOTE = ('Converted from Pine. The source ships with `filter = false`, so the fast/slow '
        'HullMA direction filter it defines is dead code on the published settings and '
        'is not implemented here. Its KitKat support lines and pivot labels are plot-only.')

LOGIC = """
What it measures
    A round trip through a Bollinger channel. The rule waits for a bar whose LOW pierced
    the lower band, then arms; it fires on the first close back above the 20-bar
    midline, and only if no bar in between undercut the piercing low. The short side is
    the same shape around the upper band.

The claim
    A band pierce is an overshoot and the midline is fair value, so the leg from one to
    the other is a mechanical snap-back that can be taken without predicting anything.

What the position is really exposed to
    Short-horizon reversal plus short volatility — the same family as `ibs` and
    `bollinger_mr`, and it should be read next to them rather than on its own. It
    reverses rather than going flat, so the short leg carries the benchmark's drift.

How it fails
    "Pierced the band and did not make a new low" is a condition that is easiest to
    satisfy in a quiet market and hardest in the fall you actually needed to avoid. The
    rule is therefore selecting against its own tail: it declines the trades where the
    reversion does not happen and takes the ones where it was going to happen anyway,
    which is how a reversion rule posts a high hit rate and a negative expectancy.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'bb_days': 20.0, 'bb_std': 2.0, 'allow_short': 1},
        {'bb_days': 20.0, 'bb_std': 2.0, 'allow_short': 0},
        {'bb_days': 20.0, 'bb_std': 2.5, 'allow_short': 1},
        {'bb_days': 50.0, 'bb_std': 2.0, 'allow_short': 1},
)


def _outside_in(price: np.ndarray, band: np.ndarray, mid_cross: np.ndarray,
                beyond) -> np.ndarray:
    """The `BBclong` / `BBcshort` counter of the source, reaching 1 exactly on the signal.

    `beyond(a, b)` is `<` for the long side (low under the lower band) and `>` for the
    short side. The counter resets to 0 on any bar that pierces the band again or that
    exceeds the last piercing extreme, so a signal can only be the FIRST midline cross
    after an untouched pierce — which is why the state has to be walked rather than
    masked.
    """
    n = len(price)
    out = np.zeros(n, dtype=bool)
    count = 0
    extreme = np.nan          # BBlow / BBhigh: the most recent piercing low or high
    extreme_bar = -1          # BBlowN / BBhighN
    hits = []                 # bars where price exceeded the standing extreme
    for i in range(n):
        pierced = np.isfinite(band[i]) and beyond(price[i], band[i])
        if pierced:
            extreme, extreme_bar = price[i], i
        past_extreme = np.isfinite(extreme) and beyond(price[i], extreme)
        if past_extreme:
            hits.append(i)

        if pierced or past_extreme:
            count = 0
        elif (mid_cross[i] and np.isfinite(extreme)
              # valuewhen(price beyond extreme, bar_index, 1): the SECOND most recent
              # such bar. Older than the current extreme means the extreme has stood
              # unchallenged since it printed.
              and (hits[-2] if len(hits) >= 2 else -1) < extreme_bar
              and not beyond(price[i], extreme)):
            count += 1
        elif count > 0:
            count += 1
        else:
            count = 0
        out[i] = count == 1
    return out


def position(df, close, bpy, bb_days=20.0, bb_std=2.0, allow_short=1):
    """Reverse on each completed outside-in round trip."""
    n = _bars(bpy, bb_days * D)
    src = np.ascontiguousarray(close)
    upper, basis, lower = talib.BBANDS(src, timeperiod=n, nbdevup=bb_std,
                                       nbdevdn=bb_std, matype=0)
    high = df["High"].to_numpy("float64")
    low = df["Low"].to_numpy("float64")

    long_sig = _outside_in(low, lower, _cross_over(close, basis), np.less)
    short_sig = _outside_in(high, upper, _cross_over(basis, close), np.greater)
    pos = _flip(long_sig, short_sig)
    return pos if allow_short else np.maximum(pos, 0.0)
