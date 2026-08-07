"""Ensemble-consensus search: combine MANY indicators at once, not 2-4.

The beam search in beat_buyhold_search*.py can only express small combos, where
a single member's noise moves the whole position. This asks a different
question: when a large fraction of ALL indicators simultaneously turn bearish,
is that a real signal? Broad consensus is a much stronger condition than any
pair agreeing, and it is the natural way to use 211 weak rules together.

Position rule, per day per ticker, over an ensemble of K indicators:
    short   if the bearish fraction >= s_thresh
    long    if the bullish fraction >= l_thresh
    default otherwise (either stay long, or stand aside)

Thresholds are chosen on TRAIN and reported on TEST, same discipline as the
other searches. `frac_not_long` is reported alongside because a rule that is
long 99.9% of the time is buy-and-hold with extra steps, no matter what its
CAGR says - that is how pass 1's "survivors" turned out to be noise.

    python ensemble_search.py
"""

import time

import numpy as np
import pandas as pd

from beat_buyhold_search import Evaluator, Space
from signal_tensor import CACHE_PATH, load

OUT_CSV = "../data/ensemble_search_results.csv"

ENSEMBLE_SIZES = (5, 10, 20, 35, 50, 75, 100, 150, 211)
SHORT_THRESHOLDS = (0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95)
LONG_THRESHOLDS = (0.0, 0.10, 0.20, 0.30, 0.45, 0.60)
DEFAULTS = {"stay_long": 1, "stand_aside": 0}


def main():
    z = load(CACHE_PATH)
    ev, space = Evaluator(z), Space(z)
    bh = ev.baseline
    tensor = z["tensor"]
    T, N = z["returns"].shape

    # Rank the unique signals by how they do on train under the highest-exposure
    # transform, then build ensembles from the best K. Ranking is train-only.
    print("Ranking signals on train...")
    t0 = time.time()
    ranked = []
    for j in space.base_idx:
        p = np.where(tensor[j] == -1, -1, 1).astype(np.int8)   # defensive_short
        ranked.append((ev.cagr(p, "train"), j))
    ranked.sort(reverse=True)
    order = [j for _, j in ranked]
    print(f"  best single (defensive_short) train CAGR {ranked[0][0]:.2%} "
          f"vs B&H {bh['train']:.2%}  [{time.time()-t0:.0f}s]")

    # Accumulate vote counts in rank order so every ensemble size is a prefix
    # and each indicator is touched exactly once.
    counts_long = np.zeros((T, N), dtype=np.int16)
    counts_short = np.zeros((T, N), dtype=np.int16)
    snapshots = {}
    for rank, j in enumerate(order, start=1):
        t = tensor[j]
        counts_long += (t == 1)
        counts_short += (t == -1)
        if rank in ENSEMBLE_SIZES:
            snapshots[rank] = (counts_long.copy(), counts_short.copy())
    if len(order) in ENSEMBLE_SIZES and len(order) not in snapshots:
        snapshots[len(order)] = (counts_long.copy(), counts_short.copy())

    rows = []
    for k, (cl, cs) in snapshots.items():
        fl = cl.astype(np.float32) / k
        fs = cs.astype(np.float32) / k
        for s_thr in SHORT_THRESHOLDS:
            short_m = fs >= s_thr
            for l_thr in LONG_THRESHOLDS:
                long_m = fl >= l_thr
                for dname, dval in DEFAULTS.items():
                    pos = np.where(short_m, -1, np.where(long_m, 1, dval)).astype(np.int8)
                    train = ev.cagr(pos, "train")
                    test = ev.cagr(pos, "test")
                    rows.append({
                        "k": k, "short_thresh": s_thr, "long_thresh": l_thr, "default": dname,
                        "train_cagr": train, "test_cagr": test,
                        "train_excess": train - bh["train"], "test_excess": test - bh["test"],
                        "train_a_cagr": ev.cagr(pos, "train_a"), "train_b_cagr": ev.cagr(pos, "train_b"),
                        "net_test_cagr": float(np.mean(ev.per_ticker(pos, "test", costs=True))),
                        "exposure_test": ev.exposure(pos, "test"),
                        # How different is this from just being long? Near 0 means
                        # the "strategy" is buy-and-hold and its excess is noise.
                        "frac_not_long": float(np.mean(pos != 1)),
                        "frac_short": float(np.mean(pos == -1)),
                    })
        print(f"  k={k:<4} done [{time.time()-t0:.0f}s]")

    df = pd.DataFrame(rows).sort_values("train_cagr", ascending=False)
    df.to_csv(OUT_CSV, index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)

    print(f"\n{len(df)} ensemble configurations -> {OUT_CSV}")
    print(f"B&H: train {bh['train']:.2%}  test {bh['test']:.2%}\n")
    cols = ["k", "short_thresh", "long_thresh", "default", "train_cagr", "test_cagr",
            "test_excess", "frac_not_long", "frac_short", "exposure_test"]
    print("Top 15 by TRAIN CAGR (the honest selection):")
    print(df.head(15)[cols].to_string(index=False))

    beat = df[(df.train_cagr > bh["train"]) & (df.test_cagr > bh["test"])]
    material = beat[beat.frac_not_long > 0.05]
    print(f"\n{len(beat)} configs beat B&H on train AND test; "
          f"{len(material)} of those differ from buy-and-hold on >5% of ticker-days")
    if len(material):
        print(material.head(15)[cols].to_string(index=False))


if __name__ == "__main__":
    main()
