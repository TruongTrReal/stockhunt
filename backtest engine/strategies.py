"""Stage 1e: strategies other people published, at their settings and re-optimised.

Every earlier stage tested rules *this project generated* — 231 TA-Lib variants, 8
transforms on them, 4,652 pairs. All null. `prereg.py` was the one exception: five
strategies taken from the literature at their published settings, tested with no free
parameters at all, precisely because a small pre-committed set carries a far lower noise
ceiling than a search does (+0.21 at N=5 against +0.36 at N=96 over 41 out-of-sample
years). Those five were also all negative.

This is that idea done at scale. Twenty-six strategies gathered from where people
actually publish them — academic journals, QuantConnect's Strategy Library, trading
academies, forum and practitioner folklore — each recorded with its source, its exact
rule and a parameter grid, then run through the stage-1b walk-forward machinery.

Three rows come out per strategy, and which one gets quoted decides what the number
means:

    <name>        the published parameters. No fitting whatsoever, so this is the
                  cleanest test available and the one whose ceiling is lowest.
    WFO[<name>]   that strategy's grid re-selected on every 3-year in-sample window and
                  traded through the next 12 months. This is "walk-forward optimisation"
                  in the sense the term is normally used, and its value is what it
                  costs, not what it promises: stage 1b measured per-fold
                  re-optimisation *losing* on all 14 re-optimisable TA-Lib families.
    IS#1          per fold, the best cell in the WHOLE catalog on in-sample IR, traded
                  through the next out-of-sample window. The strategy a disciplined
                  researcher following this method would actually have run, and the
                  honest headline. Never quote the best fixed row.

Four controls sit in the same table, because a leaderboard of negative IRs cannot be
read without them:

    BUYHOLD       the benchmark, never charged and never flattened. Its IR against
                  itself is NaN, not 0 — the difference series is identically zero, so
                  the ratio is 0/0. That is correct and is why it cannot be selected.
    ALWAYS_LONG   always long but *charged* the real fee schedule. Its IR also comes out
                  NaN, and that is the finding: a position that never changes pays
                  nothing after entry, and its single entry falls before the first
                  out-of-sample window. Costs cannot explain a long-biased rule's
                  shortfall against buy-and-hold.
    RANDOM_50     long half the time, in 20-bar blocks, from a seeded generator. Zero
    RANDOM_75     signal by construction; long three-quarters of the time.

The two RANDOM rows are the important ones. `combo_wf.py` measured corr(IR, long_frac)
= 0.881 on daily equities: against a rising benchmark, IR approaches 0 from below as a
rule approaches always-long, so most of a leaderboard's ordering is a ranking of
time-in-market. RANDOM_50 and RANDOM_75 price that handicap directly — they are what a
strategy with *no* signal and that much market exposure scores. A strategy is only
carrying information if it beats its exposure-matched random control, and comparing it
to zero instead makes every long-biased rule look better than it is.

**Lookbacks are calendar, not bars.** "12 months" and "200 days" are converted through
the measured `bars_per_year` for each sheet, so a strategy means the same economic thing
at 1d and 4h — the same convention as `prereg.py`. The consequence is worth stating:
Connors' RSI(2) is specified as two *days*, so at 4h it becomes RSI over ~3 bars rather
than 2. Reading it as two bars instead would silently make it a different, faster
strategy on every intraday sheet, and the two sheets would no longer be comparable.

`ibs` is the exception that proves the rule: internal bar strength has no lookback at
all, so a 4h IBS genuinely is a different quantity from a daily IBS. It is run on both
and the difference is a property of the statistic, not a scaling choice.

Run::

    python strategies.py                                  # all classes, 1d and 4h
    python strategies.py --class us_etfs --tf 1d
    python strategies.py --list                           # print the catalog and exit
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd
import talib
from tqdm import tqdm

from config import (CLASSES, HEADLINE_SCENARIO, MIN_BARS, MIN_IR_COVERAGE,
                    RESULTS_DIR, TIMEFRAMES, WF_MIN_FOLDS, scenarios)
from engines import vector
import metrics
import td_loader
import walkforward as wfmod

SEP = "@"                      # `rsi2_connors@buy=5,exit_ma=10`
DEFAULT_TIMEFRAMES = ("1d", "4h")
BASELINE = "BUYHOLD"
CONTROLS = ("ALWAYS_LONG", "ALWAYS_FLAT",
            "RANDOM_25", "RANDOM_50", "RANDOM_75", "RANDOM_90")
RANDOM_BLOCK = 20              # bars a random control holds before redrawing
RANDOM_SEED = 20260807
# A single random draw carries as much sampling noise as the strategies it is meant to
# calibrate — on a 3-asset sheet one seed put RANDOM_90 *below* RANDOM_75, which would
# make the interpolated control curve non-monotonic and the adjustment meaningless. The
# control's IR is averaged over this many independent draws instead, cutting its
# standard error by sqrt(n).
RANDOM_DRAWS = 12

FREE_FEE = {"key": "gross", "commission_bps": 0.0, "half_spread_bps": 0.0,
            "sell_fee_bps": 0.0, "borrow_annual": 0.0}


# ---------------------------------------------------------------- primitives

def _bars(bpy: float, years: float, minimum: int = 2) -> int:
    """Calendar span -> bar count on this sheet. `bpy` is measured, never assumed."""
    return max(minimum, int(round(bpy * years)))


D = 1.0 / 252.0                # one trading day, as a fraction of a year
M = 1.0 / 12.0                 # one month


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


# ---------------------------------------------------------------- trend / momentum

def tsmom(df, close, bpy, lookback_months=12.0):
    n = _bars(bpy, lookback_months * M)
    past = np.full(len(close), np.nan)
    past[n:] = close[n:] / close[:-n] - 1.0
    return np.nan_to_num((past > 0).astype("float64"), nan=0.0)


def faber_gtaa(df, close, bpy, ma_months=10.0):
    ma = talib.SMA(close, timeperiod=_bars(bpy, ma_months * M))
    return np.nan_to_num((close > ma).astype("float64"), nan=0.0)


def golden_cross(df, close, bpy, fast_days=50.0, slow_days=200.0):
    fast = talib.SMA(close, timeperiod=_bars(bpy, fast_days * D))
    slow = talib.SMA(close, timeperiod=_bars(bpy, slow_days * D))
    return np.where(np.isfinite(fast) & np.isfinite(slow) & (fast > slow), 1.0, 0.0)


def donchian(df, close, bpy, entry_days=20.0, exit_days=10.0):
    """Turtle channel breakout: enter on a new N-day high, exit on an M-day low."""
    hi = df["High"].to_numpy(dtype="float64")
    lo = df["Low"].to_numpy(dtype="float64")
    up = _rolling_max(hi, _bars(bpy, entry_days * D))
    dn = _rolling_min(lo, _bars(bpy, exit_days * D))
    return _state_machine(np.nan_to_num(close > up, nan=False),
                          np.nan_to_num(close < dn, nan=False))


def macd_cross(df, close, bpy, fast_days=12.0, slow_days=26.0, signal_days=9.0):
    macd, sig, _ = talib.MACD(close,
                              fastperiod=_bars(bpy, fast_days * D),
                              slowperiod=_bars(bpy, slow_days * D),
                              signalperiod=_bars(bpy, signal_days * D))
    return np.where(np.isfinite(macd) & np.isfinite(sig) & (macd > sig), 1.0, 0.0)


def dual_thrust(df, close, bpy, lookback_days=4.0, k=0.5):
    """QuantConnect's Dual Thrust: long when price clears the open by k x recent range."""
    n = _bars(bpy, lookback_days * D)
    hi, lo = df["High"].to_numpy("float64"), df["Low"].to_numpy("float64")
    op = df["Open"].to_numpy("float64")
    hh, ll = _rolling_max(hi, n), _rolling_min(lo, n)
    hc, lc = _rolling_max(close, n), _rolling_min(close, n)
    rng = np.maximum(hh - lc, hc - ll)
    return np.nan_to_num((close > op + k * rng).astype("float64"), nan=0.0)


TREND_MAS = (3.0, 5.0, 10.0, 20.0, 50.0, 200.0)


def trend_factor(df, close, bpy, min_votes=4):
    """Han/Zhou/Zhu: a composite of price-normalised moving averages, voted.

    The paper regresses future returns on the six MA/price ratios and trades the fitted
    signal. Fitting cross-sectional loadings is not available here — this harness scores
    one asset against its own buy-and-hold — so the loadings are replaced by an equal
    vote. That is a simplification and it is flagged in the report rather than presented
    as the paper's specification.
    """
    votes = np.zeros(len(close))
    for days in TREND_MAS:
        ma = talib.SMA(close, timeperiod=_bars(bpy, days * D))
        votes += np.nan_to_num((close > ma).astype("float64"), nan=0.0)
    return (votes >= min_votes).astype("float64")


# ---------------------------------------------------------------- mean reversion

def rsi2(df, close, bpy, buy=10.0, exit_ma_days=5.0, trend_ma_days=200.0):
    """Connors' RSI(2): buy deep oversold, exit on a short MA. Long/flat only.

    `trend_ma_days=0` removes the regime filter, which is `rsi2_raw`.
    """
    r = talib.RSI(close, timeperiod=_bars(bpy, 2 * D))
    entry = np.nan_to_num(r < buy, nan=False)
    if trend_ma_days:
        trend = talib.SMA(close, timeperiod=_bars(bpy, trend_ma_days * D))
        entry &= np.nan_to_num(close > trend, nan=False)
    ex = talib.SMA(close, timeperiod=_bars(bpy, exit_ma_days * D))
    return _state_machine(entry, np.nan_to_num(close > ex, nan=False))


def ibs(df, close, bpy, buy=0.2, sell=0.8):
    """Internal bar strength: where in its own range the bar closed."""
    hi, lo = df["High"].to_numpy("float64"), df["Low"].to_numpy("float64")
    rng = hi - lo
    # A zero-range bar (limit move, or a thin instrument that printed one price) has no
    # defined IBS. Neutral 0.5 keeps it out of both the entry and the exit condition
    # rather than letting a divide-by-zero decide the trade.
    val = np.divide(close - lo, rng, out=np.full(len(close), 0.5), where=rng > 0)
    return _state_machine(val < buy, val > sell)


def connors_rsi(df, close, bpy, buy=10.0, sell=70.0):
    """ConnorsRSI = mean of RSI(3), RSI(streak,2) and the 100-bar percentile of ROC(1)."""
    a = talib.RSI(close, timeperiod=_bars(bpy, 3 * D))
    b = talib.RSI(_streak(close), timeperiod=_bars(bpy, 2 * D))
    roc = np.zeros(len(close))
    roc[1:] = close[1:] / close[:-1] - 1.0
    c = _pct_rank(roc, _bars(bpy, 100 * D))
    crsi = (a + b + c) / 3.0
    return _state_machine(np.nan_to_num(crsi < buy, nan=False),
                          np.nan_to_num(crsi > sell, nan=False))


def bollinger_mr(df, close, bpy, period_days=20.0, nbdev=2.0):
    n = _bars(bpy, period_days * D)
    upper, mid, lower = talib.BBANDS(close, timeperiod=n, nbdevup=nbdev, nbdevdn=nbdev)
    return _state_machine(np.nan_to_num(close < lower, nan=False),
                          np.nan_to_num(close > mid, nan=False))


def three_lower_lows(df, close, bpy, n_lows=3, trend_ma_days=200.0):
    """Connors' pullback count: N consecutive lower lows in an uptrend, exit on strength."""
    lo = df["Low"].to_numpy("float64")
    hi = df["High"].to_numpy("float64")
    lower = pd.Series(lo).diff() < 0
    run = lower.rolling(int(n_lows)).sum().to_numpy() >= n_lows
    entry = np.nan_to_num(run, nan=False)
    if trend_ma_days:
        trend = talib.SMA(close, timeperiod=_bars(bpy, trend_ma_days * D))
        entry &= np.nan_to_num(close > trend, nan=False)
    prev_high = pd.Series(hi).shift(1).to_numpy()
    return _state_machine(entry, np.nan_to_num(close > prev_high, nan=False))


def st_reversal(df, close, bpy, lookback_weeks=1.0):
    n = _bars(bpy, lookback_weeks / 52.0)
    past = np.full(len(close), np.nan)
    past[n:] = close[n:] / close[:-n] - 1.0
    return np.nan_to_num((past < 0).astype("float64"), nan=0.0)


# ---------------------------------------------------------------- volatility / risk

def volmanaged(df, close, bpy, var_months=1.0):
    """Moreira/Muir: always long, scaled by inverse prior realised variance, capped 1.0."""
    return _vol_scale(close, _bars(bpy, var_months * M))


def atr_chandelier(df, close, bpy, atr_days=22.0, mult=3.0, entry_ma_days=50.0):
    """Long above a moving average, exited by a LeBeau chandelier stop.

    Path-dependent — the stop hangs from the highest high *since entry* — so this is the
    one strategy here that genuinely needs a bar loop rather than a rolling window.
    """
    n = _bars(bpy, atr_days * D)
    atr = talib.ATR(df["High"].to_numpy("float64"), df["Low"].to_numpy("float64"),
                    close, timeperiod=n)
    ma = talib.SMA(close, timeperiod=_bars(bpy, entry_ma_days * D))
    hi = df["High"].to_numpy("float64")

    out = np.zeros(len(close))
    peak = -np.inf
    for i in range(1, len(close)):
        if out[i - 1] > 0:
            peak = max(peak, hi[i])
            stop = peak - mult * atr[i] if np.isfinite(atr[i]) else -np.inf
            out[i] = 0.0 if close[i] < stop else 1.0
            if out[i] == 0.0:
                peak = -np.inf
        elif np.isfinite(ma[i]) and close[i] > ma[i]:
            out[i], peak = 1.0, hi[i]
    return out


def voltgt_tsmom(df, close, bpy, lookback_months=12.0, vol_days=20.0):
    """Baltas/Kosowski: time-series momentum sized by inverse realised volatility."""
    return tsmom(df, close, bpy, lookback_months) * _vol_scale(
        close, _bars(bpy, vol_days * D))


# ---------------------------------------------------------------- supertrend
#
# TA-Lib has no Supertrend, which is the only reason it was absent from all 231 rules of
# the stage-1 sweep. Its absence was an accident of the vendor's function list, not a
# judgement — so it enters here, in the catalog of hand-written published strategies,
# rather than by being bolted onto `talib_signals.get_all_indicator_names()`. That
# function is shared by all three studies in this repo; adding a name to it would change
# what every previous sweep enumerated and silently invalidate their caches and their
# trial counts.
#
# The construction is Olivier Seban's, as implemented by every charting package: bands at
# the bar's midpoint plus/minus a multiple of ATR, which then RATCHET — the upper band can
# only fall while price stays under it, the lower can only rise while price stays over it
# — and the trend flips when a close breaks the opposite band. The ratchet is what makes
# it a trailing stop rather than a channel, and it is path-dependent, so like
# `atr_chandelier` this needs a genuine bar loop.
#
# Positions are read off bar t's close and traded at t+1 by the engine's `pos.shift(1)`,
# the same convention as every other rule here. No band is computed from a bar the rule
# has not seen.

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


def supertrend(df, close, bpy, atr_days=10.0, mult=3.0, allow_short=1):
    """The textbook rule: long in an uptrend, short in a downtrend.

    `allow_short=0` gives the long/flat variant. That switch is not cosmetic on this
    project's metric — IR is measured against buy-and-hold, and against a rising
    benchmark a short leg is charged the benchmark's drift twice over. The two versions
    are different strategies and both are scored.
    """
    t = _supertrend_trend(df, close, bpy, atr_days, mult)
    return t if allow_short else np.maximum(t, 0.0)


def supertrend_adx(df, close, bpy, atr_days=10.0, mult=3.0,
                   adx_days=14.0, adx_min=25.0):
    """Supertrend long, taken only while ADX says there is a trend worth following.

    The standard pairing, and the one with an actual thesis behind it: an ATR trailing
    stop whipsaws in a range, and ADX is the conventional measure of whether price is
    ranging. ADX is direction-blind by construction, so this is a pure filter — it can
    only remove exposure, never add or reverse it.
    """
    t = np.maximum(_supertrend_trend(df, close, bpy, atr_days, mult), 0.0)
    adx = talib.ADX(df["High"].to_numpy("float64"), df["Low"].to_numpy("float64"),
                    close, timeperiod=_bars(bpy, adx_days * D))
    return np.where(np.isfinite(adx) & (adx >= adx_min), t, 0.0)


def supertrend_regime(df, close, bpy, atr_days=10.0, mult=3.0, ma_days=200.0):
    """Supertrend long, gated by a long-horizon moving-average regime filter."""
    t = np.maximum(_supertrend_trend(df, close, bpy, atr_days, mult), 0.0)
    ma = talib.SMA(close, timeperiod=_bars(bpy, ma_days * D))
    return np.where(np.isfinite(ma) & (close > ma), t, 0.0)


def supertrend_dual(df, close, bpy, atr_days=10.0, mult=3.0,
                    slow_days=20.0, slow_mult=6.0):
    """A fast Supertrend for the entry, a slow one as the regime gate.

    The multi-timeframe Supertrend that trading forums reach for, expressed on one series
    by widening the band instead of resampling the bars. Same information, no resampling
    seam, and it keeps the 1d and 4h sheets measuring the same thing.
    """
    fast = np.maximum(_supertrend_trend(df, close, bpy, atr_days, mult), 0.0)
    slow = np.maximum(_supertrend_trend(df, close, bpy, slow_days, slow_mult), 0.0)
    return fast * slow


def supertrend_pullback(df, close, bpy, atr_days=10.0, mult=3.0,
                        rsi_days=2.0, buy=25.0):
    """Supertrend sets the direction; entry waits for an RSI(2) dip inside the uptrend.

    Deliberately the meta-labelling shape — the trend rule decides *whether* the market is
    ownable and a second, unrelated signal decides *when* to pay for it. It can only cut
    time-in-market relative to plain Supertrend, so a lower turnover here is expected and
    an improvement in IR would not be.
    """
    up = _supertrend_trend(df, close, bpy, atr_days, mult) > 0
    rsi = talib.RSI(close, timeperiod=_bars(bpy, rsi_days * D))
    return _state_machine(up & np.isfinite(rsi) & (rsi < buy), ~up)


# ---------------------------------------------------------------- calendar
#
# Calendar rules read the index, and the index is known in advance: what day of the
# month it is, and which sessions the exchange will be closed, are both published years
# ahead. Using them is not look-ahead in any economically meaningful sense — it is the
# one kind of "future" information a trader genuinely has.

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


def turn_of_month(df, close, bpy, before=1, after=3):
    """Long the last `before` sessions of a month and the first `after` of the next."""
    pos, size = _day_ordinals(df.index)
    return ((pos <= after) | (pos > size - before)).astype("float64")


def sell_in_may(df, close, bpy, start_month=11, end_month=4):
    """Bouman/Jacobsen: long November through April, flat May through October."""
    m = df.index.month.to_numpy()
    on = (m >= start_month) | (m <= end_month)
    return on.astype("float64")


def preholiday(df, close, bpy, sessions=1):
    """Long the `sessions` sessions before an exchange holiday.

    A holiday is detected as a gap in the session calendar: the next trading date is
    more than one business day away, with weekends excluded. Derived from the index
    rather than a hardcoded holiday table so it stays correct for any venue and any
    year the vendor serves.
    """
    dates = pd.DatetimeIndex(pd.unique(df.index.normalize()))
    if len(dates) < 3:
        return np.zeros(len(close))
    # Business days strictly between this session and the next; > 0 means a closure.
    gaps = np.array([len(pd.bdate_range(dates[i] + pd.Timedelta(days=1),
                                        dates[i + 1] - pd.Timedelta(days=1)))
                     for i in range(len(dates) - 1)] + [0])
    flag = pd.Series(gaps > 0, index=dates)
    for k in range(1, int(sessions)):
        flag |= flag.shift(-k).fillna(False)
    return flag.reindex(df.index.normalize()).to_numpy().astype("float64")


def monday_effect(df, close, bpy):
    """French (1980): flat on Mondays, long otherwise."""
    return (df.index.dayofweek.to_numpy() != 0).astype("float64")


# ---------------------------------------------------------------- regime composites

def momo_regime(df, close, bpy, lookback_months=12.0, ma_days=200.0):
    ma = talib.SMA(close, timeperiod=_bars(bpy, ma_days * D))
    regime = np.nan_to_num((close > ma).astype("float64"), nan=0.0)
    return tsmom(df, close, bpy, lookback_months) * regime


def ichimoku(df, close, bpy, tenkan_days=9.0, kijun_days=26.0, senkou_days=52.0):
    """Long above the Kumo cloud. The cloud is displaced forward, so it reads backward."""
    hi, lo = df["High"].to_numpy("float64"), df["Low"].to_numpy("float64")
    nt, nk, ns = (_bars(bpy, tenkan_days * D), _bars(bpy, kijun_days * D),
                  _bars(bpy, senkou_days * D))
    mid = lambda n: (pd.Series(hi).rolling(n).max().to_numpy()
                     + pd.Series(lo).rolling(n).min().to_numpy()) / 2.0
    tenkan, kijun = mid(nt), mid(nk)
    # Displaced forward by `nk`, which is exactly what makes reading it causal: the
    # cloud above bar t was computed from data at or before bar t - nk.
    span_a = pd.Series((tenkan + kijun) / 2.0).shift(nk).to_numpy()
    span_b = pd.Series(mid(ns)).shift(nk).to_numpy()
    top = np.maximum(span_a, span_b)
    return np.nan_to_num((close > top).astype("float64"), nan=0.0)


def dyn_breakout2(df, close, bpy, base_days=20.0, vol_days=30.0, lo_days=20.0,
                  hi_days=60.0):
    """Pruitt's Dynamic Break Out II: the breakout lookback tracks realised volatility.

    Volatility rising lengthens the window (fewer, more selective breakouts); falling
    volatility shortens it. Window length varies per bar, so this cannot be a fixed
    rolling call and is computed in a loop.
    """
    n_vol = _bars(bpy, vol_days * D)
    lo_n, hi_n = _bars(bpy, lo_days * D), _bars(bpy, hi_days * D)
    ret = np.zeros(len(close))
    ret[1:] = close[1:] / close[:-1] - 1.0
    sd = pd.Series(ret).rolling(n_vol).std(ddof=1).to_numpy()
    # Expanding, not whole-sample: normalising today's volatility against the median of
    # a series that includes tomorrow decides today's lookback with tomorrow's data.
    ref = _causal_median(sd, n_vol)
    ratio = np.divide(sd, ref, out=np.ones_like(sd),
                      where=np.isfinite(sd) & np.isfinite(ref) & (ref > 0))
    span = np.clip(np.nan_to_num(_bars(bpy, base_days * D) * ratio, nan=lo_n),
                   lo_n, hi_n).astype(int)

    hi, lo = df["High"].to_numpy("float64"), df["Low"].to_numpy("float64")
    entry = np.zeros(len(close), dtype=bool)
    ex = np.zeros(len(close), dtype=bool)
    for i in range(1, len(close)):
        n = span[i]
        j = max(0, i - n)
        if j == i:
            continue
        entry[i] = close[i] > hi[j:i].max()
        ex[i] = close[i] < lo[j:i].min()
    return _state_machine(entry, ex)


# ---------------------------------------------------------------- the catalog

@dataclass(frozen=True)
class Strategy:
    fn: Callable
    rule: str                          # the rule in plain English
    source: str                        # where it was published
    family: str
    grid: tuple                        # dicts of params; grid[0] IS the published one
    anchor: bool = False               # already run by prereg.py on 2026-08-05
    classes: tuple | None = None       # None = every class
    note: str = ""

    @property
    def published(self) -> dict:
        return self.grid[0]


CATALOG: dict[str, Strategy] = {
    # ------------------------------------------------------------ trend / momentum
    "tsmom12": Strategy(
        tsmom, "Long if the trailing 12-month return is positive, else flat.",
        "Moskowitz, Ooi & Pedersen (2012), 'Time Series Momentum', JFE", "trend",
        ({"lookback_months": 12.0}, {"lookback_months": 3.0},
         {"lookback_months": 6.0}, {"lookback_months": 9.0}), anchor=True),
    "faber_gtaa": Strategy(
        faber_gtaa, "Long while the close is above its 10-month moving average.",
        "Faber (2007), 'A Quantitative Approach to Tactical Asset Allocation'", "trend",
        ({"ma_months": 10.0}, {"ma_months": 6.0}, {"ma_months": 8.0},
         {"ma_months": 12.0}), anchor=True),
    "golden_cross": Strategy(
        golden_cross, "Long while the 50-day SMA is above the 200-day SMA.",
        "Folk / StockCharts ChartSchool; the retail default", "trend",
        ({"fast_days": 50.0, "slow_days": 200.0},
         {"fast_days": 20.0, "slow_days": 100.0},
         {"fast_days": 10.0, "slow_days": 50.0},
         {"fast_days": 50.0, "slow_days": 150.0}), anchor=True),
    "donchian_s1": Strategy(
        donchian, "Turtle System 1: enter on a 20-day high, exit on a 10-day low.",
        "Dennis & Eckhardt Turtle rules; QuantifiedStrategies 'Donchian Channels'",
        "trend",
        ({"entry_days": 20.0, "exit_days": 10.0},
         {"entry_days": 10.0, "exit_days": 5.0},
         {"entry_days": 30.0, "exit_days": 15.0},
         {"entry_days": 40.0, "exit_days": 20.0})),
    "donchian_s2": Strategy(
        donchian, "Turtle System 2: enter on a 55-day high, exit on a 20-day low.",
        "Dennis & Eckhardt Turtle rules", "trend",
        ({"entry_days": 55.0, "exit_days": 20.0},
         {"entry_days": 55.0, "exit_days": 10.0},
         {"entry_days": 80.0, "exit_days": 30.0},
         {"entry_days": 100.0, "exit_days": 40.0})),
    "macd_cross": Strategy(
        macd_cross, "Long while MACD(12,26) sits above its 9-period signal line.",
        "QuantifiedStrategies, 'Bitcoin MACD Trading Strategy'", "trend",
        ({"fast_days": 12.0, "slow_days": 26.0, "signal_days": 9.0},
         {"fast_days": 5.0, "slow_days": 35.0, "signal_days": 5.0},
         {"fast_days": 20.0, "slow_days": 50.0, "signal_days": 9.0},
         {"fast_days": 8.0, "slow_days": 17.0, "signal_days": 9.0})),
    "dual_thrust": Strategy(
        dual_thrust, "Long when the close clears the open by k x the recent 4-day range.",
        "QuantConnect Strategy Library, 'Dual Thrust Trading Algorithm'", "trend",
        ({"lookback_days": 4.0, "k": 0.5}, {"lookback_days": 4.0, "k": 0.2},
         {"lookback_days": 10.0, "k": 0.5}, {"lookback_days": 20.0, "k": 0.7})),
    "trend_factor": Strategy(
        trend_factor,
        "Long when at least 4 of the 6 moving averages (3/5/10/20/50/200d) sit below price.",
        "Han, Zhou & Zhu (2016), 'A Trend Factor', RFS", "trend",
        ({"min_votes": 4}, {"min_votes": 3}, {"min_votes": 5}, {"min_votes": 6}),
        note="The paper fits cross-sectional loadings on the six MA/price ratios; this "
             "harness is single-asset, so the loadings become an equal vote."),

    # ------------------------------------------------------------ supertrend
    #
    # Six cells, four parameter settings each. That count is deliberate and it is the
    # number that has to be carried into any claim made from this block: selecting the
    # best of 24 cells per class means the relevant null is the maximum of 24 draws, not
    # a single one. The grids stay at four so this stays affordable in trial terms —
    # a 12-point grid per entry would triple the multiplicity to buy parameter detail
    # nobody would trade on.
    #
    # Not included, on purpose: a vol-targeted Supertrend. That family was tested to
    # destruction in `../top 20 stocks/edge/p9_long_history.py` — over 31.5 years the
    # overlay is significantly HARMFUL (t = -3.18), not merely unproven, and the holdout
    # is dirty for it. Re-running it wearing a Supertrend hat would spend multiplicity on
    # a question already answered.
    "supertrend": Strategy(
        supertrend, "Long above the ATR trailing stop, short below it (ATR 10, 3x).",
        "Seban, 'SuperTrend' (2007); the TradingView/MT4 default", "trend",
        ({"atr_days": 10.0, "mult": 3.0, "allow_short": 1},
         {"atr_days": 7.0, "mult": 3.0, "allow_short": 1},
         {"atr_days": 14.0, "mult": 2.0, "allow_short": 1},
         {"atr_days": 20.0, "mult": 4.0, "allow_short": 1}),
        note="Absent from the 231-rule sweep only because TA-Lib has no Supertrend "
             "function. This is its first appearance in the project."),
    "supertrend_lf": Strategy(
        supertrend, "The same ATR trailing stop, long/flat instead of long/short.",
        "Seban, ibid — the long-only variant retail platforms ship by default", "trend",
        ({"atr_days": 10.0, "mult": 3.0, "allow_short": 0},
         {"atr_days": 7.0, "mult": 3.0, "allow_short": 0},
         {"atr_days": 14.0, "mult": 2.0, "allow_short": 0},
         {"atr_days": 20.0, "mult": 4.0, "allow_short": 0}),
        note="Read against `supertrend` to price the short leg, and against the exposure "
             "controls before reading its IR at all — the controls are long/flat, so "
             "`ir_vs_random` is only defined for this variant and not for the long/short "
             "one."),
    "supertrend_adx": Strategy(
        supertrend_adx, "Supertrend long, only while ADX(14) is at or above 25.",
        "The conventional Supertrend + ADX range filter", "trend",
        ({"atr_days": 10.0, "mult": 3.0, "adx_days": 14.0, "adx_min": 25.0},
         {"atr_days": 10.0, "mult": 3.0, "adx_days": 14.0, "adx_min": 20.0},
         {"atr_days": 10.0, "mult": 3.0, "adx_days": 14.0, "adx_min": 30.0},
         {"atr_days": 10.0, "mult": 3.0, "adx_days": 20.0, "adx_min": 25.0})),
    "supertrend_regime": Strategy(
        supertrend_regime, "Supertrend long, gated by price above its 200-day average.",
        "Supertrend + the Faber/GTAA regime filter", "trend",
        ({"atr_days": 10.0, "mult": 3.0, "ma_days": 200.0},
         {"atr_days": 10.0, "mult": 3.0, "ma_days": 100.0},
         {"atr_days": 10.0, "mult": 3.0, "ma_days": 50.0},
         {"atr_days": 7.0, "mult": 3.0, "ma_days": 200.0})),
    "supertrend_dual": Strategy(
        supertrend_dual, "A fast Supertrend for entry, a wider slow one as the gate.",
        "The multi-timeframe Supertrend, expressed by band width on one series", "trend",
        ({"atr_days": 10.0, "mult": 3.0, "slow_days": 20.0, "slow_mult": 6.0},
         {"atr_days": 7.0, "mult": 2.0, "slow_days": 20.0, "slow_mult": 6.0},
         {"atr_days": 10.0, "mult": 3.0, "slow_days": 30.0, "slow_mult": 8.0},
         {"atr_days": 10.0, "mult": 3.0, "slow_days": 14.0, "slow_mult": 4.0})),
    "supertrend_pullback": Strategy(
        supertrend_pullback, "Supertrend picks the trend; RSI(2) below 25 picks the entry.",
        "Supertrend direction + the Connors RSI(2) dip entry", "trend",
        ({"atr_days": 10.0, "mult": 3.0, "rsi_days": 2.0, "buy": 25.0},
         {"atr_days": 10.0, "mult": 3.0, "rsi_days": 2.0, "buy": 10.0},
         {"atr_days": 10.0, "mult": 3.0, "rsi_days": 2.0, "buy": 35.0},
         {"atr_days": 10.0, "mult": 3.0, "rsi_days": 3.0, "buy": 25.0})),

    # ------------------------------------------------------------ mean reversion
    "rsi2_connors": Strategy(
        rsi2, "Buy when RSI(2) < 10 while above the 200-day SMA; exit above the 5-day SMA.",
        "Connors & Alvarez, 'Short Term Trading Strategies That Work'; "
        "QuantifiedStrategies 'RSI-2'", "reversion",
        ({"buy": 10.0, "exit_ma_days": 5.0, "trend_ma_days": 200.0},
         {"buy": 5.0, "exit_ma_days": 5.0, "trend_ma_days": 200.0},
         {"buy": 15.0, "exit_ma_days": 5.0, "trend_ma_days": 200.0},
         {"buy": 10.0, "exit_ma_days": 10.0, "trend_ma_days": 200.0},
         {"buy": 10.0, "exit_ma_days": 2.0, "trend_ma_days": 100.0})),
    "rsi2_raw": Strategy(
        rsi2, "The same RSI(2) dip buy with no 200-day trend filter.",
        "Connors & Alvarez, ibid — the unfiltered variant", "reversion",
        ({"buy": 10.0, "exit_ma_days": 5.0, "trend_ma_days": 0.0},
         {"buy": 5.0, "exit_ma_days": 5.0, "trend_ma_days": 0.0},
         {"buy": 15.0, "exit_ma_days": 10.0, "trend_ma_days": 0.0})),
    "ibs": Strategy(
        ibs, "Long when the bar closes in the bottom 20% of its own range; exit above 80%.",
        "QuantifiedStrategies, 'Internal Bar Strength'", "reversion",
        ({"buy": 0.2, "sell": 0.8}, {"buy": 0.1, "sell": 0.9},
         {"buy": 0.3, "sell": 0.7}, {"buy": 0.2, "sell": 0.6}),
        note="The only lookback-free rule here, so a 4h IBS is genuinely a different "
             "statistic from a daily IBS rather than a rescaled one."),
    "connors_rsi": Strategy(
        connors_rsi, "ConnorsRSI below 10 buys, above 70 exits.",
        "Connors Research, 'ConnorsRSI'", "reversion",
        ({"buy": 10.0, "sell": 70.0}, {"buy": 5.0, "sell": 70.0},
         {"buy": 15.0, "sell": 60.0}, {"buy": 20.0, "sell": 80.0})),
    "bollinger_mr": Strategy(
        bollinger_mr, "Long below the lower Bollinger band (20,2); exit at the midline.",
        "Bollinger; QuantifiedStrategies mean-reversion write-ups", "reversion",
        ({"period_days": 20.0, "nbdev": 2.0}, {"period_days": 20.0, "nbdev": 2.5},
         {"period_days": 10.0, "nbdev": 2.0}, {"period_days": 50.0, "nbdev": 2.0})),
    "three_lower_lows": Strategy(
        three_lower_lows,
        "Long after 3 consecutive lower lows above the 200-day SMA; exit above the prior high.",
        "Connors, 'the 3-day high/low' pullback setup", "reversion",
        ({"n_lows": 3, "trend_ma_days": 200.0}, {"n_lows": 2, "trend_ma_days": 200.0},
         {"n_lows": 4, "trend_ma_days": 200.0}, {"n_lows": 3, "trend_ma_days": 0.0})),
    "st_reversal": Strategy(
        st_reversal, "Long after a down week, flat after an up week.",
        "Jegadeesh (1990), 'Evidence of Predictable Behavior of Security Returns', JF",
        "reversion",
        ({"lookback_weeks": 1.0}, {"lookback_weeks": 0.5},
         {"lookback_weeks": 2.0}, {"lookback_weeks": 4.0}), anchor=True),

    # ------------------------------------------------------------ volatility / risk
    "volmanaged": Strategy(
        volmanaged, "Always long, scaled by inverse prior-month realised variance, capped 1.0.",
        "Moreira & Muir (2017), 'Volatility-Managed Portfolios', JF", "volatility",
        ({"var_months": 1.0}, {"var_months": 0.5}, {"var_months": 2.0},
         {"var_months": 3.0}), anchor=True),
    "atr_chandelier": Strategy(
        atr_chandelier, "Long above the 50-day SMA, exited by a 3 x ATR(22) chandelier stop.",
        "LeBeau's chandelier exit; Tradeciety / TrendSpider write-ups", "volatility",
        ({"atr_days": 22.0, "mult": 3.0, "entry_ma_days": 50.0},
         {"atr_days": 14.0, "mult": 3.0, "entry_ma_days": 50.0},
         {"atr_days": 22.0, "mult": 2.0, "entry_ma_days": 50.0},
         {"atr_days": 22.0, "mult": 4.0, "entry_ma_days": 200.0})),
    "voltgt_tsmom": Strategy(
        voltgt_tsmom, "12-month time-series momentum sized by inverse realised volatility.",
        "Baltas & Kosowski (2013), 'Momentum Strategies in Futures Markets'", "volatility",
        ({"lookback_months": 12.0, "vol_days": 20.0},
         {"lookback_months": 12.0, "vol_days": 60.0},
         {"lookback_months": 6.0, "vol_days": 20.0},
         {"lookback_months": 3.0, "vol_days": 10.0})),

    # ------------------------------------------------------------ calendar
    "turn_of_month": Strategy(
        turn_of_month, "Long the last session of each month plus the first 3 of the next.",
        "QuantConnect, 'Turn of the Month in Equity Indexes'; Lakonishok & Smidt (1988)",
        "calendar",
        ({"before": 1, "after": 3}, {"before": 1, "after": 1},
         {"before": 2, "after": 3}, {"before": 3, "after": 5})),
    "sell_in_may": Strategy(
        sell_in_may, "Long November through April, flat May through October.",
        "Bouman & Jacobsen (2002), 'The Halloween Indicator', AER", "calendar",
        ({"start_month": 11, "end_month": 4}, {"start_month": 10, "end_month": 5},
         {"start_month": 11, "end_month": 5})),
    "preholiday": Strategy(
        preholiday, "Long the session before an exchange holiday.",
        "QuantConnect, 'Pre-holiday Effect'; Ariel (1990)", "calendar",
        ({"sessions": 1}, {"sessions": 2}, {"sessions": 3}),
        classes=("us_stocks", "us_etfs"),
        note="A 24/7 market has no exchange closures, so this is undefined on crypto "
             "rather than merely unprofitable there."),
    "monday_effect": Strategy(
        monday_effect, "Flat on Mondays, long every other session.",
        "French (1980), 'Stock Returns and the Weekend Effect', JFE", "calendar",
        ({},), classes=("us_stocks", "us_etfs"),
        note="The effect is about the Friday-to-Monday non-trading gap, which a 24/7 "
             "market does not have."),

    # ------------------------------------------------------------ regime composites
    "momo_regime": Strategy(
        momo_regime, "12-month momentum, taken only while price is above the 200-day SMA.",
        "QuantConnect, 'Momentum and State of Market Filters'", "regime",
        ({"lookback_months": 12.0, "ma_days": 200.0},
         {"lookback_months": 6.0, "ma_days": 200.0},
         {"lookback_months": 12.0, "ma_days": 100.0},
         {"lookback_months": 3.0, "ma_days": 50.0})),
    "ichimoku": Strategy(
        ichimoku, "Long while the close sits above the Ichimoku cloud.",
        "QuantConnect, 'Ichimoku Clouds in the Energy Sector'", "regime",
        ({"tenkan_days": 9.0, "kijun_days": 26.0, "senkou_days": 52.0},
         {"tenkan_days": 20.0, "kijun_days": 60.0, "senkou_days": 120.0},
         {"tenkan_days": 5.0, "kijun_days": 13.0, "senkou_days": 26.0})),
    "dyn_breakout2": Strategy(
        dyn_breakout2, "Channel breakout whose lookback lengthens as volatility rises.",
        "QuantConnect, 'Dynamic Breakout II'; Pruitt's Dynamic Break Out II", "regime",
        ({"base_days": 20.0, "vol_days": 30.0, "lo_days": 20.0, "hi_days": 60.0},
         {"base_days": 40.0, "vol_days": 30.0, "lo_days": 20.0, "hi_days": 80.0},
         {"base_days": 20.0, "vol_days": 60.0, "lo_days": 10.0, "hi_days": 40.0})),
}


# ---------------------------------------------------------------- labels

def encode(name: str, params: dict, strategy: Strategy) -> str:
    """`rsi2_connors` for the published cell, `rsi2_connors@buy=5.0` for the rest.

    Only parameters that DIFFER from the published values are written, so the published
    cell keeps a bare name and every other label reads as a diff against it.
    """
    diff = {k: v for k, v in params.items() if strategy.published.get(k) != v}
    if not diff:
        return name
    return name + SEP + ",".join(f"{k}={v}" for k, v in sorted(diff.items()))


def decode(label: str) -> tuple[str, dict]:
    """Label -> (strategy name, full parameter dict), published values filling the rest."""
    name, _, tail = label.partition(SEP)
    strategy = CATALOG[name]
    params = dict(strategy.published)
    if tail:
        for piece in tail.split(","):
            k, _, v = piece.partition("=")
            params[k] = type(strategy.published[k])(float(v))
    return name, params


def cells(asset_class: str) -> list[str]:
    """Every (strategy, parameter) label runnable on this class, published cells first."""
    out = []
    for name, s in CATALOG.items():
        if s.classes is not None and asset_class not in s.classes:
            continue
        out.extend(encode(name, p, s) for p in s.grid)
    return out


def skipped_for(asset_class: str) -> list[str]:
    """Strategies this class cannot express. Counted and reported, never run as flat.

    A rule that is undefined on a class and gets scored anyway produces a flat position,
    which on a leaderboard is indistinguishable from a rule that simply does nothing —
    the same reasoning as `signals.usable_rules` and the crypto volume rules.
    """
    return [n for n, s in CATALOG.items()
            if s.classes is not None and asset_class not in s.classes]


def random_control(n: int, p_long: float, symbol: str, draw: int = 0) -> np.ndarray:
    """Signal-free long/flat at `p_long` exposure, in blocks, deterministic per symbol.

    Blocks rather than per-bar draws because a coin flipped every bar would turn over
    ~130 times a year and be dominated by fees, which is not the quantity being
    measured — the question is what a rule with *no information* and this much market
    exposure scores, not what one with absurd turnover scores.

    Seeded from the symbol via CRC32, not `hash()`, which is randomised per process by
    PYTHONHASHSEED and would make the control a different series on every run. Per-symbol
    seeds also matter: one shared draw would correlate every asset's IR and hand the
    breadth statistic an agreement it did not earn.
    """
    from zlib import crc32
    rng = np.random.default_rng(RANDOM_SEED + crc32(symbol.encode("utf-8"))
                                + 7919 * draw)
    n_blocks = int(np.ceil(n / RANDOM_BLOCK))
    blocks = (rng.random(n_blocks) < p_long).astype("float64")
    return np.repeat(blocks, RANDOM_BLOCK)[:n]


def build(label: str, df: pd.DataFrame, close: np.ndarray, bpy: float,
          symbol: str = "", draw: int = 0) -> np.ndarray | None:
    if label in (BASELINE, "ALWAYS_LONG"):
        return np.ones(len(df), dtype="float64")
    if label == "ALWAYS_FLAT":
        return np.zeros(len(df), dtype="float64")
    if label.startswith("RANDOM_"):
        return random_control(len(df), int(label.split("_")[1]) / 100.0, symbol, draw)
    try:
        name, params = decode(label)
        pos = CATALOG[name].fn(df, close, bpy, **params)
    except Exception:
        return None
    pos = np.nan_to_num(np.asarray(pos, dtype="float64"), nan=0.0,
                        posinf=0.0, neginf=0.0)
    return pos if pos.size == len(df) else None


# ---------------------------------------------------------------- the run

def _diagnostics(pos: np.ndarray, mask: np.ndarray, bpy: float) -> dict:
    """Time-in-market and churn over the scored window.

    `long_frac` is not decoration. `combo_wf.py` measured corr(IR, long_frac) = 0.881 on
    daily equities: against a rising benchmark, IR approaches 0 from below as a rule
    approaches always-long, so a leaderboard sorted by IR is partly a ranking of
    time-in-market. Any equity improvement is read against this column before it is
    believed.
    """
    p = pos[mask]
    if p.size == 0:
        return {"long_frac": np.nan, "exposure": np.nan, "turnover_yr": np.nan}
    years = p.size / bpy if bpy > 0 else np.nan
    return {
        "long_frac": float(np.mean(p > 0)),
        "exposure": float(np.mean(p != 0)),
        "turnover_yr": float(np.abs(np.diff(p)).sum() / years) if years > 0 else np.nan,
    }


def excess_cagr(per_symbol: pd.DataFrame) -> dict:
    """Compounded excess growth rate per asset, then aggregated. Three numbers.

    `walkforward.leaderboard_row` reports `excess_return_pct` as the mean of per-asset
    *total* returns, and on this universe that statistic is close to meaningless: SOXL
    buy-and-hold compounds 230x over the window while SPY does 6.4x, so averaging their
    percentages lets one asset write the answer. Annualising first and averaging second
    puts them on the same scale.

    It is reported alongside IR rather than instead of it because on leveraged ETFs the
    two genuinely disagree, and the disagreement is the finding. IR is built on the
    arithmetic mean of a difference series and is blind to variance drag; compounding is
    not. A rule that gives up 9%/yr of arithmetic mean while cutting annualised
    volatility from 94% to 63% *loses* on IR and *wins* on terminal wealth, and both
    statements are true of the same trades.

    `excess_cagr_min` is the one that decides anything. A positive mean carried by a
    single asset is the failure mode the leave-one-out gate exists to catch, and on a
    three-asset sheet the minimum says it more directly than breadth can.
    """
    if not {"ret_pct", "bench_pct", "years_oos"} <= set(per_symbol.columns):
        return {}
    yrs = per_symbol["years_oos"].to_numpy(dtype="float64")
    with np.errstate(invalid="ignore", divide="ignore"):
        # Clipped at -99.99%: a rule that lost everything has no defined growth rate,
        # and a negative base under a fractional power is NaN rather than an error.
        grow = lambda pct: np.power(
            np.clip(1.0 + per_symbol[pct].to_numpy(dtype="float64") / 100.0, 1e-4, None),
            np.divide(1.0, yrs, out=np.full_like(yrs, np.nan), where=yrs > 0)) - 1.0
        d = grow("ret_pct") - grow("bench_pct")
    d = d[np.isfinite(d)]
    if d.size == 0:
        return {"excess_cagr": np.nan, "excess_cagr_min": np.nan,
                "excess_cagr_hit": np.nan}
    return {"excess_cagr": float(np.mean(d)), "excess_cagr_min": float(np.min(d)),
            "excess_cagr_hit": float(np.mean(d > 0))}


def add_exposure_adjusted(summary: pd.DataFrame) -> pd.DataFrame:
    """Add `ir_random` and `ir_vs_random`: IR with the time-in-market handicap removed.

    Against a rising benchmark, being out of the market is expensive whether or not the
    rule knows anything — `combo_wf.py` measured corr(IR, long_frac) = 0.881 on daily
    equities, so most of a leaderboard's ordering is just exposure. The six controls
    trace that relationship empirically on this exact data: ALWAYS_FLAT pins x = 0, four
    seeded random rules sit between, and ALWAYS_LONG pins x = 1 at IR = 0 (an always-long
    rule *is* the benchmark, so its IR is 0 by definition — the NaN it computes is 0/0,
    not a missing value).

    `ir_vs_random` is the row's IR minus that curve at its own exposure. It is the number
    that answers "does this strategy know something", where raw IR only answers "was this
    strategy in the market". A strategy at -0.22 that is long 36% of the time is doing
    far better than one at -0.38 that is long 74% of the time, and the raw column says
    the opposite.

    Interpolated, not fitted: with six points a straight line would smooth over whatever
    curvature is really there, and the curve is measured, not modelled.
    """
    out = []
    for scen, grp in summary.groupby("scenario", sort=False):
        ctrl = grp[grp["rule"].str.startswith(("RANDOM_", "ALWAYS_"))]
        pts = {}
        for r in ctrl.itertuples():
            # ALWAYS_LONG is the benchmark; its 0/0 IR is definitionally 0.
            ir = 0.0 if r.rule == "ALWAYS_LONG" else r.ir_net
            if np.isfinite(r.long_frac) and np.isfinite(ir):
                pts[round(float(r.long_frac), 4)] = float(ir)
        grp = grp.copy()
        if len(pts) >= 2:
            xs = np.array(sorted(pts))
            ys = np.array([pts[x] for x in xs])
            grp["ir_random"] = np.interp(grp["long_frac"].to_numpy(dtype="float64"),
                                         xs, ys, left=ys[0], right=ys[-1])
            grp["ir_vs_random"] = grp["ir_net"] - grp["ir_random"]
        else:
            grp["ir_random"] = np.nan
            grp["ir_vs_random"] = np.nan
        out.append(grp)
    return pd.concat(out, ignore_index=True)


def _stitched_diagnostics(data: dict, folds, masks: dict, picks: pd.DataFrame,
                          bench: dict, label: str) -> pd.DataFrame:
    """`long_frac` / exposure / turnover for a stitched path.

    `walkforward.stitch` returns scores, not positions, so the same fold-by-fold write
    is repeated here to measure what the stitched path actually *held*. Without this the
    diagnostic column is blank on exactly the rows that matter — IS#1 and the WFO paths
    — and `long_frac` is the column that tells an equity IR improvement apart from a
    rule that simply spent more time in the market.
    """
    rows = []
    for symbol, df in data.items():
        ms = masks[symbol]
        mine = picks[picks["symbol"] == symbol]
        if mine.empty:
            continue
        b = bench[symbol]
        cache = {r: build(r, df, b["close"], b["bpy"], symbol)
                 for r in mine["rule"].unique()}
        for scen, grp in mine.groupby("scenario"):
            stitched = np.zeros(len(df))
            used = np.zeros(len(df), dtype=bool)
            for r in grp.itertuples():
                m, p = ms[r.fold], cache.get(r.rule)
                if m is None or p is None:
                    continue
                stitched[m[1]] = p[m[1]]
                used |= m[1]
            if used.any():
                rows.append({"rule": label, "symbol": symbol, "scenario": scen,
                             **_diagnostics(stitched, used, b["bpy"])})
    return pd.DataFrame(rows)


def run_pair(asset_class: str, timeframe: str) -> tuple[dict, dict]:
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        return {}, {}

    start = min(df.index[0] for df in data.values())
    end = max(df.index[-1] for df in data.values())
    folds = wfmod.generate_folds(start, end)
    if len(folds) < WF_MIN_FOLDS:
        return {}, {"skipped": f"only {len(folds)} folds in {start.date()}..{end.date()}"}

    bench = {}
    for symbol, df in data.items():
        close = df["Close"].to_numpy(dtype="float64")
        bpy = vector.bars_per_year(df.index)
        bench[symbol] = {
            "net": vector.net_returns(np.ones(len(df), dtype="float64"), close,
                                      FREE_FEE, bpy),
            "bpy": bpy, "close": close,
        }

    masks = {s: wfmod.fold_masks(df.index, folds) for s, df in data.items()}
    union = {s: np.logical_or.reduce([m[1] for m in ms if m is not None])
             for s, ms in masks.items() if any(m is not None for m in ms)}
    if not union:
        return {}, {"skipped": "no scoreable folds"}

    labels = cells(asset_class)
    fee_grid = scenarios(asset_class)

    fold_rows, union_rows = [], []
    for label in tqdm(labels + [BASELINE, *CONTROLS],
                      desc=f"strat {asset_class}/{timeframe}"):
        for symbol, df in data.items():
            if symbol not in union:
                continue
            b = bench[symbol]
            u = union[symbol]
            # One draw for everything except the random controls, whose score is the
            # mean over independent draws — see RANDOM_DRAWS.
            n_draws = RANDOM_DRAWS if label.startswith("RANDOM_") else 1
            positions = [build(label, df, b["close"], b["bpy"], symbol, d)
                         for d in range(n_draws)]
            positions = [p for p in positions if p is not None]
            if not positions:
                continue
            diag = {k: float(np.mean([_diagnostics(p, u, b["bpy"])[k]
                                      for p in positions]))
                    for k in ("long_frac", "exposure", "turnover_yr")}
            # The baseline is the thing being beaten; charging it would make it a
            # different strategy and would flatter every rule measured against it.
            fees = [FREE_FEE] if label == BASELINE else fee_grid
            for fee in fees:
                nets = [vector.net_returns(p, b["close"], fee, b["bpy"])
                        for p in positions]

                def mean_ir(mask, _nets=nets, _b=b):
                    """Mean IR across draws. All-NaN is a real outcome, not an error.

                    An always-long rule's IR against buy-and-hold is 0/0 on every draw,
                    and `np.nanmean` of an all-NaN list warns about an empty slice. The
                    answer is NaN either way; the guard just stops it being reported as
                    a numerical problem when it is a definitional one.
                    """
                    vals = [wfmod._ir(n, _b["net"], mask, _b["bpy"]) for n in _nets]
                    vals = [v for v in vals if np.isfinite(v)]
                    return float(np.mean(vals)) if vals else float("nan")
                union_rows.append({
                    "rule": label, "symbol": symbol, "scenario": fee["key"],
                    "ir_wf": mean_ir(u),
                    "ret_pct": float(np.mean([wfmod._total_return_pct(n, u)
                                              for n in nets])),
                    "bench_pct": wfmod._total_return_pct(b["net"], u),
                    "years_oos": float(u.sum() / b["bpy"]) if b["bpy"] > 0 else np.nan,
                    "n_folds": len(folds), "n_switches": 0, **diag,
                })
                for f, m in zip(folds, masks[symbol]):
                    if m is None:
                        continue
                    fold_rows.append((label, symbol, fee["key"], f.index,
                                      mean_ir(m[0]), mean_ir(m[1])))

    fold_table = pd.DataFrame(
        fold_rows, columns=["rule", "symbol", "scenario", "fold", "ir_is", "ir_oos"])
    fixed = pd.DataFrame(union_rows)
    if fold_table.empty:
        return {}, {"skipped": "no scoreable folds"}

    def position_fn(label, symbol, df):
        b = bench[symbol]
        return build(label, df, b["close"], b["bpy"], symbol)

    # Candidates exclude both controls: letting the selector pick buy-and-hold answers a
    # different question, and answers it trivially.
    candidates = set(labels)
    jobs = [("IS#1", candidates)]
    for name in CATALOG:
        family_cells = {c for c in labels if decode(c)[0] == name}
        # A single-cell strategy has nothing to re-optimise; emitting WFO[x] for it would
        # print a duplicate of x wearing a label that implies fitting happened.
        if len(family_cells) >= 2:
            jobs.append((f"WFO[{name}]", family_cells))

    stitched, diags = [], []
    for label, pool in jobs:
        picks = wfmod.pick_champions(fold_table, pool)
        if picks.empty:
            continue
        out = wfmod.stitch(data, asset_class, timeframe, folds, bench, picks, label,
                           position_fn=position_fn)
        if out.empty:
            continue
        stitched.append(out)
        diags.append(_stitched_diagnostics(data, folds, masks, picks, bench, label))

    wf = pd.concat([s for s in stitched if not s.empty], ignore_index=True)
    if diags:
        wf = wf.merge(pd.concat(diags, ignore_index=True),
                      on=["rule", "symbol", "scenario"], how="left")
    allrows = pd.concat([wf, fixed], ignore_index=True)
    ir_by_scen = allrows.groupby(["rule", "scenario"])["ir_wf"].mean().unstack()

    label_set = set(labels)
    out = []
    for (label, scen), grp in allrows.groupby(["rule", "scenario"]):
        mode = ("is1_selection" if label == "IS#1"
                else "wfo" if label.startswith("WFO[")
                else "control" if label in (BASELINE, *CONTROLS)
                else "published" if SEP not in label else "grid_cell")
        row = wfmod.leaderboard_row(grp, label, mode, asset_class, timeframe, scen,
                                    ir_by_scen.loc[label] if label in ir_by_scen.index
                                    else pd.Series(dtype=float))
        if mode == "wfo":
            base = label[4:-1]
        elif label in label_set:
            base = decode(label)[0]
        else:
            base = ""
        row["strategy"] = base
        row["family"] = CATALOG[base].family if base in CATALOG else mode
        row["source"] = CATALOG[base].source if base in CATALOG else ""
        row["anchor"] = bool(base in CATALOG and CATALOG[base].anchor)
        row.update(excess_cagr(grp))
        for col in ("long_frac", "exposure", "turnover_yr"):
            row[col] = float(grp[col].mean()) if col in grp else np.nan
        row["rankable"] = metrics.rankable(row, MIN_IR_COVERAGE)
        out.append(row)
    summary = pd.DataFrame(out).sort_values(["scenario", "ir_net"],
                                            ascending=[True, False])
    summary = add_exposure_adjusted(summary)

    headline = HEADLINE_SCENARIO[asset_class]
    years = float(fixed["years_oos"].median())
    n_published = len([c for c in labels if SEP not in c])
    meta = {
        "class": asset_class, "timeframe": timeframe, "n_assets": len(union),
        "n_strategies": n_published, "n_cells": len(labels),
        "n_skipped": len(skipped_for(asset_class)),
        "skipped": ";".join(skipped_for(asset_class)),
        "n_folds": len(folds), "years_oos": years,
        "ceiling_published": metrics.noise_ceiling(n_published, years),
        "ceiling_cells": metrics.noise_ceiling(len(labels), years),
        "se_ir": metrics.se_ir(years),
        "oos_start": str(folds[0].is_end.date()),
        "oos_end": str(folds[-1].oos_end.date()),
        "ranking_stability_spearman": wfmod.ranking_stability(
            fold_table.groupby(["rule", "scenario", "fold"])[["ir_is", "ir_oos"]]
            .mean().reset_index(), headline),
    }
    tables = {"summary": summary, "folds": fold_table, "per_asset": fixed,
              "schedule": wfmod.pick_champions(fold_table, candidates)}
    return tables, meta


def report(tables: dict, meta: dict) -> None:
    s = tables["summary"]
    scen = HEADLINE_SCENARIO[meta["class"]]
    h = s[(s.scenario == scen) & s.rankable]
    pub = h[h.wf_mode == "published"]
    wfo = h[h.wf_mode == "wfo"]
    is1 = h[h.wf_mode == "is1_selection"]

    print(f"\n=== {meta['class']}_{meta['timeframe']} ({meta['seconds']:.0f}s) ===")
    print(f"  {meta['n_strategies']} published strategies / {meta['n_cells']} grid cells"
          f" | {meta['n_folds']} folds | OOS {meta['oos_start']} -> {meta['oos_end']}"
          f" ({meta['years_oos']:.1f}y median)")
    if meta["n_skipped"]:
        print(f"  skipped on this class: {meta['skipped']}")
    print(f"  noise ceiling: IR {meta['ceiling_published']:+.3f} at "
          f"{meta['n_strategies']} published, {meta['ceiling_cells']:+.3f} at "
          f"{meta['n_cells']} cells  (SE {meta['se_ir']:.3f})")
    print(f"  ranking stability (consecutive-fold Spearman): "
          f"{meta['ranking_stability_spearman']:.3f}")

    if not is1.empty:
        r = is1.iloc[0]
        print(f"\n  IS#1 (the strategy you could actually have picked): "
              f"IR {r['ir_net']:+.3f}  breadth {r['ir_hit_rate']:.0%}  "
              f"t {r['t_stat']:+.2f}  {r['gates_passed']}/4 gates  "
              f"long {r['long_frac']:.0%}  {r['n_switches']:.0f} switches"
              f"  | vs random at that exposure {r['ir_vs_random']:+.3f}")

    print("\n  published parameters, best 8 by raw IR:")
    for r in pub.nlargest(8, "ir_net").itertuples():
        mark = " [anchor]" if r.anchor else ""
        verdict = "ABOVE" if r.ir_net > meta["ceiling_published"] else "below"
        print(f"    {r.rule:<18} IR {r.ir_net:+.3f}  breadth {r.ir_hit_rate:>4.0%}  "
              f"t {r.t_stat:+.2f}  head {r.headroom:>5.2f}  long {r.long_frac:>4.0%}  "
              f"{r.gates_passed}/4  {verdict} ceiling{mark}")

    # The ordering that means something. Raw IR ranks by time-in-market; this ranks by
    # what is left after the exposure-matched control curve is subtracted.
    print("\n  best 8 by IR vs an exposure-matched random control:")
    for r in pub.nlargest(8, "ir_vs_random").itertuples():
        mark = " [anchor]" if r.anchor else ""
        print(f"    {r.rule:<18} {r.ir_vs_random:+.3f}   "
              f"(IR {r.ir_net:+.3f} vs random {r.ir_random:+.3f} at long "
              f"{r.long_frac:.0%}){mark}")

    if not wfo.empty and not pub.empty:
        base_ir = pub.set_index("rule")["ir_net"]
        deltas = []
        for r in wfo.itertuples():
            b = base_ir.get(r.strategy, np.nan)
            if np.isfinite(b):
                deltas.append((r.strategy, r.ir_net, b, r.ir_net - b))
        if deltas:
            helped = sum(1 for _, _, _, d in deltas if d > 0)
            print(f"\n  walk-forward optimisation vs published parameters "
                  f"(helped {helped} of {len(deltas)}):")
            for name, w, b, d in sorted(deltas, key=lambda x: -x[3])[:8]:
                print(f"    WFO[{name}]{'':<{max(0, 12 - len(name))}} {w:+.3f}   "
                      f"published {b:+.3f}   delta {d:+.3f}")
            print(f"    mean delta {np.mean([d for *_, d in deltas]):+.3f}")

    # Not filtered to the headline scenario: BUYHOLD is never charged, so it exists only
    # under `gross` and would vanish from a headline-only view of its own leaderboard.
    ctrl = s[(s.wf_mode == "control") & (s.scenario.isin([scen, "gross"]))]
    if not ctrl.empty:
        print("\n  controls — what NO signal at this much market exposure scores:")
        for r in ctrl.sort_values("long_frac").itertuples():
            ir = "    nan" if not np.isfinite(r.ir_net) else f"{r.ir_net:+.3f}"
            print(f"    {r.rule:<12} @{r.scenario:<7} long {r.long_frac:>4.0%}  IR {ir}")
        if not pub.empty:
            beat = pub[pub["ir_vs_random"] > 0]
            print(f"    -> {len(beat)} of {len(pub)} published strategies beat a random "
                  f"control at their OWN exposure. The rest carry no information that "
                  f"time-in-market does not already explain.")

    # IR is arithmetic and blind to variance drag; compounding is not. On leveraged ETFs
    # the two disagree, so both are printed and neither is allowed to stand alone.
    print("\n  best 8 by compounded excess CAGR vs buy-and-hold "
          "(min across assets in brackets):")
    for r in pub.nlargest(8, "excess_cagr").itertuples():
        flag = "  <- all assets" if r.excess_cagr_min > 0 else ""
        print(f"    {r.rule:<18} {r.excess_cagr:>+7.1%}  [worst asset "
              f"{r.excess_cagr_min:>+7.1%}]  {r.excess_cagr_hit:>4.0%} of assets"
              f"{flag}")

    body = h[h.wf_mode != "control"]
    gate_counts = {g: int(body[f"gate_{g}"].sum())
                   for g in ("ir", "breadth", "headroom", "t")}
    print(f"\n  rows passing each gate: {gate_counts}")
    print(f"  cleared ALL FOUR: {int((body.gates_passed == 4).sum())} of {len(body)}")


def print_catalog() -> None:
    print(f"{len(CATALOG)} strategies, "
          f"{sum(len(s.grid) for s in CATALOG.values())} grid cells\n")
    for fam in ("trend", "reversion", "volatility", "calendar", "regime"):
        print(f"--- {fam} ---")
        for name, s in CATALOG.items():
            if s.family != fam:
                continue
            mark = " [anchor]" if s.anchor else ""
            only = f" [{'/'.join(s.classes)} only]" if s.classes else ""
            print(f"  {name}{mark}{only}\n      {s.rule}\n      {s.source}"
                  f"\n      {len(s.grid)} cells, published: {s.published}")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", choices=list(TIMEFRAMES),
                    default=list(DEFAULT_TIMEFRAMES))
    ap.add_argument("--list", action="store_true", help="print the catalog and exit")
    args = ap.parse_args()

    if args.list:
        print_catalog()
        return

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    metas = []
    for asset_class in args.classes:
        for timeframe in args.timeframes:
            t0 = time.time()
            tables, meta = run_pair(asset_class, timeframe)
            if not tables:
                print(f"{asset_class}/{timeframe}: "
                      f"{meta.get('skipped', 'no cached data')}, skipped")
                continue
            tag = f"{asset_class}_{timeframe}"
            for key, name in (("summary", "strat_summary"), ("folds", "strat_folds"),
                              ("per_asset", "strat_per_asset"),
                              ("schedule", "strat_schedule")):
                tables[key].to_csv(RESULTS_DIR / f"{name}_{tag}.csv", index=False)
            meta["seconds"] = time.time() - t0
            metas.append(meta)
            report(tables, meta)

    if metas:
        # Merged, not overwritten: this file indexes every sheet, and a `--tf 1d` run
        # must not erase the 4h rows it never touched.
        fresh = pd.DataFrame(metas)
        path = RESULTS_DIR / "strat_meta.csv"
        if path.exists():
            old = pd.read_csv(path)
            keys = set(zip(fresh["class"], fresh["timeframe"]))
            old = old[[k not in keys for k in zip(old["class"], old["timeframe"])]]
            fresh = pd.concat([old, fresh], ignore_index=True)
        fresh.sort_values(["class", "timeframe"]).to_csv(path, index=False)
        print(f"\nwrote {RESULTS_DIR}")


if __name__ == "__main__":
    main()
