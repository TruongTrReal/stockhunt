"""The same ATR trailing stop, long/flat instead of long/short."""

from __future__ import annotations


# Same mechanism as `supertrend`, published with different parameters;
# the implementation is imported rather than copied so the two cannot drift.
from strategies.published.supertrend import position   # noqa: F401


RULE = 'The same ATR trailing stop, long/flat instead of long/short.'
SOURCE = 'Seban, ibid — the long-only variant retail platforms ship by default'
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = 'Read against `supertrend` to price the short leg, and against the exposure controls before reading its IR at all — the controls are long/flat, so `ir_vs_random` is only defined for this variant and not for the long/short one.'

LOGIC = """
What it measures
    The same ATR trailing stop, but long/flat rather than long/short.

The claim
    Identical trend claim, without the assertion that the short side is also profitable.

What the position is really exposed to
    Trend plus market beta — removing the short leg reintroduces long-bias.

How it fails
    Compare against `supertrend` to see whether the short side helped or hurt; on a
    rising benchmark, going flat instead of short usually flatters the numbers.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'atr_days': 10.0, 'mult': 3.0, 'allow_short': 0},
        {'atr_days': 7.0, 'mult': 3.0, 'allow_short': 0},
        {'atr_days': 14.0, 'mult': 2.0, 'allow_short': 0},
        {'atr_days': 20.0, 'mult': 4.0, 'allow_short': 0},
)
