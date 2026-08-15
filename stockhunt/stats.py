"""The one definition of each summary statistic.

Every function here previously existed two or three times under slightly different
names in `walk-forward optimization/`. They were *not* identical, and the differences
were invisible because they lived in different files:

| concept | `riskmatch_wf` | `portfolio_wf` | `focus_wf` | `curves` |
|---|---|---|---|---|
| Sharpe | `sharpe`, min 3 obs | `_sharpe`, min **30** obs | — | — |
| max drawdown | `_max_dd`, NaN-poisoning | `_max_dd`, NaN-poisoning | `drawdown`, **filters** | `_drawdown` |
| CAGR | `_cagr` | `_cagr_of` | `cagr` | — |

The CAGR trio were genuinely the same arithmetic (`prod^(bpy/n)` and `prod^(1/years)`
are the same number) and collapse with no argument at all. The other two did not, so
the divergence is preserved here as a **parameter with an explicit default**, and each
caller passes what it used to do. That is the only way to unify them without silently
moving a published number.

Where the two behaviours disagree, the docstring says which one is *right*, so the
choice to change it later is a deliberate, diffable decision rather than a cleanup.

`ddof=1` everywhere. pandas defaults to 1 and numpy to 0, and mixing them is how two
backtests become silently incomparable.
"""

from __future__ import annotations

import numpy as np


def _finite(r: np.ndarray) -> np.ndarray:
    return np.asarray(r, dtype="float64")[np.isfinite(np.asarray(r, dtype="float64"))]


def ann_vol(r: np.ndarray, bpy: float) -> float:
    """Annualised standard deviation of a bar-level return series."""
    return float(np.std(r, ddof=1) * np.sqrt(bpy))


def cagr(r: np.ndarray, bpy: float) -> float:
    """Compound annual growth rate of a bar-level return series.

    `prod(1+r) ** (bpy / n)` — identical to the `** (1 / years)` spelling that
    `focus_wf` used, since `years = n / bpy`. Both were already in the repo and both
    were correct; this is the single survivor.
    """
    r = _finite(r)
    if r.size < 2 or bpy <= 0:
        return float("nan")
    return float(np.prod(1.0 + r) ** (bpy / r.size) - 1.0)


def max_drawdown(r: np.ndarray, dropna: bool = False) -> float:
    """Worst peak-to-trough fraction of the equity curve built from `r`.

    `dropna=False` reproduces `riskmatch_wf._max_dd` and `portfolio_wf._max_dd`, which
    passed the raw array to `cumprod`. **That behaviour is a bug**: one non-finite bar
    propagates through the cumulative product and the function returns NaN for the whole
    series, silently, and a NaN drawdown reads downstream as "not computed" rather than
    "poisoned". `focus_wf.drawdown` filtered first and was right.

    It defaults to the buggy form anyway, because switching it changes published numbers
    on any sheet that carries a non-finite bar, and that deserves its own task with the
    result diff as the deliverable rather than arriving as a side effect of deduplication.
    Pass `dropna=True` for the correct behaviour in new code.
    """
    r = _finite(r) if dropna else np.asarray(r, dtype="float64")
    if r.size < 2:
        return float("nan")
    eq = np.cumprod(1.0 + r)
    return float(np.min(eq / np.maximum.accumulate(eq) - 1.0))


def sharpe(r: np.ndarray, rf: np.ndarray | float, bpy: float,
           min_obs: int = 3) -> float:
    """Annualised Sharpe of `r` in excess of `rf`.

    `min_obs` is the divergence. `riskmatch_wf.sharpe` used 3 and `portfolio_wf._sharpe`
    used 30; each caller keeps its own value so neither sheet moves. 30 is the defensible
    number — a Sharpe estimated on a handful of bars has an error bar wider than any
    value it could report — but riskmatch's per-fold cells legitimately run short, and
    raising its floor would turn scored cells into NaN.

    The zero-variance test is **relative**, and that is not a nicety. An `sd <= 0` guard is
    not enough, for the same reason it was not enough in `metrics.information_ratio`:

    A strategy that sits in cash for a whole fold earns exactly the bill rate, so its
    excess series is a CONSTANT. In exact arithmetic that has sd 0 and an absolute guard
    fires. In float64 it does not — `np.std(np.full(100, 0.001), ddof=1)` is **2.18e-19**,
    a denormal-ish positive number — so the guard passes, `mean / sd` divides by dust, and
    the function returns **7.28e16**. It is value-dependent and therefore invisible in
    testing: the same construction at 0.0001 lands on a true 0.0 and behaves.

    Measured damage before the fix: 55 of 178 us_stocks 1d rows in
    `walk-forward optimization/results/edge_standard.csv` carried |edge_dsharpe| > 10,
    the worst at **-5.70e12** across 29 distinct values, dragging that column's mean to
    **-2.21e11**. `edge_dsharpe` is the S of the six-criterion EDGE_STANDARD. The verdicts
    themselves did not flip — a garbage-negative delta-Sharpe fails either way — but every
    mean, ranking and noise ceiling computed over that column was poisoned, which is
    exactly the incident `information_ratio` already documents (1,600 of 49,950 cells,
    `mean_is_ir` at -2.1e13). The guard was written there and not ported here; this is the
    port, and the threshold matches it exactly.

    **Re-run stage 1g (`riskmatch_wf.py`) to clear the poisoned rows** — this changes what
    is computed, not what is already on disk.
    """
    x = np.asarray(r, dtype="float64") - np.asarray(rf, dtype="float64")
    x = x[np.isfinite(x)]
    if x.size < min_obs:
        return float("nan")
    sd = float(np.std(x, ddof=1))
    mean = float(np.mean(x))
    scale = float(np.max(np.abs(x))) if x.size else 0.0
    if not np.isfinite(sd) or sd <= 0 or sd <= 1e-10 * max(scale, abs(mean)):
        return float("nan")
    return float(mean / sd * np.sqrt(bpy))


def bars(bpy: float, years: float, minimum: int = 2) -> int:
    """A calendar span as a bar count, on measured bars per year.

    Never hardcode 252. A US equity 4h "day" is one 4h bar plus a 2.5h stub, so "a
    50-day moving average" is a different window on every sheet. Mirrors
    `strategies._indicators._bars`, which stays where it is because `strategies/`
    must not depend on anything outside itself.
    """
    n = int(round(bpy * years))
    return max(minimum, n)
