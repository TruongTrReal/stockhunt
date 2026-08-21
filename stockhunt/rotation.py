"""The rotation signal itself: rank a basket on trailing return, hold the best one.

**One definition, two callers.** `walk-forward optimization/rotation.py` scores this
offline over 25 years; `paper trading engine/rotation_manager.py` computes the same thing
on live bars and sends the order. If those two ever disagree the forward test is measuring
something the backtest never scored, and nobody would see it — the live book would simply
drift away from its own research and every explanation would be plausible. So the
arithmetic lives here, and both import it.

That is the same reason `stats.py` holds one definition of Sharpe: not tidiness, but
that a second copy is a defect waiting for a date.

Numpy and pandas only, and it imports from no pipeline folder, per the rules for this
package.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["scores", "pick", "LOOKBACK"]

# 63 trading days ~ three months. Published in a 2013 Seeking Alpha article and used here
# unchanged: a parameter chosen before the data is worth more than one chosen after, and
# this repo's measurement of it (a smooth hump peaking at 63-66, decaying either side) is
# a description of that choice, not a re-optimisation of it. Do not tune this in the live
# path -- if it moves, it moves in the research first and then here.
LOOKBACK = 63


def scores(closes: pd.DataFrame, lookback: int = LOOKBACK,
           f_vol: float = 0.0) -> np.ndarray:
    """`(T, N)` matrix of each name's trailing total return, NaN where it is not eligible.

    A name scores only when all `lookback + 1` of the most recent rows carry a price.
    That is what lets a basket GROW over time instead of erroring on a fund that had not
    listed yet: the name is simply not a candidate until it has the history, and the
    rotation picks among whoever is ready.

    **Row t reads rows t and t-lookback and nothing later.** The caller is responsible for
    the other half of causality — that the row it acts on was built only from prices
    already printed. Offline that is a shifted weight matrix; live it is
    `stockhunt.sessions.fold_sessions`.
    """
    p = np.asarray(closes, dtype="float64")
    if p.ndim != 2:
        raise ValueError("scores() wants a (T, N) frame of closes")
    t, n = p.shape
    w = int(lookback) + 1
    if t < w:
        return np.full((t, n), np.nan)

    ok = np.isfinite(p)
    # A window is usable only if every one of its w rows is present. Cumulative sums of
    # the finite mask answer that in one pass instead of t*n slice tests.
    c = np.vstack([np.zeros(n), np.cumsum(ok, axis=0)])
    full = np.zeros((t, n), dtype=bool)
    full[w - 1:] = (c[w:] - c[:t - w + 1]) == w

    start = np.full((t, n), np.nan)
    start[w - 1:] = p[:t - w + 1]
    ret = np.where(full & (start > 1e-9), p / start - 1.0, np.nan)

    if f_vol > 0:
        d = np.full((t, n), np.nan)
        d[1:] = p[1:] / p[:-1] - 1.0
        vol = (pd.DataFrame(d).rolling(lookback, min_periods=lookback)
               .std(ddof=1).to_numpy())
        ret = np.where(np.isfinite(vol) & (vol > 1e-9), ret / vol ** f_vol, np.nan)
    return ret


def pick(row: np.ndarray, names: list[str] | None = None):
    """The winner of one score row, or `None` when nothing is eligible yet.

    Returns the column index, or the name if `names` is given. `None` is a real answer and
    the caller must handle it: early in a basket's life no member has enough history, and
    the honest response is to hold whatever is already held rather than to guess.
    """
    r = np.asarray(row, dtype="float64")
    valid = np.flatnonzero(np.isfinite(r))
    if valid.size == 0:
        return None
    best = int(valid[np.argmax(r[valid])])
    return names[best] if names is not None else best
