"""Turn every one of TA-Lib's 161 functions into a long/flat/short position series.

~120 functions have a well-known, specific trading rule (moving-average
crossover, RSI overbought/oversold, MACD signal-line cross, etc.) - see the
category lists below. The remaining ~40 (pure arithmetic like LN/ADD,
magnitude-only measures like ATR/STDDEV, trend-*strength*-only measures like
ADX) have no conventional directional rule, so they fall back to one generic
rule: long when the raw value is above its own trailing SMA. That fallback is
mechanical, not a textbook strategy - GENERIC_FALLBACK_FUNCTIONS below marks
which indicators use it, so results can be weighted accordingly. BETA/CORREL
additionally need a benchmark series (SPY) rather than just OHLCV.
"""

import numpy as np
import pandas as pd
import talib
from talib import abstract

PATTERN_HOLD_DAYS = 5
SELF_TREND_SMA_PERIOD = 20
BENCHMARK_TICKER = "SPY"

# Indicators with no conventional directional rule, using the generic
# "value vs. its own trailing SMA" fallback instead of a textbook rule.
GENERIC_FALLBACK_FUNCTIONS = {
    # Volatility/dispersion magnitude, no inherent direction
    "ATR": "volatility magnitude - fallback treats rising volatility as bullish",
    "NATR": "volatility magnitude - fallback treats rising volatility as bullish",
    "TRANGE": "volatility magnitude - fallback treats rising volatility as bullish",
    "STDDEV": "dispersion measure - fallback treats rising dispersion as bullish",
    "VAR": "dispersion measure - fallback treats rising dispersion as bullish",
    "AVGDEV": "dispersion measure - fallback treats rising dispersion as bullish",
    # Trend *strength*, not trend *direction* (direction is PLUS_DI/MINUS_DI)
    "ADX": "trend strength only - fallback treats strengthening trend as bullish",
    "ADXR": "trend strength only - fallback treats strengthening trend as bullish",
    "DX": "trend strength only - fallback treats strengthening trend as bullish",
    "CORREL": "correlation to SPY - fallback treats rising correlation as bullish",
    # Pure arithmetic on price series - not trading indicators by design
    **{f: "arithmetic operator, no trading meaning - generic fallback only"
       for f in talib.get_function_groups()["Math Operators"]},
    **{f: "arithmetic transform, no trading meaning - generic fallback only"
       for f in talib.get_function_groups()["Math Transform"]},
}

# Single-output, price-scale indicators: long when close is above the indicator.
# The MA_FAMILY members are handled separately as period variants (see below),
# not tested at their bare TA-Lib-default period.
PRICE_CROSSOVER = [
    "HT_TRENDLINE", "AVGPRICE", "MEDPRICE", "TYPPRICE", "WCLPRICE", "SAR",
]

# SAREXT is NOT a plain positive stop level like SAR - TA-Lib encodes the
# current trend directly in the SIGN of the output (negative = downtrend,
# with the actual stop price being abs(value)). Comparing it to close like a
# normal PRICE_CROSSOVER indicator is wrong: a negative value makes
# close - raw always large and positive, so the old code signaled "long"
# essentially 100% of the time regardless of the real trend.
SAREXT_NAME = "SAREXT"

# Moving-average-family PRICE_CROSSOVER members that take a single
# `timeperiod` parameter - tested at several periods instead of just each
# function's (inconsistent, not-very-meaningful) TA-Lib default, so e.g.
# SMA_50 and EMA_50 are directly comparable to each other.
MA_FAMILY = [
    "SMA", "EMA", "WMA", "DEMA", "TEMA", "TRIMA", "KAMA", "T3", "MA",
    "MIDPOINT", "MIDPRICE", "LINEARREG", "LINEARREG_INTERCEPT", "TSF",
]
MA_PERIODS = [9, 21, 50, 100, 200, 1000]
MA_VARIANT_SEP = "_"


def _ma_variant_name(base: str, period: int) -> str:
    return f"{base}{MA_VARIANT_SEP}{period}"


def _parse_ma_variant(name: str):
    """Return (base_name, period) if `name` is e.g. 'SMA_50', else None."""
    if MA_VARIANT_SEP not in name:
        return None
    base, _, period_str = name.rpartition(MA_VARIANT_SEP)
    if base in MA_FAMILY and period_str.isdigit():
        return base, int(period_str)
    return None

# Unbounded oscillators centered on zero: long when value > 0
ZERO_LINE = [
    "MOM", "ROC", "ROCP", "CMO", "TRIX", "PPO", "APO", "BOP", "ADOSC",
    "AROONOSC", "CCI", "LINEARREG_SLOPE", "LINEARREG_ANGLE",
]

# Oscillators centered elsewhere: long when value > center
CENTERED = {"ROCR": 1.0, "ROCR100": 100.0}

# Bounded oscillators with a fixed neutral midpoint: long when value > midpoint
MIDPOINT_OSC = {"RSI": 50.0, "MFI": 50.0, "WILLR": -50.0, "ULTOSC": 50.0, "IMI": 50.0}

# Two-line indicators: long when the first output is above the second
LINE_CROSSOVER = [
    "MACD", "MACDEXT", "MACDFIX", "STOCH", "STOCHF", "STOCHRSI", "MAMA",
    "HT_SINE", "HT_PHASOR", "AROON",
]
LINE_CROSSOVER_REVERSED = {"AROON"}  # outputs are (aroondown, aroonup); long when aroonup > aroondown

# Two-output functions with no natural "line0 vs line1" cross (max is never
# below min): collapse to a single derived series (output1 - output0) first,
# then apply the same generic self-trend fallback as everything else here.
DERIVED_DIFF = {"MINMAX": ("min", "max"), "MINMAXINDEX": ("minidx", "maxidx")}

# Single-output series with no fixed reference level: long when above own SMA.
# Includes both legitimate cases (OBV, AD, cycle period/phase) and every
# GENERIC_FALLBACK_FUNCTIONS entry (see module docstring for why).
SELF_TREND = [
    "OBV", "AD", "HT_DCPERIOD", "HT_DCPHASE",
    "ATR", "NATR", "TRANGE", "STDDEV", "VAR", "AVGDEV", "ADX", "ADXR", "DX",
    "ADD", "SUB", "MULT", "DIV", "MAX", "MIN", "MAXINDEX", "MININDEX", "SUM",
    "ACOS", "ASIN", "ATAN", "CEIL", "COS", "COSH", "EXP", "FLOOR", "LN",
    "LOG10", "SIN", "SINH", "SQRT", "TAN", "TANH",
]

# Already a 0/1 regime flag
BINARY_REGIME = ["HT_TRENDMODE"]

BBANDS_NAME = "BBANDS"  # mean-reversion: long below lower band, short above upper band
ACCBANDS_NAME = "ACCBANDS"  # breakout: long above upper band, short below lower band
DI_PAIR = ("PLUS_DI", "MINUS_DI")
DM_PAIR = ("PLUS_DM", "MINUS_DM")  # raw directional movement crossover (same idea as DI_PAIR)
MAVP_NAME = "MAVP"  # variable-period MA; fed a constant 20-bar "periods" array
BETA_NAME = "BETA"  # long when beta-to-SPY > 1 (more aggressive than the market)
CORREL_NAME = "CORREL"  # correlation to SPY; generic self-trend fallback

PATTERN_FUNCTIONS = talib.get_function_groups()["Pattern Recognition"]


def _call(name: str, inputs: dict, **params):
    func = abstract.Function(name)
    out = func(inputs, **params)
    if isinstance(out, tuple):
        return tuple(np.where(np.isinf(o), np.nan, o) for o in out)
    return np.where(np.isinf(out), np.nan, out)


def _price_inputs(df: pd.DataFrame) -> dict:
    return {
        "open": df["Open"].to_numpy(dtype=float),
        "high": df["High"].to_numpy(dtype=float),
        "low": df["Low"].to_numpy(dtype=float),
        "close": df["Close"].to_numpy(dtype=float),
        "volume": df["Volume"].to_numpy(dtype=float),
    }


def get_all_indicator_names() -> list[str]:
    """All TA-Lib-derived signals this module knows how to turn into a rule:
    161 TA-Lib functions, minus the 14 MA_FAMILY functions tested at their
    bare default period, plus those 14 x 6 period variants (e.g. 'SMA_50')."""
    names = set(PRICE_CROSSOVER) | set(ZERO_LINE) | set(CENTERED) | set(MIDPOINT_OSC)
    names |= set(LINE_CROSSOVER) | set(SELF_TREND) | set(BINARY_REGIME) | set(DERIVED_DIFF)
    names |= {BBANDS_NAME, ACCBANDS_NAME, MAVP_NAME, BETA_NAME, CORREL_NAME, SAREXT_NAME}
    names |= set(DI_PAIR) | set(DM_PAIR) | set(PATTERN_FUNCTIONS)
    names |= {_ma_variant_name(base, p) for base in MA_FAMILY for p in MA_PERIODS}
    return sorted(names)


def generate_position(name: str, df: pd.DataFrame, benchmark_close: pd.Series | None = None) -> pd.Series:
    """Return a position series (1 = long, -1 = short, 0 = flat) indexed like df.

    `benchmark_close` (e.g. SPY close, reindexed to df's dates) is only needed
    for BETA and CORREL.
    """
    idx = df.index
    inputs = _price_inputs(df)
    close = df["Close"]

    ma_variant = _parse_ma_variant(name)
    if ma_variant is not None:
        base, period = ma_variant
        raw = pd.Series(_call(base, inputs, timeperiod=period), index=idx)
        return np.sign(close - raw).fillna(0)

    if name == BBANDS_NAME:
        upper, middle, lower = _call(name, inputs)
        upper, lower = pd.Series(upper, index=idx), pd.Series(lower, index=idx)
        raw = pd.Series(np.select([close < lower, close > upper], [1, -1], default=np.nan), index=idx)
        return raw.ffill().fillna(0)

    if name == ACCBANDS_NAME:
        upper, middle, lower = _call(name, inputs)
        upper, lower = pd.Series(upper, index=idx), pd.Series(lower, index=idx)
        raw = pd.Series(np.select([close > upper, close < lower], [1, -1], default=np.nan), index=idx)
        return raw.ffill().fillna(0)

    if name == SAREXT_NAME:
        raw = pd.Series(_call(name, inputs), index=idx)
        return np.sign(raw).fillna(0)

    if name == MAVP_NAME:
        periods = np.full(len(close), 20.0)
        raw = talib.MAVP(close.to_numpy(dtype=float), periods, minperiod=2, maxperiod=30, matype=0)
        raw = pd.Series(np.where(np.isinf(raw), np.nan, raw), index=idx)
        return np.sign(close - raw).fillna(0)

    if name in (BETA_NAME, CORREL_NAME):
        if benchmark_close is None:
            raise ValueError(f"{name} requires benchmark_close (e.g. SPY)")
        bench = benchmark_close.reindex(idx).ffill().to_numpy(dtype=float)
        timeperiod = 5 if name == BETA_NAME else 30
        func = talib.BETA if name == BETA_NAME else talib.CORREL
        raw = func(close.to_numpy(dtype=float), bench, timeperiod=timeperiod)
        raw = pd.Series(np.where(np.isinf(raw), np.nan, raw), index=idx)
        if name == BETA_NAME:
            return np.sign(raw - 1.0).fillna(0)
        own_sma = raw.rolling(SELF_TREND_SMA_PERIOD).mean()
        return np.sign(raw - own_sma).fillna(0)

    if name in DERIVED_DIFF:
        out0_name, out1_name = DERIVED_DIFF[name]
        out = _call(name, inputs)
        out0, out1 = pd.Series(out[0], index=idx), pd.Series(out[1], index=idx)
        raw = out1 - out0  # e.g. MINMAX -> max - min (a spread/range series)
        own_sma = raw.rolling(SELF_TREND_SMA_PERIOD).mean()
        return np.sign(raw - own_sma).fillna(0)

    if name in DI_PAIR:
        plus_di = pd.Series(_call("PLUS_DI", inputs), index=idx)
        minus_di = pd.Series(_call("MINUS_DI", inputs), index=idx)
        return np.sign(plus_di - minus_di).fillna(0)

    if name in DM_PAIR:
        plus_dm = pd.Series(_call("PLUS_DM", inputs), index=idx)
        minus_dm = pd.Series(_call("MINUS_DM", inputs), index=idx)
        return np.sign(plus_dm - minus_dm).fillna(0)

    if name in PATTERN_FUNCTIONS:
        raw = pd.Series(_call(name, inputs), index=idx)
        signal = np.sign(raw)
        return signal.replace(0, np.nan).ffill(limit=PATTERN_HOLD_DAYS).fillna(0)

    if name in BINARY_REGIME:
        raw = pd.Series(_call(name, inputs), index=idx)
        return raw.fillna(0)

    if name in SELF_TREND:
        raw = pd.Series(_call(name, inputs), index=idx)
        own_sma = raw.rolling(SELF_TREND_SMA_PERIOD).mean()
        return np.sign(raw - own_sma).fillna(0)

    if name in LINE_CROSSOVER:
        out = _call(name, inputs)
        line0, line1 = pd.Series(out[0], index=idx), pd.Series(out[1], index=idx)
        if name in LINE_CROSSOVER_REVERSED:
            line0, line1 = line1, line0
        return np.sign(line0 - line1).fillna(0)

    if name in MIDPOINT_OSC:
        raw = pd.Series(_call(name, inputs), index=idx)
        return np.sign(raw - MIDPOINT_OSC[name]).fillna(0)

    if name in CENTERED:
        raw = pd.Series(_call(name, inputs), index=idx)
        return np.sign(raw - CENTERED[name]).fillna(0)

    if name in ZERO_LINE:
        raw = pd.Series(_call(name, inputs), index=idx)
        return np.sign(raw).fillna(0)

    if name in PRICE_CROSSOVER:
        raw = pd.Series(_call(name, inputs), index=idx)
        return np.sign(close - raw).fillna(0)

    raise ValueError(f"No signal rule registered for {name!r}")


# Two-line indicators where the two lines have specific, well-known names -
# used for a readable tldr rather than the generic "line0 vs line1" phrasing.
_LINE_NAMES = {
    "MACD": ("MACD line", "signal line"), "MACDEXT": ("MACD line", "signal line"),
    "MACDFIX": ("MACD line", "signal line"), "STOCH": ("%K", "%D"), "STOCHF": ("%K", "%D"),
    "STOCHRSI": ("%K", "%D"), "MAMA": ("MAMA", "FAMA"), "HT_SINE": ("Sine", "LeadSine"),
    "HT_PHASOR": ("in-phase component", "quadrature component"), "AROON": ("Aroon Up", "Aroon Down"),
}

# Hand-written overrides for indicators where the generic per-category
# template would be uninformative or the indicator is common enough to
# deserve precise wording. Everything else falls back to a template below.
_TLDR_OVERRIDES = {
    "RSI": "Long when RSI(14) > 50 (bullish momentum); short when below. Held until it crosses back.",
    "MACD": "Long when the MACD line crosses above its signal line; short when it crosses below.",
    "SAR": "Long while price stays above the Parabolic SAR stop level; flips short the moment price crosses below it (and vice versa).",
    "SAREXT": "Long while TA-Lib's extended Parabolic SAR reports an uptrend state, short while it reports a downtrend (TA-Lib encodes this directly in the sign of the output).",
    "BBANDS": "Mean-reversion: long when price closes below the lower Bollinger Band (oversold), short when above the upper band (overbought).",
    "ACCBANDS": "Breakout: long when price closes above the upper acceleration band, short when below the lower band.",
    "ADX": "No trading direction of its own (measures trend strength, not direction) - generic fallback: long when ADX is rising above its own 20-day average.",
    "HT_TRENDMODE": "Binary regime flag: long only while the Hilbert Transform says the market is in 'trend mode'; flat during 'cycle mode'. Never goes short.",
    "OBV": "Long when On-Balance Volume is above its own 20-day average (volume flow confirms an uptrend); short when below.",
    "AD": "Long when the Accumulation/Distribution line is above its own 20-day average; short when below.",
}


def describe_signal(name: str) -> str:
    """One-sentence, human-readable entry/exit rule for `name` - shown as a
    hover tooltip in the report. Falls back to a per-category template built
    from the same rule tables `generate_position` uses, so it can never drift
    out of sync with the actual backtest logic."""
    if name in _TLDR_OVERRIDES:
        return _TLDR_OVERRIDES[name]

    exit_note = " Position is held until the rule flips (no fixed exit)."

    ma_variant = _parse_ma_variant(name)
    if ma_variant is not None:
        base, period = ma_variant
        return f"Long when price closes above {base}({period}); short when it closes below." + exit_note

    if name == BBANDS_NAME:
        return _TLDR_OVERRIDES["BBANDS"]
    if name == ACCBANDS_NAME:
        return _TLDR_OVERRIDES["ACCBANDS"]
    if name == MAVP_NAME:
        return "Long when price is above a 20-period variable moving average; short when below." + exit_note
    if name == BETA_NAME:
        return "Long when the stock's rolling beta to SPY is above 1.0 (more volatile than the market); short when below."
    if name == CORREL_NAME:
        return "Long when the stock's rolling correlation to SPY is above its own 20-day average (correlation rising); short when falling." + exit_note
    if name in DI_PAIR:
        return "Long when +DI (upward directional movement) is above -DI (downward); short when -DI is higher." + exit_note
    if name in DM_PAIR:
        return "Long when raw +DM exceeds raw -DM; short when -DM is higher (unsmoothed version of the DI crossover)." + exit_note
    if name in DERIVED_DIFF:
        out0, out1 = DERIVED_DIFF[name]
        return f"No trading direction of its own ({out1}-{out0} spread) - generic fallback: long when the spread is above its own 20-day average."
    if name in PATTERN_FUNCTIONS:
        return "Candlestick pattern: enters long (bullish) or short (bearish) the day the pattern fires, held for a fixed 5 trading days regardless of what happens after."
    if name in BINARY_REGIME:
        return _TLDR_OVERRIDES["HT_TRENDMODE"]
    if name in SELF_TREND:
        prefix = "No trading direction of its own - generic fallback: " if name in GENERIC_FALLBACK_FUNCTIONS else ""
        return f"{prefix}Long when {name} is above its own 20-day moving average; short when below." + exit_note
    if name in LINE_CROSSOVER:
        # _LINE_NAMES already gives the pair in "long when A > B" order that
        # matches generate_position's actual signal - including for AROON,
        # whose raw abstract output (aroondown, aroonup) generate_position
        # swaps before comparing, so no swap is applied here too.
        a, b = _LINE_NAMES.get(name, ("the first line", "the second line"))
        return f"Long when {a} crosses above {b}; short when it crosses below."
    if name in MIDPOINT_OSC:
        mid = MIDPOINT_OSC[name]
        return f"Long when {name} > {mid:g} (bullish); short when below {mid:g}." + exit_note
    if name in CENTERED:
        c = CENTERED[name]
        return f"Long when {name} > {c:g}; short when below {c:g}." + exit_note
    if name in ZERO_LINE:
        return f"Long when {name} > 0; short when below 0." + exit_note
    if name in PRICE_CROSSOVER:
        return f"Long when price closes above {name}; short when it closes below." + exit_note
    return "Entry/exit rule not documented."
