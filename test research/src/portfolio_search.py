"""Search combos on NET PORTFOLIO Sharpe instead of average per-ticker Sharpe.

Two findings from the earlier work say the previous objective was the wrong one:

  diversification  Market risk is 21% of a single stock's strategy variance but
      90% of the portfolio's. Averaging 501 individually-measured Sharpes throws
      that away, so it undervalues anything that cuts common risk and overvalues
      stock-specific noise. The hedge overlay scores -0.012 per-ticker and +0.305
      per-portfolio on identical positions.
  costs            Every candidate so far dies between 9 and 14bps. Cost was
      always a post-hoc filter, never part of the objective, so the search never
      had a reason to prefer a cheaper way of expressing the same edge.

This optimises the thing you would actually trade: the equal-weight book's
Sharpe, net of 5bps of turnover, so a candidate that trades less to reach the
same signal wins on its own merits rather than being discovered afterwards.

Selection is train-only. Survivors must beat buy-and-hold AND the current
champion on the unseen test window, survive an extra bar of lag, and be
materially different from buy-and-hold.

    python portfolio_search.py [--pool N] [--beam N] [--max-size N]
"""

import argparse
import math
import time

import numpy as np
import pandas as pd

from beat_buyhold_search import TRADING_DAYS, TRANSFORMS, Evaluator, _transform
from beat_buyhold_search2 import Space2
from sharpe_search import memmel_z, sharpe_of
from signal_tensor import CACHE_PATH, load

OUT_CSV = "../data/portfolio_search_results.csv"
COST_BPS = 5.0

# Minimum fraction of ticker-days the rule must actually hold a position.
#
# Sharpe alone rewards doing nothing: a rule in the market 0.4% of the time
# earns 0.2%/yr at 0.1% drawdown, and that tiny return over an even tinier
# volatility scores 1.96 - better than anything real. The first run of this
# search spent all four beam stages climbing toward exactly that, so the floor
# has to be inside the objective rather than a filter applied at the end, or the
# beam never explores the tradable part of the space. Buy-and-hold is 1.0 and
# the current champion is 0.58, so 0.25 is permissive without being degenerate.
MIN_EXPOSURE = 0.25


class PortfolioEvaluator:
    """Equal-weight book Sharpe, net of turnover costs."""

    def __init__(self, z, ev):
        self.valid = ev.valid
        self.slices = ev.slices
        self.R = np.where(self.valid, np.nan_to_num(z["returns"]), 0.0).astype(np.float32)
        self.N = self.R.shape[1]
        self.n = {w: np.maximum(self.valid[s:e].sum(axis=1), 1).astype(np.float32)
                  for w, (s, e) in ev.slices.items()}

    def _held(self, pos, s, e, extra_lag=False):
        p = np.vstack([np.zeros((1, self.N), pos.dtype), pos[:-1]]) if extra_lag else pos
        if s > 0:
            return p[s - 1:e - 1]
        return np.vstack([np.zeros((1, self.N), p.dtype), p[:e - 1]])

    def daily(self, pos, window, cost_bps=COST_BPS, extra_lag=False):
        s, e = self.slices[window]
        held = self._held(pos, s, e, extra_lag)
        m, n = self.valid[s:e], self.n[window]
        out = (held * self.R[s:e]).sum(axis=1) / n
        if cost_bps:
            first = pos[s - 2:s - 1] if s >= 2 else np.zeros((1, self.N), pos.dtype)
            traded = (np.abs(np.diff(np.vstack([first, held]), axis=0)) * m).sum(axis=1) / n
            out = out - traded * cost_bps / 1e4
        return out

    def sharpe(self, pos, window, cost_bps=COST_BPS, extra_lag=False):
        return sharpe_of(self.daily(pos, window, cost_bps, extra_lag))

    def objective(self, pos, window):
        """Net portfolio Sharpe, but -inf for rules that are barely invested.
        See MIN_EXPOSURE - without this the beam optimises toward doing nothing."""
        if self.exposure(pos, window) < MIN_EXPOSURE:
            return -math.inf
        return self.sharpe(pos, window)

    def turnover(self, pos, window):
        s, e = self.slices[window]
        held = self._held(pos, s, e)
        m, n = self.valid[s:e], self.n[window]
        first = pos[s - 2:s - 1] if s >= 2 else np.zeros((1, self.N), pos.dtype)
        return float(((np.abs(np.diff(np.vstack([first, held]), axis=0)) * m).sum(axis=1)
                      / n).mean() * TRADING_DAYS)

    def exposure(self, pos, window):
        s, e = self.slices[window]
        m = self.valid[s:e]
        return float(((np.abs(pos[s:e]) * m).sum()) / m.sum())

    def stats(self, pos, window, cost_bps=COST_BPS):
        r = self.daily(pos, window, cost_bps)
        eq = np.cumprod(1 + np.clip(r, -0.999, None))
        yrs = len(r) / TRADING_DAYS
        dd = float((eq / np.maximum.accumulate(eq) - 1).min())
        cagr = float(eq[-1] ** (1 / yrs) - 1) if eq[-1] > 0 else np.nan
        return {"cagr": cagr, "sharpe": sharpe_of(r), "maxdd": dd,
                "calmar": cagr / abs(dd) if dd < 0 else np.nan}


def variants(space, raw):
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
    pe = PortfolioEvaluator(z, ev)
    space = Space2(z)
    names = list(z["names"])
    T, N = z["returns"].shape
    ones = np.ones((T, N), np.int8)

    bh = {w: pe.sharpe(ones, w, cost_bps=0) for w in ("train_a", "train_b", "train", "test")}
    print("Buy-and-hold PORTFOLIO Sharpe (the bar):")
    for w, v in bh.items():
        print(f"  {w:<9}{v:>8.4f}")

    # Current champion: best combo + 50% short overlay below SPY's 100-day.
    atoms_ch = tuple(sorted((names.index(t.lstrip("~")), -1 if t.startswith("~") else 1)
                            for t in "~ADOSC + ~CDLCLOSINGMARUBOZU + ~VAR".split(" + ")))
    champ_base = space.build2(atoms_ch, None, 1, "defensive", "none").astype(np.float32)
    spy = z["spy"]
    sma = pd.Series(spy).rolling(100, min_periods=100).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        below = np.where(np.isnan(sma), 0.0, (spy <= sma).astype(float))
    champ = (champ_base - 0.5 * below[:, None].astype(np.float32)).astype(np.float32)
    print(f"\nCurrent champion (hedged combo): train {pe.sharpe(champ,'train'):.4f}  "
          f"test {pe.sharpe(champ,'test'):.4f}  turnover {pe.turnover(champ,'test'):.1f}/yr\n")

    t0, results = time.time(), []
    singles = [(a,) for a in space.atoms]
    print(f"Stage 1: {len(singles)} atoms x {len(TRANSFORMS)} transforms x "
          f"{len(space.gates)} gates = {len(singles)*len(TRANSFORMS)*len(space.gates):,}")
    for combo in singles:
        raw = space.raw_position(combo)
        for pos, tr, g in variants(space, raw):
            results.append((pe.objective(pos, "train"), combo, tr, g))
    results = [r for r in results if math.isfinite(r[0])]
    results.sort(key=lambda r: -r[0])
    print(f"  best train net portfolio Sharpe {results[0][0]:.4f} "
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
            for pos, tr, g in variants(space, raw):
                stage.append((pe.objective(pos, "train"), combo, tr, g))
        stage = [r for r in stage if math.isfinite(r[0])]
        stage.sort(key=lambda r: -r[0])
        results += stage
        print(f"Stage {size}: {len(cands):,} memberships -> best {stage[0][0]:.4f} "
              f"[{time.time()-t0:.0f}s]")
        beam = list(dict.fromkeys([c for _, c, _, _ in stage]))[:args.beam]

    results.sort(key=lambda r: -r[0])
    print(f"\n{len(results):,} candidates scored in {time.time()-t0:.0f}s. Validating...")

    champ_test = pe.sharpe(champ, "test")
    rows, seen = [], set()
    for score, combo, transform, gate in results:
        if score <= bh["train"]:
            break
        key = (combo, transform, gate)
        if key in seen:
            continue
        seen.add(key)
        pos = space.build2(combo, None, 1, transform, gate)
        expo = pe.exposure(pos, "test")
        if expo > 0.98 or expo < MIN_EXPOSURE:
            continue        # buy-and-hold in disguise, or barely invested
        test = pe.sharpe(pos, "test")
        if test <= bh["test"]:
            continue
        st = pe.stats(pos, "test")
        rows.append({
            "combo": " + ".join(space.atom_name(a) for a in combo),
            "size": len(combo), "transform": transform, "gate": gate,
            "train_sharpe": score, "test_sharpe": test,
            "train_a_sharpe": pe.sharpe(pos, "train_a"),
            "train_b_sharpe": pe.sharpe(pos, "train_b"),
            "test_sharpe_lag2": pe.sharpe(pos, "test", extra_lag=True),
            "test_cagr": st["cagr"], "test_maxdd": st["maxdd"], "test_calmar": st["calmar"],
            "turnover_yr": pe.turnover(pos, "test"),
            "exposure_test": expo,
            "z_vs_bh": memmel_z(pe.daily(pos, "test"), pe.daily(ones, "test", cost_bps=0))[0],
            "z_vs_champ": memmel_z(pe.daily(pos, "test"), pe.daily(champ, "test"))[0],
            "beats_champ": test > champ_test,
        })
        if len(rows) >= 400:
            break

    df = pd.DataFrame(rows)
    if df.empty:
        print("\nNO SURVIVORS.")
        return
    df["robust"] = (df.train_a_sharpe > bh["train_a"]) & (df.train_b_sharpe > bh["train_b"])
    df.to_csv(OUT_CSV, index=False)
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 40)
    pd.set_option("display.max_colwidth", 44)

    cols = ["combo", "transform", "gate", "robust", "train_sharpe", "test_sharpe",
            "test_sharpe_lag2", "test_cagr", "test_maxdd", "turnover_yr",
            "exposure_test", "z_vs_bh", "z_vs_champ"]
    print(f"\n{len(df)} survivors beat B&H on train and unseen test -> {OUT_CSV}")
    print(f"  {int(df.robust.sum())} robust across both train halves; "
          f"{int(df.beats_champ.sum())} beat the current champion's test Sharpe "
          f"({champ_test:.4f})")
    print("\nTop 15 by TRAIN net portfolio Sharpe (selection is train-only):")
    print(df.sort_values("train_sharpe", ascending=False).head(15)[cols].to_string(
        index=False, float_format=lambda x: f"{x:.3f}"))


if __name__ == "__main__":
    main()
