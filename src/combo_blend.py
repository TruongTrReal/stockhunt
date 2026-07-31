"""Cut drawdown by DIVERSIFYING the combo, not by filtering it.

combo_layers.py showed every risk-reducing filter fails out of sample, and the
reason is structural: the combo is a dip buyer whose edge is buying the rebound,
so de-risking during a decline removes the edge exactly when it pays. Drawdown
control actually made the test drawdown worse (-13.3% vs -11.1%).

Diversification attacks the same problem from the opposite side. Trend following
is the natural complement: it loses in the choppy, mean-reverting markets where
dip buying thrives, and it makes money in the sustained declines where dip
buying bleeds. Two streams with a negative correlation in exactly the regime
that hurts is worth more than any filter on one of them.

Each leg is normalised to equal volatility on TRAIN before blending, so the
blend weight means what it says, and the blended book is then levered to a
common target volatility - again on train only. Comparing at equal risk is what
makes "lower drawdown" a real gain rather than a smaller position.

    python combo_blend.py
"""

import numpy as np
import pandas as pd

from beat_buyhold_search import Evaluator, TRADING_DAYS, _transform
from beat_buyhold_search2 import Space2
from sharpe_search import SharpeEvaluator, sharpe_of
from signal_tensor import CACHE_PATH, load

OUT = "../data/combo_blend_results.csv"
COST_BPS = 5.0
TARGET_VOL = 0.12
MAX_LEVERAGE = 3.0
BASE = ("~ADOSC + ~CDLCLOSINGMARUBOZU + ~VAR", "defensive")
WEIGHTS = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.65)


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
    s_tr, e_tr = se.slices["train"]
    s_te, e_te = se.slices["test"]
    valid = se.valid

    def book(w, s, e, costs=True):
        held = w[s - 1:e - 1] if s > 0 else np.vstack([np.zeros((1, N), w.dtype), w[:e - 1]])
        m, R = valid[s:e], se.R[s:e]
        n = np.maximum(m.sum(axis=1), 1)
        out = ((held * R) * m).sum(axis=1) / n
        if costs:
            first = w[s - 2:s - 1] if s >= 2 else np.zeros((1, N), w.dtype)
            out = out - (np.abs(np.diff(np.vstack([first, held]), axis=0)) * m).sum(axis=1) / n \
                * COST_BPS / 1e4
        return out

    atoms = tuple(sorted((names.index(t.lstrip("~")), -1 if t.startswith("~") else 1)
                         for t in BASE[0].split(" + ")))
    base = sp.build2(atoms, None, 1, BASE[1], "none").astype(np.float32)

    # Diversifier candidates. Trend rules first (the structural complement),
    # plus the SPY regime overlay as a pure market-timing stream.
    spy = z["spy"]
    spy_sma = pd.Series(spy).rolling(200, min_periods=200).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        spy_trend = np.where(np.isnan(spy_sma), 0.0, np.where(spy > spy_sma, 1.0, -1.0))
    candidates = {}
    for nm in ("SMA_200", "EMA_200", "MA_1000", "SMA_50", "HT_TRENDMODE"):
        if nm in names:
            candidates[nm] = z["tensor"][names.index(nm)].astype(np.float32)
    candidates["SPY_TREND"] = np.broadcast_to(spy_trend[:, None], (T, N)).astype(np.float32)
    candidates["SMA_200_longonly"] = _transform(
        z["tensor"][names.index("SMA_200")], "longonly").astype(np.float32)

    def unit_vol(w):
        """Scale a leg to 1% annual vol on train so blend weights are comparable."""
        v = float(book(w, s_tr, e_tr).std(ddof=1) * np.sqrt(TRADING_DAYS))
        return w / max(v, 1e-9)

    base_u = unit_vol(base)
    r_base_tr = book(base, s_tr, e_tr)

    rows = []
    for dname, dpos in candidates.items():
        div_u = unit_vol(dpos)
        rho = float(np.corrcoef(r_base_tr, book(dpos, s_tr, e_tr))[0, 1])
        for a in WEIGHTS:
            w = ((1 - a) * base_u + a * div_u).astype(np.float32)
            v_tr = float(book(w, s_tr, e_tr).std(ddof=1) * np.sqrt(TRADING_DAYS))
            L = min(TARGET_VOL / max(v_tr, 1e-9), MAX_LEVERAGE)
            wl = (w * L).astype(np.float32)
            rec = {"diversifier": dname, "weight": a, "corr_train": rho, "leverage": L}
            for lbl, (s, e) in (("train", (s_tr, e_tr)), ("test", (s_te, e_te))):
                rec.update({f"{lbl}_{k}": v for k, v in curve_stats(book(wl, s, e)).items()})
            rows.append(rec)

    df = pd.DataFrame(rows)
    df.to_csv(OUT, index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)

    bh_te = curve_stats(book(np.ones((T, N), np.float32), s_te, e_te, costs=False))
    b0 = df[(df.weight == 0)].iloc[0]
    print(f"All books levered on TRAIN to {TARGET_VOL:.0%} vol -> equal-risk comparison.\n")
    print(f"buy & hold      test: CAGR {bh_te['cagr']:>7.2%}  maxDD {bh_te['maxdd']:>8.2%}  "
          f"Sharpe {bh_te['sharpe']:.3f}")
    print(f"combo alone     test: CAGR {b0['test_cagr']:>7.2%}  maxDD {b0['test_maxdd']:>8.2%}  "
          f"Sharpe {b0['test_sharpe']:.3f}  (train maxDD {b0['train_maxdd']:.2%})\n")

    print("Correlation of each diversifier with the combo, on train:")
    print(df.groupby("diversifier").corr_train.first().sort_values().to_string(
        float_format=lambda x: f"{x:+.3f}"))

    cols = ["diversifier", "weight", "corr_train", "leverage", "train_cagr", "train_maxdd",
            "train_sharpe", "train_calmar", "test_cagr", "test_maxdd", "test_sharpe", "test_calmar"]
    print("\nTop 12 blends by TRAIN Calmar (selection is train-only):")
    print(df[df.weight > 0].sort_values("train_calmar", ascending=False).head(12)[cols]
          .to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    better = df[(df.weight > 0) & (df.test_maxdd > b0["test_maxdd"])
                & (df.test_sharpe > b0["test_sharpe"])]
    print(f"\n{len(better)} blends improve BOTH test drawdown and test Sharpe vs the combo alone:")
    if len(better):
        print(better.sort_values("test_calmar", ascending=False)[cols].to_string(
            index=False, float_format=lambda x: f"{x:.4f}"))


if __name__ == "__main__":
    main()
