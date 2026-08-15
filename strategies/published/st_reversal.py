"""Long after a down week, flat after an up week."""

from __future__ import annotations

import numpy as np

from strategies._indicators import _bars


RULE = 'Long after a down week, flat after an up week.'
SOURCE = "Jegadeesh (1990), 'Evidence of Predictable Behavior of Security Returns', JF"
FAMILY = 'reversion'
ANCHOR = True
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    The sign of last week's return.

The claim
    The weekly short-term reversal effect: assets that fell last week tend to outperform
    next week, documented since Jegadeesh (1990).

What the position is really exposed to
    The cleanest possible short-term reversal exposure — no thresholds, no filters, one
    parameter.

How it fails
    It is the textbook effect, which means it is also the most arbitraged. Its published
    life is largely pre-2000, and it is precisely the kind of premium that survives in
    gross returns and dies in net ones.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'lookback_weeks': 1.0},
        {'lookback_weeks': 0.5},
        {'lookback_weeks': 2.0},
        {'lookback_weeks': 4.0},
)

def position(df, close, bpy, lookback_weeks=1.0):
    n = _bars(bpy, lookback_weeks / 52.0)
    past = np.full(len(close), np.nan)
    past[n:] = close[n:] / close[:-n] - 1.0
    return np.nan_to_num((past < 0).astype("float64"), nan=0.0)
