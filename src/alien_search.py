"""Search AWAY from the champion's family, then test the winners as diversifiers.

Every search so far reconverges on ~ADOSC/~AD + ~CDLCLOSINGMARUBOZU + ~VAR or
~STDDEV. That is partly because the family is genuinely good and partly an
artefact of greedy beam search: stage 1 keeps the best singles and every later
stage inherits them, so whole regions of the space are never visited.

This forces exploration two ways:
  1. The champion's own components are BANNED outright.
  2. A separate beam is run per TA-Lib function group (momentum, cycle, volume,
     statistics, patterns, ...), so each region gets its own search budget
     instead of competing with a head start it cannot overcome.

Then the payoff: for each group's winner, measure its correlation with the
champion and blend the two. An "alien" combo that is individually WORSE can
still improve the book if it is uncorrelated - that is exactly how the trend
hedge (4.10% CAGR standalone) added 0.17 Sharpe. Individual quality is not the
thing being tested here; independence is.

    python alien_search.py [--beam N] [--pool N]
"""

import argparse
import math
import re
import time

import numpy as np
import pandas as pd
import talib

from beat_buyhold_search import TRANSFORMS, Evaluator, _transform
from beat_buyhold_search2 import Space2
from portfolio_search import MIN_EXPOSURE, PortfolioEvaluator
from sharpe_search import memmel_z, sharpe_of
from signal_tensor import CACHE_PATH, load

OUT_CSV = "../data/alien_search_results.csv"
BANNED = {"ADOSC", "AD", "VAR", "STDDEV", "CDLCLOSINGMARUBOZU"}
BLEND_WEIGHTS = (0.15, 0.25, 0.35, 0.50)
# Gates are kept minimal: every prior search selected "none", so 13 gates spent
# 4x the budget re-confirming that.
KEEP_GATES = ("none", "spy100", "spy200")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--beam", type=int, default=15)
    ap.add_argument("--pool", type=int, default=25)
    ap.add_argument("--max-size", type=int, default=3)
    args = ap.parse_args()

    z = load(CACHE_PATH)
    ev = Evaluator(z)
    pe = PortfolioEvaluator(z, ev)
    space = Space2(z)
    space.gates = {k: v for k, v in space.gates.items() if k in KEEP_GATES}
    names = list(z["names"])
    T, N = z["returns"].shape
    ones = np.ones((T, N), np.int8)

    spy = z["spy"]
    sma = pd.Series(spy).rolling(100, min_periods=100).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        below = np.where(np.isnan(sma), 0.0, (spy <= sma).astype(float))
    BEL = below[:, None].astype(np.float32)

    ch_atoms = tuple(sorted((names.index(n), -1) for n in
                            ("ADOSC", "CDLCLOSINGMARUBOZU", "VAR")))
    champ = (space.build2(ch_atoms, None, 1, "defensive", "none").astype(np.float32)
             - 0.5 * BEL).astype(np.float32)
    ch_te = pe.sharpe(champ, "test")
    ch_tr = pe.sharpe(champ, "train")
    r_ch_tr = pe.daily(champ, "train")
    print(f"Champion: train {ch_tr:.4f}  test {ch_te:.4f}")

    # Map each tensor name to a TA-Lib group. MA-family variants carry a period
    # suffix (SMA_50); strip it to find the underlying function.
    groups = talib.get_function_groups()
    fn_group = {fn: g for g, fns in groups.items() for fn in fns}
    by_group = {}
    for j, nm in enumerate(names):
        base = re.sub(r"_\d+$", "", nm)
        if base in BANNED or nm in BANNED:
            continue
        g = fn_group.get(base)
        if g is None:
            continue
        by_group.setdefault(g, []).append(j)
    print(f"banned {sorted(BANNED)}; {sum(len(v) for v in by_group.values())} signals "
          f"across {len(by_group)} groups\n")

    def score(combo, tag):
        out = []
        raw = space.raw_position(combo)
        for tr in TRANSFORMS:
            p = _transform(raw, tr)
            for gname, g in space.gates.items():
                if g is None:
                    pos = p
                else:
                    mode, mask = g
                    pos = ((p * mask).astype(np.int8) if mode == "flat"
                           else np.where(mask, p, -1).astype(np.int8))
                s = pe.objective(pos, "train")
                if math.isfinite(s):
                    out.append((s, combo, tr, gname, tag))
        return out

    t0, winners = time.time(), []
    for gname, idxs in sorted(by_group.items(), key=lambda kv: -len(kv[1])):
        atoms = [(j, s) for j in idxs for s in (1, -1)]
        res = []
        for a in atoms:
            res += score((a,), gname)
        if not res:
            continue
        res.sort(key=lambda r: -r[0])
        best_by_atom = {}
        for s, c, *_ in res:
            best_by_atom[c[0]] = max(best_by_atom.get(c[0], -9), s)
        pool = [a for a, _ in sorted(best_by_atom.items(), key=lambda kv: -kv[1])[:args.pool]]
        beam = list(dict.fromkeys([c for _, c, *_ in res]))[:args.beam]
        for size in range(2, args.max_size + 1):
            cands = []
            for combo in beam:
                for atom in pool:
                    if atom[0] in {j for j, _ in combo}:
                        continue
                    cands.append(tuple(sorted(combo + (atom,))))
            stage = []
            for c in dict.fromkeys(cands):
                stage += score(c, gname)
            if not stage:
                break
            stage.sort(key=lambda r: -r[0])
            res += stage
            beam = list(dict.fromkeys([c for _, c, *_ in stage]))[:args.beam]
        res.sort(key=lambda r: -r[0])
        winners.append(res[0])
        s, combo, tr, gt, _ = res[0]
        print(f"{gname:<24} n={len(idxs):>3}  best train {s:.4f}  "
              f"({' + '.join(space.atom_name(a) for a in combo)[:52]}, {tr}, {gt}) "
              f"[{time.time()-t0:.0f}s]")

    print(f"\n--- blending each group's winner into the champion ---")
    print(f"{'group':<22}{'own_test':>9}{'corr':>7}{'w':>6}{'blend_tr':>10}{'blend_te':>10}"
          f"{'lag2':>7}{'maxDD':>8}{'turn':>7}{'z_ch':>7}")
    rows = []
    for s_tr, combo, tr, gt, gname in winners:
        pos = space.build2(combo, None, 1, tr, gt).astype(np.float32)
        own = pe.sharpe(pos, "test")
        rho = float(np.corrcoef(pe.daily(pos, "train"), r_ch_tr)[0, 1])
        for w in BLEND_WEIGHTS:
            B = ((1 - w) * champ + w * pos).astype(np.float32)
            if np.abs(B).max() > 1.0 + 1e-6:
                continue
            bt, be = pe.sharpe(B, "train"), pe.sharpe(B, "test")
            st = pe.stats(B, "test")
            zz = memmel_z(pe.daily(B, "test"), pe.daily(champ, "test"))[0]
            rows.append({"group": gname,
                         "combo": " + ".join(space.atom_name(a) for a in combo),
                         "transform": tr, "gate": gt, "weight": w, "corr_train": rho,
                         "own_test_sharpe": own, "blend_train": bt, "blend_test": be,
                         "blend_lag2": pe.sharpe(B, "test", extra_lag=True),
                         "blend_maxdd": st["maxdd"], "turnover_yr": pe.turnover(B, "test"),
                         "z_vs_champ": zz})
            print(f"{gname[:21]:<22}{own:>9.3f}{rho:>7.3f}{w:>6.2f}{bt:>10.3f}{be:>10.3f}"
                  f"{rows[-1]['blend_lag2']:>7.3f}{st['maxdd']:>8.2%}"
                  f"{rows[-1]['turnover_yr']:>7.1f}{zz:>7.2f}")

    df = pd.DataFrame(rows)
    if df.empty:
        print("no blends produced")
        return
    df.to_csv(OUT_CSV, index=False)
    better = df[(df.blend_train > ch_tr) & (df.blend_test > ch_te)]
    print(f"\n{len(better)} blends beat the champion on BOTH train and test "
          f"(train {ch_tr:.4f} / test {ch_te:.4f}) -> {OUT_CSV}")
    if len(better):
        print(better.sort_values("blend_train", ascending=False).head(10).to_string(
            index=False, float_format=lambda x: f"{x:.3f}"))


if __name__ == "__main__":
    main()
