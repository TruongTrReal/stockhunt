"""Plan 7 - stop forecasting returns; scale risk instead.

Six plans have tried to predict which names go up, and all six died. P0 explains why
that was always the long shot: only an overlay correlated 0.95+ with the benchmark has
a detection floor (~0.19) low enough to be provable on 8 years, and every return
forecast tested so far either sat below that floor or evaporated under cost,
survivorship, or orthogonalisation.

Volatility targeting is the one remaining idea that fits the provable shape and has an
independent prior behind it. It forecasts *volatility*, not direction - and unlike
returns, volatility is genuinely autocorrelated and forecastable at short horizons.
Scale exposure by target/realised and the book stays ~0.97 correlated with buy-and-hold,
so the floor is low, while the mechanism (Moreira-Muir style vol management) is
documented and not a price-pattern rule of the kind this project has already exhausted.

Two honest caveats, stated before the numbers:

  * `sharpe-beating-combos` already found this project's timing survivors were "dip
    buyers, not vol timers". That is weak evidence against. It tested PER-NAME vol
    signals though; this is portfolio-level exposure scaling, which is a different
    object, so it earns one clean test rather than an assumption.
  * A levered variant that raises Sharpe by taking more risk in calm periods is only
    honest if the drawdown is reported too, because leverage moves tail risk somewhere
    the Sharpe ratio does not look. Both are printed.

Idle capital earns 0%, which is conservative for 2015-2022 when rates were near zero
for most of the window and mildly understates the de-risking variants.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import (COST_BPS_GRID, TRADING_DAYS, block_bootstrap_se, deflated_threshold,
                    load_daily, log_trial, sharpe, split, summarise)

TARGET_VOL = 0.15
LOOKBACKS = [20, 60]
CAPS = [1.0, 2.0]


def main() -> None:
    data = load_daily()
    close = pd.DataFrame({t: df["Close"] for t, df in data.items()})
    rets_all = close.pct_change()
    rets, _ = split(rets_all)
    rets = rets.dropna(how="all").fillna(0.0)

    base = rets.mean(axis=1)                       # equal-weight book = the thing to beat
    bh_sr = sharpe(base)
    se = block_bootstrap_se(base, sharpe, block=21, n_boot=3000)
    b = summarise(base)
    print(f"TRAIN {rets.index[0].date()} -> {rets.index[-1].date()}")
    print(f"EW buy&hold: Sharpe {bh_sr:.3f}  CAGR {b['cagr']:.2%}  vol {b['vol']:.2%}  "
          f"maxDD {b['max_dd']:.1%}  | SE(SR) {se:.3f}\n")

    n_tr = len(LOOKBACKS) * len(CAPS)
    print(f"{'lookback':>9} {'cap':>5} {'turn/yr':>8} {'rho':>6} {'avg_exp':>8} "
          f"{'CAGR@5':>8} {'maxDD':>7} {'ex@0':>7} {'ex@5':>7} {'ex@10':>7} {'t':>6} {'floor':>7}")
    rows = []
    for lb in LOOKBACKS:
        # Realised vol known at t-1 only; exposure applies to t. shift(1) twice in effect,
        # since `base` is already a same-day return series.
        rv = base.rolling(lb).std() * np.sqrt(TRADING_DAYS)
        for cap in CAPS:
            exp = (TARGET_VOL / rv).clip(upper=cap).shift(1).fillna(0.0)
            gross = exp * base
            turn = exp.diff().abs().fillna(exp.abs())
            yrs = len(rets) / TRADING_DAYS
            rho = float(pd.concat([gross, base], axis=1).dropna().corr().iloc[0, 1])
            se_d = se * np.sqrt(max(2 * (1 - rho), 1e-9))
            row = {"lookback": lb, "cap": cap, "turnover": float(turn.sum() / yrs),
                   "rho": rho, "avg_exposure": float(exp.mean())}
            for bps in COST_BPS_GRID:
                net = (gross - turn * bps / 1e4).clip(lower=-0.999)
                s = summarise(net)
                row[f"ex_{bps:g}"] = s["sharpe"] - bh_sr
                row[f"cagr_{bps:g}"], row[f"dd_{bps:g}"] = s["cagr"], s["max_dd"]
            row["t"] = row["ex_5"] / se_d if se_d > 0 else np.nan
            row["floor"] = deflated_threshold(n_tr, se_d)
            rows.append(row)
            print(f"{lb:>9d} {cap:>5.1f} {row['turnover']:>8.1f} {rho:>6.3f} "
                  f"{row['avg_exposure']:>8.2f} {row['cagr_5']:>8.2%} {row['dd_5']:>7.1%} "
                  f"{row['ex_0']:>+7.3f} {row['ex_5']:>+7.3f} {row['ex_10']:>+7.3f} "
                  f"{row['t']:>6.2f} {row['floor']:>7.3f}")

    r = pd.DataFrame(rows)
    r.to_csv("edge/p7_voltarget.csv", index=False)
    best = r.loc[r["ex_5"].idxmax()]
    ok = best["ex_5"] > best["floor"]
    print(f"\nbest: lookback={best['lookback']:.0f} cap={best['cap']:.1f} "
          f"ex@5bps={best['ex_5']:+.3f} t={best['t']:.2f} floor={best['floor']:.3f} "
          f"-> {'PASS' if ok else 'FAIL'}")
    log_trial("P7", "portfolio volatility targeting", "TRAIN",
              f"best ex@5={best['ex_5']:+.3f} t={best['t']:.2f}",
              {k: float(best[k]) for k in ("lookback", "cap", "ex_0", "ex_5", "ex_10",
                                           "t", "floor", "rho", "cagr_5", "dd_5")},
              "PASS" if ok else "fail")


if __name__ == "__main__":
    main()
