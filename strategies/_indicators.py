"""Shared building blocks for the published strategies.

Nothing here is a strategy. These are the primitives the files in `published/`
compose, kept in one place because a second copy of `_causal_median` is exactly how
a look-ahead leak gets fixed in one strategy and left in another.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import talib


def _bars(bpy: float, years: float, minimum: int = 2) -> int:
    """Calendar span -> bar count on this sheet. `bpy` is measured, never assumed."""
    return max(minimum, int(round(bpy * years)))


D = 1.0 / 252.0


M = 1.0 / 12.0


def _state_machine(entry: np.ndarray, exit_: np.ndarray) -> np.ndarray:
    """Long on `entry`, flat on `exit_`, hold otherwise. Starts flat.

    Vectorised through a forward-fill rather than a Python loop: entries write 1,
    exits write 0, everything else is NaN and inherits the last decision. Entry wins a
    same-bar tie, which is the convention every published version of these rules uses
    (you do not enter and exit on the same close).
    """
    s = np.full(len(entry), np.nan)
    s[exit_] = 0.0
    s[entry] = 1.0
    out = pd.Series(s).ffill().fillna(0.0).to_numpy()
    return out


def _pct_rank(x: np.ndarray, window: int) -> np.ndarray:
    """Rolling percentile rank of the last value within its trailing window, 0-100.

    `Rolling.rank` ranks the newest value against the window, which is exactly
    ConnorsRSI's PercentRank, and runs at C speed — a `rolling.apply` lambda here costs
    ~1M Python calls per sheet. It differs from the textbook definition only in that the
    current value is one of the ranked values rather than excluded, which on a 100-bar
    window shifts the result by at most one percentile.
    """
    return pd.Series(x).rolling(window).rank(pct=True).to_numpy() * 100.0


def _streak(close: np.ndarray) -> np.ndarray:
    """Signed run length of consecutive up or down closes — ConnorsRSI's middle term."""
    out = np.zeros(len(close))
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            out[i] = out[i - 1] + 1 if out[i - 1] > 0 else 1
        elif close[i] < close[i - 1]:
            out[i] = out[i - 1] - 1 if out[i - 1] < 0 else -1
    return out


def _causal_median(x: np.ndarray, min_periods: int) -> np.ndarray:
    """Expanding median — the level a trader could have computed by bar t.

    This exists because the obvious version does not work. `prereg.volmanaged`,
    `variants._vol_scale` and Pruitt's published Dynamic Break Out all normalise current
    volatility against `np.nanmedian(whole_series)`, which is a single scalar computed
    from data that had not happened yet. It is a mild leak — one number, not a per-bar
    signal — but it is a leak, and the truncation test in `scratchpad/verify.py` catches
    it: 11 of 93 cells changed their *past* positions when the series was shortened.

    A rule whose value at bar t depends on which bars came after t is not a strategy,
    however small the dependence. The earlier stages in this repo carry the same
    construction and their volatility-scaled rows are optimistic by whatever it is worth.
    """
    return pd.Series(x).expanding(min_periods=min_periods).median().to_numpy()


def _vol_scale(close: np.ndarray, window: int) -> np.ndarray:
    """Inverse realised-vol scaling toward the asset's own median vol, capped at 1.0.

    Capped on purpose. Levering to hit a target is a different strategy with a different
    risk profile, and this project's gates are stated for an unlevered book — an
    uncapped version would buy Sharpe with borrowed money and report it as signal.
    """
    ret = np.empty_like(close)
    ret[0] = 0.0
    ret[1:] = close[1:] / close[:-1] - 1.0
    sd = pd.Series(ret).rolling(window).std(ddof=1).to_numpy()
    target = _causal_median(sd, window)
    ok = np.isfinite(sd) & (sd > 0) & np.isfinite(target) & (target > 0)
    scale = np.divide(target, sd, out=np.ones_like(sd), where=ok)
    return np.clip(np.nan_to_num(scale, nan=1.0), 0.0, 1.0)


def _rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    """Highest value over the `n` bars ENDING AT t-1 — never including bar t itself.

    The exclusion is the whole point of a breakout rule. Comparing close[t] against a
    window that already contains close[t] makes "a new 20-day high" nearly unreachable
    and quietly turns a trend system into a different, much rarer one.
    """
    return pd.Series(x).rolling(n).max().shift(1).to_numpy()


def _rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n).min().shift(1).to_numpy()


TREND_MAS = (3.0, 5.0, 10.0, 20.0, 50.0, 200.0)


def _supertrend_trend(df, close: np.ndarray, bpy: float,
                      atr_days: float, mult: float) -> np.ndarray:
    """+1 in an uptrend, -1 in a downtrend, 0 before the first valid ATR.

    Split out from the strategies below because four of them need the same line and a
    combo that recomputed it slightly differently would be comparing itself to a
    near-copy of itself rather than to the base rule.
    """
    n = _bars(bpy, atr_days * D)
    high = df["High"].to_numpy("float64")
    low = df["Low"].to_numpy("float64")
    atr = talib.ATR(high, low, close, timeperiod=n)
    mid = 0.5 * (high + low)
    upper = mid + mult * atr
    lower = mid - mult * atr

    out = np.zeros(len(close))
    fu = fl = np.nan
    trend = 1.0
    for i in range(1, len(close)):
        if not np.isfinite(atr[i]):
            continue
        if not np.isfinite(fu):
            # Seed on the first bar with a defined ATR. Direction is taken from where
            # price already sits rather than assumed long, so the warmup does not hand
            # the rule an arbitrary opening position it never chose.
            fu, fl = upper[i], lower[i]
            trend = 1.0 if close[i] >= fu else -1.0
            out[i] = trend
            continue
        # The ratchet: a band holds its previous level unless price has closed through it.
        fu = min(upper[i], fu) if close[i - 1] <= fu else upper[i]
        fl = max(lower[i], fl) if close[i - 1] >= fl else lower[i]
        if close[i] > fu:
            trend = 1.0
        elif close[i] < fl:
            trend = -1.0
        out[i] = trend
    return out


def _day_ordinals(index: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """(position of each bar's session within its month, sessions in that month).

    Computed on unique *dates*, then broadcast back to bars, so an intraday sheet gets
    the same answer as a daily one: every bar of the third trading day is the third
    trading day.
    """
    dates = index.normalize()
    uniq = pd.DatetimeIndex(pd.unique(dates))
    frame = pd.DataFrame({"d": uniq}, index=uniq)
    grp = frame.groupby([uniq.year, uniq.month])["d"]
    pos = grp.rank(method="first").astype(int)
    size = grp.transform("size").astype(int)
    return pos.reindex(dates).to_numpy(), size.reindex(dates).to_numpy()
