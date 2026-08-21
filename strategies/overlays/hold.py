"""Rebalance conditioning: a base rule's position is only allowed to change on a grid."""

from __future__ import annotations

from strategies._forecast import decimate
from strategies._indicators import D, _bars


HOLD_PREFIX = "hold"


HOLD_SEP = ":"


def apply(label, df, close, bpy, symbol, build):
    """Decode `hold:<days>:<base label>` and hold the base position between rebalances.

    Every rule in this repo re-decides its exposure on every bar, which is the right
    default when the question is "does the signal work". It is the wrong default when
    the source being replicated rebalanced weekly or monthly, because the two differ by
    an order of magnitude in turnover and this project's rules die on cost far more often
    than they die on signal. `hold:30:X` is X sampled every 30 calendar days and held flat
    in between — the same signal, a tenth of the trading.

    **The grid is anchored at bar 0, not at the end of the series, and that is the whole
    of what makes it causal.** Anchoring on the last bar (the obvious way to write it,
    since that is where "today" is) would make every past rebalance date depend on when
    you happened to run it, and `test_causality.py` would fail it by truncation — which
    is exactly the check that anchoring choice needs.

    It is not the same thing as sampling the PRICE monthly. The signal still sees every
    bar and is computed on daily data; only the act of trading is decimated. A rule that
    only looked at monthly bars is a different rule and would need a different overlay.

    `build` arrives as an argument rather than as an import, for the same circularity
    reason as the other overlays.
    """
    prefix = HOLD_PREFIX + HOLD_SEP
    if not label.startswith(prefix):
        return None
    tail = label[len(prefix):]
    spec, _, base_label = tail.partition(HOLD_SEP)
    if not base_label:
        return None
    try:
        days = float(spec)
    except ValueError:
        return None
    if days <= 0:
        return None

    pos = build(base_label, df, close, bpy, symbol)
    if pos is None:
        return None

    return decimate(pos, _bars(bpy, days * D, minimum=1))
