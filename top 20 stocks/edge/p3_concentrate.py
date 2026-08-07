"""Plan 3 - is the overnight tilt real, is it new, and does concentration help?

P2 left one lead: an overnight-share tilt worth +0.169 excess Sharpe at 5bps, almost
untouched by cost, but marginal against even a single-test floor of 0.183. Three
questions decide whether it survives, and they have to be asked in this order:

  1. IS IT NEW?  overnight_share is a trailing 252-day sum of overnight returns, and
     momentum_12_1 is a trailing 12-month price change. Those may be the same bet with
     different labels, in which case there is no new information here at all - just
     momentum, which is known, already priced, and not worth rediscovering. Measured by
     cross-sectional rank correlation of the signals, and by orthogonalising one
     against the other and re-testing the residual.

  2. DOES CONCENTRATION HELP?  Every P2 tilt sat at rho~0.95 to the benchmark, meaning
     the weights barely moved off equal. If the signal is real, leaning harder should
     scale the excess return. If it is noise, leaning harder scales the noise instead
     and the t-statistic stays flat. That distinction is the actual test: effect size
     rising with concentration while the t-stat holds is signal; t-stat decaying is not.
     Weights are w ~ exp(lambda * z), so lambda=0 is exactly equal weight.

  3. WHAT DOES IT COST?  Concentration raises turnover and lowers rho, which raises the
     detection floor. Reported at every step so the trade-off is visible.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import (COST_BPS_GRID, HEADLINE_COST_BPS, TRADING_DAYS, block_bootstrap_se,
                    deflated_threshold, load_daily, log_trial, sharpe, split, summarise)
from p2_tilts import drifted_turnover, rebalance_dates

LOOKBACK = 252


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score, row by row — puts every signal on one scale so the
    concentration parameter means the same thing regardless of the signal's units."""
    mu = df.mean(axis=1)
    sd = df.std(axis=1).replace(0.0, np.nan)
    return df.sub(mu, axis=0).div(sd, axis=0)


def exp_weights(z: pd.DataFrame, lam: float, rebal, idx) -> pd.DataFrame:
    """w ~ exp(lambda * z), long-only and fully invested by construction."""
    w = {}
    for d in rebal:
        s = z.loc[:d]
        if s.empty:
            continue
        s = s.iloc[-1].fillna(0.0).clip(-3, 3)
        e = np.exp(lam * s)
        w[d] = e / e.sum()
    return pd.DataFrame(w).T.reindex(idx).ffill() if w else pd.DataFrame(index=idx)


def run(z: pd.DataFrame, lam: float, rets: pd.DataFrame, freq: str):
    rebal = rebalance_dates(rets.index, freq)
    tgt = exp_weights(z, lam, rebal, rets.index).fillna(1.0 / rets.shape[1])
    held, turn = drifted_turnover(tgt, rets, rebal)
    return (held.shift(1) * rets).sum(axis=1), turn, held


def main() -> None:
    data = load_daily()
    close = pd.DataFrame({t: df["Close"] for t, df in data.items()})
    opn = pd.DataFrame({t: df["Open"] for t, df in data.items()})
    rets_all = close.pct_change()
    on_all = opn / close.shift(1) - 1.0

    rets, _ = split(rets_all)
    rets = rets.dropna(how="all").fillna(0.0)
    on = on_all.reindex(rets.index).fillna(0.0)
    close_tr = close.reindex(rets.index)

    sig_on = on.rolling(LOOKBACK).sum()
    sig_mom = close_tr.shift(21) / close_tr.shift(LOOKBACK) - 1.0
    z_on, z_mom = zscore(sig_on), zscore(sig_mom)

    # ---- Q1: is the overnight tilt just momentum? ----------------------------
    both = pd.concat([z_on.stack(), z_mom.stack()], axis=1).dropna()
    both.columns = ["overnight", "momentum"]
    pear = float(both.corr().iloc[0, 1])
    spear = float(both.corr(method="spearman").iloc[0, 1])
    daily_rank = z_on.corrwith(z_mom, axis=1).mean()
    print(f"signal overlap: pooled Pearson {pear:+.3f}   Spearman {spear:+.3f}   "
          f"mean daily cross-sectional corr {daily_rank:+.3f}")

    # Residual of overnight after projecting out momentum, cross-section by cross-section.
    resid = {}
    for d in z_on.index:
        a, b = z_on.loc[d], z_mom.loc[d]
        ok = a.notna() & b.notna()
        if ok.sum() < 5:
            continue
        beta = np.polyfit(b[ok], a[ok], 1)[0]
        resid[d] = a - beta * b
    z_resid = zscore(pd.DataFrame(resid).T.reindex(z_on.index))
    print(f"residual overnight signal built on {int(z_resid.notna().any(axis=1).sum())} days\n")

    flat = pd.DataFrame(0.0, index=rets.index, columns=rets.columns)
    bench, _, _ = run(flat, 0.0, rets, "ME")
    bh_sr = sharpe(bench)
    se_sr = block_bootstrap_se(bench, sharpe, block=21, n_boot=3000)
    print(f"EW benchmark Sharpe {bh_sr:.3f}   SE(SR) {se_sr:.3f}\n")

    cands = {"overnight": z_on, "momentum": z_mom, "overnight_resid_of_mom": z_resid,
             "blend_on_plus_mom": zscore(z_on.fillna(0) + z_mom.fillna(0))}
    lams = [0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0]
    n_tr = len(cands) * len(lams)

    rows = []
    print(f"{'signal':>24} {'lam':>5} {'turn/yr':>8} {'rho':>6} {'maxw':>6} "
          f"{'ex@0':>7} {'ex@5':>7} {'ex@10':>7} {'t':>6} {'floor16':>8}")
    for nm, z in cands.items():
        for lam in lams:
            gross, turn, held = run(z, lam, rets, "ME")
            yrs = len(rets) / TRADING_DAYS
            tpy = float(turn.sum() / yrs)
            rho = float(pd.concat([gross, bench], axis=1).dropna().corr().iloc[0, 1])
            se_d = se_sr * np.sqrt(max(2 * (1 - rho), 1e-9))
            row = {"signal": nm, "lam": lam, "turnover": tpy, "rho": rho,
                   "max_weight": float(held.max().max()), "se_d": se_d}
            for bps in COST_BPS_GRID:
                net = gross - turn * bps / 1e4
                row[f"ex_{bps:g}"] = sharpe(net) - bh_sr
                row[f"cagr_{bps:g}"] = summarise(net)["cagr"]
            row["t"] = row["ex_5"] / se_d if se_d > 0 else np.nan
            row["floor"] = deflated_threshold(n_tr, se_d)
            rows.append(row)
            print(f"{nm:>24} {lam:>5.2f} {tpy:>8.2f} {rho:>6.3f} {row['max_weight']:>6.1%} "
                  f"{row['ex_0']:>+7.3f} {row['ex_5']:>+7.3f} {row['ex_10']:>+7.3f} "
                  f"{row['t']:>6.2f} {row['floor']:>8.3f}")

    r = pd.DataFrame(rows)
    r.to_csv("edge/p3_concentrate.csv", index=False)
    best = r.loc[r["ex_5"].idxmax()]
    print(f"\nbest: {best['signal']} lam={best['lam']}  ex@5bps {best['ex_5']:+.3f}  "
          f"t={best['t']:.2f}  floor(16 trials)={best['floor']:.3f}  "
          f"-> {'PASS' if best['ex_5'] > best['floor'] else 'FAIL'}")
    log_trial("P3", "concentration sweep + momentum orthogonalisation", "TRAIN",
              f"best {best['signal']} lam={best['lam']} ex@5={best['ex_5']:+.3f}",
              {"pearson_on_vs_mom": pear, "spearman": spear,
               "best_signal": best["signal"], "best_lam": float(best["lam"]),
               "best_ex_5": float(best["ex_5"]), "best_t": float(best["t"]),
               "floor": float(best["floor"])},
              "PASS" if best["ex_5"] > best["floor"] else "fail")


if __name__ == "__main__":
    main()
