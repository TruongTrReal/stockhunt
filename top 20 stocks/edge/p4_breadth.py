"""Plan 4 - buy statistical power with breadth, then check whether it transfers.

P1-P3 all died the same death, and it was never the idea that failed - it was the
sample. SE(Sharpe)=0.368 on 8 years of a 20-name book means a true +0.2 Sharpe edge
is indistinguishable from luck no matter how good the signal is. That is a power
problem, and the only cure is more independent bets.

The repo already has 503 Twelve Data tickers cached. So the correct experiment is:

  * ESTIMATE on 501 names, where a cross-sectional tilt averages over ~25x more bets
    per rebalance and the standard error collapses accordingly;
  * then TRANSFER the fitted signal, unchanged and unrefitted, onto the 20-name book
    that is actually going to be traded.

If the effect is real it shows up with a decent t on 501 names, and the 20-name
version is then a noisy but unbiased read of the same thing. If it dies on 501 names,
it was never there - and the 20-name +0.288 from P3 was the small sample flattering a
handful of trending mega-caps.

Portfolio maths is exact and vectorised per holding period rather than looped daily:
within a period the book is buy-and-hold, so its value is sum_i w_i * cumprod_i and
the drifted end weights fall straight out - which is what turnover is charged on.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

from common import (CACHE_TD, COST_BPS_GRID, TRADING_DAYS, UNIVERSE, block_bootstrap_se,
                    deflated_threshold, log_trial, sharpe, split, summarise)

LOOKBACK = 252
LAMS = [0.25, 0.5, 1.0]


def load_all(min_rows: int = 2000) -> tuple[pd.DataFrame, pd.DataFrame]:
    close, opn = {}, {}
    for p in sorted(glob.glob(str(CACHE_TD / "*.parquet"))):
        t = os.path.basename(p)[:-8]
        if t == "SPY":
            continue
        df = pd.read_parquet(p)
        if len(df) < min_rows:
            continue
        df.index = pd.DatetimeIndex(df.index).tz_localize(None)
        close[t], opn[t] = df["Close"], df["Open"]
    return pd.DataFrame(close).sort_index(), pd.DataFrame(opn).sort_index()


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    mu, sd = df.mean(axis=1), df.std(axis=1).replace(0.0, np.nan)
    return df.sub(mu, axis=0).div(sd, axis=0)


def backtest(z: pd.DataFrame, rets: pd.DataFrame, lam: float):
    """Monthly-rebalanced exp(lam*z) book. Returns (daily gross return, turnover)."""
    idx = rets.index
    months = pd.Series(idx, index=idx).groupby([idx.year, idx.month]).last().values
    rebal = pd.DatetimeIndex(months)

    port = pd.Series(0.0, index=idx)
    turn = pd.Series(0.0, index=idx)
    prev_w = None
    for i, start in enumerate(rebal):
        end = rebal[i + 1] if i + 1 < len(rebal) else idx[-1]
        window = rets.loc[start:end].iloc[1:]          # trade at `start`, earn from next bar
        if window.empty:
            continue
        s = z.loc[:start]
        if s.empty or s.iloc[-1].isna().all():
            continue
        sig = s.iloc[-1].clip(-3, 3)
        live = rets.columns[rets.loc[start].notna() & sig.notna()]
        if len(live) < 10:
            continue
        e = np.exp(lam * sig[live].fillna(0.0))
        w = (e / e.sum()).reindex(rets.columns).fillna(0.0)

        turn.loc[start] = float((w - prev_w).abs().sum()) if prev_w is not None else 1.0
        growth = (1 + window.fillna(0.0)).cumprod()
        value = growth.mul(w, axis=1).sum(axis=1)
        port.loc[window.index] = value.pct_change().fillna(value.iloc[0] - 1.0)
        end_val = growth.iloc[-1] * w
        prev_w = end_val / end_val.sum()
    return port, turn


def evaluate(name: str, z, rets, lam, bench, bh_sr, se_sr, n_trials):
    gross, turn = backtest(z, rets, lam)
    yrs = len(rets) / TRADING_DAYS
    rho = float(pd.concat([gross, bench], axis=1).dropna().corr().iloc[0, 1])
    se_d = se_sr * np.sqrt(max(2 * (1 - rho), 1e-9))
    out = {"book": name, "lam": lam, "turnover": float(turn.sum() / yrs), "rho": rho,
           "se_d": se_d, "floor": deflated_threshold(n_trials, se_d)}
    for bps in COST_BPS_GRID:
        net = gross - turn * bps / 1e4
        out[f"ex_{bps:g}"] = sharpe(net) - bh_sr
        out[f"cagr_{bps:g}"] = summarise(net)["cagr"]
    out["t"] = out["ex_5"] / se_d if se_d > 0 else np.nan
    return out


def main() -> None:
    close_all, opn_all = load_all()
    print(f"loaded {close_all.shape[1]} tickers, {close_all.shape[0]} days")

    books = {"S&P-wide (501)": list(close_all.columns),
             "top-20": [t for t in UNIVERSE if t in close_all.columns]}

    rows = []
    n_tr = len(books) * len(LAMS)
    for bname, cols in books.items():
        close = close_all[cols]
        rets_all = close.pct_change()
        rets, _ = split(rets_all)
        rets = rets.dropna(how="all")
        mom = zscore(close.reindex(rets.index).shift(21)
                     / close.reindex(rets.index).shift(LOOKBACK) - 1.0)

        bench, _ = backtest(pd.DataFrame(0.0, index=rets.index, columns=rets.columns), rets, 0.0)
        bh_sr = sharpe(bench)
        se_sr = block_bootstrap_se(bench, sharpe, block=21, n_boot=2000)
        print(f"\n=== {bname}: {len(cols)} names | EW benchmark Sharpe {bh_sr:.3f} "
              f"CAGR {summarise(bench)['cagr']:.2%} | SE(SR) {se_sr:.3f} ===")
        print(f"{'lam':>6} {'turn/yr':>8} {'rho':>6} {'SE(dSR)':>8} {'ex@0':>7} {'ex@5':>7} "
              f"{'ex@10':>7} {'t':>6} {'floor':>7} {'verdict':>8}")
        for lam in LAMS:
            x = evaluate(bname, mom, rets, lam, bench, bh_sr, se_sr, n_tr)
            rows.append(x)
            v = "PASS" if x["ex_5"] > x["floor"] else "fail"
            print(f"{lam:>6.2f} {x['turnover']:>8.2f} {x['rho']:>6.3f} {x['se_d']:>8.3f} "
                  f"{x['ex_0']:>+7.3f} {x['ex_5']:>+7.3f} {x['ex_10']:>+7.3f} "
                  f"{x['t']:>6.2f} {x['floor']:>7.3f} {v:>8}")

    r = pd.DataFrame(rows)
    r.to_csv("edge/p4_breadth.csv", index=False)
    wide = r[r.book.str.startswith("S&P")]
    best = wide.loc[wide["t"].idxmax()]
    print(f"\nbest on 501 names: lam={best['lam']} ex@5={best['ex_5']:+.3f} t={best['t']:.2f} "
          f"floor={best['floor']:.3f} -> {'PASS' if best['ex_5'] > best['floor'] else 'FAIL'}")
    log_trial("P4", "breadth buys power: estimate momentum tilt on 501 names", "TRAIN",
              f"best t={best['t']:.2f} ex@5={best['ex_5']:+.3f}",
              {k: float(best[k]) for k in ("lam", "ex_0", "ex_5", "ex_10", "t", "floor", "rho")},
              "PASS" if best["ex_5"] > best["floor"] else "fail")


if __name__ == "__main__":
    main()
