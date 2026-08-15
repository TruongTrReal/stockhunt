"""Long the last session of each month plus the first 3 of the next."""

from __future__ import annotations


from strategies._indicators import _day_ordinals


RULE = 'Long the last session of each month plus the first 3 of the next.'
SOURCE = "QuantConnect, 'Turn of the Month in Equity Indexes'; Lakonishok & Smidt (1988)"
FAMILY = 'calendar'
ANCHOR = False
CLASSES = None
NOTE = """Boundary-only causality caveat, flagged by strategies/tests/test_causality.py.

`_day_ordinals` counts how many sessions a month contains from the OBSERVED bars, so
truncating the series mid-month makes the final partial month look short and flips
`pos > size - before` for the last 1-3 bars.

Economically this is not look-ahead: exchange calendars are published years in advance
and a trader genuinely does know which session is January's last. But the implementation
infers it from data rather than from a calendar, so any series boundary is ambiguous.
Positions are built on the full series and sliced afterwards, so the affected bars are
the last few of the whole dataset, not of every fold. Fixing it means deriving month-end
from the timestamp rather than from which bars happen to exist."""

LOGIC = """
What it measures
    Calendar only: long the last session of a month and the first few of the next.

The claim
    Lakonishok & Smidt (1988): pension and payroll flows concentrate around month end,
    pushing returns into a predictable window.

What the position is really exposed to
    Pure calendar exposure — no price information is used at all.

How it fails
    A flow-based anomaly whose cause can change when market structure changes. It also
    carries a boundary caveat: session counts are inferred from the data, so the final
    bars of a truncated series can shift. See its NOTE.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'before': 1, 'after': 3},
        {'before': 1, 'after': 1},
        {'before': 2, 'after': 3},
        {'before': 3, 'after': 5},
)

def position(df, close, bpy, before=1, after=3):
    """Long the last `before` sessions of a month and the first `after` of the next."""
    pos, size = _day_ordinals(df.index)
    return ((pos <= after) | (pos > size - before)).astype("float64")
