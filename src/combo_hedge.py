"""Hedge the dip-buying combo with a market-trend overlay, and validate it.

combo_blend.py found the mechanism: a small allocation to a market-trend stream
improves the combo's drawdown AND its Sharpe, while every filter-style layer
failed. The distinction matters - a regime FILTER can only hold less, whereas a
trend overlay goes short the universe in a downtrend and therefore actually
hedges. That is why the filter lost 4-8pp of return for nothing and the overlay
does not.

The per-ticker trend rules (SMA_200 at -0.31 correlation) score better on train
but decay on test: they are poor standalone strategies, so their return drag
eventually outweighs the variance benefit. The market-level overlay carries far
less drag, so this sweeps overlay lookbacks and weights rather than per-ticker
trend rules.

Validation matches the rest of the project: parameters on TRAIN only, must beat
the unhedged combo in BOTH train halves, survive an extra bar of execution lag,
and clear a cost sensitivity check. Significance is the Jobson-Korkie/Memmel z
against the UNHEDGED COMBO - the question is whether the hedge adds anything,
not whether the whole thing beats buy-and-hold, which is already established.

    python combo_hedge.py
"""

import math

import numpy as np
import pandas as pd

from beat_buyhold_search import Evaluator, TRADING_DAYS
from beat_buyhold_search2 import Space2
from sharpe_search import SharpeEvaluator, memmel_z, sharpe_of
from signal_tensor import CACHE_PATH, load

OUT = "../data/combo_hedge_results.csv"
COST_BPS = 5.0
TARGET_VOL = 0.12
MAX_LEVERAGE = 3.0
BASE = ("~ADOSC + ~CDLCLOSINGMARUBOZU + ~VAR", "defensive")
LOOKBACKS = (50, 100, 150, 200, 250)
WEIGHTS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40)


def curve_stats(r):
    eq = np.cumprod(1 + np.clip(r, -0.999, None))
    yrs = len(r) / TRADING_DAYS
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    cagr = float(eq[-1] ** (1 / yrs) - 1) if eq[-1] > 0 else np.nan
    return {"cagr": cagr, "vol": float(r.std(ddof=1) * np.sqrt(TRADING_DAYS)),
            "sharpe": sharpe_of(r), "maxdd": dd,
            "calmar": cagr / abs(dd) if dd < 0 else np.nan}


def main():
    z = load(CACHE_PATH)
    ev = Evaluator(z)
    se = SharpeEvaluator(z, ev)
    sp = Space2(z)
    names = list(z["names"])
    T, N = z["returns"].shape
    valid = se.valid
    s_tr, e_tr = se.slices["train"]

    def book(w, s, e, costs=True, extra_lag=False):
        p = np.vstack([np.zeros((1, N), w.dtype), w[:-1]]) if extra_lag else w
        held = p[s - 1:e - 1] if s > 0 else np.vstack([np.zeros((1, N), p.dtype), p[:e - 1]])
        m, R = valid[s:e], se.R[s:e]
        n = np.maximum(m.sum(axis=1), 1)
        out = ((held * R) * m).sum(axis=1) / n
        if costs:
            first = p[s - 2:s - 1] if s >= 2 else np.zeros((1, N), p.dtype)
            out = out - (np.abs(np.diff(np.vstack([first, held]), axis=0)) * m).sum(axis=1) / n \
                * COST_BPS / 1e4
        return out

    def turnover(w, s, e):
        held = w[s - 1:e - 1] if s > 0 else np.vstack([np.zeros((1, N), w.dtype), w[:e - 1]])
        m = valid[s:e]
        n = np.maximum(m.sum(axis=1), 1)
        first = w[s - 2:s - 1] if s >= 2 else np.zeros((1, N), w.dtype)
        return (np.abs(np.diff(np.vstack([first, held]), axis=0)) * m).sum(axis=1) / n

    atoms = tuple(sorted((names.index(t.lstrip("~")), -1 if t.startswith("~") else 1)
                         for t in BASE[0].split(" + ")))
    base = sp.build2(atoms, None, 1, BASE[1], "none").astype(np.float32)

    def unit_vol(w):
        v = float(book(w, s_tr, e_tr).std(ddof=1) * np.sqrt(TRADING_DAYS))
        return w / max(v, 1e-9)

    base_u = unit_vol(base)

    def matched(w):
        v = float(book(w, s_tr, e_tr).std(ddof=1) * np.sqrt(TRADING_DAYS))
        return (w * min(TARGET_VOL / max(v, 1e-9), MAX_LEVERAGE)).astype(np.float32)

    ref = matched(base_u)                      # unhedged combo, risk-matched
    ref_stats = {w: curve_stats(book(ref, *se.slices[w]))
                 for w in ("train_a", "train_b", "train", "test")}

    spy = z["spy"]
    rows = []
    for lb in LOOKBACKS:
        sma = pd.Series(spy).rolling(lb, min_periods=lb).mean().to_numpy()
        with np.errstate(invalid="ignore"):
            trend = np.where(np.isnan(sma), 0.0, np.where(spy > sma, 1.0, -1.0))
        hedge_u = unit_vol(np.broadcast_to(trend[:, None], (T, N)).astype(np.float32))
        for a in WEIGHTS:
            w = matched(((1 - a) * base_u + a * hedge_u).astype(np.float32))
            rec = {"lookback": lb, "weight": a}
            for lbl in ("train_a", "train_b", "train", "test"):
                s, e = se.slices[lbl]
                rec.update({f"{lbl}_{k}": v for k, v in curve_stats(book(w, s, e)).items()})
            s, e = se.slices["test"]
            r_net = book(w, s, e)
            r_ref = book(ref, s, e)
            rec["z_vs_combo"] = memmel_z(r_net, r_ref)[0]
            rec["test_sharpe_lag2"] = sharpe_of(book(w, s, e, extra_lag=True))
            tv = turnover(w, s, e)
            gross = book(w, s, e, costs=False)
            rec["turnover_yr"] = float(tv.mean() * 252)
            for c in (0, 10, 20):
                rec[f"sharpe_{c}bps"] = sharpe_of(gross - tv * c / 1e4)
            rows.append(rec)

    df = pd.DataFrame(rows)
    df.to_csv(OUT, index=False)
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 40)

    print(f"All books levered on train to {TARGET_VOL:.0%} vol.\n")
    print("Unhedged combo, risk-matched:")
    for w in ("train_a", "train_b", "train", "test"):
        st = ref_stats[w]
        print(f"  {w:<9} CAGR {st['cagr']:>7.2%}  maxDD {st['maxdd']:>8.2%}  "
              f"Sharpe {st['sharpe']:.3f}  Calmar {st['calmar']:.3f}")

    # Selection: must beat the unhedged combo's Calmar on train AND in both
    # halves separately. Only then is the test column consulted.
    ok = df[(df.train_calmar > ref_stats["train"]["calmar"])
            & (df.train_a_calmar > ref_stats["train_a"]["calmar"])
            & (df.train_b_calmar > ref_stats["train_b"]["calmar"])]
    print(f"\n{len(ok)} of {len(df)} hedge configs beat the unhedged combo's Calmar "
          f"on train AND in both halves")

    cols = ["lookback", "weight", "train_calmar", "train_a_calmar", "train_b_calmar",
            "test_cagr", "test_maxdd", "test_sharpe", "test_calmar",
            "test_sharpe_lag2", "z_vs_combo", "turnover_yr", "sharpe_0bps",
            "sharpe_10bps", "sharpe_20bps"]
    if len(ok):
        print(ok.sort_values("train_calmar", ascending=False)[cols].to_string(
            index=False, float_format=lambda x: f"{x:.4f}"))

    survive = ok[(ok.test_maxdd > ref_stats["test"]["maxdd"])
                 & (ok.test_sharpe > ref_stats["test"]["sharpe"])
                 & (ok.test_sharpe_lag2 > ref_stats["test"]["sharpe"])
                 & (ok.sharpe_20bps > 0)]
    print(f"\n{len(survive)} also improve BOTH test drawdown and test Sharpe, "
          f"survive an extra bar of lag, and stay positive at 20bps:")
    if len(survive):
        print(survive.sort_values("test_calmar", ascending=False)[cols].to_string(
            index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()
