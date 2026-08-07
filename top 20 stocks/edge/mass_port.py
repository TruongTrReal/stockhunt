"""Stage 3: turn the surviving signals into portfolios and charge them for trading.

A significant IC is a statement about rank prediction, not about money. The gap between
the two has killed every lead in this project so far, and the survivors from stage 2
split cleanly along it:

  * the top-20 winners (idio vol, 12m momentum) do not appear on the S&P-PIT
    leaderboard at all, and the idio-vol sign says HIGH idiosyncratic vol predicts HIGH
    returns - which on 2015-2022 mega-caps is a description of NVDA and TSLA, not an
    anomaly. Tested here anyway, because "I think it is an artefact" is a hypothesis,
    not a result;
  * the S&P-PIT winners are nearly all one family, cross-sectional reversal, and it
    survives at the 21-day horizon as well as at 1 day. The 1-day version rebalances
    daily and P0 already priced that at 25%/yr in costs at 5bps, so it is dead on
    arrival. The 21-day version is the only survivor whose statistics and cost profile
    point the same way.

Both expressions are built for each signal, because they answer different questions:
LONG-ONLY tilt (can this improve a book I already hold?) and DOLLAR-NEUTRAL long-short
(is there alpha here at all, independent of market direction?). The long-short book is
the one cross_sectional_backtest.py used, and it is where a reversal signal should be
strongest, since reversal has no market-direction view in it.

PIT membership is enforced throughout - P6 showed a size/liquidity tilt loses its
entire edge without it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import (COST_BPS_GRID, TRADING_DAYS, block_bootstrap_se, deflated_threshold,
                    log_trial, sharpe, split, summarise)
from hypotheses import build
from mass_ic import load, pit_mask

REBAL = {1: 5, 5: 5, 21: 21}          # trade weekly for <=5d signals, monthly for 21d
CANDIDATES = [
    ("S&P-PIT", "B_reversal_21", 21), ("S&P-PIT", "B_rel_reversal_21", 21),
    ("S&P-PIT", "B_reversal_10", 1), ("S&P-PIT", "B_reversal_5", 5),
    ("S&P-PIT", "B_reversal_1", 1), ("S&P-PIT", "F_on_share_21", 21),
    ("S&P-PIT", "A_mom_21", 21), ("S&P-PIT", "I_idio_mom_252", 1),
    ("top-20", "C_idio_vol_126", 21), ("top-20", "A_mom_252_skip21", 1),
    ("top-20", "A_mom_252", 1),
]


def zscore(df):
    mu, sd = df.mean(axis=1), df.std(axis=1).replace(0.0, np.nan)
    return df.sub(mu, axis=0).div(sd, axis=0)


def run_book(z, rets, mask, step, mode, lam=1.0):
    """mode='long' -> exp(lam*z) fully invested; 'ls' -> dollar-neutral, gross 1."""
    idx = rets.index
    rebal = idx[::step]
    port = pd.Series(0.0, index=idx)
    turn = pd.Series(0.0, index=idx)
    prev = None
    for i, start in enumerate(rebal):
        end = rebal[i + 1] if i + 1 < len(rebal) else idx[-1]
        window = rets.loc[start:end].iloc[1:]
        if window.empty:
            continue
        s = z.loc[:start]
        if s.empty:
            continue
        sig = s.iloc[-1]
        live = rets.columns[mask.loc[start] & sig.notna() & rets.loc[start].notna()]
        # Minimum must scale with the book — a flat 30 silently skipped every rebalance
        # on the 20-name universe and returned an all-NaN curve rather than an error.
        if len(live) < max(10, min(30, len(rets.columns) // 2)):
            continue
        x = sig[live].clip(-3, 3)
        if mode == "long":
            e = np.exp(lam * x)
            w = (e / e.sum())
        else:
            d = x - x.mean()
            w = d / d.abs().sum() if d.abs().sum() > 0 else d
        w = w.reindex(rets.columns).fillna(0.0)
        turn.loc[start] = float((w - prev).abs().sum()) if prev is not None else float(w.abs().sum())
        growth = (1 + window.fillna(0.0)).cumprod()
        if mode == "long":
            val = growth.mul(w, axis=1).sum(axis=1)
            port.loc[window.index] = val.pct_change().fillna(val.iloc[0] - 1.0)
            endv = growth.iloc[-1] * w
            prev = endv / endv.sum()
        else:
            port.loc[window.index] = window.mul(w, axis=1).sum(axis=1)
            prev = w * growth.iloc[-1].reindex(w.index).fillna(1.0)
            prev = prev / prev.abs().sum() if prev.abs().sum() > 0 else prev
    return port, turn


def main() -> None:
    close, opn, high, low, vol = load()
    bench_px = close["SPY"] if "SPY" in close.columns else None
    sig_all = build(close, opn, high, low, vol, bench_px)
    cols_all = list(close.columns)
    from common import UNIVERSE
    books = {"top-20": [t for t in UNIVERSE if t in cols_all],
             "S&P-PIT": [t for t in cols_all if t != "SPY"]}

    rows = []
    for bname, bcols in books.items():
        c = close[bcols]
        rets_all, _ = split(c.pct_change().dropna(how="all"))
        rets = rets_all.fillna(0.0)
        idx = rets.index
        mask = pd.DataFrame(
            pit_mask(idx, bcols) if bname == "S&P-PIT" else np.ones((len(idx), len(bcols)), bool),
            index=idx, columns=bcols)

        ew, ewturn = run_book(pd.DataFrame(0.0, index=idx, columns=bcols), rets, mask, 21, "long")
        bh = sharpe(ew)
        se = block_bootstrap_se(ew, sharpe, block=21, n_boot=2000)
        print(f"\n=== {bname} === EW benchmark Sharpe {bh:.3f} "
              f"CAGR {summarise(ew)['cagr']:.2%} | SE(SR) {se:.3f}")
        todo = [(s, h) for b, s, h in CANDIDATES if b == bname]
        n_tr = len(todo) * 2
        print(f"{'signal':>20} {'mode':>5} {'reb':>4} {'turn/yr':>8} {'SR@0':>7} {'SR@5':>7} "
              f"{'SR@10':>7} {'SR@20':>7} {'ex@5':>7} {'floor':>7} {'verdict':>8}")
        for sname, h in todo:
            z = zscore(sig_all[sname].reindex(index=idx, columns=bcols).where(mask))
            for mode in ("long", "ls"):
                g, turn = run_book(z, rets, mask, REBAL[h], mode)
                yrs = len(rets) / TRADING_DAYS
                tpy = float(turn.sum() / yrs)
                if mode == "long":
                    rho = float(pd.concat([g, ew], axis=1).dropna().corr().iloc[0, 1])
                    se_d = se * np.sqrt(max(2 * (1 - rho), 1e-9))
                    ref = bh
                else:
                    # Market-neutral book competes against zero, so the relevant SE is
                    # that of ITS OWN Sharpe, not the benchmark's.
                    se_d, ref = block_bootstrap_se(g, sharpe, block=21, n_boot=2000), 0.0
                srs = {}
                for bps in COST_BPS_GRID:
                    srs[bps] = sharpe((g - turn * bps / 1e4).clip(lower=-0.999))
                ex5 = srs[5.0] - ref
                floor = deflated_threshold(n_tr, se_d)
                ok = ex5 > floor
                rows.append({"book": bname, "signal": sname, "mode": mode, "turnover": tpy,
                             **{f"sr_{b:g}": srs[b] for b in COST_BPS_GRID},
                             "ex_5": ex5, "floor": floor, "pass": ok})
                print(f"{sname:>20} {mode:>5} {REBAL[h]:>4} {tpy:>8.1f} {srs[0.0]:>7.3f} "
                      f"{srs[5.0]:>7.3f} {srs[10.0]:>7.3f} {srs[20.0]:>7.3f} {ex5:>+7.3f} "
                      f"{floor:>7.3f} {'PASS' if ok else 'fail':>8}")

    r = pd.DataFrame(rows)
    r.to_csv("edge/mass_port.csv", index=False)
    winners = r[r["pass"]]
    print(f"\n{len(winners)} of {len(r)} portfolio tests clear their floor")
    if len(winners):
        print(winners[["book", "signal", "mode", "turnover", "sr_5", "ex_5", "floor"]]
              .to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    log_trial("MASS-PORT", "surviving signals as long-only and long-short books", "TRAIN",
              f"{len(winners)}/{len(r)} clear floor",
              {"n_tests": len(r), "n_pass": int(len(winners)),
               "best_ex5": float(r["ex_5"].max())},
              "PASS" if len(winners) else "fail")


if __name__ == "__main__":
    main()
