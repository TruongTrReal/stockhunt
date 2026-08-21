"""The range filter, but a leg is only entered while the MACD histogram agrees."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D
from strategies.published.range_filter import raw_signals


RULE = 'The range filter, but a leg is only entered while the MACD histogram agrees.'
SOURCE = "DonovanWall, 'Range Filter [DW]' + MACD gate; packaged as 'RF-100 Inverse' (Pine v5)"
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ('Shares `range_filter`\'s implementation of the filter itself, deliberately — the '
        'pair only prices the MACD gate if the filter underneath them is identical. The '
        'gate is asymmetric on purpose: a blocked entry still CLOSES the opposite leg, '
        'so this rule has a flat state that `range_filter` does not.')

LOGIC = """
What it measures
    `range_filter`'s ratcheted trend line, plus a slow MACD histogram
    (EMA 25 - EMA 100, signalled by EMA 50) used only as a yes/no gate on entry.

The claim
    The filter says which way the trend has turned; the histogram says whether the
    longer trend agrees. Taking only the agreeing turns is meant to cut the whipsaws
    that the filter alone eats.

What the position is really exposed to
    The same trend exposure as `range_filter`, minus the legs the histogram vetoed.
    Because a vetoed entry still closes the opposite position, this rule sits in cash
    during every disagreement — so part of any difference between the two is simply
    lower exposure, not better timing. Read it against the exposure controls, not
    against `range_filter` alone.

How it fails
    A 25/100/50 MACD on a rule whose whole point is speed is a very slow second opinion,
    and it will be pointing the wrong way at exactly the turns worth catching. The
    likeliest outcome is that it removes good and bad legs in the same proportion and
    the only durable effect is the reduced time in market.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'per_days': 20.0, 'mult': 3.5, 'fast_days': 25.0, 'slow_days': 100.0, 'signal_days': 50.0, 'allow_short': 1},
        {'per_days': 20.0, 'mult': 3.5, 'fast_days': 12.0, 'slow_days': 26.0, 'signal_days': 9.0, 'allow_short': 1},
        {'per_days': 10.0, 'mult': 3.5, 'fast_days': 25.0, 'slow_days': 100.0, 'signal_days': 50.0, 'allow_short': 1},
        {'per_days': 20.0, 'mult': 3.5, 'fast_days': 25.0, 'slow_days': 100.0, 'signal_days': 50.0, 'allow_short': 0},
)


def position(df, close, bpy, per_days=20.0, mult=3.5, fast_days=25.0, slow_days=100.0,
             signal_days=50.0, allow_short=1):
    """Enter only with the histogram; exit the opposite leg regardless of it.

    `allow_short=0` gives the long/flat version. On this project's metric that
    switch is the single largest term in the result, not a cosmetic variant: a
    reversing rule is charged the benchmark's drift twice over every downtrend,
    and the two versions are different strategies. Same convention as
    `supertrend` / `supertrend_lf`.
    """
    n = _bars(bpy, per_days * D)
    long_sig, short_sig = raw_signals(close, n, mult)

    src = np.ascontiguousarray(close)
    macd = (talib.EMA(src, _bars(bpy, fast_days * D))
            - talib.EMA(src, _bars(bpy, slow_days * D)))
    signal = talib.EMA(np.ascontiguousarray(macd), _bars(bpy, signal_days * D))
    hist = macd - signal

    # The four Pine statements, in the order the script submits them: entry long,
    # entry short, close-long-on-short-signal, close-short-on-long-signal. A long and a
    # short condition cannot both fire on one bar (one needs src above the filter, the
    # other below), so the branches below are exclusive.
    out = np.zeros(len(close))
    pos = 0.0
    for i in range(len(close)):
        if long_sig[i]:
            if hist[i] > 0:
                pos = 1.0
            elif pos < 0:
                pos = 0.0
        elif short_sig[i]:
            if hist[i] < 0:
                pos = -1.0
            elif pos > 0:
                pos = 0.0
        out[i] = pos
    return out if allow_short else np.maximum(out, 0.0)
