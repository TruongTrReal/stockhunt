"""Search new COMBINATION OPERATORS, not more permutations of the same one.

Every prior search merged signals exactly one way - sum the members, take the
sign - and varied only which members went in. alien_search.py showed why that
cannot help much further: every TA-Lib group's best combo is 0.71-0.80
correlated with the champion, so re-mixing the same ingredients re-expresses the
same bet. What has never been varied is the *operator*.

Four operators, each chosen because it does something sum-then-sign structurally
cannot:

  strict_and  Unanimity instead of majority. Sign-of-sum lets one member drag
      the whole position; requiring every member to agree is far more selective
      and produces a different exposure profile entirely.
  asym        Enter on one signal, exit on another. Real trading is asymmetric,
      and a slow exit paired with a fast entry cuts round trips - which matters
      because transaction costs are the binding constraint on every result here.
  regime      Use signal C to choose WHICH of A or B to follow. Averaging two
      signals that work in opposite regimes destroys both; switching between
      them keeps each where it works.
  persist     Ramp into a position over k bars instead of jumping to full size,
      so a whipsaw costs a fraction of a full round trip.

Objective is net-of-cost portfolio Sharpe on train with the MIN_EXPOSURE floor
inside it, and the short-overlay hedge weight is searched jointly. Survivors are
validated on the unseen test window against the champion.

    python operator_search.py [--pool N]
"""

import argparse
import itertools
import math
import time

import numpy as np
import pandas as pd

from beat_buyhold_search import TRANSFORMS, Evaluator, _transform
from portfolio_search import MIN_EXPOSURE, PortfolioEvaluator
from sharpe_search import memmel_z
from signal_tensor import CACHE_PATH, load

VARIANT_PATH = "../data/param_variant_tensor.npz"
OUT_CSV = "../data/operator_search_results.csv"
HEDGES = (0.0, 0.3, 0.5)
PERSIST_K = (3, 5, 10, 20)


def strict_and(parts):
    """Long only if every member is long; short only if every member is short."""
    allL = np.logical_and.reduce([p == 1 for p in parts])
    allS = np.logical_and.reduce([p == -1 for p in parts])
    return np.where(allL, 1, np.where(allS, -1, 0)).astype(np.int8)


def asym(enter_pos, exit_pos):
    """Hold long from the bar `enter_pos` turns long until `exit_pos` turns
    short. Vectorised as a comparison of the most recent entry and exit index."""
    T = enter_pos.shape[0]
    ar = np.arange(T, dtype=np.int32)[:, None]
    last_in = np.maximum.accumulate(np.where(enter_pos == 1, ar, -1), axis=0)
    last_out = np.maximum.accumulate(np.where(exit_pos == -1, ar, -1), axis=0)
    return (last_in > last_out).astype(np.int8)


def regime(switch, hi, lo):
    return np.where(switch > 0, hi, lo).astype(np.int8)


def persist(pos, k):
    """Scale in linearly over k bars after each change of position."""
    T = pos.shape[0]
    ar = np.arange(T, dtype=np.int32)[:, None]
    chg = np.empty(pos.shape, dtype=bool)
    chg[0] = True
    chg[1:] = pos[1:] != pos[:-1]
    last = np.maximum.accumulate(np.where(chg, ar, 0), axis=0)
    ramp = np.clip((ar - last + 1) / k, 0.0, 1.0).astype(np.float32)
    return (pos.astype(np.float32) * ramp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=22)
    args = ap.parse_args()

    z = dict(np.load(VARIANT_PATH, allow_pickle=False))
    ev = Evaluator(z)
    pe = PortfolioEvaluator(z, ev)
    names = list(z["names"])
    tensor = z["tensor"]
    T, N = z["returns"].shape
    ones = np.ones((T, N), np.int8)

    spy = z["spy"]
    sma100 = pd.Series(spy).rolling(100, min_periods=100).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        below = np.where(np.isnan(sma100), 0.0, (spy <= sma100).astype(float))
        spy_up = np.where(np.isnan(sma100), 0.0, (spy > sma100).astype(float))
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

    # Pool: the best atoms individually, so the operators are combining
    # ingredients already known to carry signal.
    atoms = [(j, s) for j in range(len(names)) for s in (1, -1)]
    singles = []
    for j, s in atoms:
        p = (s * tensor[j]).astype(np.int8)
        singles.append((pe.sharpe(_transform(p, "defensive"), "train"), j, s))
    singles.sort(reverse=True)
    pool = [(j, s) for _, j, s in singles[:args.pool]]
    print(f"pool: {len(pool)} atoms, best single train Sharpe {singles[0][0]:.4f}")

    def sig(a):
        j, s = a
        return (s * tensor[j]).astype(np.int8)

    # Regime switches: market trend, plus the two strongest volatility signals.
    switches = {"spy100": np.broadcast_to(np.where(spy_up > 0, 1, -1)[:, None], (T, N)).astype(np.int8)}
    for nm in ("VAR_20_sma60", "STDDEV_20_sma60"):
        if nm in names:
            switches[nm] = tensor[names.index(nm)].astype(np.int8)

    results, t0, n_eval = [], time.time(), 0

    def consider(pos, tag, desc):
        nonlocal n_eval
        for tr in (TRANSFORMS if pos.dtype == np.int8 else ("raw",)):
            base = _transform(pos, tr) if pos.dtype == np.int8 else pos
            for b in HEDGES:
                w = base.astype(np.float32) if b == 0 else (base.astype(np.float32) - b * BEL)
                if np.abs(w).max() > 1.0 + 1e-6:
                    continue
                n_eval += 1
                s = pe.objective(w, "train")
                if math.isfinite(s) and s > ch_tr * 0.85:
                    results.append((s, tag, desc, tr, b, w))

    print("\nstrict_and over pairs and triples...")
    for combo in itertools.combinations(pool, 2):
        consider(strict_and([sig(a) for a in combo]), "strict_and",
                 " & ".join(f"{'~' if s<0 else ''}{names[j]}" for j, s in combo))
    for combo in itertools.combinations(pool[:14], 3):
        consider(strict_and([sig(a) for a in combo]), "strict_and",
                 " & ".join(f"{'~' if s<0 else ''}{names[j]}" for j, s in combo))
    print(f"  {n_eval:,} evals [{time.time()-t0:.0f}s]")

    print("asym (enter on A, exit on B)...")
    for a, b_ in itertools.permutations(pool[:16], 2):
        consider(asym(sig(a), sig(b_)), "asym",
                 f"enter {'~' if a[1]<0 else ''}{names[a[0]]} / exit {'~' if b_[1]<0 else ''}{names[b_[0]]}")
    print(f"  {n_eval:,} evals [{time.time()-t0:.0f}s]")

    print("regime (switch chooses between two signals)...")
    for sw_name, sw in switches.items():
        for a, b_ in itertools.permutations(pool[:12], 2):
            consider(regime(sw, sig(a), sig(b_)), "regime",
                     f"{sw_name}? {'~' if a[1]<0 else ''}{names[a[0]]} : {'~' if b_[1]<0 else ''}{names[b_[0]]}")
    print(f"  {n_eval:,} evals [{time.time()-t0:.0f}s]")

    print("persist (ramp in over k bars)...")
    for combo in itertools.combinations(pool[:18], 2):
        acc2 = (sig(combo[0]) + sig(combo[1])).astype(np.int8)
        base = np.sign(acc2).astype(np.int8)
        for k in PERSIST_K:
            consider(persist(base, k), "persist",
                     " + ".join(f"{'~' if s<0 else ''}{names[j]}" for j, s in combo) + f" ramp{k}")
    print(f"  {n_eval:,} evals [{time.time()-t0:.0f}s]")

    print(f"\n{n_eval:,} candidates evaluated, {len(results):,} passed the train screen. "
          f"Validating...")
    results.sort(key=lambda r: -r[0])
    rows = []
    for s_tr, tag, desc, tr, b, w in results[:500]:
        expo = pe.exposure(w, "test")
        if not MIN_EXPOSURE <= expo <= 0.98:
            continue
        st = pe.stats(w, "test")
        rows.append({"operator": tag, "combo": desc, "transform": tr, "hedge_b": b,
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
    pd.set_option("display.width", 270)
    pd.set_option("display.max_columns", 40)
    pd.set_option("display.max_colwidth", 58)

    print(f"\n{len(df)} material -> {OUT_CSV}")
    print(f"  robust {int(df.robust.sum())} | beat champion ({ch_te:.4f}) "
          f"{int(df.beats_champ.sum())} | both {int((df.robust & df.beats_champ).sum())}")
    print("\nby operator, best TRAIN Sharpe each:")
    cols = ["operator", "combo", "transform", "hedge_b", "robust", "train_sharpe",
            "test_sharpe", "test_lag2", "test_maxdd", "turnover_yr", "exposure_test", "z_vs_champ"]
    print(df.sort_values("train_sharpe", ascending=False).groupby("operator").head(3)[cols]
          .sort_values("train_sharpe", ascending=False).to_string(index=False,
                                                                 float_format=lambda x: f"{x:.3f}"))
    w = df[df.robust & df.beats_champ]
    if len(w):
        print(f"\nROBUST AND BEATING THE CHAMPION ({len(w)}):")
        print(w.sort_values("train_sharpe", ascending=False).head(12)[cols].to_string(
            index=False, float_format=lambda x: f"{x:.3f}"))


if __name__ == "__main__":
    main()
