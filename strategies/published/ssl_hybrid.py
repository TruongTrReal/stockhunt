"""Long when an SSL channel cross, a Keltner baseline and two QQE lines all agree."""

from __future__ import annotations

import numpy as np
import pandas as pd
import talib

from strategies._indicators import _bars, D, _cross_over, _flip, _hma


RULE = ('Long when an SSL channel cross, a Keltner baseline break and two QQE trend '
        'lines all agree; short on the mirror.')
SOURCE = "Mihkel00, 'SSL Hybrid' + QQE MOD (Mango2Juice); packaged as 'SH-SSL-HB v4' (Pine v5)"
FAMILY = 'trend'
ANCHOR = False
CLASSES = None
NOTE = ('Converted from Pine. TWO defects of the source are reproduced rather than '
        'fixed, because fixing either makes this a different rule than the one that was '
        'published: (1) the SHORT leg tests `close < upperk`, the UPPER Keltner band, '
        'where the symmetric rule would use the lower one — so the short side has a much '
        'weaker location filter than the long side; (2) the script computes an SSL2/JMA '
        'continuation line and an ATR criterion that reach no condition at all. Only the '
        'four terms that actually gate an order are implemented.')

LOGIC = """
What it measures
    Four agreeing filters, three of them trend and one of them location.
      * SSL exit channel: an HMA of highs and an HMA of lows, with a latch that picks
        whichever side price last closed through. Crossing it is the trigger.
      * Keltner baseline: a 150-bar linear-regression line plus 0.2 x EMA(true range).
        Price must be outside it, which is the "not in the middle of the range" test.
      * QQE #1: a smoothed RSI with a Wilder-style volatility trailing line, then
        Bollinger bands on that line. The RSI must be outside its own band.
      * QQE #2: the same construction with a faster factor, used only for its sign.

The claim
    Each filter alone is noisy; the conjunction of four is meant to fire rarely and be
    right when it does. This is the "confluence" school of TradingView scripting, and
    the honest question it raises is whether four correlated trend filters are four
    pieces of evidence or one piece counted four times.

What the position is really exposed to
    Trend, both directions, gated hard enough that it trades seldom. The four filters
    are all computed from the same close series, so their agreement is much less
    informative than four independent votes would be.

How it fails
    Two ways, and they pull opposite directions. The conjunction makes signals so rare
    that a daily sheet may not produce enough of them to say anything — the sample, not
    the edge, becomes the binding constraint. And because the trigger is a cross of a
    latching channel, the rule enters late by construction: the channel only flips after
    price has already closed through the opposite HMA.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'baseline_days': 150.0, 'exit_days': 25.0, 'kc_mult': 0.2, 'atr_days': 14.0, 'rsi_days': 6.0, 'smooth_days': 5.0, 'qqe1': 3.0, 'qqe2': 1.61, 'bb_days': 50.0, 'bb_mult': 0.35, 'allow_short': 1},
        {'baseline_days': 60.0, 'exit_days': 25.0, 'kc_mult': 0.2, 'atr_days': 14.0, 'rsi_days': 6.0, 'smooth_days': 5.0, 'qqe1': 3.0, 'qqe2': 1.61, 'bb_days': 50.0, 'bb_mult': 0.35, 'allow_short': 1},
        {'baseline_days': 150.0, 'exit_days': 10.0, 'kc_mult': 0.2, 'atr_days': 14.0, 'rsi_days': 6.0, 'smooth_days': 5.0, 'qqe1': 3.0, 'qqe2': 1.61, 'bb_days': 50.0, 'bb_mult': 0.35, 'allow_short': 1},
        {'baseline_days': 150.0, 'exit_days': 25.0, 'kc_mult': 0.5, 'atr_days': 14.0, 'rsi_days': 6.0, 'smooth_days': 5.0, 'qqe1': 3.0, 'qqe2': 1.61, 'bb_days': 50.0, 'bb_mult': 0.35, 'allow_short': 1},
        {'baseline_days': 150.0, 'exit_days': 25.0, 'kc_mult': 0.2, 'atr_days': 14.0, 'rsi_days': 6.0, 'smooth_days': 5.0, 'qqe1': 3.0, 'qqe2': 1.61, 'bb_days': 50.0, 'bb_mult': 0.35, 'allow_short': 0},
)


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Pine's `ta.tr(true)`: high-low on the first bar rather than na."""
    tr = talib.TRANGE(np.ascontiguousarray(high), np.ascontiguousarray(low),
                      np.ascontiguousarray(close))
    tr[0] = high[0] - low[0]
    return tr


def _qqe(src: np.ndarray, rsi_len: int, sf: int, factor: float):
    """QQE: a smoothed RSI and its Wilder-style volatility trailing line.

    Returns `(RsiMa, FastAtrRsiTL)`. The trailing line ratchets — it only tightens while
    the RSI stays on the same side of it — so the loop cannot be vectorised without
    changing what it computes.
    """
    wilders = max(2, rsi_len * 2 - 1)
    rsi = talib.RSI(np.ascontiguousarray(src), rsi_len)
    rsi_ma = talib.EMA(np.ascontiguousarray(rsi), sf)
    atr_rsi = np.abs(np.diff(rsi_ma, prepend=rsi_ma[0]))
    dar = talib.EMA(np.ascontiguousarray(
        talib.EMA(np.ascontiguousarray(atr_rsi), wilders)), wilders) * factor

    n = len(src)
    longband = np.zeros(n)
    shortband = np.zeros(n)
    trend = np.ones(n)
    lb = sb = 0.0
    tr_state = 1.0
    for i in range(n):
        idx = rsi_ma[i]
        d = dar[i]
        if not (np.isfinite(idx) and np.isfinite(d)):
            longband[i], shortband[i], trend[i] = lb, sb, tr_state
            continue
        prev_idx = rsi_ma[i - 1] if i else np.nan
        new_long = idx - d
        new_short = idx + d
        prev_lb, prev_sb = lb, sb
        lb = max(prev_lb, new_long) if (np.isfinite(prev_idx) and prev_idx > prev_lb
                                        and idx > prev_lb) else new_long
        sb = min(prev_sb, new_short) if (np.isfinite(prev_idx) and prev_idx < prev_sb
                                         and idx < prev_sb) else new_short
        # `ta.cross(RSIndex, shortband[1])` compares the index against the band as it
        # stood one bar ago, on this bar and on the last one.
        if i >= 2 and np.isfinite(prev_idx):
            up = (idx - shortband[i - 1]) * (prev_idx - shortband[i - 2])
            dn = (longband[i - 1] - idx) * (longband[i - 2] - prev_idx)
            if up < 0:
                tr_state = 1.0
            elif dn < 0:
                tr_state = -1.0
        longband[i], shortband[i], trend[i] = lb, sb, tr_state
    return rsi_ma, np.where(trend == 1.0, longband, shortband)


def _ssl_line(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    """The SSL channel: HMA of highs and of lows, latched to whichever side price broke."""
    ma_hi, ma_lo = _hma(high, n), _hma(low, n)
    out = np.full(len(close), np.nan)
    state = 0
    for i in range(len(close)):
        if np.isfinite(ma_hi[i]) and close[i] > ma_hi[i]:
            state = 1
        elif np.isfinite(ma_lo[i]) and close[i] < ma_lo[i]:
            state = -1
        out[i] = ma_hi[i] if state < 0 else ma_lo[i]
    return out


def position(df, close, bpy, baseline_days=150.0, exit_days=25.0, kc_mult=0.2,
             atr_days=14.0, rsi_days=6.0, smooth_days=5.0, qqe1=3.0, qqe2=1.61,
             bb_days=50.0, bb_mult=0.35, allow_short=1):
    """All four filters must agree; the position reverses on the opposing signal.

    `allow_short=0` gives the long/flat version. On this project's metric that
    switch is the single largest term in the result, not a cosmetic variant: a
    reversing rule is charged the benchmark's drift twice over every downtrend,
    and the two versions are different strategies. Same convention as
    `supertrend` / `supertrend_lf`.
    """
    high = df["High"].to_numpy("float64")
    low = df["Low"].to_numpy("float64")
    src = np.ascontiguousarray(close)

    n_base = _bars(bpy, baseline_days * D)
    tr = _true_range(high, low, close)
    baseline = talib.LINEARREG(src, n_base)                 # the source's LSMA baseline
    upperk = baseline + talib.EMA(np.ascontiguousarray(tr), n_base) * kc_mult

    ssl_exit = _ssl_line(high, low, close, _bars(bpy, exit_days * D))
    cross_long = _cross_over(close, ssl_exit)
    cross_short = _cross_over(ssl_exit, close)

    n_rsi, n_sf = _bars(bpy, rsi_days * D), _bars(bpy, smooth_days * D)
    rsi_ma, tl1 = _qqe(close, n_rsi, n_sf, qqe1)
    _, tl2 = _qqe(close, n_rsi, n_sf, qqe2)

    n_bb = _bars(bpy, bb_days * D)
    centred = pd.Series(tl1 - 50.0)
    basis = centred.rolling(n_bb).mean().to_numpy()
    dev = bb_mult * centred.rolling(n_bb).std(ddof=1).to_numpy()

    osc = rsi_ma - 50.0
    buy = cross_long & (close > upperk) & (osc > basis + dev) & (tl2 - 50.0 > 0)
    # `close < upperk`, not `lowerk` — see NOTE. The source is asymmetric here and the
    # asymmetry is most of the difference between its two legs.
    sell = cross_short & (close < upperk) & (osc < basis - dev) & (tl2 - 50.0 < 0)
    pos = _flip(np.nan_to_num(buy).astype(bool), np.nan_to_num(sell).astype(bool))
    return pos if allow_short else np.maximum(pos, 0.0)
