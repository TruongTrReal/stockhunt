"""Buy a pullback inside an intact 50/200 uptrend; exit under SMA50 minus half an ATR."""

from __future__ import annotations

import numpy as np
import talib

from strategies._indicators import _bars, D


RULE = ('Long when SMA50 is over SMA200, price is over SMA50 and RSI(2) is under 40; '
        'exit under SMA50 - 0.5*ATR14, on a 20% stop, or when price loses its own '
        'SMA200.')
SOURCE = ("StrategyQuant 'SP100 Momentum' (AlgoCloud Stockpicker template), "
          'via a NautilusTrader port')
FAMILY = 'reversion'
ANCHOR = False
CLASSES = None
NOTE = (
    'Converted from a CROSS-SECTIONAL Stockpicker, and three of the four things that '
    'made it one do not fit `fn(df, close, bpy) -> exposure`. DROPPED: the position '
    'score, `ROC(close,20)[1]`, which ranks the candidates and takes the best ones '
    'first; the `max_positions=10` cap those ranks compete for; and risk-percent '
    'sizing, which sets the stake from the distance to the stop. All three exist only '
    'once you can see the whole panel or the account, so what is left here is the '
    'ENTRY AND EXIT CONDITION of one symbol, taken every time it fires. SUBSTITUTED: '
    "the source's market filter is SPY's own close against SPY's 200-bar SMA, a "
    'cross-asset read with nowhere to come from here; this uses the SYMBOL\'s own '
    'close against its own SMA200 instead, on both the entry gate and the exit. '
    'MEASURED, AND IT IS ALMOST INERT: on the ENTRY it is structurally implied -- '
    '`SMA50[1] > SMA200[1]` and `close > SMA50[1]` together already give '
    '`close > SMA200[1]` -- so it can only bite on the EXIT, in the narrow window where '
    'the fast average has crossed back under the slow one but price is still inside the '
    'ATR band. On the 1d probes that is 0 to 2 round trips in ~150 over 26 years, and on '
    'the 2026-08-26 sheets `regime=0` and `regime=1` rank within three places of each '
    'other on all eight. It is carried because it was registered as a trial and because '
    'a reader has to be able to see that the substitution bought nothing; it is NOT a '
    'stand-in for the market filter, which is simply gone. DEVIATION: the 20% stop is measured '
    'on the CLOSE where the source submits a stop-market order that fills intrabar. '
    "REPRODUCED: the source's `[1]` indexing, so SMA50, SMA200 and ATR14 are read as "
    'of the bar BEFORE the one that just closed while Close and RSI(2) are read on it. '
    'READ THE TIMEFRAME BEFORE READING A SCORE: this repo denominates windows in DAYS '
    "and the source's `2` is a BAR COUNT. On the 1d equity and ETF sheets the two "
    'readings coincide exactly -- 50/200/2/14, position agreement 100% -- so those '
    'numbers test the rule as written. They coincide NOWHERE ELSE: RSI(2) becomes '
    'RSI(3) on crypto 1d, RSI(4) on equity 4h and RSI(17) on crypto 4h, and against '
    '`chart:` (the bar-count reading) the 4h position agrees only 60-80% of the time '
    'with up to 6x the turnover. Quote a 4h cell as the day-denominated variant of this '
    'rule, never as this rule.')

LOGIC = """
What it measures
    Three conditions that have to hold at once. A 50-over-200 moving-average cross that
    is already in place, price still above the faster of the two, and a two-day RSI
    under 40 — a trend that is intact and a symbol that has just had a bad few days
    inside it. The exit is not the mirror of the entry: it is a band half an average
    true range under the same SMA50, so the rule tolerates a dip it would not buy.

The claim
    That the pullback inside an uptrend is the cheap moment to own it, and that a
    market-regime gate keeps you out of the case where every pullback keeps falling.
    It is the standard Stockpicker template: a slow trend filter, a fast oversold
    trigger, a wide disaster stop.

What the position is really exposed to
    Long-only, in cash most of the time, and heavily conditioned on the trend filter —
    the entry cannot fire at all while SMA50 is under SMA200, so the rule is structurally
    absent from every bear market it is measured over. That is most of what its
    drawdown will look like, and it is also why its exposure is the first column to
    read: a rule that is out of the market for the worst quarter of the sample scores
    well on any measure that does not price time-in-market. Rank it against
    `ir_vs_random`, not against buy-and-hold.

How it fails
    Two ways, and they are the two halves of the source that could not come across.
    The ranking is gone, so this takes EVERY signal rather than the ten best — the
    original is a concentrated book and this is a breadth rule wearing its conditions.
    And the market filter is gone with it: what stands in its place is the symbol's own
    200-bar average, which the entry conditions already imply, so nothing here is
    conditioned on the market at all. `regime=0` is in the grid to show that, and it
    does: the two cells rank within three places of each other on every sheet.

    What that leaves is a long-only pullback rule that is out of the market between 52%
    and 91% of the time. Read `ir_vs_random`, not `ir_net` — the raw information ratio
    on a rule this often flat is mostly a measure of the exposure, which is the whole
    reason the exposure-control column exists.

    And the 20% stop is very nearly dead code. Measured over ten symbols across all five
    classes at 1d and 4h it fires on exactly one of them — DOGE/USD, three times in its
    whole history — because the ATR band under SMA50 is far tighter than 20% and always
    reaches first. Whatever the source's risk control does for it there, it does nothing
    here; the exit is the band, alone. That also makes entry and exit mutually exclusive
    by construction (`close > SMA50[1]` cannot hold while `close < SMA50[1] - k·ATR[1]`,
    and the entry already implies `close > SMA200[1]`), so no bar both closes and opens
    a position and no round trip escapes the cost model.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'fast_days': 50.0, 'slow_days': 200.0, 'rsi_days': 2.0, 'rsi_entry': 40.0,
         'atr_days': 14.0, 'atr_mult': 0.5, 'stop': 0.20, 'regime': 1},
        {'fast_days': 50.0, 'slow_days': 200.0, 'rsi_days': 2.0, 'rsi_entry': 40.0,
         'atr_days': 14.0, 'atr_mult': 0.5, 'stop': 0.20, 'regime': 0},
        {'fast_days': 50.0, 'slow_days': 200.0, 'rsi_days': 2.0, 'rsi_entry': 30.0,
         'atr_days': 14.0, 'atr_mult': 0.5, 'stop': 0.20, 'regime': 1},
        {'fast_days': 50.0, 'slow_days': 200.0, 'rsi_days': 2.0, 'rsi_entry': 40.0,
         'atr_days': 14.0, 'atr_mult': 1.0, 'stop': 0.20, 'regime': 1},
)


def _lag1(x: np.ndarray) -> np.ndarray:
    """The source's `[1]`: this series as of the previous bar. Never the current one."""
    out = np.roll(x, 1)
    out[0] = np.nan
    return out


def position(df, close, bpy, fast_days=50.0, slow_days=200.0, rsi_days=2.0,
             rsi_entry=40.0, atr_days=14.0, atr_mult=0.5, stop=0.20, regime=1):
    """Enter on the pullback, then walk the trade so the stop can read its entry price."""
    high = np.ascontiguousarray(df["High"].to_numpy("float64"))
    low = np.ascontiguousarray(df["Low"].to_numpy("float64"))
    src = np.ascontiguousarray(close)
    n = len(close)

    fast = talib.SMA(src, _bars(bpy, fast_days * D))
    slow = talib.SMA(src, _bars(bpy, slow_days * D))
    rsi = talib.RSI(src, _bars(bpy, rsi_days * D))
    atr = talib.ATR(high, low, src, timeperiod=_bars(bpy, atr_days * D))

    fast1, slow1, atr1 = _lag1(fast), _lag1(slow), _lag1(atr)

    # NaN compares False, so every warm-up bar is a non-entry without a mask of its own.
    bull = (close > slow1) if regime else np.ones(n, dtype=bool)
    entry = (fast1 > slow1) & (close > fast1) & (rsi < rsi_entry) & bull
    leave = (close < (fast1 - atr_mult * atr1)) | ~bull

    out = np.zeros(n)
    open_rate = np.nan
    for i in range(n):
        if np.isfinite(open_rate):
            if close[i] <= open_rate * (1.0 - stop) or leave[i]:
                open_rate = np.nan
        if not np.isfinite(open_rate) and entry[i]:
            open_rate = close[i]
        out[i] = 1.0 if np.isfinite(open_rate) else 0.0
    return out
