"""Heikin-Ashi conditioning: the base rule SEES synthetic candles, the money never does."""

from __future__ import annotations

import numpy as np
import pandas as pd


HA_PREFIX = "ha"


HA_SEP = ":"


def ha_bars(df: pd.DataFrame) -> pd.DataFrame:
    """The standard recursive Heikin-Ashi transform, one synthetic bar per real bar.

        ha_close[t] = (O[t] + H[t] + L[t] + C[t]) / 4
        ha_open[t]  = (ha_open[t-1] + ha_close[t-1]) / 2,  seeded (O[0] + C[0]) / 2
        ha_high[t]  = max(H[t], ha_open[t], ha_close[t])
        ha_low[t]   = min(L[t], ha_open[t], ha_close[t])

    The recursion runs forward only, so the transform is causal and tail-truncation
    leaves every earlier bar bit-identical — which is what the causality gate measures.
    It is evaluated with `ewm(alpha=0.5, adjust=False)` over the lagged ha_close rather
    than a Python loop: `adjust=False` computes exactly `y[t] = 0.5*y[t-1] + 0.5*x[t]`,
    which with `x[t] = ha_close[t-1]` (and `x[0]` = the seed) IS the recurrence, on a
    series that can be three million bars long. `tests/test_heikin.py` holds the loop
    it must agree with.

    Volume, if present, passes through unchanged: HA redraws prices, not turnover.
    """
    o = df["Open"].to_numpy("float64")
    h = df["High"].to_numpy("float64")
    l = df["Low"].to_numpy("float64")
    c = df["Close"].to_numpy("float64")

    ha_close = (o + h + l + c) / 4.0
    lagged = np.concatenate(([(o[0] + c[0]) / 2.0], ha_close[:-1]))
    ha_open = pd.Series(lagged).ewm(alpha=0.5, adjust=False).mean().to_numpy()
    ha_high = np.maximum(h, np.maximum(ha_open, ha_close))
    ha_low = np.minimum(l, np.minimum(ha_open, ha_close))

    out = {"Open": ha_open, "High": ha_high, "Low": ha_low, "Close": ha_close}
    for col in df.columns:
        if col not in out:
            out[col] = df[col].to_numpy()
    return pd.DataFrame(out, index=df.index)[list(df.columns)]


def apply(label, df, close, bpy, symbol, build):
    """Decode `ha:<base label>` and build the base rule on Heikin-Ashi candles.

    Only the SIGNAL sees the synthetic bars. The exposure series returned here is
    settled downstream on the real price series, exactly as for any other rule —
    because an HA close is an average nobody can transact at, and filling at it is
    the trap that makes every HA backtest on a TradingView chart read too well
    (their broker emulator fills at HA prices unless told otherwise). That fill
    flattery is the thing this overlay exists NOT to reproduce.

    `build` arrives as an argument rather than as an import: the registry owns label
    resolution and would be a circular import from here.
    """
    prefix = HA_PREFIX + HA_SEP
    if not label.startswith(prefix):
        return None
    base_label = label[len(prefix):]
    if not base_label:
        return None
    hdf = ha_bars(df)
    return build(base_label, hdf, hdf["Close"].to_numpy("float64"), bpy, symbol)
