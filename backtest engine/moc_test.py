"""Can IBS be traded market-on-close? Compute the signal 5 minutes early, fill at the close.

The published convention fills at the same close whose high/low/close produced the signal,
which is not knowable when the order is sent. The next-open convention avoids that and
costs most of the edge. Between them sits what a desk would actually do: at 15:55 compute
IBS from the session so far and send a market-on-close order.

That is honest ONLY if the signal uses no information after 15:55. So:

    partial IBS = (price at 15:55 - low so far) / (high so far - low so far)

built from the 5m bars stamped 09:30..15:50 (the 15:50 bar spans 15:50-15:55, so its
close IS the 15:55 price). The 15:55-stamped bar - the final five minutes, whose close is
the official close - is excluded from the signal and used only as the fill.

Everything else is held identical to the full-IBS run: same universe, same equal-weight
daily rebalance, same fee grid, same cash credit, same bars. The only difference is
whether the last five minutes of the session were allowed to inform the decision.

21 symbols (the old mega-20 plus SPY), not the 614-name book - that is all the intraday
cache holds, so read this as directional.
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, r"C:\Users\Truong\Documents\work desk\quant python projects\stockhunt\backtest engine")
import config  # noqa: F401  (puts the repo root on sys.path)
from engines import vector
from strategies._indicators import _state_machine

DATA = r"C:\Users\Truong\Documents\work desk\quant python projects\stockhunt\data\stocks\5m"
_SC = {s["key"]: s for s in config.FEE_SCENARIOS["us_stocks"]}
FEE, FREE = _SC["retail"], _SC["gross"]
BUY, SELL = 0.2, 0.8


def daily_from_5m(df: pd.DataFrame):
    """Daily OHLC plus the 15:55 price and the session-so-far range excluding the last bar."""
    g = df.groupby(df.index.date)
    rows = []
    for day, x in g:
        if len(x) < 20:                      # half day or a stub; no usable late session
            continue
        head = x.iloc[:-1]                   # everything strictly before the final 5 minutes
        rows.append({
            "date": pd.Timestamp(day),
            "Open": x["Open"].iloc[0],
            "High": x["High"].max(), "Low": x["Low"].min(), "Close": x["Close"].iloc[-1],
            # what a desk can see at 15:55
            "px1555": head["Close"].iloc[-1],
            "hi1555": head["High"].max(), "lo1555": head["Low"].min(),
        })
    return pd.DataFrame(rows).set_index("date")


def ibs_pos(val: np.ndarray) -> np.ndarray:
    return _state_machine(val < BUY, val > SELL)


def book(frames: dict, key: str):
    """Equal-weight, rebalanced every bar, across whoever has a bar that day."""
    S, B = {}, {}
    for sym, d in frames.items():
        close = d["Close"].to_numpy("float64")
        bpy = vector.bars_per_year(d.index)
        rng_f = d["High"].to_numpy() - d["Low"].to_numpy()
        full = np.divide(close - d["Low"].to_numpy(), rng_f,
                         out=np.full(len(d), 0.5), where=rng_f > 0)
        rng_p = d["hi1555"].to_numpy() - d["lo1555"].to_numpy()
        part = np.divide(d["px1555"].to_numpy() - d["lo1555"].to_numpy(), rng_p,
                         out=np.full(len(d), 0.5), where=rng_p > 0)
        pos = ibs_pos(full if key in ("full", "nextopen") else part)
        if key == "long":
            pos = np.ones(len(d))
        px = close
        if key == "nextopen":
            # signal at close(t) -> filled at open(t+1), earned open to open
            px = d["Open"].to_numpy("float64")
            pos = np.r_[0.0, pos[:-1]]
        S[sym] = pd.Series(vector.net_returns(pos, px, FEE if key != "long" else FREE, bpy),
                           index=d.index)
        B[sym] = pd.Series(pos, index=d.index)
    Sd = pd.DataFrame(S)
    live = Sd.notna()
    n = live.sum(axis=1)
    w = live.astype(float).div(n.where(n > 0), axis=0)
    return (Sd.fillna(0.0) * w).sum(axis=1), (pd.DataFrame(B).fillna(0.0) * w).sum(axis=1)


def stats(r: pd.Series, bpy: float) -> dict:
    r = r.to_numpy()
    eq = float(np.prod(1 + r))
    return {"CAGR": eq ** (bpy / len(r)) - 1, "Sharpe": r.mean() / r.std(ddof=1) * np.sqrt(bpy),
            "vol": r.std(ddof=1) * np.sqrt(bpy), "wealth": 10000 * eq,
            "maxDD": float((pd.Series(np.cumprod(1 + r)) /
                            pd.Series(np.cumprod(1 + r)).cummax() - 1).min())}


frames = {}
for f in sorted(glob.glob(os.path.join(DATA, "*.parquet"))):
    sym = os.path.basename(f)[:-8]
    d = daily_from_5m(pd.read_parquet(f))
    if len(d) > 250:
        frames[sym] = d
idx = sorted(set.intersection(*[set(d.index) for d in frames.values()]))
frames = {s: d.loc[idx] for s, d in frames.items()}
print(f"{len(frames)} symbols, {len(idx)} common days, {idx[0].date()} -> {idx[-1].date()}")

# How often do the two signals even disagree?
agree = tot = 0
flips = []
for sym, d in frames.items():
    close = d["Close"].to_numpy("float64")
    rf = d["High"].to_numpy() - d["Low"].to_numpy()
    full = np.divide(close - d["Low"].to_numpy(), rf, out=np.full(len(d), 0.5), where=rf > 0)
    rp = d["hi1555"].to_numpy() - d["lo1555"].to_numpy()
    part = np.divide(d["px1555"].to_numpy() - d["lo1555"].to_numpy(), rp,
                     out=np.full(len(d), 0.5), where=rp > 0)
    a, b = ibs_pos(full), ibs_pos(part)
    agree += int((a == b).sum()); tot += len(a)
    flips.append({"symbol": sym, "agree_pct": 100 * (a == b).mean(),
                  "full_long_pct": 100 * a.mean(), "part_long_pct": 100 * b.mean(),
                  "corr_ibs": float(np.corrcoef(full, part)[0, 1])})
print(f"\nsignal agreement across all symbol-days: {100*agree/tot:.2f}%")
print(pd.DataFrame(flips).round(2).to_string(index=False))

bpy = vector.bars_per_year(pd.DatetimeIndex(idx))
out = {}
for key, label in (("full", "A  IBS at close, filled at close (published)"),
                   ("partial", "B  IBS at 15:55, filled MOC (realistic)"),
                   ("nextopen", "C  IBS at close, filled next open"),
                   ("long", "D  buy & hold, equal weight")):
    r, expo = book(frames, key)
    s = stats(r, bpy); s["exposure"] = float(expo.mean())
    out[label] = s
res = pd.DataFrame(out).T
res["CAGR"] = (res["CAGR"] * 100).round(2)
res["vol"] = (res["vol"] * 100).round(1)
res["maxDD"] = (res["maxDD"] * 100).round(1)
res["exposure"] = (res["exposure"] * 100).round(1)
res["wealth"] = res["wealth"].round(0)
res["Sharpe"] = res["Sharpe"].round(3)
print("\n" + res.to_string())
