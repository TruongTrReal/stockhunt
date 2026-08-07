"""Stage 1e: strategies other people published, at their settings and re-optimised.

Every earlier stage tested rules *this project generated* — 231 TA-Lib variants, 8
transforms on them, 4,652 pairs. All null. `prereg.py` was the one exception: five
strategies taken from the literature at their published settings, tested with no free
parameters at all, precisely because a small pre-committed set carries a far lower noise
ceiling than a search does (+0.21 at N=5 against +0.36 at N=96 over 41 out-of-sample
years). Those five were also all negative.

This is that idea done at scale. Twenty-six strategies gathered from where people
actually publish them — academic journals, QuantConnect's Strategy Library, trading
academies, forum and practitioner folklore — each recorded with its source, its exact
rule and a parameter grid, then run through the stage-1b walk-forward machinery.

Three rows come out per strategy, and which one gets quoted decides what the number
means:

    <name>        the published parameters. No fitting whatsoever, so this is the
                  cleanest test available and the one whose ceiling is lowest.
    WFO[<name>]   that strategy's grid re-selected on every 3-year in-sample window and
                  traded through the next 12 months. This is "walk-forward optimisation"
                  in the sense the term is normally used, and its value is what it
                  costs, not what it promises: stage 1b measured per-fold
                  re-optimisation *losing* on all 14 re-optimisable TA-Lib families.
    IS#1          per fold, the best cell in the WHOLE catalog on in-sample IR, traded
                  through the next out-of-sample window. The strategy a disciplined
                  researcher following this method would actually have run, and the
                  honest headline. Never quote the best fixed row.

Four controls sit in the same table, because a leaderboard of negative IRs cannot be
read without them:

    BUYHOLD       the benchmark, never charged and never flattened. Its IR against
                  itself is NaN, not 0 — the difference series is identically zero, so
                  the ratio is 0/0. That is correct and is why it cannot be selected.
    ALWAYS_LONG   always long but *charged* the real fee schedule. Its IR also comes out
                  NaN, and that is the finding: a position that never changes pays
                  nothing after entry, and its single entry falls before the first
                  out-of-sample window. Costs cannot explain a long-biased rule's
                  shortfall against buy-and-hold.
    RANDOM_50     long half the time, in 20-bar blocks, from a seeded generator. Zero
    RANDOM_75     signal by construction; long three-quarters of the time.

The two RANDOM rows are the important ones. `combo_wf.py` measured corr(IR, long_frac)
= 0.881 on daily equities: against a rising benchmark, IR approaches 0 from below as a
rule approaches always-long, so most of a leaderboard's ordering is a ranking of
time-in-market. RANDOM_50 and RANDOM_75 price that handicap directly — they are what a
strategy with *no* signal and that much market exposure scores. A strategy is only
carrying information if it beats its exposure-matched random control, and comparing it
to zero instead makes every long-biased rule look better than it is.

**Lookbacks are calendar, not bars.** "12 months" and "200 days" are converted through
the measured `bars_per_year` for each sheet, so a strategy means the same economic thing
at 1d and 4h — the same convention as `prereg.py`. The consequence is worth stating:
Connors' RSI(2) is specified as two *days*, so at 4h it becomes RSI over ~3 bars rather
than 2. Reading it as two bars instead would silently make it a different, faster
strategy on every intraday sheet, and the two sheets would no longer be comparable.

`ibs` is the exception that proves the rule: internal bar strength has no lookback at
all, so a 4h IBS genuinely is a different quantity from a daily IBS. It is run on both
and the difference is a property of the statistic, not a scaling choice.

Run::

    python strat_wf.py                                    # all classes, 1d and 4h
    python strat_wf.py --class us_etfs --tf 1d
    python strat_wf.py --list                             # print the catalog and exit
"""

from __future__ import annotations

import argparse
import time

import numpy as np
import pandas as pd
from tqdm import tqdm

from config import (CLASSES, HEADLINE_SCENARIO, MIN_BARS, MIN_IR_COVERAGE,
                    RESULTS_DIR, TIMEFRAMES, WF_MIN_FOLDS, scenarios)
from engines import vector
import metrics
import td_loader
import walkforward as wfmod

# The catalog itself is repo-level and engine-free; this module is the half that needs
# the fee model and the fold machinery. `config` is imported first on purpose -- it is
# what puts the repo root on sys.path so `strategies` resolves.
from strategies.catalog import (BASELINE, CATALOG, CONTROLS, RANDOM_DRAWS, SEP,
                                build, cells, decode, skipped_for)

DEFAULT_TIMEFRAMES = ("1d", "4h")

FREE_FEE = {"key": "gross", "commission_bps": 0.0, "half_spread_bps": 0.0,
            "sell_fee_bps": 0.0, "borrow_annual": 0.0}


# ---------------------------------------------------------------- the run

def _diagnostics(pos: np.ndarray, mask: np.ndarray, bpy: float) -> dict:
    """Time-in-market and churn over the scored window.

    `long_frac` is not decoration. `combo_wf.py` measured corr(IR, long_frac) = 0.881 on
    daily equities: against a rising benchmark, IR approaches 0 from below as a rule
    approaches always-long, so a leaderboard sorted by IR is partly a ranking of
    time-in-market. Any equity improvement is read against this column before it is
    believed.
    """
    p = pos[mask]
    if p.size == 0:
        return {"long_frac": np.nan, "exposure": np.nan, "turnover_yr": np.nan}
    years = p.size / bpy if bpy > 0 else np.nan
    return {
        "long_frac": float(np.mean(p > 0)),
        "exposure": float(np.mean(p != 0)),
        "turnover_yr": float(np.abs(np.diff(p)).sum() / years) if years > 0 else np.nan,
    }


def excess_cagr(per_symbol: pd.DataFrame) -> dict:
    """Compounded excess growth rate per asset, then aggregated. Three numbers.

    `walkforward.leaderboard_row` reports `excess_return_pct` as the mean of per-asset
    *total* returns, and on this universe that statistic is close to meaningless: SOXL
    buy-and-hold compounds 230x over the window while SPY does 6.4x, so averaging their
    percentages lets one asset write the answer. Annualising first and averaging second
    puts them on the same scale.

    It is reported alongside IR rather than instead of it because on leveraged ETFs the
    two genuinely disagree, and the disagreement is the finding. IR is built on the
    arithmetic mean of a difference series and is blind to variance drag; compounding is
    not. A rule that gives up 9%/yr of arithmetic mean while cutting annualised
    volatility from 94% to 63% *loses* on IR and *wins* on terminal wealth, and both
    statements are true of the same trades.

    `excess_cagr_min` is the one that decides anything. A positive mean carried by a
    single asset is the failure mode the leave-one-out gate exists to catch, and on a
    three-asset sheet the minimum says it more directly than breadth can.
    """
    if not {"ret_pct", "bench_pct", "years_oos"} <= set(per_symbol.columns):
        return {}
    yrs = per_symbol["years_oos"].to_numpy(dtype="float64")
    with np.errstate(invalid="ignore", divide="ignore"):
        # Clipped at -99.99%: a rule that lost everything has no defined growth rate,
        # and a negative base under a fractional power is NaN rather than an error.
        grow = lambda pct: np.power(
            np.clip(1.0 + per_symbol[pct].to_numpy(dtype="float64") / 100.0, 1e-4, None),
            np.divide(1.0, yrs, out=np.full_like(yrs, np.nan), where=yrs > 0)) - 1.0
        d = grow("ret_pct") - grow("bench_pct")
    d = d[np.isfinite(d)]
    if d.size == 0:
        return {"excess_cagr": np.nan, "excess_cagr_min": np.nan,
                "excess_cagr_hit": np.nan}
    return {"excess_cagr": float(np.mean(d)), "excess_cagr_min": float(np.min(d)),
            "excess_cagr_hit": float(np.mean(d > 0))}


def add_exposure_adjusted(summary: pd.DataFrame) -> pd.DataFrame:
    """Add `ir_random` and `ir_vs_random`: IR with the time-in-market handicap removed.

    Against a rising benchmark, being out of the market is expensive whether or not the
    rule knows anything — `combo_wf.py` measured corr(IR, long_frac) = 0.881 on daily
    equities, so most of a leaderboard's ordering is just exposure. The six controls
    trace that relationship empirically on this exact data: ALWAYS_FLAT pins x = 0, four
    seeded random rules sit between, and ALWAYS_LONG pins x = 1 at IR = 0 (an always-long
    rule *is* the benchmark, so its IR is 0 by definition — the NaN it computes is 0/0,
    not a missing value).

    `ir_vs_random` is the row's IR minus that curve at its own exposure. It is the number
    that answers "does this strategy know something", where raw IR only answers "was this
    strategy in the market". A strategy at -0.22 that is long 36% of the time is doing
    far better than one at -0.38 that is long 74% of the time, and the raw column says
    the opposite.

    Interpolated, not fitted: with six points a straight line would smooth over whatever
    curvature is really there, and the curve is measured, not modelled.
    """
    out = []
    for scen, grp in summary.groupby("scenario", sort=False):
        ctrl = grp[grp["rule"].str.startswith(("RANDOM_", "ALWAYS_"))]
        pts = {}
        for r in ctrl.itertuples():
            # ALWAYS_LONG is the benchmark; its 0/0 IR is definitionally 0.
            ir = 0.0 if r.rule == "ALWAYS_LONG" else r.ir_net
            if np.isfinite(r.long_frac) and np.isfinite(ir):
                pts[round(float(r.long_frac), 4)] = float(ir)
        grp = grp.copy()
        if len(pts) >= 2:
            xs = np.array(sorted(pts))
            ys = np.array([pts[x] for x in xs])
            grp["ir_random"] = np.interp(grp["long_frac"].to_numpy(dtype="float64"),
                                         xs, ys, left=ys[0], right=ys[-1])
            grp["ir_vs_random"] = grp["ir_net"] - grp["ir_random"]
        else:
            grp["ir_random"] = np.nan
            grp["ir_vs_random"] = np.nan
        out.append(grp)
    return pd.concat(out, ignore_index=True)


def _stitched_diagnostics(data: dict, folds, masks: dict, picks: pd.DataFrame,
                          bench: dict, label: str) -> pd.DataFrame:
    """`long_frac` / exposure / turnover for a stitched path.

    `walkforward.stitch` returns scores, not positions, so the same fold-by-fold write
    is repeated here to measure what the stitched path actually *held*. Without this the
    diagnostic column is blank on exactly the rows that matter — IS#1 and the WFO paths
    — and `long_frac` is the column that tells an equity IR improvement apart from a
    rule that simply spent more time in the market.
    """
    rows = []
    for symbol, df in data.items():
        ms = masks[symbol]
        mine = picks[picks["symbol"] == symbol]
        if mine.empty:
            continue
        b = bench[symbol]
        cache = {r: build(r, df, b["close"], b["bpy"], symbol)
                 for r in mine["rule"].unique()}
        for scen, grp in mine.groupby("scenario"):
            stitched = np.zeros(len(df))
            used = np.zeros(len(df), dtype=bool)
            for r in grp.itertuples():
                m, p = ms[r.fold], cache.get(r.rule)
                if m is None or p is None:
                    continue
                stitched[m[1]] = p[m[1]]
                used |= m[1]
            if used.any():
                rows.append({"rule": label, "symbol": symbol, "scenario": scen,
                             **_diagnostics(stitched, used, b["bpy"])})
    return pd.DataFrame(rows)


def run_pair(asset_class: str, timeframe: str) -> tuple[dict, dict]:
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        return {}, {}

    start = min(df.index[0] for df in data.values())
    end = max(df.index[-1] for df in data.values())
    folds = wfmod.generate_folds(start, end)
    if len(folds) < WF_MIN_FOLDS:
        return {}, {"skipped": f"only {len(folds)} folds in {start.date()}..{end.date()}"}

    bench = {}
    for symbol, df in data.items():
        close = df["Close"].to_numpy(dtype="float64")
        bpy = vector.bars_per_year(df.index)
        bench[symbol] = {
            "net": vector.net_returns(np.ones(len(df), dtype="float64"), close,
                                      FREE_FEE, bpy),
            "bpy": bpy, "close": close,
        }

    masks = {s: wfmod.fold_masks(df.index, folds) for s, df in data.items()}
    union = {s: np.logical_or.reduce([m[1] for m in ms if m is not None])
             for s, ms in masks.items() if any(m is not None for m in ms)}
    if not union:
        return {}, {"skipped": "no scoreable folds"}

    labels = cells(asset_class)
    fee_grid = scenarios(asset_class)

    fold_rows, union_rows = [], []
    for label in tqdm(labels + [BASELINE, *CONTROLS],
                      desc=f"strat {asset_class}/{timeframe}"):
        for symbol, df in data.items():
            if symbol not in union:
                continue
            b = bench[symbol]
            u = union[symbol]
            # One draw for everything except the random controls, whose score is the
            # mean over independent draws — see RANDOM_DRAWS.
            n_draws = RANDOM_DRAWS if label.startswith("RANDOM_") else 1
            positions = [build(label, df, b["close"], b["bpy"], symbol, d)
                         for d in range(n_draws)]
            positions = [p for p in positions if p is not None]
            if not positions:
                continue
            diag = {k: float(np.mean([_diagnostics(p, u, b["bpy"])[k]
                                      for p in positions]))
                    for k in ("long_frac", "exposure", "turnover_yr")}
            # The baseline is the thing being beaten; charging it would make it a
            # different strategy and would flatter every rule measured against it.
            fees = [FREE_FEE] if label == BASELINE else fee_grid
            for fee in fees:
                nets = [vector.net_returns(p, b["close"], fee, b["bpy"])
                        for p in positions]

                def mean_ir(mask, _nets=nets, _b=b):
                    """Mean IR across draws. All-NaN is a real outcome, not an error.

                    An always-long rule's IR against buy-and-hold is 0/0 on every draw,
                    and `np.nanmean` of an all-NaN list warns about an empty slice. The
                    answer is NaN either way; the guard just stops it being reported as
                    a numerical problem when it is a definitional one.
                    """
                    vals = [wfmod._ir(n, _b["net"], mask, _b["bpy"]) for n in _nets]
                    vals = [v for v in vals if np.isfinite(v)]
                    return float(np.mean(vals)) if vals else float("nan")
                union_rows.append({
                    "rule": label, "symbol": symbol, "scenario": fee["key"],
                    "ir_wf": mean_ir(u),
                    "ret_pct": float(np.mean([wfmod._total_return_pct(n, u)
                                              for n in nets])),
                    "bench_pct": wfmod._total_return_pct(b["net"], u),
                    "years_oos": float(u.sum() / b["bpy"]) if b["bpy"] > 0 else np.nan,
                    "n_folds": len(folds), "n_switches": 0, **diag,
                })
                for f, m in zip(folds, masks[symbol]):
                    if m is None:
                        continue
                    fold_rows.append((label, symbol, fee["key"], f.index,
                                      mean_ir(m[0]), mean_ir(m[1])))

    fold_table = pd.DataFrame(
        fold_rows, columns=["rule", "symbol", "scenario", "fold", "ir_is", "ir_oos"])
    fixed = pd.DataFrame(union_rows)
    if fold_table.empty:
        return {}, {"skipped": "no scoreable folds"}

    def position_fn(label, symbol, df):
        b = bench[symbol]
        return build(label, df, b["close"], b["bpy"], symbol)

    # Candidates exclude both controls: letting the selector pick buy-and-hold answers a
    # different question, and answers it trivially.
    candidates = set(labels)
    jobs = [("IS#1", candidates)]
    for name in CATALOG:
        family_cells = {c for c in labels if decode(c)[0] == name}
        # A single-cell strategy has nothing to re-optimise; emitting WFO[x] for it would
        # print a duplicate of x wearing a label that implies fitting happened.
        if len(family_cells) >= 2:
            jobs.append((f"WFO[{name}]", family_cells))

    stitched, diags = [], []
    for label, pool in jobs:
        picks = wfmod.pick_champions(fold_table, pool)
        if picks.empty:
            continue
        out = wfmod.stitch(data, asset_class, timeframe, folds, bench, picks, label,
                           position_fn=position_fn)
        if out.empty:
            continue
        stitched.append(out)
        diags.append(_stitched_diagnostics(data, folds, masks, picks, bench, label))

    wf = pd.concat([s for s in stitched if not s.empty], ignore_index=True)
    if diags:
        wf = wf.merge(pd.concat(diags, ignore_index=True),
                      on=["rule", "symbol", "scenario"], how="left")
    allrows = pd.concat([wf, fixed], ignore_index=True)
    ir_by_scen = allrows.groupby(["rule", "scenario"])["ir_wf"].mean().unstack()

    label_set = set(labels)
    out = []
    for (label, scen), grp in allrows.groupby(["rule", "scenario"]):
        mode = ("is1_selection" if label == "IS#1"
                else "wfo" if label.startswith("WFO[")
                else "control" if label in (BASELINE, *CONTROLS)
                else "published" if SEP not in label else "grid_cell")
        row = wfmod.leaderboard_row(grp, label, mode, asset_class, timeframe, scen,
                                    ir_by_scen.loc[label] if label in ir_by_scen.index
                                    else pd.Series(dtype=float))
        if mode == "wfo":
            base = label[4:-1]
        elif label in label_set:
            base = decode(label)[0]
        else:
            base = ""
        row["strategy"] = base
        row["family"] = CATALOG[base].family if base in CATALOG else mode
        row["source"] = CATALOG[base].source if base in CATALOG else ""
        row["anchor"] = bool(base in CATALOG and CATALOG[base].anchor)
        row.update(excess_cagr(grp))
        for col in ("long_frac", "exposure", "turnover_yr"):
            row[col] = float(grp[col].mean()) if col in grp else np.nan
        row["rankable"] = metrics.rankable(row, MIN_IR_COVERAGE)
        out.append(row)
    summary = pd.DataFrame(out).sort_values(["scenario", "ir_net"],
                                            ascending=[True, False])
    summary = add_exposure_adjusted(summary)

    headline = HEADLINE_SCENARIO[asset_class]
    years = float(fixed["years_oos"].median())
    n_published = len([c for c in labels if SEP not in c])
    meta = {
        "class": asset_class, "timeframe": timeframe, "n_assets": len(union),
        "n_strategies": n_published, "n_cells": len(labels),
        "n_skipped": len(skipped_for(asset_class)),
        "skipped": ";".join(skipped_for(asset_class)),
        "n_folds": len(folds), "years_oos": years,
        "ceiling_published": metrics.noise_ceiling(n_published, years),
        "ceiling_cells": metrics.noise_ceiling(len(labels), years),
        "se_ir": metrics.se_ir(years),
        "oos_start": str(folds[0].is_end.date()),
        "oos_end": str(folds[-1].oos_end.date()),
        "ranking_stability_spearman": wfmod.ranking_stability(
            fold_table.groupby(["rule", "scenario", "fold"])[["ir_is", "ir_oos"]]
            .mean().reset_index(), headline),
    }
    tables = {"summary": summary, "folds": fold_table, "per_asset": fixed,
              "schedule": wfmod.pick_champions(fold_table, candidates)}
    return tables, meta


def report(tables: dict, meta: dict) -> None:
    s = tables["summary"]
    scen = HEADLINE_SCENARIO[meta["class"]]
    h = s[(s.scenario == scen) & s.rankable]
    pub = h[h.wf_mode == "published"]
    wfo = h[h.wf_mode == "wfo"]
    is1 = h[h.wf_mode == "is1_selection"]

    print(f"\n=== {meta['class']}_{meta['timeframe']} ({meta['seconds']:.0f}s) ===")
    print(f"  {meta['n_strategies']} published strategies / {meta['n_cells']} grid cells"
          f" | {meta['n_folds']} folds | OOS {meta['oos_start']} -> {meta['oos_end']}"
          f" ({meta['years_oos']:.1f}y median)")
    if meta["n_skipped"]:
        print(f"  skipped on this class: {meta['skipped']}")
    print(f"  noise ceiling: IR {meta['ceiling_published']:+.3f} at "
          f"{meta['n_strategies']} published, {meta['ceiling_cells']:+.3f} at "
          f"{meta['n_cells']} cells  (SE {meta['se_ir']:.3f})")
    print(f"  ranking stability (consecutive-fold Spearman): "
          f"{meta['ranking_stability_spearman']:.3f}")

    if not is1.empty:
        r = is1.iloc[0]
        print(f"\n  IS#1 (the strategy you could actually have picked): "
              f"IR {r['ir_net']:+.3f}  breadth {r['ir_hit_rate']:.0%}  "
              f"t {r['t_stat']:+.2f}  {r['gates_passed']}/4 gates  "
              f"long {r['long_frac']:.0%}  {r['n_switches']:.0f} switches"
              f"  | vs random at that exposure {r['ir_vs_random']:+.3f}")

    print("\n  published parameters, best 8 by raw IR:")
    for r in pub.nlargest(8, "ir_net").itertuples():
        mark = " [anchor]" if r.anchor else ""
        verdict = "ABOVE" if r.ir_net > meta["ceiling_published"] else "below"
        print(f"    {r.rule:<18} IR {r.ir_net:+.3f}  breadth {r.ir_hit_rate:>4.0%}  "
              f"t {r.t_stat:+.2f}  head {r.headroom:>5.2f}  long {r.long_frac:>4.0%}  "
              f"{r.gates_passed}/4  {verdict} ceiling{mark}")

    # The ordering that means something. Raw IR ranks by time-in-market; this ranks by
    # what is left after the exposure-matched control curve is subtracted.
    print("\n  best 8 by IR vs an exposure-matched random control:")
    for r in pub.nlargest(8, "ir_vs_random").itertuples():
        mark = " [anchor]" if r.anchor else ""
        print(f"    {r.rule:<18} {r.ir_vs_random:+.3f}   "
              f"(IR {r.ir_net:+.3f} vs random {r.ir_random:+.3f} at long "
              f"{r.long_frac:.0%}){mark}")

    if not wfo.empty and not pub.empty:
        base_ir = pub.set_index("rule")["ir_net"]
        deltas = []
        for r in wfo.itertuples():
            b = base_ir.get(r.strategy, np.nan)
            if np.isfinite(b):
                deltas.append((r.strategy, r.ir_net, b, r.ir_net - b))
        if deltas:
            helped = sum(1 for _, _, _, d in deltas if d > 0)
            print(f"\n  walk-forward optimisation vs published parameters "
                  f"(helped {helped} of {len(deltas)}):")
            for name, w, b, d in sorted(deltas, key=lambda x: -x[3])[:8]:
                print(f"    WFO[{name}]{'':<{max(0, 12 - len(name))}} {w:+.3f}   "
                      f"published {b:+.3f}   delta {d:+.3f}")
            print(f"    mean delta {np.mean([d for *_, d in deltas]):+.3f}")

    # Not filtered to the headline scenario: BUYHOLD is never charged, so it exists only
    # under `gross` and would vanish from a headline-only view of its own leaderboard.
    ctrl = s[(s.wf_mode == "control") & (s.scenario.isin([scen, "gross"]))]
    if not ctrl.empty:
        print("\n  controls — what NO signal at this much market exposure scores:")
        for r in ctrl.sort_values("long_frac").itertuples():
            ir = "    nan" if not np.isfinite(r.ir_net) else f"{r.ir_net:+.3f}"
            print(f"    {r.rule:<12} @{r.scenario:<7} long {r.long_frac:>4.0%}  IR {ir}")
        if not pub.empty:
            beat = pub[pub["ir_vs_random"] > 0]
            print(f"    -> {len(beat)} of {len(pub)} published strategies beat a random "
                  f"control at their OWN exposure. The rest carry no information that "
                  f"time-in-market does not already explain.")

    # IR is arithmetic and blind to variance drag; compounding is not. On leveraged ETFs
    # the two disagree, so both are printed and neither is allowed to stand alone.
    print("\n  best 8 by compounded excess CAGR vs buy-and-hold "
          "(min across assets in brackets):")
    for r in pub.nlargest(8, "excess_cagr").itertuples():
        flag = "  <- all assets" if r.excess_cagr_min > 0 else ""
        print(f"    {r.rule:<18} {r.excess_cagr:>+7.1%}  [worst asset "
              f"{r.excess_cagr_min:>+7.1%}]  {r.excess_cagr_hit:>4.0%} of assets"
              f"{flag}")

    body = h[h.wf_mode != "control"]
    gate_counts = {g: int(body[f"gate_{g}"].sum())
                   for g in ("ir", "breadth", "headroom", "t")}
    print(f"\n  rows passing each gate: {gate_counts}")
    print(f"  cleared ALL FOUR: {int((body.gates_passed == 4).sum())} of {len(body)}")


def print_catalog() -> None:
    print(f"{len(CATALOG)} strategies, "
          f"{sum(len(s.grid) for s in CATALOG.values())} grid cells\n")
    for fam in ("trend", "reversion", "volatility", "calendar", "regime"):
        print(f"--- {fam} ---")
        for name, s in CATALOG.items():
            if s.family != fam:
                continue
            mark = " [anchor]" if s.anchor else ""
            only = f" [{'/'.join(s.classes)} only]" if s.classes else ""
            print(f"  {name}{mark}{only}\n      {s.rule}\n      {s.source}"
                  f"\n      {len(s.grid)} cells, published: {s.published}")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", choices=list(TIMEFRAMES),
                    default=list(DEFAULT_TIMEFRAMES))
    ap.add_argument("--list", action="store_true", help="print the catalog and exit")
    args = ap.parse_args()

    if args.list:
        print_catalog()
        return

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    metas = []
    for asset_class in args.classes:
        for timeframe in args.timeframes:
            t0 = time.time()
            tables, meta = run_pair(asset_class, timeframe)
            if not tables:
                print(f"{asset_class}/{timeframe}: "
                      f"{meta.get('skipped', 'no cached data')}, skipped")
                continue
            tag = f"{asset_class}_{timeframe}"
            for key, name in (("summary", "strat_summary"), ("folds", "strat_folds"),
                              ("per_asset", "strat_per_asset"),
                              ("schedule", "strat_schedule")):
                tables[key].to_csv(RESULTS_DIR / f"{name}_{tag}.csv", index=False)
            meta["seconds"] = time.time() - t0
            metas.append(meta)
            report(tables, meta)

    if metas:
        # Merged, not overwritten: this file indexes every sheet, and a `--tf 1d` run
        # must not erase the 4h rows it never touched.
        fresh = pd.DataFrame(metas)
        path = RESULTS_DIR / "strat_meta.csv"
        if path.exists():
            old = pd.read_csv(path)
            keys = set(zip(fresh["class"], fresh["timeframe"]))
            old = old[[k not in keys for k in zip(old["class"], old["timeframe"])]]
            fresh = pd.concat([old, fresh], ignore_index=True)
        fresh.sort_values(["class", "timeframe"]).to_csv(path, index=False)
        print(f"\nwrote {RESULTS_DIR}")


if __name__ == "__main__":
    main()
