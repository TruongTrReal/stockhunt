"""Plan 5 - stop testing portfolio Sharpe; test the information coefficient.

P4 exposed the flaw in P1-P4's whole method. SE(Sharpe) came out at 0.381 on 472 names
and 0.379 on 20 - breadth bought no power whatsoever, because SE(Sharpe) ~
sqrt((1+SR^2/2)/T) is governed by the LENGTH of the return series, not the number of
names in it. A portfolio is one time series no matter how many stocks are inside it,
and 8 years is 8 years. No universe size fixes that, and reaching SE=0.1 would take
roughly a century of data.

So portfolio Sharpe is simply the wrong statistic to search on. The right one is the
information coefficient: the cross-sectional rank correlation between a signal today
and each name's return over the next period. There, breadth pays - every name on every
date is an observation, so 472 names x 2000 days is ~950,000 of them, and the standard
error on mean IC collapses accordingly. A signal with a real but tiny edge shows a
significant IC long before it could ever show a significant portfolio Sharpe.

This is also the frame that produced this project's one durable positive result:
cross_sectional_results.csv found TA signals systematically ANTI-predictive, which is
an IC statement. That finding was reached almost by accident; here it is the method.

Forward windows are non-overlapping (a 5-day IC is sampled every 5 days), because
overlapping windows share return data and would inflate the t-statistic - the same
class of error as the memmel_z bug already retracted in this project.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

from common import CACHE_TD, UNIVERSE, log_trial, split

LOOKBACK = 252
HORIZONS = [1, 5, 21]


def load_all(min_rows: int = 2000):
    close, opn, high, low, vol = {}, {}, {}, {}, {}
    for p in sorted(glob.glob(str(CACHE_TD / "*.parquet"))):
        t = os.path.basename(p)[:-8]
        if t == "SPY":
            continue
        df = pd.read_parquet(p)
        if len(df) < min_rows:
            continue
        df.index = pd.DatetimeIndex(df.index).tz_localize(None)
        close[t], opn[t] = df["Close"], df["Open"]
        high[t], low[t], vol[t] = df["High"], df["Low"], df["Volume"]
    f = lambda d: pd.DataFrame(d).sort_index()
    return f(close), f(opn), f(high), f(low), f(vol)


def build_signals(close, opn, high, low, vol):
    rets = close.pct_change()
    overnight = opn / close.shift(1) - 1.0
    intraday = close / opn - 1.0
    dollar_vol = (close * vol).rolling(21).mean()
    return {
        "momentum_12_1":     close.shift(21) / close.shift(LOOKBACK) - 1.0,
        "reversal_1m":      -rets.rolling(21).sum(),
        "reversal_1w":      -rets.rolling(5).sum(),
        "reversal_1d":      -rets,
        "overnight_252":     overnight.rolling(LOOKBACK).sum(),
        "overnight_21":      overnight.rolling(21).sum(),
        "intraday_21":       intraday.rolling(21).sum(),
        "low_vol_60":       -rets.rolling(60).std(),
        "low_vol_252":      -rets.rolling(LOOKBACK).std(),
        "illiquidity":       (rets.abs() / (close * vol)).rolling(21).mean(),
        "dollar_volume":     np.log(dollar_vol),
        "vol_shock":         vol.rolling(5).mean() / vol.rolling(63).mean(),
        "high_52w_prox":     close / close.rolling(LOOKBACK).max(),
        "skew_252":          rets.rolling(LOOKBACK).skew(),
        "range_21":         -((high - low) / close).rolling(21).mean(),
    }


def ic_series(sig: pd.DataFrame, fwd: pd.DataFrame, step: int) -> pd.Series:
    """Spearman IC per date, sampled every `step` days so forward windows never overlap."""
    dates = sig.index[::step]
    out = {}
    for d in dates:
        if d not in fwd.index:
            continue
        s, f = sig.loc[d], fwd.loc[d]
        ok = s.notna() & f.notna()
        if ok.sum() < 20:
            continue
        out[d] = float(s[ok].rank().corr(f[ok].rank()))
    return pd.Series(out).dropna()


def main() -> None:
    close, opn, high, low, vol = load_all()
    print(f"loaded {close.shape[1]} tickers x {close.shape[0]} days")
    signals = build_signals(close, opn, high, low, vol)

    books = {"S&P-wide": list(close.columns),
             "top-20": [t for t in UNIVERSE if t in close.columns]}
    n_tr = len(signals) * len(HORIZONS) * len(books)
    print(f"{n_tr} trials -> |t| must exceed ~{abs(np.sqrt(2 * np.log(n_tr))):.2f} "
          f"to be notable after multiple testing\n")

    rows = []
    for bname, cols in books.items():
        c = close[cols]
        print(f"=== {bname} ({len(cols)} names) ===")
        print(f"{'signal':>16} " + " ".join(f"{'IC_' + str(h) + 'd':>10} {'t':>6}" for h in HORIZONS))
        for sname, sig_full in signals.items():
            sig_full = sig_full[cols] if set(cols) <= set(sig_full.columns) else sig_full.reindex(columns=cols)
            sig, _ = split(sig_full)
            line, rec = f"{sname:>16} ", {"book": bname, "signal": sname}
            for h in HORIZONS:
                fwd_full = c.shift(-h) / c - 1.0
                fwd, _ = split(fwd_full)
                ics = ic_series(sig, fwd.reindex(sig.index), step=h)
                if len(ics) < 20:
                    line += f"{'-':>10} {'-':>6} "
                    continue
                m, t = ics.mean(), ics.mean() / ics.std() * np.sqrt(len(ics))
                rec[f"ic_{h}"], rec[f"t_{h}"], rec[f"n_{h}"] = float(m), float(t), int(len(ics))
                line += f"{m:>+10.4f} {t:>6.2f} "
            rows.append(rec)
            print(line)
        print()

    r = pd.DataFrame(rows)
    r.to_csv("edge/p5_ic.csv", index=False)
    tcols = [f"t_{h}" for h in HORIZONS if f"t_{h}" in r.columns]
    r["max_abs_t"] = r[tcols].abs().max(axis=1)
    top = r.sort_values("max_abs_t", ascending=False).head(8)
    print("strongest |t| overall:")
    print(top[["book", "signal"] + tcols + ["max_abs_t"]].to_string(index=False,
          float_format=lambda v: f"{v:.2f}"))
    best = top.iloc[0]
    log_trial("P5", "information coefficient across signal panel", "TRAIN",
              f"max |t| = {best['max_abs_t']:.2f} ({best['book']}/{best['signal']})",
              {"book": best["book"], "signal": best["signal"],
               "max_abs_t": float(best["max_abs_t"]), "n_trials": n_tr},
              "candidate" if best["max_abs_t"] > np.sqrt(2 * np.log(n_tr)) else "fail")


if __name__ == "__main__":
    main()
