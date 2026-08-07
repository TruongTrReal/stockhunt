"""Plan 8 - spend the holdout on the single best candidate. One look, recorded.

Seven plans produced exactly one candidate worth this: portfolio volatility targeting.
It is the only idea that is simultaneously (a) the provable shape P0 identified, a
high-rho overlay on an always-invested book; (b) cost-tolerant, +0.282 excess Sharpe
still standing at 10bps; (c) motivated by something outside this dataset, since
volatility really is autocorrelated and forecastable while returns are not - so unlike
every other candidate here it was not discovered by searching, and does not carry the
same multiple-testing debt.

It did NOT clear the in-sample floor (+0.313 against 0.563 for 4 trials, t=1.50), so
this is not a confirmation run. It is the opposite: an out-of-sample check that can
only kill the candidate or leave it standing. Declared before running:

  * Parameters are FROZEN at what P7 used - 15% target, 20-day lookback, caps 1.0 and
    2.0. Nothing is refit on holdout. If it needs new parameters to work, it failed.
  * Two configurations = two looks, and the ledger records them as such.
  * PASS requires the sign to hold and the magnitude to be within a plausible band of
    the train estimate. A holdout excess Sharpe near zero or negative kills it outright,
    regardless of how good the train number looked.

After this the holdout is dirty for this hypothesis family and must not be reused for
it. That is the cost of looking, and it is why nothing else was allowed near it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import (COST_BPS_GRID, HOLDOUT_START, TRADING_DAYS, block_bootstrap_se,
                    deflated_threshold, load_daily, log_trial, sharpe, split, summarise)

TARGET_VOL = 0.15
LOOKBACK = 20
CAPS = [1.0, 2.0]


def vol_target(base: pd.Series, lookback: int, cap: float, warmup: pd.Series | None = None):
    """Exposure = target/realised, capped, known one day in advance.

    `warmup` supplies the trailing window that precedes the evaluation period, so the
    holdout run does not start with `lookback` days of zero exposure — without it the
    first month of holdout would sit in cash and understate the strategy.
    """
    series = pd.concat([warmup, base]) if warmup is not None else base
    rv = series.rolling(lookback).std() * np.sqrt(TRADING_DAYS)
    exp = (TARGET_VOL / rv).clip(upper=cap).shift(1).reindex(base.index).fillna(0.0)
    gross = exp * base
    turn = exp.diff().abs().fillna(exp.abs())
    return gross, turn, exp


def report(label: str, base: pd.Series, gross: pd.Series, turn: pd.Series,
           exp: pd.Series, se: float, n_trials: int) -> dict:
    bh_sr = sharpe(base)
    b = summarise(base)
    yrs = len(base) / TRADING_DAYS
    rho = float(pd.concat([gross, base], axis=1).dropna().corr().iloc[0, 1])
    se_d = se * np.sqrt(max(2 * (1 - rho), 1e-9))
    out = {"period": label, "bh_sharpe": bh_sr, "bh_cagr": b["cagr"], "bh_dd": b["max_dd"],
           "rho": rho, "turnover": float(turn.sum() / yrs), "avg_exposure": float(exp.mean())}
    for bps in COST_BPS_GRID:
        s = summarise((gross - turn * bps / 1e4).clip(lower=-0.999))
        out[f"ex_{bps:g}"] = s["sharpe"] - bh_sr
        out[f"sr_{bps:g}"], out[f"cagr_{bps:g}"], out[f"dd_{bps:g}"] = (
            s["sharpe"], s["cagr"], s["max_dd"])
    out["t"] = out["ex_5"] / se_d if se_d > 0 else np.nan
    out["floor"] = deflated_threshold(n_trials, se_d)
    return out


def main() -> None:
    data = load_daily()
    rets_all = pd.DataFrame({t: df["Close"] for t, df in data.items()}).pct_change()
    rets_all = rets_all.dropna(how="all").fillna(0.0)
    base_all = rets_all.mean(axis=1)
    tr, ho = split(base_all)

    print(f"TRAIN   {tr.index[0].date()} -> {tr.index[-1].date()}  ({len(tr)} days)")
    print(f"HOLDOUT {ho.index[0].date()} -> {ho.index[-1].date()}  ({len(ho)} days)  "
          f"<- opened now, for this hypothesis only\n")

    rows = []
    for cap in CAPS:
        for label, series, warm in (("TRAIN", tr, None),
                                    ("HOLDOUT", ho, tr.tail(LOOKBACK * 3))):
            se = block_bootstrap_se(series, sharpe, block=21, n_boot=3000)
            g, t_, e = vol_target(series, LOOKBACK, cap, warmup=warm)
            row = report(label, series, g, t_, e, se, n_trials=len(CAPS))
            row["cap"] = cap
            rows.append(row)

    r = pd.DataFrame(rows)
    for cap in CAPS:
        sub = r[r.cap == cap]
        print(f"=== vol target {TARGET_VOL:.0%}, {LOOKBACK}d lookback, cap {cap:.1f}x ===")
        print(f"{'period':>8} {'B&H SR':>7} {'B&H CAGR':>9} {'B&H DD':>7} | "
              f"{'SR@5':>6} {'CAGR@5':>8} {'DD@5':>7} {'ex@0':>7} {'ex@5':>7} {'ex@10':>7} "
              f"{'ex@20':>7} {'t':>6}")
        for _, x in sub.iterrows():
            print(f"{x['period']:>8} {x['bh_sharpe']:>7.3f} {x['bh_cagr']:>9.2%} "
                  f"{x['bh_dd']:>7.1%} | {x['sr_5']:>6.3f} {x['cagr_5']:>8.2%} "
                  f"{x['dd_5']:>7.1%} {x['ex_0']:>+7.3f} {x['ex_5']:>+7.3f} "
                  f"{x['ex_10']:>+7.3f} {x['ex_20']:>+7.3f} {x['t']:>6.2f}")
        tr_ex = float(sub[sub.period == "TRAIN"]["ex_5"].iloc[0])
        ho_ex = float(sub[sub.period == "HOLDOUT"]["ex_5"].iloc[0])
        verdict = ("HOLDS - sign and rough magnitude survive" if ho_ex > 0.5 * tr_ex
                   else "SURVIVES WEAKLY - sign holds, magnitude decays" if ho_ex > 0
                   else "KILLED - sign flips out of sample")
        print(f"  train {tr_ex:+.3f} -> holdout {ho_ex:+.3f}   {verdict}\n")
        log_trial("P8", f"HOLDOUT look: vol target {LOOKBACK}d cap {cap}", "HOLDOUT",
                  f"train {tr_ex:+.3f} -> holdout {ho_ex:+.3f}",
                  {"train_ex_5": tr_ex, "holdout_ex_5": ho_ex,
                   "holdout_cagr_5": float(sub[sub.period == "HOLDOUT"]["cagr_5"].iloc[0]),
                   "holdout_dd_5": float(sub[sub.period == "HOLDOUT"]["dd_5"].iloc[0])},
                  verdict)

    r.to_csv("edge/p8_holdout.csv", index=False)


if __name__ == "__main__":
    main()
