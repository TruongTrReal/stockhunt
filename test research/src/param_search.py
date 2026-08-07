"""Search the parameter-swept family jointly with the hedge weight.

Two changes from every previous search:

  parameters   The atoms are now 84 parameter variants of the champion's own
      components (ADOSC fast/slow, VAR and STDDEV lookbacks, the self-trend
      comparison window, candlestick hold length) rather than 231 indicators
      frozen at TA-Lib defaults. Those five numbers define how the champion
      behaves and none had ever been fitted.
  joint hedge  The hedge weight is searched WITH the combo instead of bolted on
      afterwards. The portfolio search showed the hedge supplies the entire
      margin over buy-and-hold, so optimising the combo alone and hedging the
      winner optimises the wrong thing.

The current champion is reachable in this space (~ADOSC_3_10_zero +
~CDLCLOSINGMARUBOZU_hold5 + ~VAR_5_sma20, defensive, b=0.5), so the search can
either beat it or rediscover it - and rediscovering it is itself informative.

Objective is net-of-cost portfolio Sharpe on train with the MIN_EXPOSURE floor
inside it (a bare Sharpe objective optimises toward never trading). Survivors
must beat the champion on the unseen test window, stay robust across both train
halves, and survive an extra bar of execution lag.

    python param_search.py [--pool N] [--beam N] [--max-size N]
"""

import argparse
import math
import time

import numpy as np
import pandas as pd

from beat_buyhold_search import TRANSFORMS, Evaluator, _transform
from portfolio_search import MIN_EXPOSURE, PortfolioEvaluator
from sharpe_search import memmel_z
from signal_tensor import CACHE_PATH, load

VARIANT_PATH = "../data/param_variant_tensor.npz"
OUT_CSV = "../data/param_search_results.csv"
HEDGE_WEIGHTS = (0.0, 0.3, 0.4, 0.5, 0.6)
# Hedging a book that can already go short is incoherent, and it would push
# gross exposure past 1.0; only the long-side transforms take an overlay.
HEDGEABLE = {"longonly", "defensive"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=40)
    ap.add_argument("--beam", type=int, default=25)
    ap.add_argument("--max-size", type=int, default=4)
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

    bh = {w: pe.sharpe(ones, w, cost_bps=0) for w in ("train_a", "train_b", "train", "test")}

    def build(combo, transform, b):
        acc = tensor[combo[0][0]].astype(np.int8)
        if combo[0][1] < 0:
            acc = (-acc).astype(np.int8)
        for j, s in combo[1:]:
            acc = (acc + (s * tensor[j])).astype(np.int8)
        p = _transform(np.sign(acc).astype(np.int8), transform)
        if b == 0.0:
            return p.astype(np.float32)
        return (p.astype(np.float32) - b * BEL).astype(np.float32)

    # Champion, expressed in this space, as the bar to beat.
    ch_atoms = tuple(sorted((names.index(n), -1) for n in
                            ("ADOSC_3_10_zero", "CDLCLOSINGMARUBOZU_hold5", "VAR_5_sma20")))
    champ = build(ch_atoms, "defensive", 0.5)
    ch_tr, ch_te = pe.sharpe(champ, "train"), pe.sharpe(champ, "test")
    print(f"B&H portfolio Sharpe   train {bh['train']:.4f}  test {bh['test']:.4f}")
    print(f"Champion (in this space) train {ch_tr:.4f}  test {ch_te:.4f}")
    print(f"{len(names)} variants -> {len(names)*2} atoms\n")

    atoms = [(j, s) for j in range(len(names)) for s in (1, -1)]

    def variants(combo):
        for tr in TRANSFORMS:
            for b in (HEDGE_WEIGHTS if tr in HEDGEABLE else (0.0,)):
                yield build(combo, tr, b), tr, b

    def score(combo):
        out = []
        for pos, tr, b in variants(combo):
            s = pe.objective(pos, "train")
            if math.isfinite(s):
                out.append((s, combo, tr, b))
        return out

    t0, results = time.time(), []
    singles = [(a,) for a in atoms]
    print(f"Stage 1: {len(singles)} atoms x up to {len(TRANSFORMS)*len(HEDGE_WEIGHTS)} variants")
    for c in singles:
        results += score(c)
    results.sort(key=lambda r: -r[0])
    print(f"  best train {results[0][0]:.4f} ({names[results[0][1][0][0]]}, "
          f"{results[0][2]}, b={results[0][3]}) [{time.time()-t0:.0f}s]")

    best_by_atom = {}
    for s, c, _, _ in results:
        best_by_atom[c[0]] = max(best_by_atom.get(c[0], -9), s)
    pool = [a for a, _ in sorted(best_by_atom.items(), key=lambda kv: -kv[1])[:args.pool]]
    beam = list(dict.fromkeys([c for _, c, _, _ in results]))[:args.beam]

    for size in range(2, args.max_size + 1):
        cands = []
        for combo in beam:
            for atom in pool:
                if atom[0] in {j for j, _ in combo}:
                    continue
                cands.append(tuple(sorted(combo + (atom,))))
        cands = list(dict.fromkeys(cands))
        stage = []
        for c in cands:
            stage += score(c)
        stage.sort(key=lambda r: -r[0])
        results += stage
        print(f"Stage {size}: {len(cands):,} memberships -> best {stage[0][0]:.4f} "
              f"[{time.time()-t0:.0f}s]")
        beam = list(dict.fromkeys([c for _, c, _, _ in stage]))[:args.beam]

    results.sort(key=lambda r: -r[0])
    print(f"\n{len(results):,} scored in {time.time()-t0:.0f}s. Validating...")

    rows, seen = [], set()
    for s_tr, combo, tr, b in results:
        key = (combo, tr, b)
        if key in seen:
            continue
        seen.add(key)
        pos = build(combo, tr, b)
        expo = pe.exposure(pos, "test")
        if not MIN_EXPOSURE <= expo <= 0.98:
            continue
        st = pe.stats(pos, "test")
        rows.append({
            "combo": " + ".join(("~" if s < 0 else "") + names[j] for j, s in combo),
            "size": len(combo), "transform": tr, "hedge_b": b,
            "train_sharpe": s_tr, "test_sharpe": st["sharpe"],
            "train_a_sharpe": pe.sharpe(pos, "train_a"),
            "train_b_sharpe": pe.sharpe(pos, "train_b"),
            "test_sharpe_lag2": pe.sharpe(pos, "test", extra_lag=True),
            "test_cagr": st["cagr"], "test_maxdd": st["maxdd"], "test_calmar": st["calmar"],
            "turnover_yr": pe.turnover(pos, "test"), "exposure_test": expo,
            "z_vs_champ": memmel_z(pe.daily(pos, "test"), pe.daily(champ, "test"))[0],
        })
        if len(rows) >= 600:
            break

    df = pd.DataFrame(rows)
    if df.empty:
        print("NO SURVIVORS.")
        return
    df["robust"] = (df.train_a_sharpe > bh["train_a"]) & (df.train_b_sharpe > bh["train_b"])
    df["beats_champ"] = df.test_sharpe > ch_te
    df.to_csv(OUT_CSV, index=False)
    pd.set_option("display.width", 265)
    pd.set_option("display.max_columns", 40)
    pd.set_option("display.max_colwidth", 52)

    print(f"\n{len(df)} material candidates -> {OUT_CSV}")
    print(f"  robust: {int(df.robust.sum())} | beat champion test Sharpe ({ch_te:.4f}): "
          f"{int(df.beats_champ.sum())} | both: {int((df.robust & df.beats_champ).sum())}")
    cols = ["combo", "transform", "hedge_b", "robust", "train_sharpe", "test_sharpe",
            "test_sharpe_lag2", "test_cagr", "test_maxdd", "turnover_yr",
            "exposure_test", "z_vs_champ"]
    print("\nTop 15 by TRAIN net portfolio Sharpe:")
    print(df.sort_values("train_sharpe", ascending=False).head(15)[cols].to_string(
        index=False, float_format=lambda x: f"{x:.3f}"))
    win = df[df.robust & df.beats_champ]
    if len(win):
        print(f"\nRobust AND beating the champion, best {len(win)} by train Sharpe:")
        print(win.sort_values("train_sharpe", ascending=False).head(12)[cols].to_string(
            index=False, float_format=lambda x: f"{x:.3f}"))


if __name__ == "__main__":
    main()
