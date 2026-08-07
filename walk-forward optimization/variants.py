"""Stage 1c: can the leading rules be *twisted* into clearing more gates?

Stage 1b established that no rule clears the four gates and that choosing one in real time
costs 0.21-0.47 IR. This asks the natural follow-up: the leaders are close to buy-and-hold
and lose mainly through shorting, whipsaw and turnover — so do the standard repairs for
those three problems move any gate?

Eight transforms, each aimed at a specific gate rather than thrown at the wall:

    base            the rule as swept, for reference
    long_only       never short. Shorting a rising market is the obvious drag, and
                    `../test research/` showed the leaders hold shorts 8% of daily bars.
    invert          flip the sign. Not a joke: the cross-sectional study found TA signals
                    systematically *anti*-predictive, so this is a real hypothesis.
    hold10          minimum 10-bar holding period -> cuts turnover -> HEADROOM gate
    confirm2        act only on a signal that has persisted two bars -> kills whipsaw
    regime200       trade only above the 200-bar SMA -> IR and BREADTH gates
    long_regime200  both of the above, the classic retail "trend filter" configuration
    voltgt          scale exposure by inverse realised vol, capped at 1.0 -> IR gate.
                    Unlevered on purpose: this repo's one previous weak survivor was
                    unlevered vol targeting, and leverage would flatter it for free.

Two disciplines carried from stage 1b, both load-bearing:

**Candidates are shortlisted on in-sample IR only.** `wf_folds_*.csv` carries `ir_is` per
rule per fold; the shortlist is the top-K by mean IS IR. Shortlisting on an out-of-sample
column is selection on test, which this project has done once and had to retract.

**The transform is chosen per fold, in-sample, like any other parameter.** A `BEST[rule]`
path picks that rule's best variant on each in-sample window and trades it through the
next out-of-sample window. Reporting only the best fixed variant would re-introduce the
hindsight this stage exists to remove — the eight fixed-variant rows are printed as
diagnosis, and `BEST[...]` is the number to quote.

Multiplicity is reported, not hidden: K candidates x 8 transforms is a fresh search, and
`metrics.noise_ceiling` says what the best of that many worthless variants would reach by
luck. Any IR under that line means nothing however healthy it looks.

Run::

    python variants.py                            # 1d and 4h, both classes
    python variants.py --class us_stocks --tf 1d
"""

from __future__ import annotations

import argparse
import time

import numpy as np
import pandas as pd
import talib
from tqdm import tqdm

from wfo_paths import RESULTS_DIR          # noqa: F401  (wires sys.path first)
from config import (BASELINE_NAME, CLASSES, HEADLINE_SCENARIO, MIN_BARS,
                    MIN_IR_COVERAGE, scenarios)
from engines import vector
import metrics
import signals
import td_loader
import walkforward as wfmod

SEP = "|"                      # `MAXINDEX|long_only`
DEFAULT_TIMEFRAMES = ("1d", "4h")
DEFAULT_TOP_K = 12
REGIME_WINDOW = 200            # bars, not days: at 4h that is ~100 sessions
VOL_WINDOW = 20
MIN_HOLD = 10
CONFIRM = 2


# ---------------------------------------------------------------- transforms
# Every transform must be causal: the value at bar t may use data at or before t and
# nothing after. The engine already lags positions one bar before applying returns, so a
# transform that reads close[t] is fine; one that reads close[t+1] would not be.

def _min_hold(pos: np.ndarray, n: int) -> np.ndarray:
    """Freeze the position for `n` bars after every change."""
    out = pos.copy()
    lock = 0
    for i in range(1, len(out)):
        if lock > 0:
            out[i] = out[i - 1]
            lock -= 1
        elif out[i] != out[i - 1]:
            lock = n - 1
    return out


def _confirm(pos: np.ndarray, n: int) -> np.ndarray:
    """Adopt a new signal only once it has repeated `n` bars running."""
    out = np.zeros_like(pos)
    for i in range(len(pos)):
        if i >= n - 1 and np.all(pos[i - n + 1:i + 1] == pos[i]):
            out[i] = pos[i]
        elif i:
            out[i] = out[i - 1]
    return out


def _regime(close: np.ndarray) -> np.ndarray:
    sma = talib.SMA(close, timeperiod=REGIME_WINDOW)
    return np.nan_to_num((close > sma).astype("float64"), nan=0.0)


def _vol_scale(close: np.ndarray, bpy: float) -> np.ndarray:
    """Inverse-vol scaling capped at 1.0, targeting the asset's own median vol.

    Capped because levering to hit a target is a different strategy with a different risk
    profile, and this repo's gates are stated for an unlevered book.
    """
    ret = np.empty_like(close)
    ret[0] = 0.0
    ret[1:] = close[1:] / close[:-1] - 1.0
    s = pd.Series(ret).rolling(VOL_WINDOW).std(ddof=1).to_numpy() * np.sqrt(max(bpy, 1.0))
    target = np.nanmedian(s)
    if not np.isfinite(target) or target <= 0:
        return np.ones_like(close)
    scale = np.divide(target, s, out=np.ones_like(s), where=np.isfinite(s) & (s > 0))
    return np.clip(np.nan_to_num(scale, nan=1.0), 0.0, 1.0)


TRANSFORMS = {
    "base": lambda p, close, bpy: p,
    "long_only": lambda p, close, bpy: np.clip(p, 0.0, 1.0),
    "invert": lambda p, close, bpy: -p,
    f"hold{MIN_HOLD}": lambda p, close, bpy: _min_hold(p, MIN_HOLD),
    f"confirm{CONFIRM}": lambda p, close, bpy: _confirm(p, CONFIRM),
    f"regime{REGIME_WINDOW}": lambda p, close, bpy: p * _regime(close),
    f"long_regime{REGIME_WINDOW}": lambda p, close, bpy: np.clip(p, 0.0, 1.0) * _regime(close),
    "voltgt": lambda p, close, bpy: p * _vol_scale(close, bpy),
}


def shortlist(tag: str, k: int, scen: str) -> list[str]:
    """Top-k rules by mean IN-SAMPLE IR from stage 1b. Never an out-of-sample column."""
    path = RESULTS_DIR / f"wf_folds_{tag}.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing — run `python walkforward.py` for {tag} first")
    folds = pd.read_csv(path)
    folds = folds[(folds.scenario == scen) & (folds.rule != BASELINE_NAME)]
    mean_is = folds.groupby("rule")["ir_is"].mean().dropna()
    return mean_is.nlargest(k).index.tolist()


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

    base_rules = shortlist(tag, top_k, scen_key)
    runnable, _ = signals.usable_rules(base_rules, asset_class, timeframe)

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

    # Base positions are built once per (rule, symbol); the eight transforms are cheap
    # array ops on top, so this never materialises more than one asset's worth at a time.
    def position_fn(name: str, symbol: str, df: pd.DataFrame) -> np.ndarray | None:
        rule, _, tname = name.partition(SEP)
        pos = signals.position_for(rule, df, asset_class, timeframe,
                                   baseline_name=BASELINE_NAME)
        if pos is None:
            return None
        b = bench[symbol]
        return TRANSFORMS[tname or "base"](pos, b["close"], b["bpy"])

    masks = {s: wfmod.fold_masks(df.index, folds) for s, df in data.items()}
    union = {s: np.logical_or.reduce([m[1] for m in ms if m is not None])
             for s, ms in masks.items() if any(m is not None for m in ms)}

    fold_rows, union_rows = [], []
    for rule in tqdm(runnable, desc=f"variants {tag}"):
        for symbol, df in data.items():
            if symbol not in union:
                continue
            base = signals.position_for(rule, df, asset_class, timeframe,
                                        baseline_name=BASELINE_NAME)
            if base is None:
                continue
            b = bench[symbol]
            for tname, fn in TRANSFORMS.items():
                pos = fn(base, b["close"], b["bpy"])
                label = f"{rule}{SEP}{tname}"
                for fee in scenarios(asset_class):
                    net = vector.net_returns(pos, b["close"], fee, b["bpy"])
                    union_rows.append((label, symbol, fee["key"],
                                       wfmod._ir(net, b["net"], union[symbol], b["bpy"])))
                    for f, m in zip(folds, masks[symbol]):
                        if m is None:
                            continue
                        fold_rows.append((
                            label, symbol, fee["key"], f.index,
                            wfmod._ir(net, b["net"], m[0], b["bpy"]),
                            wfmod._ir(net, b["net"], m[1], b["bpy"]),
                        ))

    fold_table = pd.DataFrame(
        fold_rows, columns=["rule", "symbol", "scenario", "fold", "ir_is", "ir_oos"])
    fixed = pd.DataFrame(
        union_rows, columns=["rule", "symbol", "scenario", "ir_wf"])
    if fold_table.empty:
        return {}, {"skipped": "no scoreable folds"}

    # Per-fold transform selection, one path per base rule.
    stitched = []
    for rule in runnable:
        variants = {f"{rule}{SEP}{t}" for t in TRANSFORMS}
        picks = wfmod.pick_champions(fold_table, variants)
        if picks.empty:
            continue
        s = wfmod.stitch(data, asset_class, timeframe, folds, bench, picks,
                         f"BEST[{rule}]", position_fn=position_fn)
        if not s.empty:
            stitched.append(s)

    yrs = (pd.concat(stitched).groupby("symbol")["years_oos"].median()
           if stitched else pd.Series(dtype=float))
    fixed = fixed.copy()
    fixed["years_oos"] = fixed["symbol"].map(yrs)
    fixed["n_folds"] = len(folds)
    fixed["n_switches"] = 0
    fixed = fixed.dropna(subset=["years_oos"])

    allrows = pd.concat(stitched + [fixed], ignore_index=True)
    ir_by_scen = allrows.groupby(["rule", "scenario"])["ir_wf"].mean().unstack()

    out = []
    for (label, scen), grp in allrows.groupby(["rule", "scenario"]):
        mode = "wf_transform" if label.startswith("BEST[") else "fixed_transform"
        row = wfmod.leaderboard_row(grp, label, mode, asset_class, timeframe, scen,
                                    ir_by_scen.loc[label] if label in ir_by_scen.index
                                    else pd.Series(dtype=float))
        row["base_rule"] = (label[5:-1] if mode == "wf_transform"
                            else label.split(SEP)[0])
        # Named `variant`, not `transform`: `df.transform` is a DataFrame *method*, so
        # `df.transform == "base"` silently evaluates to False instead of a mask.
        row["variant"] = "" if mode == "wf_transform" else label.split(SEP)[1]
        row["rankable"] = metrics.rankable(row, MIN_IR_COVERAGE)
        out.append(row)
    summary = pd.DataFrame(out).sort_values(["scenario", "ir_net"],
                                            ascending=[True, False])

    n_trials = len(runnable) * len(TRANSFORMS)
    years = float(fixed["years_oos"].median()) if not fixed.empty else float("nan")
    meta = {
        "class": asset_class, "timeframe": timeframe, "n_assets": len(data),
        "n_candidates": len(runnable), "n_transforms": len(TRANSFORMS),
        "n_trials": n_trials, "n_folds": len(folds),
        "years_oos": years,
        "noise_ceiling": metrics.noise_ceiling(n_trials, years),
        "oos_start": str(folds[0].is_end.date()),
        "oos_end": str(folds[-1].oos_end.date()),
    }
    return {"summary": summary, "folds": fold_table}, meta


def report(tables: dict, meta: dict) -> None:
    s = tables["summary"]
    scen = HEADLINE_SCENARIO[meta["class"]]
    h = s[(s.scenario == scen) & s.rankable]
    base = h[h.variant == "base"]
    print(f"\n=== {meta['class']}_{meta['timeframe']} ({meta['seconds']:.0f}s) ===")
    print(f"  {meta['n_candidates']} candidates x {meta['n_transforms']} transforms "
          f"= {meta['n_trials']} trials | {meta['n_folds']} folds | "
          f"OOS {meta['oos_start']} -> {meta['oos_end']}")
    print(f"  noise ceiling for {meta['n_trials']} trials over "
          f"{meta['years_oos']:.1f}y: IR {meta['noise_ceiling']:+.3f} "
          f"(anything below this is luck)")

    print("\n  mean IR by transform (across all candidates):")
    agg = (h[h.variant != ""].groupby("variant")
           .agg(mean_ir=("ir_net", "mean"), best_ir=("ir_net", "max"),
                mean_breadth=("ir_hit_rate", "mean"),
                mean_headroom=("headroom", "median"))
           .sort_values("mean_ir", ascending=False))
    base_mean = agg.loc["base", "mean_ir"] if "base" in agg.index else np.nan
    for t, r in agg.iterrows():
        flag = "" if t == "base" else f"   vs base {r['mean_ir'] - base_mean:+.3f}"
        print(f"    {t:<18} mean {r['mean_ir']:+.3f}  best {r['best_ir']:+.3f}  "
              f"breadth {r['mean_breadth']:.0%}  headroom {r['mean_headroom']:.2f}{flag}")

    wf = h[h.wf_mode == "wf_transform"]
    if not wf.empty:
        print("\n  per-fold transform selection (the honest number):")
        for r in wf.nlargest(5, "ir_net").itertuples():
            b = base[base.base_rule == r.base_rule]["ir_net"]
            delta = r.ir_net - b.iloc[0] if len(b) else np.nan
            print(f"    {r.rule:<28} IR {r.ir_net:+.3f}  vs base {delta:+.3f}  "
                  f"breadth {r.ir_hit_rate:.0%}  t {r.t_stat:+.2f}  "
                  f"{r.gates_passed}/4 gates")

    best = h.nlargest(1, "ir_net")
    if not best.empty:
        r = best.iloc[0]
        print(f"\n  best row overall: {r['rule']}  IR {r['ir_net']:+.3f}  "
              f"({r['gates_passed']}/4 gates)"
              f"  {'ABOVE' if r['ir_net'] > meta['noise_ceiling'] else 'below'} "
              f"the noise ceiling")
    gate_counts = {g: int(h[f"gate_{g}"].sum()) for g in ("ir", "breadth", "headroom", "t")}
    print(f"  rows passing each gate: {gate_counts} "
          f"| all four: {int((h.gates_passed == 4).sum())} of {len(h)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+",
                    default=list(DEFAULT_TIMEFRAMES))
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
            tables["summary"].to_csv(RESULTS_DIR / f"var_summary_{tag}.csv", index=False)
            tables["folds"].to_csv(RESULTS_DIR / f"var_folds_{tag}.csv", index=False)
            meta["seconds"] = time.time() - t0
            metas.append(meta)
            report(tables, meta)

    if metas:
        pd.DataFrame(metas).to_csv(RESULTS_DIR / "var_meta.csv", index=False)
        print(f"\nwrote {RESULTS_DIR}")


if __name__ == "__main__":
    main()
