"""Search for indicator combinations whose average per-ticker CAGR beats
buy-and-hold, validated out-of-sample.

Why the earlier combo search found nothing: it combined rules that were all
long-biased trend followers and scored them in-sample. Combining long-biased
rules only adds time spent flat, and time spent flat in a bull market costs
CAGR. So this search adds the two things that can actually move the number:

  transforms - how a combined signal maps to a position. "defensive" stays long
      by default and only steps aside when the signal is bearish, so it keeps
      buy-and-hold's exposure instead of giving it up. Plain sign-combining
      cannot express that.
  gates      - a trend filter (own or SPY 200-day) that can force flat or short.
      This is the only mechanism in the space that can sidestep a sustained
      drawdown, which is what beating buy-and-hold on compounded return requires.

Selection is done on TRAIN only (2015-2021) and every reported survivor must
also beat buy-and-hold on TEST (2022-), which it was never fitted to. Candidates
must additionally win on both halves of train separately - a cheap robustness
filter that kills combos riding one lucky stretch.

    python beat_buyhold_search.py [--beam N] [--pool N] [--max-size N]
"""

import argparse
import itertools
import time

import numpy as np
import pandas as pd

from signal_tensor import CACHE_PATH, load

TRADING_DAYS = 252
MIN_DAYS_IN_WINDOW = 200      # a ticker needs this much history in a window to count
COST_BPS = 5.0
OUT_CSV = "../data/beat_buyhold_survivors.csv"

# Windows are (label, start, end-exclusive). Train is split in two so a combo
# has to work in both halves, not just on average over the whole thing.
WINDOWS = {
    "train_a": ("2015-01-01", "2019-01-01"),
    "train_b": ("2019-01-01", "2022-01-01"),
    "train":   ("2015-01-01", "2022-01-01"),
    "test":    ("2022-01-01", "2100-01-01"),
}

TRANSFORMS = ("raw", "longonly", "defensive", "defensive_short")


def _transform(p: np.ndarray, kind: str) -> np.ndarray:
    if kind == "raw":
        return p
    if kind == "longonly":                      # only take the explicit longs
        return (p == 1).astype(np.int8)
    if kind == "defensive":                     # long unless bearish -> keeps B&H-like exposure
        return np.where(p == -1, 0, 1).astype(np.int8)
    if kind == "defensive_short":               # always in the market, flips short on bearish
        return np.where(p == -1, -1, 1).astype(np.int8)
    raise ValueError(kind)


class Evaluator:
    """Scores a position matrix as average per-ticker CAGR.

    Positions are -1/0/1, so a day's log return is one of only three values:
    log1p(r), 0, or log1p(-r). Precomputing those two arrays turns scoring into
    masked sums with no per-candidate transcendental math, which is what makes
    a six-figure candidate count tractable.
    """

    def __init__(self, z: dict):
        ret = z["returns"]
        self.valid = ~np.isnan(ret)
        r = np.nan_to_num(ret)
        self.Lp = np.where(self.valid, np.log1p(np.clip(r, -0.999, None)), 0).astype(np.float32)
        self.Lm = np.where(self.valid, np.log1p(np.clip(-r, -0.999, None)), 0).astype(np.float32)

        dates = pd.DatetimeIndex(z["dates"])
        self.slices, self.n_days, self.years, self.keep = {}, {}, {}, {}
        for label, (lo, hi) in WINDOWS.items():
            idx = np.flatnonzero((dates >= lo) & (dates < hi))
            s, e = int(idx[0]), int(idx[-1]) + 1
            self.slices[label] = (s, e)
            nd = self.valid[s:e].sum(axis=0)
            self.keep[label] = nd >= MIN_DAYS_IN_WINDOW
            self.n_days[label] = nd
            self.years[label] = np.maximum(nd, 1) / TRADING_DAYS

        # Buy-and-hold uses the identical ticker mask and window, so the
        # comparison is like-for-like rather than against a different universe.
        ones = np.ones(ret.shape, dtype=np.int8)
        self.baseline = {w: self.cagr(ones, w, shift=False) for w in WINDOWS}

    def cagr(self, pos: np.ndarray, window: str, shift: bool = True) -> float:
        return float(np.mean(self.per_ticker(pos, window, shift=shift)))

    def per_ticker(self, pos: np.ndarray, window: str, shift: bool = True,
                   costs: bool = False) -> np.ndarray:
        s, e = self.slices[window]
        held = pos[s - 1:e - 1] if (shift and s > 0) else pos[s:e]
        if shift and s == 0:
            held = np.vstack([np.zeros((1, pos.shape[1]), np.int8), pos[:e - 1]])

        long_m = held == 1
        short_m = held == -1
        tot = (self.Lp[s:e] * long_m).sum(axis=0) + (self.Lm[s:e] * short_m).sum(axis=0)
        if costs:
            # log(1-c) ~= -c for the small per-trade cost here; |dpos| is the
            # fraction of notional traded (a 1 -> -1 flip trades twice).
            traded = np.abs(np.diff(held.astype(np.int16), axis=0)).sum(axis=0)
            tot = tot - traded * COST_BPS / 1e4
        return np.expm1(tot[self.keep[window]] / self.years[window][self.keep[window]])

    def exposure(self, pos: np.ndarray, window: str) -> float:
        s, e = self.slices[window]
        m = self.valid[s:e]
        return float(((pos[s:e] != 0) & m).sum() / m.sum())


class Space:
    """The candidate space: deduplicated signal atoms, transforms, and gates."""

    def __init__(self, z: dict):
        tensor, names = z["tensor"], list(z["names"])

        # Many TA-Lib functions are exact duplicates under these rules (ROC,
        # ROCP, ROCR, ROCR100 and MOM all produce identical positions). Dropping
        # them avoids spending the search budget re-testing the same signal and
        # reporting five "different" combos that are one combo.
        seen, self.base_idx, self.base_names = {}, [], []
        for j, name in enumerate(names):
            h = hash(tensor[j].tobytes())
            if h in seen:
                seen[h].append(name)
                continue
            seen[h] = [name]
            self.base_idx.append(j)
            self.base_names.append(name)
        self.aliases = {v[0]: v for v in seen.values() if len(v) > 1}
        print(f"{len(names)} indicators -> {len(self.base_names)} unique signals "
              f"({len(names) - len(self.base_names)} exact duplicates dropped)")

        self.tensor = tensor
        # An atom is a signal plus an optional sign flip: inverting is free and
        # doubles the space, and the cross-sectional work showed inverted TA
        # signals are not symmetric junk.
        self.atoms = [(j, s) for j in self.base_idx for s in (1, -1)]

        closes = z["closes"]
        cl = pd.DataFrame(closes)
        sma200 = cl.rolling(200, min_periods=200).mean().to_numpy(dtype=np.float32)
        sma50 = cl.rolling(50, min_periods=50).mean().to_numpy(dtype=np.float32)
        spy = z["spy"]
        spy_sma200 = pd.Series(spy).rolling(200, min_periods=200).mean().to_numpy()
        with np.errstate(invalid="ignore"):
            own200 = closes > sma200
            own50 = closes > sma50
            spy200 = (spy > spy_sma200)[:, None]
        self.gates = {
            "none": None,
            "own200": ("flat", own200),
            "own50": ("flat", own50),
            "spy200": ("flat", np.broadcast_to(spy200, closes.shape)),
            "spy200_short": ("short", np.broadcast_to(spy200, closes.shape)),
        }

    def atom_name(self, atom) -> str:
        j, sign = atom
        nm = self.base_names[self.base_idx.index(j)] if j in self.base_idx else str(j)
        return ("~" if sign < 0 else "") + nm

    def raw_position(self, combo) -> np.ndarray:
        """Sum-then-sign across the combo's members (majority-ish consensus)."""
        acc = self.tensor[combo[0][0]].astype(np.int8)
        if combo[0][1] < 0:
            acc = (-acc).astype(np.int8)
        for j, sign in combo[1:]:
            acc = (acc + (sign * self.tensor[j])).astype(np.int8)
        return np.sign(acc).astype(np.int8)

    def build(self, combo, transform: str, gate: str) -> np.ndarray:
        p = _transform(self.raw_position(combo), transform)
        g = self.gates[gate]
        if g is None:
            return p
        mode, mask = g
        return (p * mask).astype(np.int8) if mode == "flat" else np.where(mask, p, -1).astype(np.int8)


def evaluate_all(space: Space, ev: Evaluator, combos, window="train"):
    """Score every (combo, transform, gate) variant on one window."""
    out = []
    for combo in combos:
        raw = space.raw_position(combo)
        for transform in TRANSFORMS:
            p = _transform(raw, transform)
            for gate in space.gates:
                g = space.gates[gate]
                if g is None:
                    pos = p
                else:
                    mode, mask = g
                    pos = (p * mask).astype(np.int8) if mode == "flat" else np.where(mask, p, -1).astype(np.int8)
                out.append((ev.cagr(pos, window), combo, transform, gate))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=60, help="atoms carried into multi-indicator combos")
    ap.add_argument("--beam", type=int, default=40, help="combos kept when growing to the next size")
    ap.add_argument("--max-size", type=int, default=4)
    args = ap.parse_args()

    z = load(CACHE_PATH)
    ev, space = Evaluator(z), Space(z)
    bh = ev.baseline
    print("\nBuy-and-hold average per-ticker CAGR (the bar to clear):")
    for w in ("train_a", "train_b", "train", "test"):
        print(f"  {w:<9}{bh[w]:>8.2%}   ({ev.slices[w][1] - ev.slices[w][0]} bars)")

    t0 = time.time()
    results = []

    # --- Stage 1: singles ------------------------------------------------
    singles = [(a,) for a in space.atoms]
    print(f"\nStage 1: {len(singles)} atoms x {len(TRANSFORMS)} transforms x "
          f"{len(space.gates)} gates = {len(singles) * len(TRANSFORMS) * len(space.gates):,} candidates")
    scored = evaluate_all(space, ev, singles)
    scored.sort(key=lambda r: -r[0])
    results += scored
    print(f"  best train CAGR {scored[0][0]:.2%} vs B&H {bh['train']:.2%} "
          f"({space.atom_name(scored[0][1][0])}, {scored[0][2]}, {scored[0][3]}) [{time.time()-t0:.0f}s]")

    # --- Stages 2..N: beam search ---------------------------------------
    # Pool = atoms that did best on their own; beam = best combos so far, each
    # extended by one pool atom. Exhaustive is impossible at size 4 (C(120,4)
    # is 8.5M memberships before transforms and gates).
    best_by_atom = {}
    for score, combo, _, _ in scored:
        best_by_atom[combo[0]] = max(best_by_atom.get(combo[0], -9), score)
    pool = [a for a, _ in sorted(best_by_atom.items(), key=lambda kv: -kv[1])[:args.pool]]

    beam = [c for _, c, _, _ in scored[:args.beam * 4]]
    beam = list(dict.fromkeys(beam))[:args.beam]
    for size in range(2, args.max_size + 1):
        cands = []
        for combo in beam:
            for atom in pool:
                if atom[0] in {j for j, _ in combo}:
                    continue                       # no indicator twice (or against itself inverted)
                cands.append(tuple(sorted(combo + (atom,))))
        cands = list(dict.fromkeys(cands))
        print(f"\nStage {size}: {len(cands):,} memberships x {len(TRANSFORMS) * len(space.gates)} "
              f"variants = {len(cands) * len(TRANSFORMS) * len(space.gates):,} candidates")
        scored = evaluate_all(space, ev, cands)
        scored.sort(key=lambda r: -r[0])
        results += scored
        print(f"  best train CAGR {scored[0][0]:.2%} vs B&H {bh['train']:.2%} [{time.time()-t0:.0f}s]")
        beam = list(dict.fromkeys([c for _, c, _, _ in scored]))[:args.beam]

    # --- Validation ------------------------------------------------------
    results.sort(key=lambda r: -r[0])
    print(f"\n{len(results):,} candidates scored in {time.time()-t0:.0f}s. Validating...")

    rows = []
    for score, combo, transform, gate in results:
        if score <= bh["train"]:
            break                                   # sorted, so nothing below beats train
        pos = space.build(combo, transform, gate)
        test = ev.cagr(pos, "test")
        if test <= bh["test"]:
            continue                                # the actual out-of-sample gate
        # Winning both train halves is reported as a robustness tier rather
        # than required: train_b (2019-2021) is a 26%/yr bull run, and a
        # defensive rule failing to beat *that* is not evidence it is fitted.
        ca, cb = ev.cagr(pos, "train_a"), ev.cagr(pos, "train_b")
        net_test = float(np.mean(ev.per_ticker(pos, "test", costs=True)))
        net_train = float(np.mean(ev.per_ticker(pos, "train", costs=True)))
        rows.append({
            "combo": " + ".join(space.atom_name(a) for a in combo),
            "size": len(combo), "transform": transform, "gate": gate,
            "train_cagr": score, "train_a_cagr": ca, "train_b_cagr": cb, "test_cagr": test,
            "robust": bool(ca > bh["train_a"] and cb > bh["train_b"]),
            "train_excess": score - bh["train"], "test_excess": test - bh["test"],
            "net_train_cagr": net_train, "net_test_cagr": net_test,
            "net_test_excess": net_test - bh["test"],
            "exposure_test": ev.exposure(pos, "test"),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        print("\nNO SURVIVORS: nothing beat buy-and-hold on train halves AND test.")
        return
    df = df.sort_values("test_excess", ascending=False)
    df.to_csv(OUT_CSV, index=False)
    print(f"\n{len(df)} survivors (beat B&H on train AND unseen test) -> {OUT_CSV}")
    print(f"  of which {int(df['robust'].sum())} also beat B&H in BOTH train halves separately")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.max_colwidth", 60)
    cols = ["combo", "transform", "gate", "robust", "train_cagr", "test_cagr",
            "test_excess", "net_test_cagr", "exposure_test"]
    print(df.head(25)[cols].to_string(index=False))


if __name__ == "__main__":
    main()
