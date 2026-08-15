"""12-month time-series momentum sized by inverse realised volatility."""

from __future__ import annotations


from strategies._indicators import _bars, D, _vol_scale

# Built ON another published strategy. In the old flat module this was a bare
# name; split across files it must be an import, and omitting it fails SILENTLY:
# `registry.build` catches the NameError and returns None, so the cell vanishes
# from the sheet instead of erroring. Caught by hashing positions before and after.
from strategies.published.tsmom12 import position as tsmom


RULE = '12-month time-series momentum sized by inverse realised volatility.'
SOURCE = "Baltas & Kosowski (2013), 'Momentum Strategies in Futures Markets'"
FAMILY = 'volatility'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    12-month momentum, sized by inverse realised volatility.

The claim
    Baltas & Kosowski: combine the direction of time-series momentum with the sizing of
    volatility targeting, so each position contributes similar risk.

What the position is really exposed to
    Trend for direction, volatility scaling for size.

How it fails
    Two effects in one row. If it beats plain `tsmom12`, the vol-targeting did the work,
    not the momentum — compare them directly rather than reading this in isolation.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'lookback_months': 12.0, 'vol_days': 20.0},
        {'lookback_months': 12.0, 'vol_days': 60.0},
        {'lookback_months': 6.0, 'vol_days': 20.0},
        {'lookback_months': 3.0, 'vol_days': 10.0},
)

def position(df, close, bpy, lookback_months=12.0, vol_days=20.0):
    """Baltas/Kosowski: time-series momentum sized by inverse realised volatility."""
    return tsmom(df, close, bpy, lookback_months) * _vol_scale(
        close, _bars(bpy, vol_days * D))
