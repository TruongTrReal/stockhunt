"""Turtle System 1: enter on a 20-day high, exit on a 10-day low."""

from __future__ import annotations

import numpy as np

from strategies._indicators import _bars, D, _state_machine, _rolling_max, _rolling_min


RULE = 'Turtle System 1: enter on a 20-day high, exit on a 10-day low.'
SOURCE = "Dennis & Eckhardt Turtle rules; QuantifiedStrategies 'Donchian Channels'"
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    Entry on a 20-day high, exit on a 10-day low — the original Turtle System 1.

The claim
    Dennis & Eckhardt's claim, taught to non-traders in 1983: breakouts to new highs
    continue, and a mechanical exit is what makes the expectancy positive.

What the position is really exposed to
    Trend, with an explicit asymmetry — a slow entry and a fast exit.

How it fails
    The Turtle rules were designed for a diversified FUTURES portfolio with risk-parity
    sizing, not single equities at fixed weight. Missing that context is why it usually
    disappoints here.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'entry_days': 20.0, 'exit_days': 10.0},
        {'entry_days': 10.0, 'exit_days': 5.0},
        {'entry_days': 30.0, 'exit_days': 15.0},
        {'entry_days': 40.0, 'exit_days': 20.0},
)

def position(df, close, bpy, entry_days=20.0, exit_days=10.0):
    """Turtle channel breakout: enter on a new N-day high, exit on an M-day low."""
    hi = df["High"].to_numpy(dtype="float64")
    lo = df["Low"].to_numpy(dtype="float64")
    up = _rolling_max(hi, _bars(bpy, entry_days * D))
    dn = _rolling_min(lo, _bars(bpy, exit_days * D))
    return _state_machine(np.nan_to_num(close > up, nan=False),
                          np.nan_to_num(close < dn, nan=False))
