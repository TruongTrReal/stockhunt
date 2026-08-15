"""Long the session before an exchange holiday."""

from __future__ import annotations

import numpy as np
import pandas as pd


RULE = 'Long the session before an exchange holiday.'
SOURCE = "QuantConnect, 'Pre-holiday Effect'; Ariel (1990)"
FAMILY = 'calendar'
ANCHOR = False
CLASSES = ('us_stocks', 'us_etfs')
NOTE = 'A 24/7 market has no exchange closures, so this is undefined on crypto rather than merely unprofitable there.'

LOGIC = """
What it measures
    Calendar only: long the session before an exchange holiday.

The claim
    Pre-holiday sessions have historically shown elevated returns on thin volume and
    positive sentiment.

What the position is really exposed to
    Pure calendar exposure, and very little of it — a handful of sessions a year.

How it fails
    Exposure is so low that it can barely move a portfolio, and with ~8 events a year
    its sample is tiny. Undefined on 24/7 markets, so it is skipped on crypto.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'sessions': 1},
        {'sessions': 2},
        {'sessions': 3},
)

def position(df, close, bpy, sessions=1):
    """Long the `sessions` sessions before an exchange holiday.

    A holiday is detected as a gap in the session calendar: the next trading date is
    more than one business day away, with weekends excluded. Derived from the index
    rather than a hardcoded holiday table so it stays correct for any venue and any
    year the vendor serves.
    """
    dates = pd.DatetimeIndex(pd.unique(df.index.normalize()))
    if len(dates) < 3:
        return np.zeros(len(close))
    # Business days strictly between this session and the next; > 0 means a closure.
    gaps = np.array([len(pd.bdate_range(dates[i] + pd.Timedelta(days=1),
                                        dates[i + 1] - pd.Timedelta(days=1)))
                     for i in range(len(dates) - 1)] + [0])
    flag = pd.Series(gaps > 0, index=dates)
    for k in range(1, int(sessions)):
        flag |= flag.shift(-k).fillna(False)
    return flag.reindex(df.index.normalize()).to_numpy().astype("float64")
