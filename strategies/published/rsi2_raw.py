"""The same RSI(2) dip buy with no 200-day trend filter."""

from __future__ import annotations


# Same mechanism as `rsi2_connors`, published with different parameters;
# the implementation is imported rather than copied so the two cannot drift.
from strategies.published.rsi2_connors import position   # noqa: F401


RULE = 'The same RSI(2) dip buy with no 200-day trend filter.'
SOURCE = 'Connors & Alvarez, ibid — the unfiltered variant'
FAMILY = 'reversion'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    The same RSI(2) oversold trigger with the 200-day trend gate REMOVED.

The claim
    It exists as a control, not as a proposal: it isolates how much of `rsi2_connors` is
    the dip-buy and how much is the trend filter.

What the position is really exposed to
    Pure short-term reversal with no regime condition, so it buys weakness in downtrends
    as readily as in uptrends.

How it fails
    This is the version that catches falling knives. Any gap between it and
    `rsi2_connors` is the value of refusing to buy assets below their 200-day average.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'buy': 10.0, 'exit_ma_days': 5.0, 'trend_ma_days': 0.0},
        {'buy': 5.0, 'exit_ma_days': 5.0, 'trend_ma_days': 0.0},
        {'buy': 15.0, 'exit_ma_days': 10.0, 'trend_ma_days': 0.0},
)
