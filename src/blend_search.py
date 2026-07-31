"""Blend many signals CONTINUOUSLY instead of picking a few and signing them.

Every previous search collapsed the combined signal with sign(), which throws
away magnitude: "all 40 indicators agree" and "21 of 40 agree" both become a
full-size position. Two constructions here keep that information.

  graded   Weighted average of the top K signals, kept continuous in [-1, 1] and
      then amplified by a gain. Gain 1 is fully proportional (small position
      when indicators disagree); large gain approaches the old sign() behaviour,
      so the sweep contains the previous construction as a limiting case.
  tangency Mean-variance optimal weights, w proportional to inv(Sigma) mu over
      the signals' own return streams, with ridge shrinkage on Sigma. This is
      the best possible linear blend on train rather than an equal-weighted
      guess - and since everything here is 0.71-0.80 correlated, inv(Sigma) is
      where the little remaining independence would actually be exploited.

Shrinkage matters: with ~170 correlated signals and ~1760 train days the sample
covariance is near-singular, and unshrunk tangency weights are famously an
overfitting machine. Lambda is swept and chosen on train only.

    python blend_search.py [--top N]
"""

import argparse
import math
import time

import numpy as np
import pandas as pd

from beat_buyhold_search import Evaluator, _transform
from portfolio_search import MIN_EXPOSURE, PortfolioEvaluator
from sharpe_search import memmel_z, sharpe_of
from signal_tensor import CACHE_PATH, load

VARIANT_PATH = "../data/param_variant_tensor.npz"
OUT_CSV = "../data/blend_search_results.csv"
K_VALUES = (5, 10, 20, 40, 80, 168)
GAINS = (1.0, 1.5, 2.0, 3.0, 5.0, 10.0)
LAMBDAS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0)
HEDGES = (0.0, 0.3, 0.5)
CLIPS = ("both", "longonly")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=168)
    args = ap.parse_args()

    z = dict(np.load(VARIANT_PATH, allow_pickle=False))
    ev = Evaluator(z)
    pe = PortfolioEvaluator(z, ev)
    names = list(z["names"])
    tensor = z["tensor"]
    T, N = z["returns"].shape
    ones = np.ones((T, N), np.int8)

    spy = z["spy"]
    sma = pd.Series(spy).rolling(100, min_periods=100).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        below = np.where(np.isnan(sma), 0.0, (spy <= sma).astype(float))
    BEL = below[:, None].astype(np.float32)

    ch = tuple(sorted((names.index(n), -1) for n in
                      ("ADOSC_3_10_zero", "CDLCLOSINGMARUBOZU_hold5", "VAR_5_sma20")))
    acc = None
    for j, s in ch:
        t = (s * tensor[j]).astype(np.int8)
        acc = t if acc is None else (acc + t).astype(np.int8)
    champ = (_transform(np.sign(acc).astype(np.int8), "defensive").astype(np.float32)
             - 0.5 * BEL).astype(np.float32)
    ch_tr, ch_te = pe.sharpe(champ, "train"), pe.sharpe(champ, "test")
    print(f"champion  train {ch_tr:.4f}  test {ch_te:.4f}")

    # Rank atoms, then keep each one's train return stream for the tangency fit.
    t0 = time.time()
    atoms = [(j, s) for j in range(len(names)) for s in (1, -1)]
    scored = []
    for j, s in atoms:
        p = _transform((s * tensor[j]).astype(np.int8), "defensive")
        scored.append((pe.sharpe(p, "train"), j, s))
    scored.sort(reverse=True)
    keep = [(j, s) for _, j, s in scored[:args.top]]
    print(f"ranked {len(atoms)} atoms in {time.time()-t0:.0f}s; "
          f"best single train {scored[0][0]:.4f}, using top {len(keep)}")

    # Signal matrices and their gross train return streams.
    sigs = [(s * tensor[j]).astype(np.float32) for j, s in keep]
    Rtr = np.column_stack([pe.daily(_transform(p.astype(np.int8), "defensive"),
                                    "train", cost_bps=0) for p in sigs])
    mu = Rtr.mean(axis=0)
    Sig = np.cov(Rtr, rowvar=False)
    print(f"signal return matrix {Rtr.shape}; mean pairwise corr "
          f"{np.corrcoef(Rtr, rowvar=False)[np.triu_indices(len(keep),1)].mean():.3f}")

    results, n_eval = [], 0

    def consider(raw, tag, desc, extra):
        """raw is a continuous consensus in roughly [-1,1]."""
        nonlocal n_eval
        for clip in CLIPS:
            base = np.clip(raw, -1.0, 1.0) if clip == "both" else np.clip(raw, 0.0, 1.0)
            for b in HEDGES:
                w = base if b == 0 else (base - b * BEL)
                if np.abs(w).max() > 1.0 + 1e-6:
                    w = np.clip(w, -1.0, 1.0)
                n_eval += 1
                s = pe.objective(w.astype(np.float32), "train")
                if math.isfinite(s) and s > ch_tr * 0.9:
                    results.append((s, tag, desc, clip, b, w.astype(np.float32), extra))

    print("\ngraded blends (equal and Sharpe-weighted, swept gain)...")
    sharpe_w = np.array([max(sc, 0.0) for sc, _, _ in scored[:len(keep)]], dtype=np.float32)
    for K in K_VALUES:
        if K > len(keep):
            continue
        stack = np.zeros((T, N), np.float32)
        for p in sigs[:K]:
            stack += p
        eq = stack / K
        sw = np.zeros((T, N), np.float32)
        wsum = sharpe_w[:K].sum()
        if wsum > 0:
            for p, wt in zip(sigs[:K], sharpe_w[:K]):
                sw += wt * p
            sw /= wsum
        for g in GAINS:
            consider(eq * g, "graded_eq", f"top{K} equal gain{g}", {"K": K, "gain": g})
            if wsum > 0:
                consider(sw * g, "graded_sharpe", f"top{K} sharpe-wt gain{g}", {"K": K, "gain": g})
    print(f"  {n_eval:,} evals [{time.time()-t0:.0f}s]")

    print("tangency blends (inv(Sigma+lambda I) mu, shrinkage swept)...")
    for lam in LAMBDAS:
        A = Sig + lam * np.trace(Sig) / len(keep) * np.eye(len(keep))
        try:
            wts = np.linalg.solve(A, mu)
        except np.linalg.LinAlgError:
            continue
        denom = np.abs(wts).sum()
        if denom <= 0:
            continue
        wts = wts / denom
        blend = np.zeros((T, N), np.float32)
        for p, wt in zip(sigs, wts):
            if wt != 0:
                blend += np.float32(wt) * p
        # Scale so the blend spans a usable range rather than a sliver.
        scale = np.percentile(np.abs(blend[pe.slices["train"][0]:pe.slices["train"][1]]), 99)
        if scale <= 0:
            continue
        for g in (0.5, 1.0, 2.0):
            consider(blend / scale * g, "tangency", f"lambda{lam} gain{g}",
                     {"lambda": lam, "gain": g, "n_pos": int((wts > 0).sum())})
    print(f"  {n_eval:,} evals [{time.time()-t0:.0f}s]")

    print(f"\n{n_eval:,} evaluated, {len(results):,} passed the train screen. Validating...")
    results.sort(key=lambda r: -r[0])
    rows = []
    for s_tr, tag, desc, clip, b, w, extra in results[:400]:
        expo = pe.exposure(w, "test")
        if not MIN_EXPOSURE <= expo <= 0.98:
            continue
        st = pe.stats(w, "test")
        rows.append({"method": tag, "config": desc, "clip": clip, "hedge_b": b,
                     "train_sharpe": s_tr, "test_sharpe": st["sharpe"],
                     "train_a": pe.sharpe(w, "train_a"), "train_b": pe.sharpe(w, "train_b"),
                     "test_lag2": pe.sharpe(w, "test", extra_lag=True),
                     "test_cagr": st["cagr"], "test_maxdd": st["maxdd"],
                     "turnover_yr": pe.turnover(w, "test"), "exposure_test": expo,
                     "z_vs_champ": memmel_z(pe.daily(w, "test"), pe.daily(champ, "test"))[0]})
    df = pd.DataFrame(rows)
    if df.empty:
        print("no material survivors")
        return
    bh = {k: pe.sharpe(ones, k, cost_bps=0) for k in ("train_a", "train_b")}
    df["robust"] = (df.train_a > bh["train_a"]) & (df.train_b > bh["train_b"])
    df["beats_champ"] = df.test_sharpe > ch_te
    df.to_csv(OUT_CSV, index=False)
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 40)

    print(f"\n{len(df)} material -> {OUT_CSV}")
    print(f"  robust {int(df.robust.sum())} | beat champion ({ch_te:.4f}) "
          f"{int(df.beats_champ.sum())} | both {int((df.robust & df.beats_champ).sum())}")
    cols = ["method", "config", "clip", "hedge_b", "robust", "train_sharpe", "test_sharpe",
            "test_lag2", "test_maxdd", "turnover_yr", "exposure_test", "z_vs_champ"]
    print("\nbest 3 per method by TRAIN Sharpe:")
    print(df.sort_values("train_sharpe", ascending=False).groupby("method").head(3)[cols]
          .sort_values("train_sharpe", ascending=False).to_string(index=False,
                                                                 float_format=lambda x: f"{x:.3f}"))
    w = df[df.robust & df.beats_champ]
    if len(w):
        print(f"\nROBUST AND BEATING THE CHAMPION ({len(w)}):")
        print(w.sort_values("train_sharpe", ascending=False).head(12)[cols].to_string(
            index=False, float_format=lambda x: f"{x:.3f}"))


if __name__ == "__main__":
    main()
