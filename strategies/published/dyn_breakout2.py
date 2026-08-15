"""Channel breakout whose lookback lengthens as volatility rises."""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies._indicators import _bars, D, _state_machine, _causal_median


RULE = 'Channel breakout whose lookback lengthens as volatility rises.'
SOURCE = "QuantConnect, 'Dynamic Breakout II'; Pruitt's Dynamic Break Out II"
FAMILY = 'regime'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    Pruitt's Dynamic Break Out II: a channel breakout whose LOOKBACK lengthens as
    volatility rises.

The claim
    A fixed lookback is wrong in one regime or the other; adapting the window is meant
    to keep the breakout meaningful across both.

What the position is really exposed to
    Trend / breakout with an adaptive window.

How it fails
    The published version normalises volatility against the median of the WHOLE series,
    which is look-ahead. This implementation uses an expanding median instead — see
    `_causal_median`. Numbers from the original construction are optimistic.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'base_days': 20.0, 'vol_days': 30.0, 'lo_days': 20.0, 'hi_days': 60.0},
        {'base_days': 40.0, 'vol_days': 30.0, 'lo_days': 20.0, 'hi_days': 80.0},
        {'base_days': 20.0, 'vol_days': 60.0, 'lo_days': 10.0, 'hi_days': 40.0},
)

def position(df, close, bpy, base_days=20.0, vol_days=30.0, lo_days=20.0,
                  hi_days=60.0):
    """Pruitt's Dynamic Break Out II: the breakout lookback tracks realised volatility.

    Volatility rising lengthens the window (fewer, more selective breakouts); falling
    volatility shortens it. Window length varies per bar, so this cannot be a fixed
    rolling call and is computed in a loop.
    """
    n_vol = _bars(bpy, vol_days * D)
    lo_n, hi_n = _bars(bpy, lo_days * D), _bars(bpy, hi_days * D)
    ret = np.zeros(len(close))
    ret[1:] = close[1:] / close[:-1] - 1.0
    sd = pd.Series(ret).rolling(n_vol).std(ddof=1).to_numpy()
    # Expanding, not whole-sample: normalising today's volatility against the median of
    # a series that includes tomorrow decides today's lookback with tomorrow's data.
    ref = _causal_median(sd, n_vol)
    ratio = np.divide(sd, ref, out=np.ones_like(sd),
                      where=np.isfinite(sd) & np.isfinite(ref) & (ref > 0))
    span = np.clip(np.nan_to_num(_bars(bpy, base_days * D) * ratio, nan=lo_n),
                   lo_n, hi_n).astype(int)

    hi, lo = df["High"].to_numpy("float64"), df["Low"].to_numpy("float64")
    entry = np.zeros(len(close), dtype=bool)
    ex = np.zeros(len(close), dtype=bool)
    for i in range(1, len(close)):
        n = span[i]
        j = max(0, i - n)
        if j == i:
            continue
        entry[i] = close[i] > hi[j:i].max()
        ex[i] = close[i] < lo[j:i].min()
    return _state_machine(entry, ex)
