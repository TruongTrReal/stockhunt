"""Turtle System 2: enter on a 55-day high, exit on a 20-day low."""

from __future__ import annotations


# Same mechanism as `donchian_s1`, published with different parameters;
# the implementation is imported rather than copied so the two cannot drift.
from strategies.published.donchian_s1 import position   # noqa: F401


RULE = 'Turtle System 2: enter on a 55-day high, exit on a 20-day low.'
SOURCE = 'Dennis & Eckhardt Turtle rules'
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    The same channel breakout at 55 days in and 20 days out.

The claim
    System 2 was the slower Turtle variant, meant to catch the longer trends System 1
    exits out of.

What the position is really exposed to
    Trend, slower and lower-turnover than System 1.

How it fails
    Same context caveat as System 1, and with a 55-day window it trades so rarely that
    its statistics rest on very few independent events.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'entry_days': 55.0, 'exit_days': 20.0},
        {'entry_days': 55.0, 'exit_days': 10.0},
        {'entry_days': 80.0, 'exit_days': 30.0},
        {'entry_days': 100.0, 'exit_days': 40.0},
)
