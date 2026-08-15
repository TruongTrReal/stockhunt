"""Long November through April, flat May through October."""

from __future__ import annotations



RULE = 'Long November through April, flat May through October.'
SOURCE = "Bouman & Jacobsen (2002), 'The Halloween Indicator', AER"
FAMILY = 'calendar'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    Calendar only: long November to April, flat May to October.

The claim
    The 'Halloween indicator' — the claim that equity returns are seasonally
    concentrated in the winter half of the year.

What the position is really exposed to
    Pure seasonal exposure, in the market roughly half the time.

How it fails
    One observation per year. Fifty years of data is fifty independent events, so its
    t-statistic can never be large no matter how clean the pattern looks.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'start_month': 11, 'end_month': 4},
        {'start_month': 10, 'end_month': 5},
        {'start_month': 11, 'end_month': 5},
)

def position(df, close, bpy, start_month=11, end_month=4):
    """Bouman/Jacobsen: long November through April, flat May through October."""
    m = df.index.month.to_numpy()
    on = (m >= start_month) | (m <= end_month)
    return on.astype("float64")
