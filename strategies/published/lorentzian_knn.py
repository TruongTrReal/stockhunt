"""A k-nearest-neighbour vote over five oscillators, under a Lorentzian distance."""

from __future__ import annotations

import numpy as np
import pandas as pd
import talib

from strategies._indicators import _bars, D, _flip


RULE = ('A k-nearest-neighbour vote over five normalised oscillators under a Lorentzian '
        'distance, gated by a volatility and a trend-regime filter and a kernel slope.')
SOURCE = "jdehorty, 'Machine Learning: Lorentzian Classification' (Pine v5); packaged as 'SH-Machine v1'"
FAMILY = 'other'
ANCHOR = False
CLASSES = ('us_stocks', 'us_etfs', 'crypto', 'commodities', 'cme_futures')
NOTE = ('Converted from Pine, including its two external libraries (MLExtensions, '
        'KernelFunctions). THREE properties of the original are reproduced deliberately '
        'and none of them is a transcription slip. (1) The training LABEL is backward '
        'looking: `close[4] < close[0] ? short : long` stores, at bar t, the direction of '
        'the move that ENDED at t, and pairs it with bar t\'s features — so the model is '
        'fitted to describe the past four bars, not to predict the next four, and the '
        'sign makes it a reversion classifier. (2) The neighbour search only ever scans '
        'the OLDEST `max_bars_back` bars, because `sizeLoop` is capped at that count from '
        'index 0 — the training set is frozen early and never rolls forward. (3) The '
        'neighbour buffer is a `var` array and PERSISTS across bars; only the distance '
        'threshold resets. The one thing NOT reproduced is `bar_index >= last_bar_index - '
        'maxBarsBack`, which gates computation on where the chart happens to end and is '
        'not causal; predictions are computed on every bar instead.')

LOGIC = """
What it measures
    Five oscillators, each rescaled to 0-1 — RSI(14), a wave-trend of hlc3, CCI(20), a
    hand-rolled ADX(20) and RSI(9). For each bar it finds the historical bars whose
    five-vector is closest under a Lorentzian metric, `sum log(1 + |dx|)`, and sums the
    stored labels of those neighbours. Positive sum means long, negative means short.

The claim
    Price-feature space is warped by volatility events the way spacetime is warped by
    mass, so a log-scaled distance is more robust than Euclidean to the outlier bars
    that dominate a squared metric. The chronological skip (`i % 4`) and the running
    distance threshold together spread the neighbours across history rather than letting
    them clump in one regime.

What the position is really exposed to
    Short-horizon reversion, once the label misalignment in NOTE is taken into account —
    a bar whose oscillators resemble bars that had just risen is labelled short. It
    reverses rather than going flat, so it is always in the market after the first
    signal. The two filters (recent ATR above historical ATR; kernel slope not in steep
    decline) cut the trade count without changing the direction of anything.

How it fails
    This is the most-forwarded "ML strategy" on TradingView and it is worth being
    precise about what it is. There is no fitting: no weights, no optimiser, no held-out
    set. It is a 5-nearest-neighbour lookup with k=8 over a frozen sample of at most
    2000 bars, and its label is backwards. What it will look like on a leaderboard is a
    noisy reversion rule with an unusually high trade count, and the burden on any good
    result here is to show it beats the same catalog's plain reversion rules — `ibs`
    and `rsi2_connors` are the right comparison, not buy-and-hold.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'neighbors': 8, 'max_bars_back': 2000, 'feature_count': 5, 'kernel_h': 8.0, 'kernel_r': 8.0, 'kernel_x': 25, 'regime_threshold': -0.1, 'use_filters': 1, 'allow_short': 1},
        {'neighbors': 8, 'max_bars_back': 2000, 'feature_count': 5, 'kernel_h': 8.0, 'kernel_r': 8.0, 'kernel_x': 25, 'regime_threshold': -0.1, 'use_filters': 0, 'allow_short': 1},
        {'neighbors': 4, 'max_bars_back': 2000, 'feature_count': 5, 'kernel_h': 8.0, 'kernel_r': 8.0, 'kernel_x': 25, 'regime_threshold': -0.1, 'use_filters': 1, 'allow_short': 1},
        {'neighbors': 8, 'max_bars_back': 2000, 'feature_count': 2, 'kernel_h': 8.0, 'kernel_r': 8.0, 'kernel_x': 25, 'regime_threshold': -0.1, 'use_filters': 1, 'allow_short': 1},
        {'neighbors': 8, 'max_bars_back': 2000, 'feature_count': 5, 'kernel_h': 8.0, 'kernel_r': 8.0, 'kernel_x': 25, 'regime_threshold': -0.1, 'use_filters': 1, 'allow_short': 0},
)


def _rescale(x: np.ndarray, old_lo: float, old_hi: float) -> np.ndarray:
    """MLExtensions' `rescale` onto 0-1, with the library's own 1e-10 floor."""
    return (x - old_lo) / max(old_hi - old_lo, 1e-10)


def _normalize(x: np.ndarray) -> np.ndarray:
    """MLExtensions' `normalize`: rescale onto 0-1 by the min and max seen SO FAR.

    Expanding, not whole-series — the library seeds `var _historicMin = 10e10` and takes
    a running min, so bar t only ever sees bars <= t. That is the same distinction
    `_causal_median` exists to preserve, and it is the reason this feature set passes
    the truncation gate at all.
    """
    s = pd.Series(x)
    lo = s.cummin().to_numpy()
    hi = s.cummax().to_numpy()
    return (x - lo) / np.maximum(hi - lo, 1e-10)


def _n_wt(src: np.ndarray, n1: int, n2: int) -> np.ndarray:
    """MLExtensions' `n_wt` — LazyBear's wave trend, normalised."""
    src = np.ascontiguousarray(src)
    ema1 = talib.EMA(src, n1)
    ema2 = talib.EMA(np.ascontiguousarray(np.abs(src - ema1)), n1)
    ci = np.divide(src - ema1, 0.015 * ema2, out=np.zeros_like(src),
                   where=np.isfinite(ema2) & (ema2 != 0))
    wt1 = talib.EMA(np.ascontiguousarray(ci), n2)
    wt2 = talib.SMA(np.ascontiguousarray(wt1), 4)
    return _normalize(wt1 - wt2)


def _n_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    """MLExtensions' `n_adx`. Its own smoothing, not TA-Lib's, and the difference shows.

    The library uses a running `x - x/n + new` accumulator rather than Wilder's RMA for
    the three sums, which is a different filter with a different gain. Reproduced,
    because the published neighbour distances were computed against these numbers.
    """
    n_bars = len(close)
    prev_c = np.concatenate(([close[0]], close[:-1]))
    prev_h = np.concatenate(([high[0]], high[:-1]))
    prev_l = np.concatenate(([low[0]], low[:-1]))
    tr = np.maximum(np.maximum(high - low, np.abs(high - prev_c)), np.abs(low - prev_c))
    up_move = high - prev_h
    down_move = prev_l - low
    dm_plus = np.where(up_move > down_move, np.maximum(up_move, 0.0), 0.0)
    dm_minus = np.where(down_move > up_move, np.maximum(down_move, 0.0), 0.0)

    def _accum(x):
        out = np.empty(n_bars)
        s = 0.0
        for i in range(n_bars):
            s = s - s / n + x[i]
            out[i] = s
        return out

    tr_s, dp_s, dn_s = _accum(tr), _accum(dm_plus), _accum(dm_minus)
    di_p = np.divide(dp_s, tr_s, out=np.zeros(n_bars), where=tr_s != 0) * 100.0
    di_n = np.divide(dn_s, tr_s, out=np.zeros(n_bars), where=tr_s != 0) * 100.0
    tot = di_p + di_n
    dx = np.divide(np.abs(di_p - di_n), tot, out=np.zeros(n_bars), where=tot != 0) * 100.0
    # `ta.rma` is Wilder's, which is TA-Lib's EMA at alpha = 1/n.
    adx = pd.Series(dx).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()
    return _rescale(adx, 0.0, 100.0)


def _kernel(src: np.ndarray, lookback: float, weight: float, start: int) -> np.ndarray:
    """KernelFunctions' `rationalQuadratic`.

    The published loop runs `for i = 0 to _size + startAtBar` where `_size` is
    `array.size(array.from(_src))` — the size of a one-element array, so 1. The window
    is therefore `startAtBar + 2` bars, not the whole history, and the weights are
    constant. That is what everyone running this indicator is actually getting.
    """
    taps = int(start) + 2
    i = np.arange(taps)
    w = np.power(1.0 + (i ** 2) / (lookback ** 2 * 2.0 * weight), -weight)
    w = w / w.sum()
    return np.convolve(src, w, mode="full")[:len(src)] / np.concatenate(
        (np.cumsum(w)[:min(taps, len(src))], np.ones(max(0, len(src) - taps))))


def _regime_ok(df, close: np.ndarray, threshold: float) -> np.ndarray:
    """MLExtensions' `regime_filter`: a Kalman-ish line whose slope is not collapsing."""
    high = df["High"].to_numpy("float64")
    low = df["Low"].to_numpy("float64")
    src = (df["Open"].to_numpy("float64") + high + low + close) / 4.0
    n = len(close)
    klmf = np.empty(n)
    v1 = v2 = 0.0
    k = 0.0
    for i in range(n):
        v1 = 0.2 * (src[i] - (src[i - 1] if i else src[i])) + 0.8 * v1
        v2 = 0.1 * (high[i] - low[i]) + 0.8 * v2
        omega = abs(v1 / v2) if v2 != 0 else 0.0
        alpha = (-omega ** 2 + np.sqrt(omega ** 4 + 16.0 * omega ** 2)) / 8.0
        k = alpha * src[i] + (1.0 - alpha) * k if i else src[i]
        klmf[i] = k
    slope = np.abs(np.diff(klmf, prepend=klmf[0]))
    avg = talib.EMA(np.ascontiguousarray(slope), 200)
    decline = np.divide(slope - avg, avg, out=np.full(n, np.nan),
                        where=np.isfinite(avg) & (avg != 0))
    return np.nan_to_num(decline, nan=threshold) >= threshold


def _predict(feats: np.ndarray, labels: np.ndarray, neighbors: int,
             max_bars_back: int) -> np.ndarray:
    """The neighbour loop, bar by bar, with the source's persistent buffer.

    Vectorised where it can be and walked where it cannot. The distance to every
    candidate is one numpy expression; the acceptance scan is sequential because
    `lastDistance` is written inside it, but it can jump straight to the next candidate
    that clears the threshold instead of testing every index — which turns an inner loop
    of `max_bars_back` per bar into one of roughly the number of accepted neighbours.
    """
    n_bars, n_feat = feats.shape
    cap = max(1, int(max_bars_back) - 1)
    # `i % 4` is the source's chronological skip: index 0, 4, 8 ... are never neighbours.
    eligible = (np.arange(cap + 1) % 4) != 0
    drop_at = int(round(neighbors * 3 / 4))

    out = np.zeros(n_bars)
    dist_buf: list[float] = []
    pred_buf: list[float] = []
    for t in range(n_bars):
        limit = min(cap, t)
        cand = feats[:limit + 1]
        d = np.log1p(np.abs(feats[t] - cand)).sum(axis=1)
        ok = eligible[:limit + 1]

        last = -1.0
        pos = 0
        while pos <= limit:
            hit = ok[pos:] & (d[pos:] >= last)
            if not hit.any():
                break
            i = pos + int(np.argmax(hit))
            last = d[i]
            dist_buf.append(d[i])
            pred_buf.append(labels[i])
            if len(pred_buf) > neighbors:
                last = dist_buf[drop_at]
                dist_buf.pop(0)
                pred_buf.pop(0)
            pos = i + 1
        out[t] = float(np.sum(pred_buf))
    return out


def position(df, close, bpy, neighbors=8, max_bars_back=2000, feature_count=5,
             kernel_h=8.0, kernel_r=8.0, kernel_x=25, regime_threshold=-0.1,
             use_filters=1, allow_short=1):
    """Five features, a neighbour vote, two filters and a kernel slope.

    `max_bars_back` stays a BAR COUNT rather than a calendar span: it is the size of the
    training sample, and rescaling it by `bpy` would change how much evidence the model
    has rather than how far back it reaches.
    """
    high = df["High"].to_numpy("float64")
    low = df["Low"].to_numpy("float64")
    src = np.ascontiguousarray(close)
    hlc3 = np.ascontiguousarray((high + low + close) / 3.0)

    # ta.ema(x, 1) is x, which is what paramB = 1 means on three of these five.
    f1 = _rescale(talib.RSI(src, _bars(bpy, 14 * D)), 0.0, 100.0)
    f2 = _n_wt(hlc3, _bars(bpy, 10 * D), _bars(bpy, 11 * D))
    f3 = _normalize(talib.CCI(np.ascontiguousarray(high), np.ascontiguousarray(low),
                              src, timeperiod=_bars(bpy, 20 * D)))
    f4 = _n_adx(high, low, close, _bars(bpy, 20 * D))
    f5 = _rescale(talib.RSI(src, _bars(bpy, 9 * D)), 0.0, 100.0)
    feats = np.nan_to_num(np.column_stack([f1, f2, f3, f4, f5])[:, :int(feature_count)],
                          nan=0.0, posinf=0.0, neginf=0.0)

    n = len(close)
    labels = np.zeros(n)
    labels[4:] = np.where(close[:-4] < close[4:], -1.0,
                          np.where(close[:-4] > close[4:], 1.0, 0.0))

    prediction = _predict(feats, labels, int(neighbors), int(max_bars_back))

    if use_filters:
        atr_fast = talib.ATR(np.ascontiguousarray(high), np.ascontiguousarray(low), src, 1)
        atr_slow = talib.ATR(np.ascontiguousarray(high), np.ascontiguousarray(low), src, 10)
        vol_ok = np.nan_to_num(atr_fast, nan=0.0) > np.nan_to_num(atr_slow, nan=0.0)
        passes = vol_ok & _regime_ok(df, close, regime_threshold)
    else:
        passes = np.ones(n, dtype=bool)

    # signal := prediction > 0 and filters ? long : prediction < 0 and filters ? short
    #                                              : nz(signal[1])
    raw = np.where(passes & (prediction > 0), 1.0,
                   np.where(passes & (prediction < 0), -1.0, np.nan))
    signal = pd.Series(raw).ffill().fillna(0.0).to_numpy()
    changed = np.zeros(n, dtype=bool)
    changed[1:] = signal[1:] != signal[:-1]

    yhat = _kernel(close, kernel_h, kernel_r, int(kernel_x))
    bullish = np.zeros(n, dtype=bool)
    bearish = np.zeros(n, dtype=bool)
    bullish[1:] = yhat[:-1] < yhat[1:]
    bearish[1:] = yhat[:-1] > yhat[1:]

    pos = _flip(changed & (signal == 1.0) & bullish,
                changed & (signal == -1.0) & bearish)
    return pos if allow_short else np.maximum(pos, 0.0)
