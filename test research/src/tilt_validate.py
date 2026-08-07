"""Final honest validation of the consensus-tilt strategies.

Protocol, fixing the two ways the earlier tables flattered themselves:
  1. Rank configurations by TRAIN performance only. The smoothed-tilt table was
     sorted by test excess, so reading its top rows was selection on test.
  2. Benchmark against constant-weight buy-and-hold at the SAME realized average
     exposure. Train exposure is pinned to 1.0 by construction, but test
     exposure drifts above it, and holding more of a rising market is not skill.

Significance is computed on the daily series of (strategy - matched benchmark)
portfolio returns, so the 501 tickers' shared market factor is already netted
out and each day contributes one observation.

    python tilt_validate.py
"""

import numpy as np
import pandas as pd

from beat_buyhold_search import COST_BPS, Evaluator, Space
from signal_tensor import CACHE_PATH, load
from tilt_search import WeightedEvaluator
from tilt_smoothed import HYSTERESIS, SMOOTH_WINDOWS, THRESHOLDS, TILTS, _hysteretic

OUT_CSV = "../data/tilt_validated.csv"
TOP_BY_TRAIN = 12


def main():
    z = load(CACHE_PATH)
    ev = Evaluator(z)
    wev = WeightedEvaluator(z, ev)
    space = Space(z)
    bh = ev.baseline
    T, N = z["returns"].shape
    ret = np.nan_to_num(z["returns"])
    valid = ev.valid

    counts_short = np.zeros((T, N), dtype=np.int16)
    for j in space.base_idx:
        counts_short += (z["tensor"][j] == -1)
    fs_raw = counts_short.astype(np.float32) / len(space.base_idx)

    s_tr, e_tr = ev.slices["train"]
    train_valid = valid[s_tr:e_tr]
    smoothed = {w: (fs_raw if w == 1 else
                    pd.DataFrame(fs_raw).rolling(w, min_periods=1).mean().to_numpy(dtype=np.float32))
                for w in SMOOTH_WINDOWS}

    def weights(win, thr, band, d):
        hot = _hysteretic(smoothed[win], thr, band)
        p = float((hot[s_tr:e_tr] & train_valid).sum() / train_valid.sum())
        if not 0.02 < p < 0.98:
            return None
        w_lo = 1.0 - d * p / (1.0 - p)
        if w_lo < 0:
            return None
        return np.where(hot, np.float32(1.0 + d), np.float32(w_lo)).astype(np.float32)

    # --- rank on train only ---------------------------------------------
    configs = []
    for win in SMOOTH_WINDOWS:
        for thr in THRESHOLDS:
            for band in HYSTERESIS:
                for d in TILTS:
                    w = weights(win, thr, band, d)
                    if w is None:
                        continue
                    configs.append((wev.cagr(w, "train", costs=True), win, thr, band, d))
    configs.sort(reverse=True)
    print(f"{len(configs)} configs; ranking on NET TRAIN CAGR only "
          f"(B&H train {bh['train']:.2%}, test {bh['test']:.2%})\n")

    def daily_excess(w, window, w_bar):
        """Daily equal-weight portfolio return of strategy minus the matched
        constant-weight benchmark, net of turnover costs."""
        s, e = ev.slices[window]
        held, m, r = w[s - 1:e - 1], valid[s:e], ret[s:e]
        n = np.maximum(m.sum(axis=1), 1)
        strat = ((held * r) * m).sum(axis=1) / n
        cost = (np.abs(np.diff(np.vstack([w[s - 2:s - 1], held]), axis=0)) * m).sum(axis=1) / n
        bench = ((w_bar * r) * m).sum(axis=1) / n
        return strat - cost * COST_BPS / 1e4 - bench

    rows = []
    for train_score, win, thr, band, d in configs[:TOP_BY_TRAIN]:
        w = weights(win, thr, band, d)
        rec = {"smooth": win, "thresh": thr, "band": band, "tilt": d,
               "net_train_cagr": train_score, "train_excess": train_score - bh["train"]}
        for window in ("train_a", "train_b", "test"):
            exp = wev.exposure(w, window)
            matched = wev.cagr(np.full((T, N), exp, dtype=np.float32), window)
            net = wev.cagr(w, window, costs=True)
            rec[f"{window}_net_cagr"] = net
            rec[f"{window}_exposure"] = exp
            rec[f"{window}_matched_bh"] = matched
            rec[f"{window}_true_excess"] = net - matched
        de = daily_excess(w, "test", rec["test_exposure"])
        rec["test_t_stat"] = float(de.mean() / de.std(ddof=1) * np.sqrt(len(de)))
        rec["turnover_yr"] = wev.turnover(w, "test")
        rows.append(rec)

    df = pd.DataFrame(rows)
    df.to_csv(OUT_CSV, index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)

    print("Selected on train; TEST columns are out-of-sample and exposure-matched:")
    cols = ["smooth", "thresh", "band", "tilt", "net_train_cagr", "test_net_cagr",
            "test_exposure", "test_matched_bh", "test_true_excess", "test_t_stat", "turnover_yr"]
    print(df[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    good = df[df.test_true_excess > 0]
    print(f"\n{len(good)} of {len(df)} beat an exposure-matched B&H out-of-sample, net of costs")
    print(f"median true excess {df.test_true_excess.median():+.2%}, "
          f"max t-stat {df.test_t_stat.max():.2f}")
    print(f"configs with t > 2.0: {int((df.test_t_stat > 2).sum())}")
    print("\nRobustness (true excess vs matched B&H in each window):")
    print(df[["smooth", "thresh", "band", "tilt", "train_a_true_excess",
              "train_b_true_excess", "test_true_excess"]].to_string(
        index=False, float_format=lambda x: f"{x:+.4f}"))


if __name__ == "__main__":
    main()
