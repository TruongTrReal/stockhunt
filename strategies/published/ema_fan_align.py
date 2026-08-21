"""Long while a seven-deep EMA fan is stacked bullish and still widening."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D, _cross_over


RULE = ('Long while a seven-deep EMA fan is stacked bullish and still widening; exit '
        'when price crosses back under the mid-fan line.')
SOURCE = "ichiV1 lineage (freqtrade); this is the 'tesla' entry of 'ichiV1_Marius', 5m"
FAMILY = 'trend'
ANCHOR = False
CLASSES = ('us_stocks', 'us_etfs')
NOTE = ('RESTRICTED TO THE TWO CLASSES THAT HAVE VOLUME: the MFI term is a hard gate and '
        'the vendor serves no volume for crypto or commodities, so there this rule would '
        'never trade at all — which on a leaderboard is indistinguishable from a rule '
        'that trades and earns nothing. `registry.skipped_for` counts it instead. '
        'Otherwise a PARTIAL conversion, and the parts left out are named so nobody quotes this as '
        'the original. THREE conditions could not travel: a guard on BTC\'s own 1d-vs-5m '
        'move (cross-asset — the strategy layer here sees one symbol at a time), a '
        'pump/dump guard built from a 15-minute informative frame, and `rsi > rsi_1h`, '
        'which is approximated by comparing a fast RSI to a slow one on the native '
        'frame. The first two are protections and only reduce the trade count; the third '
        'is a signal term and this is an analogue of it, not the same series. The '
        'Ichimoku cloud the parent strategy computes reaches no condition in the tesla '
        'branch and is not implemented — `ichimoku` already covers that mechanism.')

LOGIC = """
What it measures
    An EMA fan at 1, 3, 6, 12, 24, 48, 72 and 96 bars, computed on the close and — for
    two of the terms — on a Heikin-Ashi open. The rule wants six specific pairwise
    orderings to hold at once, plus `fan_magnitude` (the 12-bar EMA over the 96-bar EMA)
    above 0.99 and RISING versus the previous bar, plus MFI under 70.

The claim
    A fan whose every rung is in order and whose spread is still widening is a trend in
    its acceleration phase. Requiring the ratio to increase bar over bar is the part
    that distinguishes this from a static alignment filter: it demands the trend be
    getting stronger, not merely be present.

What the position is really exposed to
    Trend, long-only, and heavily conditioned — eight simultaneous conditions on one
    price series. It exits on a single fast/slow cross, so the exit is far looser than
    the entry, which makes the holding period much longer than the entry's fussiness
    suggests.

How it fails
    Every term is an EMA of the same closes, so "eight conditions" is closer to one
    condition measured eight ways; the conjunction cuts the trade count without adding
    eight independent filters' worth of information. `fan_magnitude_gain >= 1.0022` is
    the term that does the real work, and it is a momentum-of-momentum test, which is
    the noisiest thing on the sheet.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'fan_gain': 1.0022, 'mfi_max': 70.0, 'exit_days': 24.0, 'rsi_slow_days': 168.0},
        {'fan_gain': 1.008, 'mfi_max': 70.0, 'exit_days': 24.0, 'rsi_slow_days': 168.0},
        {'fan_gain': 1.0, 'mfi_max': 70.0, 'exit_days': 24.0, 'rsi_slow_days': 168.0},
        {'fan_gain': 1.0022, 'mfi_max': 70.0, 'exit_days': 48.0, 'rsi_slow_days': 168.0},
)


def position(df, close, bpy, fan_gain=1.0022, mfi_max=70.0, exit_days=24.0,
             rsi_slow_days=168.0):
    """The tesla conjunction as an entry, the mid-fan cross as an exit.

    The fan lengths are RATIOS of the base bar in the source — 3x for "15m" on a 5m
    chart, 96x for "8h" — so they carry over as bar multiples, which on a daily sheet
    reads as 3 to 96 days. That is the same construction `_bars` gives every other rule
    here, not a special case.
    """
    open_ = df["Open"].to_numpy("float64")
    high = df["High"].to_numpy("float64")
    low = df["Low"].to_numpy("float64")
    volume = df["Volume"].to_numpy("float64")
    src = np.ascontiguousarray(close)

    # Heikin-Ashi open, which is what the source's `trend_open_*` fan is built on. It is
    # recursive, so it has to be walked; seeding on the first bar's own midpoint is the
    # qtpylib convention the strategy inherits.
    ha_close = (open_ + high + low + close) / 4.0
    ha_open = np.empty(len(close))
    ha_open[0] = (open_[0] + close[0]) / 2.0
    for i in range(1, len(close)):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0

    ema_c = lambda d: talib.EMA(src, _bars(bpy, d * D))
    ema_o = lambda d: talib.EMA(np.ascontiguousarray(ha_open), _bars(bpy, d * D))

    c15, c30 = ema_c(3.0), ema_c(6.0)
    c1h, c2h, c4h = ema_c(12.0), ema_c(24.0), ema_c(48.0)
    c6h, c8h = ema_c(72.0), ema_c(96.0)
    o15, o1h, o2h = ema_o(3.0), ema_o(12.0), ema_o(24.0)

    fan = np.divide(c1h, c8h, out=np.full(len(close), np.nan), where=np.isfinite(c8h) & (c8h != 0))
    gain = np.full(len(close), np.nan)
    gain[1:] = np.divide(fan[1:], fan[:-1], out=np.full(len(close) - 1, np.nan),
                         where=np.isfinite(fan[:-1]) & (fan[:-1] != 0))

    rsi_fast = talib.RSI(src, _bars(bpy, 14 * D))
    rsi_slow = talib.RSI(src, _bars(bpy, rsi_slow_days * D))
    mfi = talib.MFI(np.ascontiguousarray(high), np.ascontiguousarray(low), src,
                    np.ascontiguousarray(volume), timeperiod=_bars(bpy, 14 * D))

    entry = ((rsi_fast > rsi_slow) & (c8h > c6h) & (c15 > c30) & (ha_open > o15)
             & (c1h > ema_c(55.0)) & (ema_c(21.0) > c4h) & (o1h > o2h)
             & (mfi < mfi_max) & (gain >= fan_gain) & (fan > 0.99))
    exit_ = _cross_over(ema_c(exit_days), close)      # crossed_below(close, trend_2h)

    entry = np.nan_to_num(entry).astype(bool)
    out = np.zeros(len(close))
    pos = 0.0
    for i in range(len(close)):
        if pos > 0 and exit_[i]:
            pos = 0.0
        elif pos == 0.0 and entry[i] and not exit_[i]:
            pos = 1.0
        out[i] = pos
    return out
