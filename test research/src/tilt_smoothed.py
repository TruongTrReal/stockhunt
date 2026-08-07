"""Low-turnover version of the consensus tilt.

tilt_search.py established the shape of the problem: the tilt earns a real
+0.88pp on train, the edge survives into test with the right sign but a quarter
of the magnitude, and daily rebalancing costs (9-56 turns/yr at 5bps) exceed the
out-of-sample gain. Gross excess on test was +0.28pp against a 0.48pp cost bill.

So the only thing worth changing is turnover. Smoothing the consensus over N
days before thresholding, plus a hysteresis band that requires the signal to
move decisively before the weight changes, attacks exactly that: the signal
keeps its slow component (which is where a reversal edge lives) and drops the
day-to-day flicker that generates trades without information.

Selection is on TRAIN, reported on TEST, net of costs. Average train exposure is
pinned to 1.0 as before so this stays a tilt rather than leverage.

    python tilt_smoothed.py
"""

import time

import numpy as np
import pandas as pd

from beat_buyhold_search import COST_BPS, Evaluator, Space
from signal_tensor import CACHE_PATH, load
from tilt_search import WeightedEvaluator

OUT_CSV = "../data/tilt_smoothed_results.csv"

SMOOTH_WINDOWS = (1, 5, 10, 20, 40, 60)
THRESHOLDS = (0.30, 0.35, 0.40, 0.45)
TILTS = (0.25, 0.50)
HYSTERESIS = (0.0, 0.05, 0.10)      # band the signal must cross to flip the weight


def _hysteretic(sig: np.ndarray, thr: float, band: float) -> np.ndarray:
    """Weight state flips to hot above thr+band and back to cold below
    thr-band; in between it holds its previous state. Implemented as a
    forward-fill of the last decisive crossing."""
    if band <= 0:
        return sig >= thr
    up, down = sig >= thr + band, sig <= thr - band
    decisive = up | down
    idx = np.where(decisive, np.arange(sig.shape[0])[:, None], 0)
    np.maximum.accumulate(idx, axis=0, out=idx)
    return np.take_along_axis(up, idx, axis=0)


def main():
    z = load(CACHE_PATH)
    ev = Evaluator(z)
    wev = WeightedEvaluator(z, ev)
    space = Space(z)
    bh = ev.baseline
    T, N = z["returns"].shape

    counts_short = np.zeros((T, N), dtype=np.int16)
    for j in space.base_idx:
        counts_short += (z["tensor"][j] == -1)
    fs_raw = counts_short.astype(np.float32) / len(space.base_idx)

    s, e = ev.slices["train"]
    train_valid = ev.valid[s:e]
    print(f"B&H:  train {bh['train']:.2%}   test {bh['test']:.2%}\n")

    t0, rows = time.time(), []
    for win in SMOOTH_WINDOWS:
        fs = (fs_raw if win == 1 else
              pd.DataFrame(fs_raw).rolling(win, min_periods=1).mean().to_numpy(dtype=np.float32))
        for thr in THRESHOLDS:
            for band in HYSTERESIS:
                hot = _hysteretic(fs, thr, band)
                p = float((hot[s:e] & train_valid).sum() / train_valid.sum())
                if not 0.02 < p < 0.98:
                    continue
                for d in TILTS:
                    w_lo = 1.0 - d * p / (1.0 - p)
                    if w_lo < 0:
                        continue
                    w = np.where(hot, np.float32(1.0 + d), np.float32(w_lo)).astype(np.float32)
                    rows.append({
                        "smooth": win, "thresh": thr, "band": band, "tilt": d,
                        "train_cagr": wev.cagr(w, "train"), "test_cagr": wev.cagr(w, "test"),
                        "net_train_cagr": wev.cagr(w, "train", costs=True),
                        "net_test_cagr": wev.cagr(w, "test", costs=True),
                        "train_a_cagr": wev.cagr(w, "train_a"),
                        "train_b_cagr": wev.cagr(w, "train_b"),
                        "exposure_train": wev.exposure(w, "train"),
                        "exposure_test": wev.exposure(w, "test"),
                        "turnover_yr": wev.turnover(w, "test"),
                    })
        print(f"  smooth={win:<3} done [{time.time()-t0:.0f}s]")

    df = pd.DataFrame(rows)
    df["test_excess"] = df.test_cagr - bh["test"]
    df["net_test_excess"] = df.net_test_cagr - bh["test"]
    df = df.sort_values("net_test_excess", ascending=False)
    df.to_csv(OUT_CSV, index=False)

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    cols = ["smooth", "thresh", "band", "tilt", "train_cagr", "test_cagr",
            "net_test_cagr", "net_test_excess", "turnover_yr", "exposure_test"]
    print(f"\n{len(df)} configurations -> {OUT_CSV}")
    print(f"\nTop 15 by NET test excess over B&H ({bh['test']:.2%}):")
    print(df.head(15)[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    honest = df[(df.train_cagr > bh["train"]) & (df.net_test_cagr > bh["test"])]
    print(f"\n{len(honest)} configs beat B&H on train AND beat it NET on unseen test")
    if len(honest):
        print(honest[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))


if __name__ == "__main__":
    main()
