"""Plan 6 - is the illiquidity signal real, or is it survivorship bias wearing a coat?

P5's only credible finding was a liquidity effect on the S&P-wide book: dollar_volume
IC t = -3.37/-2.58/-3.03 and illiquidity t = +2.74/+2.47/+3.10 at 1/5/21 days. Two
things make it worth one careful test rather than immediate dismissal - the sign is
consistent across all three horizons, and it is significant at 21 days, meaning a
MONTHLY rebalance, which costs 2-4 turnover units a year instead of the 500 that killed
every intraday idea in this project.

But an illiquidity tilt is the single strategy most flattered by survivorship bias.
It deliberately overweights the smallest, least liquid names in the index - which is
exactly the group whose failures get deleted from a current-members list. Measuring it
on today's constituents asks "how did the small survivors do", and the answer is always
"well". survivorship-bias-measured already found 4.85pp of CAGR inflation on plain
buy-and-hold; on a size tilt it should be much worse.

So the test is the comparison, not the level: run the identical tilt on a
current-members universe and on point-in-time membership, and see how much of the edge
is left when the deleted names are allowed back in.

Source is yfinance alone (91.6% mean PIT coverage vs 81.9% for Twelve Data). Both arms
use it, so no cross-source contamination - see the never-mix-price-sources rule.

Residual bias, stated plainly: coverage is 81.3% at the 2015 start, and the 18.7%
missing are disproportionately names that were acquired or failed. This arm is
less biased, not unbiased, and the remaining bias still points the same way.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

from common import (COST_BPS_GRID, STOCKHUNT, TRADING_DAYS, block_bootstrap_se,
                    deflated_threshold, log_trial, sharpe, split, summarise)

CACHE = STOCKHUNT / "test research" / "data" / "cache"
PIT = STOCKHUNT / "test research" / "data" / "sp500_pit_membership.csv"
LAMS = [0.5, 1.0]


def load():
    close, vol = {}, {}
    for p in sorted(glob.glob(str(CACHE / "*.parquet"))):
        t = os.path.basename(p)[:-8]
        df = pd.read_parquet(p)
        if len(df) < 250:
            continue
        df.index = pd.DatetimeIndex(df.index).tz_localize(None)
        close[t], vol[t] = df["Close"], df["Volume"]
    return pd.DataFrame(close).sort_index(), pd.DataFrame(vol).sort_index()


def pit_mask(idx: pd.DatetimeIndex, cols: list[str]) -> pd.DataFrame:
    """Boolean membership, forward-filled from monthly snapshots onto the daily grid."""
    m = pd.read_csv(PIT)
    m["date"] = pd.to_datetime(m["date"])
    m = m[m.ticker.isin(cols)]
    wide = (m.assign(v=True).pivot_table(index="date", columns="ticker", values="v",
                                         aggfunc="first")
            .reindex(columns=cols).reindex(idx.union(m.date.unique())).ffill())
    return wide.reindex(idx).fillna(False).astype(bool)


def zscore(df):
    mu, sd = df.mean(axis=1), df.std(axis=1).replace(0.0, np.nan)
    return df.sub(mu, axis=0).div(sd, axis=0)


def backtest(z, rets, mask, lam):
    idx = rets.index
    rebal = pd.DatetimeIndex(pd.Series(idx, index=idx).groupby([idx.year, idx.month]).last().values)
    port = pd.Series(0.0, index=idx)
    turn = pd.Series(0.0, index=idx)
    prev_w = None
    for i, start in enumerate(rebal):
        end = rebal[i + 1] if i + 1 < len(rebal) else idx[-1]
        window = rets.loc[start:end].iloc[1:]
        if window.empty:
            continue
        s = z.loc[:start]
        if s.empty:
            continue
        sig = s.iloc[-1].clip(-3, 3)
        live = rets.columns[mask.loc[start] & sig.notna() & rets.loc[start].notna()]
        if len(live) < 30:
            continue
        e = np.exp(lam * sig[live])
        w = (e / e.sum()).reindex(rets.columns).fillna(0.0)
        turn.loc[start] = float((w - prev_w).abs().sum()) if prev_w is not None else 1.0
        growth = (1 + window.fillna(0.0)).cumprod()
        value = growth.mul(w, axis=1).sum(axis=1)
        port.loc[window.index] = value.pct_change().fillna(value.iloc[0] - 1.0)
        endv = growth.iloc[-1] * w
        prev_w = endv / endv.sum()
    return port, turn


def main() -> None:
    close, vol = load()
    print(f"loaded {close.shape[1]} tickers (yfinance) x {close.shape[0]} days")
    rets_all = close.pct_change()
    dv = (close * vol).rolling(21).mean()
    signals = {"illiquidity": zscore((rets_all.abs() / (close * vol)).rolling(21).mean()),
               "neg_dollar_volume": zscore(-np.log(dv.replace(0, np.nan))),
               "momentum_12_1": zscore(close.shift(21) / close.shift(252) - 1.0)}

    rets, _ = split(rets_all)
    rets = rets.dropna(how="all")
    full = pit_mask(rets.index, list(rets.columns))

    # Survivor arm: names with essentially complete history = today's index, looking back.
    complete = rets.columns[rets.notna().sum() >= 0.95 * len(rets)]
    survivor = pd.DataFrame(False, index=rets.index, columns=rets.columns)
    survivor[complete] = True

    print(f"PIT universe: {int(full.sum(axis=1).mean())} names/day avg | "
          f"survivor universe: {len(complete)} names\n")

    flat = pd.DataFrame(0.0, index=rets.index, columns=rets.columns)
    rows, n_tr = [], len(signals) * len(LAMS) * 2
    for arm, mask in (("survivor-biased", survivor), ("point-in-time", full)):
        bench, bturn = backtest(flat, rets, mask, 0.0)
        bh = sharpe(bench)
        se = block_bootstrap_se(bench, sharpe, block=21, n_boot=2000)
        print(f"=== {arm} === EW benchmark Sharpe {bh:.3f} CAGR {summarise(bench)['cagr']:.2%} "
              f"| SE(SR) {se:.3f}")
        print(f"{'signal':>18} {'lam':>5} {'turn/yr':>8} {'rho':>6} {'ex@0':>7} {'ex@5':>7} "
              f"{'ex@10':>7} {'ex@20':>7} {'t':>6} {'floor':>7}")
        for sname, z in signals.items():
            for lam in LAMS:
                g, turn = backtest(z, rets, mask, lam)
                yrs = len(rets) / TRADING_DAYS
                rho = float(pd.concat([g, bench], axis=1).dropna().corr().iloc[0, 1])
                se_d = se * np.sqrt(max(2 * (1 - rho), 1e-9))
                row = {"arm": arm, "signal": sname, "lam": lam, "rho": rho,
                       "turnover": float(turn.sum() / yrs), "bench_sharpe": bh}
                for bps in COST_BPS_GRID:
                    net = g - turn * bps / 1e4
                    row[f"ex_{bps:g}"] = sharpe(net) - bh
                    row[f"cagr_{bps:g}"] = summarise(net)["cagr"]
                row["t"] = row["ex_5"] / se_d if se_d > 0 else np.nan
                row["floor"] = deflated_threshold(n_tr, se_d)
                rows.append(row)
                print(f"{sname:>18} {lam:>5.2f} {row['turnover']:>8.2f} {rho:>6.3f} "
                      f"{row['ex_0']:>+7.3f} {row['ex_5']:>+7.3f} {row['ex_10']:>+7.3f} "
                      f"{row['ex_20']:>+7.3f} {row['t']:>6.2f} {row['floor']:>7.3f}")
        print()

    r = pd.DataFrame(rows)
    r.to_csv("edge/p6_illiquidity.csv", index=False)
    piv = r.pivot_table(index=["signal", "lam"], columns="arm", values="ex_5")
    piv["survivorship_inflation"] = piv["survivor-biased"] - piv["point-in-time"]
    print("excess Sharpe @5bps, and how much of it was survivorship:")
    print(piv.to_string(float_format=lambda v: f"{v:+.3f}"))

    pit_rows = r[r.arm == "point-in-time"]
    best = pit_rows.loc[pit_rows["ex_5"].idxmax()]
    print(f"\nbest surviving PIT: {best['signal']} lam={best['lam']} ex@5={best['ex_5']:+.3f} "
          f"t={best['t']:.2f} floor={best['floor']:.3f} -> "
          f"{'PASS' if best['ex_5'] > best['floor'] else 'FAIL'}")
    log_trial("P6", "illiquidity tilt, survivorship-biased vs point-in-time", "TRAIN",
              f"best PIT {best['signal']} lam={best['lam']} ex@5={best['ex_5']:+.3f}",
              {k: float(best[k]) for k in ("lam", "ex_0", "ex_5", "ex_20", "t", "floor", "rho")},
              "PASS" if best["ex_5"] > best["floor"] else "fail")


if __name__ == "__main__":
    main()
