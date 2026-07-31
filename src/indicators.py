"""TA-Lib indicator helpers applied to a single OHLCV DataFrame (as produced by data_loader)."""

import talib


def add_indicators(df):
    """Return a copy of `df` with common TA-Lib indicator columns added."""
    out = df.copy()
    close, high, low, volume = out["Close"], out["High"], out["Low"], out["Volume"]

    out["sma_20"] = talib.SMA(close, timeperiod=20)
    out["sma_50"] = talib.SMA(close, timeperiod=50)
    out["ema_20"] = talib.EMA(close, timeperiod=20)
    out["rsi_14"] = talib.RSI(close, timeperiod=14)
    macd, macd_signal, macd_hist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd, macd_signal, macd_hist
    upper, middle, lower = talib.BBANDS(close, timeperiod=20)
    out["bb_upper"], out["bb_middle"], out["bb_lower"] = upper, middle, lower
    out["atr_14"] = talib.ATR(high, low, close, timeperiod=14)
    out["adx_14"] = talib.ADX(high, low, close, timeperiod=14)
    out["obv"] = talib.OBV(close, volume)

    return out
