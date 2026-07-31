"""Beat buy-and-hold by WEIGHTING days rather than selecting them.

The skill diagnostic showed why every long/flat/short search failed: days that
TA rules avoid earn MORE than days they hold (bearish consensus >= 0.40 marks
days averaging 10.4bp vs 5.2bp), and no rule identifies a subset of days with
negative expected return. So going flat always costs compounding, and buy-and-
hold is the CAGR optimum over positions restricted to -1/0/1.

That same result says the signal carries real information with the sign
reversed. The only way to use a per-day edge without giving up market exposure
is to hold MORE on the good days and LESS on the bad ones.

The comparison is kept honest by construction: the low weight is solved so that
average exposure equals 1.0 on train, identical to buy-and-hold. This is a tilt,
not leverage - no extra capital is deployed on average, and realized exposure is
reported for both windows so any drift is visible. Thresholds and tilt size are
chosen on TRAIN and applied unchanged to TEST.

    python tilt_search.py
"""

import time

import numpy as np
import pandas as pd

from beat_buyhold_search import COST_BPS, Evaluator, Space
from signal_tensor import CACHE_PATH, load

OUT_CSV = "../data/tilt_search_results.csv"

THRESHOLDS = (0.20, 0.25, 0.30, 0.35, 0.40, 0.45)
TILTS = (0.25, 0.50, 0.75, 1.00)


class WeightedEvaluator:
    """Same windows/baselines as Evaluator, but positions may be any float."""

    def __init__(self, z, ev):
        self.ret = np.nan_to_num(z["returns"])
        self.valid = ev.valid
        self.slices, self.years, self.keep = ev.slices, ev.years, ev.keep

    def cagr(self, w, window, costs=False):
        s, e = self.slices[window]
        held = w[s - 1:e - 1] if s > 0 else np.vstack([np.zeros((1, w.shape[1]), w.dtype), w[:e - 1]])
        m = self.valid[s:e]
        lg = np.where(m, np.log1p(np.clip(held * self.ret[s:e], -0.999, None)), 0.0)
        tot = lg.sum(axis=0)
        if costs:
            tot = tot - np.abs(np.diff(held, axis=0)).sum(axis=0) * COST_BPS / 1e4
        k = self.keep[window]
        return float(np.mean(np.expm1(tot[k] / self.years[window][k])))

    def exposure(self, w, window):
        s, e = self.slices[window]
        m = self.valid[s:e]
        return float((np.abs(w[s:e]) * m).sum() / m.sum())

    def turnover(self, w, window):
        s, e = self.slices[window]
        return float(np.abs(np.diff(w[s:e], axis=0)).sum(axis=0).mean() / (e - s) * 252)


def main():
    z = load(CACHE_PATH)
    ev = Evaluator(z)
    wev = WeightedEvaluator(z, ev)
    space = Space(z)
    bh = ev.baseline
    T, N = z["returns"].shape
    tensor = z["tensor"]

    print(f"B&H average per-ticker CAGR:  train {bh['train']:.2%}   test {bh['test']:.2%}")

    # Bearish consensus across every unique signal, unranked (ranking by
    # single-rule CAGR is what biased the earlier ensemble toward never-short
    # indicators and made its consensus degenerate).
    t0 = time.time()
    counts_short = np.zeros((T, N), dtype=np.int16)
    for j in space.base_idx:
        counts_short += (tensor[j] == -1)
    fs = counts_short.astype(np.float32) / len(space.base_idx)
    print(f"Consensus built over {len(space.base_idx)} signals [{time.time()-t0:.0f}s]")

    s, e = ev.slices["train"]
    train_valid = ev.valid[s:e]

    rows = []
    for thr in THRESHOLDS:
        hot = fs >= thr
        # Fraction of train ticker-days in the "hot" (high-weight) state; the
        # low weight is solved from it so mean train exposure is exactly 1.0.
        p = float((hot[s:e] & train_valid).sum() / train_valid.sum())
        if not 0.02 < p < 0.98:
            continue
        for d in TILTS:
            w_lo = 1.0 - d * p / (1.0 - p)
            if w_lo < 0:
                continue                      # would require shorting to stay balanced
            w = np.where(hot, np.float32(1.0 + d), np.float32(w_lo)).astype(np.float32)
            tr, te = wev.cagr(w, "train"), wev.cagr(w, "test")
            rows.append({
                "short_thresh": thr, "tilt": d, "p_hot_train": p,
                "w_high": 1.0 + d, "w_low": w_lo,
                "train_cagr": tr, "test_cagr": te,
                "train_excess": tr - bh["train"], "test_excess": te - bh["test"],
                "train_a_cagr": wev.cagr(w, "train_a"), "train_b_cagr": wev.cagr(w, "train_b"),
                "net_train_cagr": wev.cagr(w, "train", costs=True),
                "net_test_cagr": wev.cagr(w, "test", costs=True),
                "exposure_train": wev.exposure(w, "train"),
                "exposure_test": wev.exposure(w, "test"),
                "turnover_yr": wev.turnover(w, "test"),
            })

    df = pd.DataFrame(rows).sort_values("train_cagr", ascending=False)
    df.to_csv(OUT_CSV, index=False)
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 30)
    cols = ["short_thresh", "tilt", "w_high", "w_low", "train_cagr", "test_cagr",
            "test_excess", "net_test_cagr", "exposure_train", "exposure_test", "turnover_yr"]
    print(f"\n{len(df)} tilt configurations -> {OUT_CSV}\n")
    print(df[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    win = df[(df.train_cagr > bh["train"]) & (df.test_cagr > bh["test"])]
    net_win = win[win.net_test_cagr > bh["test"]]
    print(f"\n{len(win)} beat B&H on train AND unseen test; "
          f"{len(net_win)} still beat it net of {COST_BPS:.0f}bps costs")


if __name__ == "__main__":
    main()
