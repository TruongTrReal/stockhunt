"""Flat on Mondays, long every other session."""

from __future__ import annotations



RULE = 'Flat on Mondays, long every other session.'
SOURCE = "French (1980), 'Stock Returns and the Weekend Effect', JFE"
FAMILY = 'calendar'
ANCHOR = False
CLASSES = ('us_stocks', 'us_etfs')
NOTE = 'The effect is about the Friday-to-Monday non-trading gap, which a 24/7 market does not have.'

LOGIC = """
What it measures
    Calendar only: flat on Mondays, long otherwise.

The claim
    The weekend effect — historically negative Monday returns, attributed to information
    accumulating over a closed market.

What the position is really exposed to
    Pure calendar exposure, ~80% invested.

How it fails
    Among the best-documented anomalies to have DISAPPEARED after publication. It is
    here as a test of whether this pipeline can detect decay, not as a proposal.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {},
)

def position(df, close, bpy):
    """French (1980): flat on Mondays, long otherwise."""
    return (df.index.dayofweek.to_numpy() != 0).astype("float64")
