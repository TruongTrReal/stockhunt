"""Always long, scaled by inverse prior-month realised variance, capped 1.0."""

from __future__ import annotations


from strategies._indicators import _bars, M, _vol_scale


RULE = 'Always long, scaled by inverse prior-month realised variance, capped 1.0.'
SOURCE = "Moreira & Muir (2017), 'Volatility-Managed Portfolios', JF"
FAMILY = 'volatility'
ANCHOR = True
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    Always long, scaled by the inverse of last month's realised variance, capped at 1.0.

The claim
    Moreira & Muir (2017): volatility is far more forecastable than returns, so scaling
    down when variance is high raises the Sharpe ratio without forecasting direction at
    all.

What the position is really exposed to
    Market beta with a time-varying weight — it never takes a directional view.

How it fails
    The cap at 1.0 matters: uncapped, this is a leveraged strategy and its published
    results assume leverage. Also note the original normalises against a whole-series
    median, which is look-ahead; this uses an expanding one.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'var_months': 1.0},
        {'var_months': 0.5},
        {'var_months': 2.0},
        {'var_months': 3.0},
)

def position(df, close, bpy, var_months=1.0):
    """Moreira/Muir: always long, scaled by inverse prior realised variance, capped 1.0."""
    return _vol_scale(close, _bars(bpy, var_months * M))
