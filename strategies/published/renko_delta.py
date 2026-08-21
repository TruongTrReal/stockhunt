"""Long after N consecutive up-bricks of a percentage renko series; flat after N down."""

from __future__ import annotations

import numpy as np

from strategies._indicators import _state_machine


RULE = ('Long after N consecutive up-bricks of a percentage renko series; flat after N '
        'consecutive down-bricks.')
SOURCE = "'renkoTrading' / 'renko_v4' (Jupyter notebooks, BTCUSDT 1h-6h, Binance)"
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ('Converted from two notebooks that implement the same algorithm — the second is a '
        'refactor of the first with a `min_steps` x smoothing grid search bolted on — so '
        'they are ONE strategy here, not two. The notebooks price fills at the next '
        "bar's open; this repo owns fill timing at book level via `--fill`, so the "
        'position is emitted on the signal bar and the choice is left where it belongs. '
        'Their volatility filter is inert on the published settings (the threshold is '
        '1e8) and is not implemented.')

LOGIC = """
What it measures
    A renko brick series, but built on percentage moves rather than a fixed price step.
    Running the mid-price (high+low)/2, it accumulates the change since the last brick
    and prints an up-brick when that accumulation exceeds `delta` of the previous price,
    a down-brick when it falls below `-delta`, and resets the accumulator either way.
    Bars that print no brick are invisible to the rule — time is discarded and only the
    sequence of bricks is left.

The claim
    Throwing away time removes the noise that a fixed-interval chart forces you to look
    at. What survives is a clean alternation of legs, and N bricks in the same direction
    is a trend.

What the position is really exposed to
    Trend, long/flat, with a threshold that scales with price — which is the one genuine
    advantage of the percentage form over classic renko: a 5% brick means the same thing
    at $100 and at $60,000, and a $500 brick does not.

How it fails
    Discarding time does not remove noise, it removes the RECORD of noise. A 5% brick on
    a daily equity sheet may take a month to print, and the rule holds its last side for
    the whole of it. The published `min_steps = 1` also means "one brick" — there is no
    confirmation at all, so the rule flips on the first brick of every reversal and the
    filtering the construction appears to buy is not actually being used. The notebooks
    were run on BTC over a period BTC rose, on a single symbol, with no benchmark other
    than holding, and reported the good case.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'delta': 0.05, 'min_steps': 1},
        {'delta': 0.05, 'min_steps': 2},
        {'delta': 0.02, 'min_steps': 1},
        {'delta': 0.10, 'min_steps': 1},
)


def position(df, close, bpy, delta=0.05, min_steps=1):
    """Walk the mid-price, print bricks, and count runs of them.

    The accumulator and its normaliser are kept exactly as the notebooks wrote them:
    `increment` is the total move since the last brick, and it is divided by the
    PREVIOUS bar's mid-price rather than by the price at the last brick. That is a
    slightly odd denominator and it is theirs, not a transcription slip — changing it
    moves the brick boundaries and would make this a different rule.
    """
    high = df["High"].to_numpy("float64")
    low = df["Low"].to_numpy("float64")
    mid = (high + low) / 2.0
    n = len(close)
    steps = max(1, int(min_steps))

    up_brick = np.zeros(n, dtype=bool)
    down_brick = np.zeros(n, dtype=bool)
    prev = mid[0]
    increment = 0.0
    for i in range(n):
        increment += mid[i] - prev
        pct = increment / prev if prev != 0 else 0.0
        prev = mid[i]
        if pct > delta:
            up_brick[i] = True
            increment = 0.0
        elif pct < -delta:
            down_brick[i] = True
            increment = 0.0

    # The run counters only advance on brick bars — the notebooks iterate over the brick
    # sequence, not over time — so a long gap between bricks changes nothing.
    enter = np.zeros(n, dtype=bool)
    exit_ = np.zeros(n, dtype=bool)
    up = down = 0
    for i in range(n):
        if up_brick[i]:
            up, down = up + 1, 0
        elif down_brick[i]:
            down, up = down + 1, 0
        else:
            continue
        if up == steps:
            enter[i] = True
        elif down == steps:
            exit_[i] = True
    return _state_machine(enter, exit_)
