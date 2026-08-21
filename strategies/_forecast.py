"""Adapter and primitives for the `mc_*` price-forecaster batch.

Nothing here is a strategy. These are the shared pieces the `mc_*` files in `published/`
compose, kept in one place for the same reason as `_indicators.py`: a second copy of
`expose` is exactly how half the batch ends up answering a different question from the
other half.

Why this module exists at all
-----------------------------
The rules in this batch were published as **price forecasters** — each one emits a guess
at the next price, not a position. Everything else in `published/` emits exposure
directly, so the batch needs one documented, single definition of how a forecast becomes
a trade. That definition is `expose`, and it is deliberately the dullest one available:

    long when the forecast is above the current close, flat otherwise

Long/flat rather than long/short, matching how the rest of this catalog is scored and
following the conversion batch that found the short leg to be the dominant loss term.
A forecaster that is right about direction and wrong about magnitude scores the same as
one that is right about both, which is the point: this measures the sign, because the
sign is all a one-exposure-per-bar series can carry.

The timing convention
---------------------
A forecast for bar t+1 is computed from bars <= t and compared against close[t], so the
position for bar t is decided on bar t's close and settles at it. That is the repo's
`close` fill convention — the optimistic bound, carrying the look-ahead described in the
root `CLAUDE.md`, and identical to what every other rule here is charged. It is not the
same convention the source site used; the site's is undocumented.

Windows
-------
Every lookback is a calendar span converted through `_bars(bpy, days * D)`, never a raw
bar count, so `days=60` means sixty trading days on the 1d sheet and sixty trading days'
worth of 4h bars on the 4h sheet. The sources specify no parameters at all, so the grids
in this batch are one cell wide and this default is the only choice being made.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from strategies._indicators import D, _bars

__all__ = ["D", "expose", "win", "roll", "rmean", "rstd", "rmin", "rmax", "rmedian",
           "rquantile", "rsum", "ema", "atr_c", "rets", "lin", "safe_div", "shift",
           "pick", "iir2", "poly_extrap", "entropy", "nan_mask", "ema_var", "decimate",
           "autocorr", "ewls", "rowmin", "rowmax", "rowmean", "rowquantile"]


def nan_mask(m: np.ndarray) -> np.ndarray:
    """True on rows of a `roll` matrix that are not fully warmed up.

    Reductions like `nansum` return 0.0 for an all-NaN row, which is a number and would
    be traded on. Every window statistic here is masked with this instead, so a warmup
    bar stays NaN and `expose` reads it as flat.
    """
    return ~np.isfinite(m).all(axis=1)


def win(bpy: float, days: float, minimum: int = 5) -> int:
    """Calendar span -> bar count on this sheet. `bpy` is measured, never assumed."""
    return _bars(bpy, days * D, minimum)


def decimate(pos: np.ndarray, step: int) -> np.ndarray:
    """Only let a position change every `step` bars; hold it in between.

    THE grid is anchored at bar 0, and that anchor is the whole of what makes this
    causal. Anchoring on the last bar -- the obvious way to write it, since that is
    where "today" is -- would make every past rebalance date depend on when the backtest
    happened to be run, and `test_causality.py` fails that by truncation.

    One implementation, two callers: `expose` uses it for the rebalance schedule a rule
    carries in its own grid, and `overlays/hold.py` uses it to wrap any other label.
    """
    step = int(step)
    if step <= 1:
        return pos
    p = np.asarray(pos, dtype="float64")
    keep = (np.arange(len(p)) % step) == 0
    return pd.Series(np.where(keep, p, np.nan)).ffill().fillna(0.0).to_numpy()


def expose(pred, close: np.ndarray, rebal_bars: int = 0) -> np.ndarray:
    """Forecast -> exposure. Long above the current close, flat otherwise.

    A non-finite forecast is flat rather than long: a window that has not warmed up, a
    fit that failed to converge and a genuine "no view" are the same thing here, and
    the alternative -- treating NaN as a signal -- is how a warmup artefact becomes a
    trade on the first bar of every sheet.

    `rebal_bars` is the source's rebalance schedule: the signal still sees every bar,
    only the act of trading is decimated. It matters more than it sounds, because these
    rules are killed by cost far more often than by signal -- the same forecast traded
    daily and monthly differ by roughly 4x in turnover.
    """
    p = np.asarray(pred, dtype="float64")
    c = np.asarray(close, dtype="float64")
    ok = np.isfinite(p) & np.isfinite(c) & (c > 0)
    return decimate(np.where(ok & (p > c), 1.0, 0.0), rebal_bars)


def roll(x: np.ndarray, w: int) -> np.ndarray:
    """`(n, w)` matrix whose row t is `x[t-w+1 : t+1]` — the window ENDING at t.

    The first `w-1` rows are NaN because those windows do not exist yet. Row t contains
    bar t and nothing after it, which is what makes everything built on this causal by
    construction — though `test_causality.py` still proves it by truncation rather than
    taking the construction on faith.
    """
    x = np.asarray(x, dtype="float64")
    n = len(x)
    w = max(1, int(w))
    if n < w:
        return np.full((n, w), np.nan)
    return np.vstack([np.full((w - 1, w), np.nan), sliding_window_view(x, w)])


def _s(x: np.ndarray) -> pd.Series:
    return pd.Series(np.asarray(x, dtype="float64"))


def rmean(x, w):
    return _s(x).rolling(int(w)).mean().to_numpy()


def rstd(x, w):
    return _s(x).rolling(int(w)).std(ddof=1).to_numpy()


def rmin(x, w):
    return _s(x).rolling(int(w)).min().to_numpy()


def rmax(x, w):
    return _s(x).rolling(int(w)).max().to_numpy()


def rmedian(x, w):
    return _s(x).rolling(int(w)).median().to_numpy()


def rquantile(x, w, q):
    return _s(x).rolling(int(w)).quantile(q).to_numpy()


def rsum(x, w):
    return _s(x).rolling(int(w)).sum().to_numpy()


def ema(x, span=None, alpha=None):
    """EWMA over the whole series. Recursive and forward-only, so causal by shape."""
    kw = {"alpha": alpha} if alpha is not None else {"span": span}
    return _s(x).ewm(adjust=False, **kw).mean().to_numpy()


def rets(close: np.ndarray) -> np.ndarray:
    """Simple returns, first bar 0.0."""
    c = np.asarray(close, dtype="float64")
    out = np.zeros(len(c))
    with np.errstate(divide="ignore", invalid="ignore"):
        out[1:] = np.where(c[:-1] > 0, c[1:] / c[:-1] - 1.0, 0.0)
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)


def atr_c(close: np.ndarray, w: int) -> np.ndarray:
    """The batch's own ATR proxy: mean absolute close-to-close change over `w`.

    Several of these rules say "ATR approximated from closes" in as many words, and the
    ones that just say "ATR" are running on a close-only series in the source. Using
    TA-Lib's true ATR here instead would be a better indicator and a worse replication.
    """
    d = np.abs(np.diff(np.asarray(close, dtype="float64"), prepend=np.nan))
    return rmean(d, w)


def safe_div(a, b, fill=0.0):
    a = np.asarray(a, dtype="float64")
    b = np.asarray(b, dtype="float64")
    out = np.full(np.broadcast(a, b).shape, float(fill))
    ok = np.isfinite(a) & np.isfinite(b) & (b != 0)
    np.divide(a, b, out=out, where=ok)
    return out


def shift(x, k: int):
    """`x` delayed by `k` bars, front-filled with NaN. Never a negative `k`."""
    return _s(x).shift(int(k)).to_numpy()


def lin(x: np.ndarray, w: int):
    """Rolling OLS of `x` on bar index within the window ending at t.

    Returns `(slope, fitted_now)` — the per-bar slope and the fitted value at the
    window's LAST bar. `t` runs 0..w-1 inside each window, so `fitted_now` is the trend
    line evaluated at the current bar and `fitted_now + slope` is the one-step
    extrapolation almost every trend rule in this batch asks for.

    Closed form rather than a fit per window: with the regressor fixed at 0..w-1 the
    denominator is a constant and only the two cross-sums move.
    """
    w = max(2, int(w))
    t = np.arange(w, dtype="float64")
    tbar = (w - 1) / 2.0
    sxx = float(((t - tbar) ** 2).sum())
    m = roll(x, w)
    bad = nan_mask(m)
    ybar = np.where(bad, np.nan, np.nansum(m, axis=1) / w)
    sxy = np.nansum(m * (t - tbar), axis=1)
    slope = np.where(bad, np.nan, sxy / sxx if sxx > 0 else 0.0)
    return slope, ybar + slope * (w - 1 - tbar)


def pick(arrays, idx: np.ndarray) -> np.ndarray:
    """Per-bar selection among precomputed series: `out[t] = arrays[idx[t]][t]`.

    Several rules in this batch specify a lookback that itself varies with volatility.
    A genuinely per-bar window length cannot be vectorised, so the window is quantised
    to a handful of candidates and chosen per bar — a deviation from the source, and one
    that is recorded in each affected file's NOTE rather than only here.
    """
    out = np.full(len(idx), np.nan)
    for i, a in enumerate(arrays):
        sel = idx == i
        out[sel] = np.asarray(a, dtype="float64")[sel]
    return out


def iir2(x: np.ndarray, c1: float, c2: float, c3: float) -> np.ndarray:
    """`y[t] = c1*(x[t]+x[t-1])/2 + c2*y[t-1] + c3*y[t-2]` — Ehlers' 2-pole form.

    A Python loop, because scipy is not a dependency of this repo and a linear
    recursion has no numpy expression. Seeded with the raw series so the filter starts
    at the data rather than at zero, which otherwise takes ~3 periods to decay out and
    shows up as a spurious signal on the first bars of every sheet.
    """
    x = np.asarray(x, dtype="float64")
    y = np.array(x, dtype="float64", copy=True)
    for t in range(2, len(x)):
        y[t] = c1 * (x[t] + x[t - 1]) / 2.0 + c2 * y[t - 1] + c3 * y[t - 2]
    return y


def poly_extrap(x: np.ndarray, w: int, degree: int, ahead: float = 1.0) -> np.ndarray:
    """Least-squares polynomial of `degree` over the window ending at t, evaluated ahead.

    A Chebyshev fit on a uniform grid and an ordinary polynomial fit of the same degree
    span the same space and therefore give the same least-squares surface, so the
    pseudo-inverse of one fixed design matrix does every window at once. Fitting each
    window separately would be ~14,000 calls per symbol per cell.
    """
    w = max(degree + 2, int(w))
    t = np.arange(w, dtype="float64") / max(1.0, w - 1.0)
    vander = np.vander(t, degree + 1, increasing=True)          # (w, degree+1)
    proj = np.linalg.pinv(vander)                               # (degree+1, w)
    m = roll(x, w)
    coef = np.nan_to_num(m, nan=0.0) @ proj.T                   # (n, degree+1)
    coef[~np.isfinite(m).all(axis=1)] = np.nan
    step = 1.0 / max(1.0, w - 1.0)
    at = np.array([(1.0 + ahead * step) ** k for k in range(degree + 1)])
    return coef @ at


def entropy(x: np.ndarray, w: int, bins: int = 10) -> np.ndarray:
    """Shannon entropy (nats) of the window ending at t, on a `bins`-wide equal grid.

    The grid is rescaled per window rather than fixed, so this measures the SHAPE of the
    distribution and not its width — two windows differing only by a volatility factor
    score the same, which is what "low entropy means a trend" is meant to capture.
    """
    m = roll(x, w)
    bad = nan_mask(m)
    lo = np.where(bad[:, None], np.nan, np.nanmin(np.where(np.isfinite(m), m, np.inf),
                                                 axis=1, keepdims=True))
    hi = np.where(bad[:, None], np.nan, np.nanmax(np.where(np.isfinite(m), m, -np.inf),
                                                  axis=1, keepdims=True))
    span = np.where(hi > lo, hi - lo, np.nan)
    codes = np.clip(((m - lo) / span * bins).astype("float64"), 0, bins - 1e-9)
    codes = np.floor(codes)
    n = np.isfinite(m).sum(axis=1).astype("float64")
    out = np.zeros(len(m))
    for k in range(bins):
        p = (codes == k).sum(axis=1) / np.where(n > 0, n, np.nan)
        out -= np.where(p > 0, p * np.log(np.where(p > 0, p, 1.0)), 0.0)
    return np.where(bad, np.nan, out)


def ema_var(x: np.ndarray, alpha: np.ndarray, seed: float | None = None) -> np.ndarray:
    """EWMA whose smoothing factor changes every bar: `y[t] = a[t]*x[t] + (1-a[t])*y[t-1]`.

    FRAMA and the other adaptive filters in this batch derive their alpha from the data,
    so `pandas.ewm` cannot express them. A loop, for the same reason as `iir2`.
    """
    x = np.asarray(x, dtype="float64")
    a = np.clip(np.nan_to_num(np.asarray(alpha, dtype="float64"), nan=1.0), 0.0, 1.0)
    y = np.empty(len(x))
    prev = float(x[0]) if seed is None else float(seed)
    for t in range(len(x)):
        v = x[t]
        prev = a[t] * v + (1.0 - a[t]) * prev if np.isfinite(v) else prev
        y[t] = prev
    return y


def autocorr(x: np.ndarray, w: int, kmax: int) -> np.ndarray:
    """`(n, kmax+1)` sample autocorrelations of the window ending at t, lag 0..kmax.

    Centred on the window's own mean and divided by the window's own lag-0 sum, which is
    the biased (Yule-Walker) estimator — the one whose Toeplitz system is guaranteed
    positive definite, and therefore the one an AR fit needs.
    """
    w = max(kmax + 2, int(w))
    m = roll(x, w)
    bad = nan_mask(m)
    mu = np.where(bad, np.nan, np.nansum(m, axis=1) / w)
    c = m - mu[:, None]
    c = np.nan_to_num(c, nan=0.0)
    denom = (c * c).sum(axis=1)
    out = np.empty((len(m), kmax + 1))
    for k in range(kmax + 1):
        num = (c[:, k:] * c[:, : w - k]).sum(axis=1) if k else denom
        out[:, k] = np.where(bad, np.nan, safe_div(num, denom, fill=np.nan))
    return out


def ewls(y: np.ndarray, lam: float):
    """Recursive least squares with forgetting factor `lam`, in closed form.

    Returns `(slope, value_now)` of an exponentially weighted regression of `y` on the
    bar index. RLS updates the same normal equations one bar at a time; because the
    regressor is just the index, the five weighted sums it maintains are each an EWMA,
    so the whole recursion collapses to four `ewm` calls. Identical answer, no loop.
    """
    y = np.asarray(y, dtype="float64")
    t = np.arange(len(y), dtype="float64")
    a = 1.0 - float(np.clip(lam, 1e-6, 1.0 - 1e-9))

    def ew(v):
        return pd.Series(v).ewm(alpha=a, adjust=True).mean().to_numpy()

    mu_t, mu_y = ew(t), ew(y)
    mu_tt, mu_ty = ew(t * t), ew(t * y)
    slope = safe_div(mu_ty - mu_t * mu_y, mu_tt - mu_t * mu_t, fill=np.nan)
    return slope, (mu_y - slope * mu_t) + slope * t


def rowmin(m: np.ndarray) -> np.ndarray:
    """Row-wise minimum of a `roll` matrix, NaN on rows that are not warmed up.

    `np.nanmin` on an all-NaN row is both a RuntimeWarning and a NaN, and every stage in
    this repo already emits enough numpy warnings that one more buries a real signal.
    Substituting +/-inf and masking afterwards gives the same answer silently.
    """
    bad = nan_mask(m)
    return np.where(bad, np.nan, np.where(np.isfinite(m), m, np.inf).min(axis=1))


def rowmax(m: np.ndarray) -> np.ndarray:
    bad = nan_mask(m)
    return np.where(bad, np.nan, np.where(np.isfinite(m), m, -np.inf).max(axis=1))


def rowmean(m: np.ndarray) -> np.ndarray:
    bad = nan_mask(m)
    return np.where(bad, np.nan, np.nansum(m, axis=1) / m.shape[1])


def rowquantile(m: np.ndarray, q: float) -> np.ndarray:
    """Row-wise quantile ignoring NaN, silent on rows with nothing finite in them."""
    ok = np.isfinite(m)
    any_ok = ok.any(axis=1)
    filled = np.where(ok, m, np.nan)
    out = np.full(len(m), np.nan)
    if any_ok.any():
        out[any_ok] = np.nanquantile(filled[any_ok], q, axis=1)
    return out
