"""Long when the bar closes in the bottom 20% of its own range; exit above 80%."""

from __future__ import annotations

import numpy as np

from strategies._indicators import _state_machine


RULE = 'Long when the bar closes in the bottom 20% of its own range; exit above 80%.'
SOURCE = "QuantifiedStrategies, 'Internal Bar Strength'"
FAMILY = 'reversion'
ANCHOR = False
CLASSES = None
NOTE = 'The only lookback-free rule here, so a 4h IBS is genuinely a different statistic from a daily IBS rather than a rescaled one.'

LOGIC = """
What it measures
    IBS = (close - low) / (high - low): where in its OWN bar the close landed, on a 0-1
    scale. It has no lookback at all — every bar is judged against itself.

The claim
    A close near the low means sellers pushed price to the bottom of the range and
    buyers did not defend it into the bell. The claim is that this is short-term
    over-supply rather than information, and it reverts within days.

What the position is really exposed to
    Short-term reversal, plus short volatility. Buying weakness means you are supplying
    liquidity to forced sellers and being paid for it. Because it exits on strength it
    sits in cash ~54% of the time, so its IR against a fully-invested benchmark is
    penalised for that regardless of skill.

How it fails
    It sells insurance, so it wins small and often and loses big and rarely. Its return
    distribution is negatively skewed and fat-tailed. Because it is daily-rebalanced
    reversal, it is the same trade as the Fama-French ST_Rev factor, which earns ~56%/yr
    GROSS and approximately nothing after real trading costs — so cost sensitivity, not
    signal decay, is the thing to watch.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'buy': 0.2, 'sell': 0.8},
        {'buy': 0.1, 'sell': 0.9},
        {'buy': 0.3, 'sell': 0.7},
        {'buy': 0.2, 'sell': 0.6},
)

def position(df, close, bpy, buy=0.2, sell=0.8):
    """Internal bar strength: where in its own range the bar closed."""
    hi, lo = df["High"].to_numpy("float64"), df["Low"].to_numpy("float64")
    rng = hi - lo
    # A zero-range bar (limit move, or a thin instrument that printed one price) has no
    # defined IBS. Neutral 0.5 keeps it out of both the entry and the exit condition
    # rather than letting a divide-by-zero decide the trade.
    val = np.divide(close - lo, rng, out=np.full(len(close), 0.5), where=rng > 0)
    return _state_machine(val < buy, val > sell)
