"""Stage 2b: do combinations of the leading rules produce an edge, walk-forward?

`combo_sweep.py` answers this on a single split. Walk-forward is now mandatory, and the
combination question deserves it more than most: a pair is a fresh search over
K-choose-2 x operators, so the multiple-testing bar moves under you while you look.

The mechanism being tested is diversification. Two rules combine usefully only if their
*return streams* are less than perfectly correlated — combining two views of the same
thing gives you the same thing with extra turnover. So this module reports the pairwise
correlation of the shortlisted legs' out-of-sample net returns alongside the results,
because that number decides in advance whether any combination can help.

Operators come from `signals.combine`, so this stage and `build_payload.py` cannot drift
apart on what an operator means:

    vote   sign(a + b)              agree -> that side, disagree -> flat
    and    a where signs agree      strictest, cuts exposure hardest
    or     a if non-zero else b     loosest, raises exposure
    gate   a only while b non-zero  asymmetric, so ordered pairs

Watch the exposure column when reading the leaderboard. Against a buy-and-hold benchmark
an IR of roughly minus the benchmark's Sharpe is what *doing nothing* scores, so `and`
and `gate` are structurally penalised for being out of the market and `or` is structurally
flattered for being in it. That ordering is a fact about the metric, not evidence that
`or` combinations work.

Shortlisting is on mean IN-SAMPLE IR, never an out-of-sample column. The honest headline
is `IS#1[combo]`: the combination re-selected on each in-sample window, which is what you
could have traded.

Run::

    python combo_wf.py --class us_stocks --tf 1d
"""

from __future__ import annotations

import argparse
import itertools
import time

import numpy as np
import pandas as pd
from tqdm import tqdm

from config import (BASELINE_NAME, CLASSES, HEADLINE_SCENARIO, MIN_BARS,
                    MIN_IR_COVERAGE, RESULTS_DIR, scenarios)
from engines import vector
import metrics
import signals
import td_loader
import walkforward as wfmod

LEG = "~"
OP = "|"
SYMMETRIC = ("vote", "and", "or")
ASYMMETRIC = ("gate",)
DEFAULT_TIMEFRAMES = ("1d", "4h")
DEFAULT_TOP_K = 8


def combo_names(rules: list[str]) -> list[str]:
    """`A~B|and` for the symmetric operators, both orders for `gate`."""
    names = []
    for a, b in itertools.combinations(rules, 2):
        names += [f"{a}{LEG}{b}{OP}{op}" for op in SYMMETRIC]
    for a, b in itertools.permutations(rules, 2):
        names += [f"{a}{LEG}{b}{OP}{op}" for op in ASYMMETRIC]
    return names


def parse(name: str) -> tuple[str, str, str]:
    legs, _, op = name.rpartition(OP)
    a, _, b = legs.partition(LEG)
    return a, b, op


def leg_correlation(leg_nets: dict[str, dict[str, np.ndarray]],
                    union: dict[str, np.ndarray]) -> tuple[float, pd.DataFrame]:
    """Mean pairwise correlation of the legs' out-of-sample net return streams.

    Averaged across assets. If this sits near 1 the legs are one signal wearing several
    names and no operator can manufacture diversification from them.
    """
    rules = sorted(leg_nets)
    mats = []
    for symbol, u in union.items():
        cols = {r: leg_nets[r][symbol][u] for r in rules if symbol in leg_nets[r]}
        if len(cols) < 2:
            continue
        mats.append(pd.DataFrame(cols).corr())
    if not mats:
        return float("nan"), pd.DataFrame()
    avg = sum(mats) / len(mats)
    off = avg.to_numpy()[~np.eye(len(avg), dtype=bool)]
    return float(np.nanmean(off)), avg


def run_pair(asset_class: str, timeframe: str, top_k: int) -> tuple[dict, dict]:
    tag = f"{asset_class}_{timeframe}"
    scen_key = HEADLINE_SCENARIO[asset_class]
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        return {}, {}

    start = min(df.index[0] for df in data.values())
    end = max(df.index[-1] for df in data.values())
    folds = wfmod.generate_folds(start, end)
    if len(folds) < 3:
        return {}, {"skipped": f"only {len(folds)} folds"}

    legs = [r for r in wfmod_shortlist(tag, top_k, scen_key)]
    legs, _ = signals.usable_rules(legs, asset_class, timeframe)
    if len(legs) < 2:
        return {}, {"skipped": "fewer than two usable legs"}

    free = {"key": "gross", "commission_bps": 0.0, "half_spread_bps": 0.0,
            "sell_fee_bps": 0.0, "borrow_annual": 0.0}
    bench = {}
    for symbol, df in data.items():
        close = df["Close"].to_numpy(dtype="float64")
        bpy = vector.bars_per_year(df.index)
        bench[symbol] = {
            "net": vector.net_returns(np.ones(len(df), dtype="float64"), close, free, bpy),
            "bpy": bpy, "close": close,
        }

    masks = {s: wfmod.fold_masks(df.index, folds) for s, df in data.items()}
    union = {s: np.logical_or.reduce([m[1] for m in ms if m is not None])
             for s, ms in masks.items() if any(m is not None for m in ms)}

    # Leg positions are built once per (rule, symbol) and reused by every operator that
    # references them; only one asset's worth is live at a time.
    leg_pos: dict[str, dict[str, np.ndarray]] = {}
    leg_nets: dict[str, dict[str, np.ndarray]] = {}
    for rule in legs:
        leg_pos[rule], leg_nets[rule] = {}, {}
        for symbol, df in data.items():
            if symbol not in union:
                continue
            p = signals.position_for(rule, df, asset_class, timeframe,
                                     baseline_name=BASELINE_NAME)
            if p is None:
                continue
            leg_pos[rule][symbol] = p
            fee = next(f for f in scenarios(asset_class) if f["key"] == scen_key)
            leg_nets[rule][symbol] = vector.net_returns(
                p, bench[symbol]["close"], fee, bench[symbol]["bpy"])

    mean_corr, corr_matrix = leg_correlation(leg_nets, union)

    def position_fn(name: str, symbol: str, df: pd.DataFrame) -> np.ndarray | None:
        a, b, op = parse(name)
        pa, pb = leg_pos.get(a, {}).get(symbol), leg_pos.get(b, {}).get(symbol)
        if pa is None or pb is None:
            return None
        return signals.combine(pa, pb, op)

    names = combo_names(legs)
    fold_rows, union_rows, expo_rows = [], [], []
    for name in tqdm(names, desc=f"combos {tag}"):
        a, b, op = parse(name)
        for symbol in union:
            pa, pb = leg_pos.get(a, {}).get(symbol), leg_pos.get(b, {}).get(symbol)
            if pa is None or pb is None:
                continue
            pos = signals.combine(pa, pb, op)
            bd = bench[symbol]
            u = union[symbol]
            # `long_frac` is the load-bearing diagnostic, not `exposure`. A combination
            # that is long ~100% of the time IS buy-and-hold, and its IR converges on 0
            # from below for that reason alone — which on this leaderboard looks exactly
            # like an improvement. Read the two columns together or be fooled.
            expo_rows.append({"rule": name, "op": op, "symbol": symbol,
                              "exposure": float(np.mean(pos[u] != 0)),
                              "long_frac": float(np.mean(pos[u] > 0)),
                              "short_frac": float(np.mean(pos[u] < 0))})
            for fee in scenarios(asset_class):
                net = vector.net_returns(pos, bd["close"], fee, bd["bpy"])
                union_rows.append((name, symbol, fee["key"],
                                   wfmod._ir(net, bd["net"], u, bd["bpy"]),
                                   # Compounded return over the same union mask the IR is
                                   # scored on, so the money and the ratio describe one
                                   # window. `leaderboard_row` averages these into the
                                   # net/bench/excess columns the dashboard reads.
                                   wfmod._total_return_pct(net, u),
                                   wfmod._total_return_pct(bd["net"], u)))
                for f, m in zip(folds, masks[symbol]):
                    if m is None:
                        continue
                    fold_rows.append((
                        name, symbol, fee["key"], f.index,
                        wfmod._ir(net, bd["net"], m[0], bd["bpy"]),
                        wfmod._ir(net, bd["net"], m[1], bd["bpy"]),
                    ))

    fold_table = pd.DataFrame(
        fold_rows, columns=["rule", "symbol", "scenario", "fold", "ir_is", "ir_oos"])
    fixed = pd.DataFrame(union_rows, columns=["rule", "symbol", "scenario", "ir_wf",
                                              "ret_pct", "bench_pct"])
    if fold_table.empty:
        return {}, {"skipped": "no scoreable folds"}

    picks = wfmod.pick_champions(fold_table, set(names))
    wf = wfmod.stitch(data, asset_class, timeframe, folds, bench, picks,
                      "IS#1[combo]", position_fn=position_fn)

    yrs = (wf.groupby("symbol")["years_oos"].median() if not wf.empty
           else pd.Series(dtype=float))
    fixed = fixed.copy()
    fixed["years_oos"] = fixed["symbol"].map(yrs)
    fixed["n_folds"] = len(folds)
    fixed["n_switches"] = 0
    fixed = fixed.dropna(subset=["years_oos"])

    allrows = pd.concat([wf, fixed], ignore_index=True) if not wf.empty else fixed
    ir_by_scen = allrows.groupby(["rule", "scenario"])["ir_wf"].mean().unstack()
    expo = pd.DataFrame(expo_rows).groupby("rule")[
        ["exposure", "long_frac", "short_frac"]].mean()

    out = []
    for (label, scen), grp in allrows.groupby(["rule", "scenario"]):
        is_wf = label.startswith("IS#1")
        row = wfmod.leaderboard_row(grp, label, "wf_combo" if is_wf else "fixed_combo",
                                    asset_class, timeframe, scen,
                                    ir_by_scen.loc[label] if label in ir_by_scen.index
                                    else pd.Series(dtype=float))
        row["op"] = "" if is_wf else parse(label)[2]
        for col in ("exposure", "long_frac", "short_frac"):
            row[col] = (float(expo.loc[label, col]) if label in expo.index
                        else float("nan"))
        row["rankable"] = metrics.rankable(row, MIN_IR_COVERAGE)
        out.append(row)
    summary = pd.DataFrame(out).sort_values(["scenario", "ir_net"], ascending=[True, False])

    years = float(fixed["years_oos"].median())
    meta = {
        "class": asset_class, "timeframe": timeframe, "n_assets": len(union),
        "n_legs": len(legs), "legs": ",".join(legs), "n_combos": len(names),
        "n_folds": len(folds), "years_oos": years,
        "mean_leg_correlation": mean_corr,
        "noise_ceiling": metrics.noise_ceiling(len(names), years),
        "oos_start": str(folds[0].is_end.date()),
        "oos_end": str(folds[-1].oos_end.date()),
    }
    return {"summary": summary, "folds": fold_table, "corr": corr_matrix}, meta


def wfmod_shortlist(tag: str, k: int, scen: str) -> list[str]:
    """Top-k singles by mean IN-SAMPLE IR from stage 1b."""
    path = RESULTS_DIR / f"wf_folds_{tag}.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing — run `python walkforward.py` first")
    folds = pd.read_csv(path)
    folds = folds[(folds.scenario == scen) & (folds.rule != BASELINE_NAME)]
    return folds.groupby("rule")["ir_is"].mean().dropna().nlargest(k).index.tolist()


def report(tables: dict, meta: dict) -> None:
    s = tables["summary"]
    scen = HEADLINE_SCENARIO[meta["class"]]
    h = s[(s.scenario == scen) & s.rankable]
    print(f"\n=== {meta['class']}_{meta['timeframe']} ({meta['seconds']:.0f}s) ===")
    print(f"  {meta['n_legs']} legs -> {meta['n_combos']} combinations | "
          f"{meta['n_folds']} folds | OOS {meta['oos_start']} -> {meta['oos_end']} "
          f"({meta['years_oos']:.1f}y)")
    print(f"  legs: {meta['legs']}")
    print(f"  mean pairwise leg correlation (OOS net returns): "
          f"{meta['mean_leg_correlation']:.3f}"
          f"   <- decides whether diversification is even available")
    print(f"  noise ceiling for {meta['n_combos']} combinations: "
          f"IR {meta['noise_ceiling']:+.3f}")

    fixed = h[h.wf_mode == "fixed_combo"]
    if not fixed.empty:
        print("\n  by operator (time-in-market explains most of the ordering):")
        agg = (fixed.groupby("op")
               .agg(mean_ir=("ir_net", "mean"), best_ir=("ir_net", "max"),
                    long_frac=("long_frac", "mean"), exposure=("exposure", "mean"),
                    breadth=("ir_hit_rate", "mean"))
               .sort_values("mean_ir", ascending=False))
        for op, r in agg.iterrows():
            print(f"    {op:<6} mean IR {r['mean_ir']:+.3f}  best {r['best_ir']:+.3f}  "
                  f"long {r['long_frac']:.0%}  exposure {r['exposure']:.0%}  "
                  f"breadth {r['breadth']:.0%}")

        print("\n  top 5 fixed combinations  (long% ~100 means it has become buy-and-hold):")
        for r in fixed.nlargest(5, "ir_net").itertuples():
            print(f"    {r.rule:<38} IR {r.ir_net:+.3f}  long {r.long_frac:.0%}  "
                  f"breadth {r.ir_hit_rate:.0%}  t {r.t_stat:+.2f}  {r.gates_passed}/4")

        # The only combination that would be evidence: one that beats its own best leg
        # while spending materially less time in the market than buy-and-hold.
        real = fixed[fixed.long_frac < 0.90]
        if not real.empty:
            b = real.nlargest(1, "ir_net").iloc[0]
            print(f"\n  best combination that is NOT ~always-long (long < 90%): "
                  f"{b['rule']}  IR {b['ir_net']:+.3f}  long {b['long_frac']:.0%}")

    wf = h[h.wf_mode == "wf_combo"]
    if not wf.empty:
        r = wf.iloc[0]
        print(f"\n  IS#1[combo] (re-selected each fold — the honest number): "
              f"IR {r['ir_net']:+.3f}, breadth {r['ir_hit_rate']:.0%}, "
              f"t {r['t_stat']:+.2f}, {r['gates_passed']}/4 gates")

    best = h.nlargest(1, "ir_net")
    if not best.empty:
        r = best.iloc[0]
        verdict = "ABOVE" if r["ir_net"] > meta["noise_ceiling"] else "below"
        print(f"  best row overall: {r['rule']}  IR {r['ir_net']:+.3f}  "
              f"({verdict} the noise ceiling)")
    print(f"  rows clearing all four gates: {int((h.gates_passed == 4).sum())} of {len(h)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", default=list(DEFAULT_TIMEFRAMES))
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    metas = []
    for asset_class in args.classes:
        for timeframe in args.timeframes:
            t0 = time.time()
            tables, meta = run_pair(asset_class, timeframe, args.top_k)
            if not tables:
                print(f"{asset_class}/{timeframe}: "
                      f"{meta.get('skipped', 'no cached data')}, skipped")
                continue
            tag = f"{asset_class}_{timeframe}"
            tables["summary"].to_csv(RESULTS_DIR / f"cwf_summary_{tag}.csv", index=False)
            tables["corr"].to_csv(RESULTS_DIR / f"cwf_legcorr_{tag}.csv")
            meta["seconds"] = time.time() - t0
            metas.append(meta)
            report(tables, meta)

    if metas:
        pd.DataFrame(metas).to_csv(RESULTS_DIR / "cwf_meta.csv", index=False)
        print(f"\nwrote {RESULTS_DIR}")


if __name__ == "__main__":
    main()
