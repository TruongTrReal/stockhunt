"""Long when at least 4 of the 6 moving averages (3/5/10/20/50/200d) sit below price."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D, TREND_MAS


RULE = 'Long when at least 4 of the 6 moving averages (3/5/10/20/50/200d) sit below price.'
SOURCE = "Han, Zhou & Zhu (2016), 'A Trend Factor', RFS"
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = 'The paper fits cross-sectional loadings on the six MA/price ratios; this harness is single-asset, so the loadings become an equal vote.'

LOGIC = """
What it measures
    A vote across six moving averages (3, 5, 10, 20, 50, 200 days): long when at least
    four sit below price.

The claim
    Aggregating horizons is meant to be more robust than any single lookback, since no
    one window is right in all regimes.

What the position is really exposed to
    Trend across multiple horizons; the short averages dominate the vote's timing.

How it fails
    A vote of six correlated indicators is not six independent opinions — they agree
    almost always, so this behaves much like a single medium-horizon trend rule.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'min_votes': 4},
        {'min_votes': 3},
        {'min_votes': 5},
        {'min_votes': 6},
)

def position(df, close, bpy, min_votes=4):
    """Han/Zhou/Zhu: a composite of price-normalised moving averages, voted.

    The paper regresses future returns on the six MA/price ratios and trades the fitted
    signal. Fitting cross-sectional loadings is not available here — this harness scores
    one asset against its own buy-and-hold — so the loadings are replaced by an equal
    vote. That is a simplification and it is flagged in the report rather than presented
    as the paper's specification.
    """
    votes = np.zeros(len(close))
    for days in TREND_MAS:
        ma = talib.SMA(close, timeperiod=_bars(bpy, days * D))
        votes += np.nan_to_num((close > ma).astype("float64"), nan=0.0)
    return (votes >= min_votes).astype("float64")
