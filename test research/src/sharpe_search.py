"""Search for indicator combinations that beat buy-and-hold on RISK-ADJUSTED
return, validated out-of-sample.

The CAGR search failed for a structural reason (see beat_buyhold_search.py and
the skill diagnostic): no TA rule identifies a subset of days with negative
expected return, so stepping aside always costs compounding, and buy-and-hold is
the CAGR optimum. Sharpe removes that trap. It is scale-invariant, so a rule is
no longer penalised simply for holding less - it is rewarded for shedding more
volatility than return. Trend gates lose 4-8pp of CAGR but cut drawdown hard,
which is exactly the trade Sharpe is supposed to price.

Two deliberate choices, both learned from earlier bugs in this project:

  * A ticker whose rule never trades gets Sharpe 0, not NaN. Averaging with
    nanmean silently shrinks the denominator and lets a rule that trades two
    tickers out of 501 top the leaderboard. Flat means zero return at zero
    risk, and zero is the honest score.
  * Significance is reported on the equal-weight PORTFOLIO return series using
    the Jobson-Korkie statistic with Memmel's correction, the standard test for
    comparing two Sharpe ratios measured on the same sample. A paired t-test
    across 501 tickers would treat one market factor as 501 observations.

    python sharpe_search.py [--pool N] [--beam N] [--max-size N]
"""

import argparse
import itertools
import math
import time

import numpy as np
import pandas as pd

from beat_buyhold_search import COST_BPS, TRADING_DAYS, TRANSFORMS, Evaluator, _transform
from beat_buyhold_search2 import Space2
from signal_tensor import CACHE_PATH, load

OUT_CSV = "../data/sharpe_survivors.csv"


class SharpeEvaluator:
    """Average per-ticker Sharpe, via precomputed sums.

    Positions are -1/0/1, so a day's strategy return is r, 0 or -r. Both the sum
    and the sum of squares therefore decompose into masked sums of r and r^2,
    with no per-candidate arithmetic on the returns themselves.
    """

    def __init__(self, z: dict, ev: Evaluator):
        self.valid = ev.valid
        self.slices, self.keep = ev.slices, ev.keep
        R = np.where(self.valid, np.nan_to_num(z["returns"]), 0.0).astype(np.float32)
        self.R, self.R2 = R, (R * R).astype(np.float32)
        self.n = {w: self.valid[s:e].sum(axis=0) for w, (s, e) in ev.slices.items()}
        ones = np.ones(R.shape, dtype=np.int8)
        self.baseline = {w: self.avg(ones, w, shift=False) for w in ev.slices}

    def _held(self, pos, s, e, shift=True):
        """Position held into each bar of [s, e). Windows starting at index 0
        have no prior bar, so the first row is flat rather than an empty slice."""
        if not shift:
            return pos[s:e]
        if s > 0:
            return pos[s - 1:e - 1]
        return np.vstack([np.zeros((1, pos.shape[1]), pos.dtype), pos[:e - 1]])

    def per_ticker(self, pos, window, shift=True):
        s, e = self.slices[window]
        held = self._held(pos, s, e, shift)
        m = self.valid[s:e]
        lm, sm = (held == 1) & m, (held == -1) & m
        R, R2 = self.R[s:e], self.R2[s:e]
        ssum = (R * lm).sum(axis=0) - (R * sm).sum(axis=0)
        ssq = (R2 * (lm | sm)).sum(axis=0)
        n = np.maximum(self.n[window], 2).astype(np.float64)
        mean = ssum / n
        var = np.maximum((ssq - n * mean * mean) / (n - 1), 0.0)
        std = np.sqrt(var)
        # std == 0 means the rule never took a position on this ticker: zero
        # return at zero risk, which is a Sharpe of 0 rather than a missing value.
        sharpe = np.divide(mean, std, out=np.zeros_like(mean), where=std > 0)
        return (sharpe * math.sqrt(TRADING_DAYS))[self.keep[window]]

    def avg(self, pos, window, shift=True):
        return float(np.mean(self.per_ticker(pos, window, shift=shift)))

    def portfolio_returns(self, pos, window, costs=False):
        """Equal-weight daily portfolio return, for the significance test."""
        s, e = self.slices[window]
        held, m, R = self._held(pos, s, e), self.valid[s:e], self.R[s:e]
        n = np.maximum(m.sum(axis=1), 1)
        out = ((held * R) * m).sum(axis=1) / n
        if costs:
            first = pos[s - 2:s - 1] if s >= 2 else np.zeros((1, pos.shape[1]), pos.dtype)
            traded = np.abs(np.diff(np.vstack([first, held]), axis=0))
            out = out - (traded * m).sum(axis=1) / n * COST_BPS / 1e4
        return out

    def exposure(self, pos, window):
        s, e = self.slices[window]
        m = self.valid[s:e]
        return float(((pos[s:e] != 0) & m).sum() / m.sum())


def sharpe_of(x):
    sd = x.std(ddof=1)
    return float(x.mean() / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else 0.0


def memmel_z(a, b):
    """Jobson-Korkie z for Sharpe(a) - Sharpe(b) with Memmel's correction.
    Both series must cover the same days.

    The variance formula assumes the Sharpe ratios are measured at the SAME
    frequency as T. T here counts daily observations, so the statistic must be
    built from per-day Sharpes; feeding it annualised ones inflates z by roughly
    sqrt(TRADING_DAYS) (measured: 15.5x on near-identical series, turning a true
    z of 0.67 into 10.34). Annualised values are still returned for display.
    """
    sa_ann, sb_ann = sharpe_of(a), sharpe_of(b)
    sa = sa_ann / math.sqrt(TRADING_DAYS)
    sb = sb_ann / math.sqrt(TRADING_DAYS)
    T = len(a)
    rho = float(np.corrcoef(a, b)[0, 1])
    v = (2 - 2 * rho + 0.5 * (sa * sa + sb * sb - 2 * rho * rho * sa * sb)) / T
    return (sa - sb) / math.sqrt(v) if v > 0 else 0.0, sa_ann, sb_ann


def _variants(space, raw):
    for transform in TRANSFORMS:
        p = _transform(raw, transform)
        for gate, g in space.gates.items():
            if g is None:
                yield p, transform, gate
            else:
                mode, mask = g
                yield ((p * mask).astype(np.int8) if mode == "flat"
                       else np.where(mask, p, -1).astype(np.int8)), transform, gate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=45)
    ap.add_argument("--beam", type=int, default=30)
    ap.add_argument("--max-size", type=int, default=4)
    args = ap.parse_args()

    z = load(CACHE_PATH)
    ev = Evaluator(z)
    se = SharpeEvaluator(z, ev)
    space = Space2(z)
    bh = se.baseline
    print("\nBuy-and-hold average per-ticker SHARPE (the bar to clear):")
    for w in ("train_a", "train_b", "train", "test"):
        print(f"  {w:<9}{bh[w]:>8.4f}")

    t0, results = time.time(), []
    singles = [(a,) for a in space.atoms]
    print(f"\nStage 1: {len(singles)} atoms x {len(TRANSFORMS)} transforms x "
          f"{len(space.gates)} gates = {len(singles)*len(TRANSFORMS)*len(space.gates):,}")
    for combo in singles:
        raw = space.raw_position(combo)
        for pos, tr, g in _variants(space, raw):
            results.append((se.avg(pos, "train"), combo, tr, g))
    results.sort(key=lambda r: -r[0])
    print(f"  best train Sharpe {results[0][0]:.4f} vs B&H {bh['train']:.4f} "
          f"({space.atom_name(results[0][1][0])}, {results[0][2]}, {results[0][3]}) "
          f"[{time.time()-t0:.0f}s]")

    best_by_atom = {}
    for score, combo, _, _ in results:
        best_by_atom[combo[0]] = max(best_by_atom.get(combo[0], -9), score)
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
        for combo in cands:
            raw = space.raw_position(combo)
            for pos, tr, g in _variants(space, raw):
                stage.append((se.avg(pos, "train"), combo, tr, g))
        stage.sort(key=lambda r: -r[0])
        results += stage
        print(f"Stage {size}: {len(cands):,} memberships -> best train Sharpe "
              f"{stage[0][0]:.4f} [{time.time()-t0:.0f}s]")
        beam = list(dict.fromkeys([c for _, c, _, _ in stage]))[:args.beam]

    results.sort(key=lambda r: -r[0])
    print(f"\n{len(results):,} candidates scored in {time.time()-t0:.0f}s. Validating...")

    rows = []
    for score, combo, transform, gate in results:
        if score <= bh["train"]:
            break
        pos = space.build2(combo, None, 1, transform, gate)
        test = se.avg(pos, "test")
        if test <= bh["test"]:
            continue
        pa = se.portfolio_returns(pos, "test", costs=True)
        pb = se.portfolio_returns(np.ones_like(pos), "test")
        zstat, sa, sb = memmel_z(pa, pb)
        net = se.avg(pos, "test")
        rows.append({
            "combo": " + ".join(space.atom_name(a) for a in combo),
            "size": len(combo), "transform": transform, "gate": gate,
            "train_sharpe": score, "test_sharpe": test,
            "train_a_sharpe": se.avg(pos, "train_a"), "train_b_sharpe": se.avg(pos, "train_b"),
            "test_excess": test - bh["test"],
            "port_sharpe_net": sa, "port_sharpe_bh": sb, "memmel_z": zstat,
            "exposure_test": se.exposure(pos, "test"),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        print("\nNO SURVIVORS: nothing beat buy-and-hold Sharpe on train AND test.")
        return
    df["robust"] = (df.train_a_sharpe > bh["train_a"]) & (df.train_b_sharpe > bh["train_b"])
    df = df.sort_values("memmel_z", ascending=False)
    df.to_csv(OUT_CSV, index=False)

    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.max_colwidth", 46)
    print(f"\n{len(df)} survivors (beat B&H Sharpe on train AND unseen test) -> {OUT_CSV}")
    print(f"  {int(df.robust.sum())} also beat B&H in both train halves; "
          f"{int((df.memmel_z > 2).sum())} have Memmel z > 2 net of costs")
    cols = ["combo", "transform", "gate", "robust", "train_sharpe", "test_sharpe",
            "port_sharpe_net", "port_sharpe_bh", "memmel_z", "exposure_test"]
    print(df.head(20)[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))


if __name__ == "__main__":
    main()
