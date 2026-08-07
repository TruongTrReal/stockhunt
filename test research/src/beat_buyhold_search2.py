"""Pass 2 of the beat-buy-and-hold search: a wider candidate space.

Pass 1 (beat_buyhold_search.py) combines a combo's members by sum-then-sign and
maps the result through four transforms. This adds the three levers that pass 1
cannot express:

  vote thresholds - "go short only if at least 2 of 3 members agree" is a
      different (and much less trigger-happy) rule than sign-of-sum, which lets
      a single member drag the whole combo out of the market.
  confirmation    - require k consecutive agreeing bars before acting. Most of
      these rules lose to buy-and-hold by whipsawing out and back in; this is
      the cheapest direct attack on that, at the cost of reacting later.
  a wider gate set - more trend-filter lookbacks, plus gates that combine the
      stock's own trend with the index's.

Everything else (windows, validation, buy-and-hold baselines) is imported from
pass 1 so the two searches remain directly comparable.

    python beat_buyhold_search2.py [--pool N] [--beam N] [--max-size N]
"""

import argparse
import time

import numpy as np
import pandas as pd

from beat_buyhold_search import (
    COST_BPS, OUT_CSV, TRANSFORMS, Evaluator, Space, WINDOWS, _transform,
)
from signal_tensor import CACHE_PATH, load

OUT_CSV2 = OUT_CSV.replace(".csv", "_pass2.csv")

# (long_threshold, short_threshold) applied to the summed member votes. None
# means sign-of-sum, i.e. pass 1's behaviour.
VOTE_RULES = (None, (1, 2), (2, 2), (1, 3), (2, 3), (3, 3))
CONFIRM_BARS = (1, 3, 5)


def _confirm(p: np.ndarray, k: int) -> np.ndarray:
    """Act only after k consecutive bars agree; otherwise hold the last acted
    position. Implemented as: a new position is adopted when the trailing k
    bars are identical, else carry forward."""
    if k <= 1:
        return p
    same = np.ones(p.shape, dtype=bool)
    for lag in range(1, k):
        same[lag:] &= p[lag:] == p[:-lag]
        same[:lag] = False
    # forward-fill the last confirmed position through the unconfirmed bars
    idx = np.where(same, np.arange(p.shape[0])[:, None], 0)
    np.maximum.accumulate(idx, axis=0, out=idx)
    return np.take_along_axis(p, idx, axis=0).astype(np.int8)


def _vote(acc: np.ndarray, rule) -> np.ndarray:
    if rule is None:
        return np.sign(acc).astype(np.int8)
    lo, hi = rule
    return np.where(acc >= lo, 1, np.where(acc <= -hi, -1, 0)).astype(np.int8)


class Space2(Space):
    def __init__(self, z):
        super().__init__(z)
        closes = z["closes"]
        cl = pd.DataFrame(closes)
        spy = z["spy"]
        smas = {n: cl.rolling(n, min_periods=n).mean().to_numpy(dtype=np.float32)
                for n in (50, 100, 150, 200)}
        spy_smas = {n: pd.Series(spy).rolling(n, min_periods=n).mean().to_numpy()
                    for n in (50, 100, 200)}
        with np.errstate(invalid="ignore"):
            own = {n: closes > smas[n] for n in smas}
            spyg = {n: np.broadcast_to((spy > spy_smas[n])[:, None], closes.shape)
                    for n in spy_smas}
        self.gates = {
            "none": None,
            "own200": ("flat", own[200]),
            "own150": ("flat", own[150]),
            "own100": ("flat", own[100]),
            "own50": ("flat", own[50]),
            "spy200": ("flat", spyg[200]),
            "spy100": ("flat", spyg[100]),
            "spy200_short": ("short", spyg[200]),
            "spy100_short": ("short", spyg[100]),
            "own200_short": ("short", own[200]),
            "both200": ("flat", own[200] & spyg[200]),
            "either200": ("flat", own[200] | spyg[200]),
            "both200_short": ("short", own[200] & spyg[200]),
        }

    def raw_acc(self, combo) -> np.ndarray:
        """Summed member votes, before any vote threshold is applied."""
        acc = self.tensor[combo[0][0]].astype(np.int8)
        if combo[0][1] < 0:
            acc = (-acc).astype(np.int8)
        for j, sign in combo[1:]:
            acc = (acc + (sign * self.tensor[j])).astype(np.int8)
        return acc

    def build2(self, combo, vote, confirm, transform, gate) -> np.ndarray:
        p = _confirm(_vote(self.raw_acc(combo), vote), confirm)
        p = _transform(p, transform)
        g = self.gates[gate]
        if g is None:
            return p
        mode, mask = g
        return (p * mask).astype(np.int8) if mode == "flat" else np.where(mask, p, -1).astype(np.int8)


def evaluate_all2(space, ev, combos, window="train"):
    out = []
    for combo in combos:
        acc = space.raw_acc(combo)
        size = len(combo)
        for vote in VOTE_RULES:
            if vote is not None and (vote[0] > size or vote[1] > size):
                continue                       # threshold unreachable for this combo size
            voted = _vote(acc, vote)
            for confirm in CONFIRM_BARS:
                c = _confirm(voted, confirm)
                for transform in TRANSFORMS:
                    p = _transform(c, transform)
                    for gate, g in space.gates.items():
                        if g is None:
                            pos = p
                        else:
                            mode, mask = g
                            pos = ((p * mask).astype(np.int8) if mode == "flat"
                                   else np.where(mask, p, -1).astype(np.int8))
                        out.append((ev.cagr(pos, window), combo, vote, confirm, transform, gate))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=40)
    ap.add_argument("--beam", type=int, default=25)
    ap.add_argument("--max-size", type=int, default=3)
    args = ap.parse_args()

    z = load(CACHE_PATH)
    ev, space = Evaluator(z), Space2(z)
    bh = ev.baseline
    print("\nBuy-and-hold average per-ticker CAGR (the bar to clear):")
    for w in ("train_a", "train_b", "train", "test"):
        print(f"  {w:<9}{bh[w]:>8.2%}")

    t0, results = time.time(), []
    singles = [(a,) for a in space.atoms]
    n_var = len(CONFIRM_BARS) * len(TRANSFORMS) * len(space.gates)
    print(f"\nStage 1: {len(singles)} atoms x ~{n_var} variants "
          f"= ~{len(singles) * n_var:,} candidates")
    scored = evaluate_all2(space, ev, singles)
    scored.sort(key=lambda r: -r[0])
    results += scored
    print(f"  best train CAGR {scored[0][0]:.2%} vs B&H {bh['train']:.2%} "
          f"[{time.time()-t0:.0f}s]")

    best_by_atom = {}
    for score, combo, *_ in scored:
        best_by_atom[combo[0]] = max(best_by_atom.get(combo[0], -9), score)
    pool = [a for a, _ in sorted(best_by_atom.items(), key=lambda kv: -kv[1])[:args.pool]]
    beam = list(dict.fromkeys([c for _, c, *_ in scored]))[:args.beam]

    for size in range(2, args.max_size + 1):
        cands = []
        for combo in beam:
            for atom in pool:
                if atom[0] in {j for j, _ in combo}:
                    continue
                cands.append(tuple(sorted(combo + (atom,))))
        cands = list(dict.fromkeys(cands))
        print(f"\nStage {size}: {len(cands):,} memberships")
        scored = evaluate_all2(space, ev, cands)
        scored.sort(key=lambda r: -r[0])
        results += scored
        print(f"  best train CAGR {scored[0][0]:.2%} [{time.time()-t0:.0f}s]")
        beam = list(dict.fromkeys([c for _, c, *_ in scored]))[:args.beam]

    results.sort(key=lambda r: -r[0])
    print(f"\n{len(results):,} candidates scored in {time.time()-t0:.0f}s. Validating...")

    rows = []
    for score, combo, vote, confirm, transform, gate in results:
        if score <= bh["train"]:
            break
        pos = space.build2(combo, vote, confirm, transform, gate)
        test = ev.cagr(pos, "test")
        if test <= bh["test"]:
            continue
        ca, cb = ev.cagr(pos, "train_a"), ev.cagr(pos, "train_b")
        rows.append({
            "combo": " + ".join(space.atom_name(a) for a in combo),
            "size": len(combo), "vote": str(vote), "confirm": confirm,
            "transform": transform, "gate": gate,
            "train_cagr": score, "train_a_cagr": ca, "train_b_cagr": cb, "test_cagr": test,
            "robust": bool(ca > bh["train_a"] and cb > bh["train_b"]),
            "train_excess": score - bh["train"], "test_excess": test - bh["test"],
            "net_train_cagr": float(np.mean(ev.per_ticker(pos, "train", costs=True))),
            "net_test_cagr": float(np.mean(ev.per_ticker(pos, "test", costs=True))),
            "exposure_test": ev.exposure(pos, "test"),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        print("\nNO SURVIVORS in pass 2 either.")
        return
    df["net_test_excess"] = df["net_test_cagr"] - bh["test"]
    df = df.sort_values("test_excess", ascending=False)
    df.to_csv(OUT_CSV2, index=False)
    print(f"\n{len(df)} survivors (beat B&H on train AND unseen test) -> {OUT_CSV2}")
    print(f"  of which {int(df['robust'].sum())} also beat B&H in BOTH train halves separately")
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.max_colwidth", 55)
    print(df.head(25)[["combo", "vote", "confirm", "transform", "gate", "robust",
                       "train_cagr", "test_cagr", "net_test_cagr",
                       "exposure_test"]].to_string(index=False))


if __name__ == "__main__":
    main()
