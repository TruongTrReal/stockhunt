"""Stage 1b: rolling walk-forward re-optimisation, and the rule you could have picked.

`sweep.py` splits each series once and reports every rule's out-of-sample IR. That is
honest about *a rule*, and silent about *choosing* one. The choice in that design is made
exactly once — by us, reading a finished leaderboard on which the test column is already
visible. Nobody trading in 2015 had that leaderboard.

This module answers the question the single split cannot:

**Parameter re-optimisation.** 84 of the 231 rules are period variants of 14 families
(`SMA_9 … SMA_1000`). Each fold picks that family's best period on the in-sample window
and trades it through the next out-of-sample window. The stitched path is the family's
real score; the leaderboard's `SMA_1000` row is the score of a period we only know was
best because we saw the whole history.

**Rule selection (`IS#1`).** Each fold trades whichever of the 231 rules ranked first on
that fold's in-sample window. This is the strategy a disciplined researcher following
this repo's own method would actually have run, and it is the number that matters. Both
external submissions this was checked against report ~0.2-0.4 Sharpe *below* their best
fixed rule once selection is priced in; there is no reason to expect better here.

Three design points, each a deliberate departure from the reference implementations:

**Selection is on IR against buy-and-hold, not raw Sharpe.** Both submissions rank folds
on Sharpe, which in a rising market mostly measures how long-biased a rule is — the exact
trap `../test research/` fell into and this repo's ranking convention exists to avoid.
Selecting on Sharpe would reintroduce it inside every fold.

**Signals are stitched, then backtested once — positions are not.** A fold boundary that
switches from `RSI` to `DEMA_50` is a real trade in a real account. Concatenating the two
rules' *return* streams silently makes that switch free. Writing the selected rule's
positions into one array and running `vector.net_returns` over the whole thing charges it
like any other, because the position series genuinely jumps.

**Walk-forward efficiency is reported as a difference, not a ratio.** The usual
`mean_oos / mean_is` assumes a positive denominator. Our IRs are mostly negative, where a
ratio inverts sign and reads backwards — two negatives divide to something flattering.
`wf_drop = mean_oos_ir - mean_is_ir` says the same thing and stays interpretable.

Causality: positions are generated once on the full series and then *masked* per fold.
Every rule here is causal — its value at bar *t* uses only data at or before *t* — so a
bar's position does not depend on which window asked for it, and slicing is identical to
recomputing per fold while leaving the 1000-bar warmups intact.

Run::

    python walkforward.py                          # everything
    python walkforward.py --class us_stocks --tf 1d
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from wfo_paths import RESULTS_DIR, write_bulk   # noqa: F401  (wires sys.path first)
from config import (headline_key,  # noqa: F401
                    BASELINE_NAME, CAPITAL_PER_TICKER, CLASSES, HEADLINE_SCENARIO,
                    MIN_BARS, MIN_IR_COVERAGE, TIMEFRAMES,
                    WF_IS_YEARS, WF_MIN_FOLDS, WF_MIN_IS_BARS, WF_MIN_OOS_BARS,
                    WF_OOS_YEARS, WF_STEP_YEARS, scenarios_for)
from engines import vector
import metrics
import signals
import td_loader

from stockhunt import parallel
from strategies.talib_signals import GENERIC_FALLBACK_FUNCTIONS, get_all_indicator_names

# `LINEARREG_INTERCEPT_50` must resolve to LINEARREG_INTERCEPT, not LINEARREG — so the
# family is everything before the *final* underscore-integer, and only when that leaves a
# name that is itself not a bare period.
_VARIANT = re.compile(r"^(?P<family>.+)_(?P<period>\d+)$")


def family_of(rule: str) -> str | None:
    """`SMA_1000` -> `SMA`; `RSI` -> None (nothing to re-optimise)."""
    m = _VARIANT.match(rule)
    return m.group("family") if m else None


@dataclass(frozen=True)
class Fold:
    """Half-open windows: in-sample is [is_start, is_end), out-of-sample [is_end, oos_end)."""
    index: int
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    oos_end: pd.Timestamp


def generate_folds(start: pd.Timestamp, end: pd.Timestamp) -> list[Fold]:
    """Rolling folds over a common calendar.

    Built from the union span of the universe rather than per asset, so "fold 7" names
    the same dates for every symbol and a champion schedule is coherent across them. An
    asset that listed late simply fails the bar minimums on the early folds.
    """
    folds: list[Fold] = []
    is_start = start
    while True:
        is_end = is_start + pd.DateOffset(years=WF_IS_YEARS)
        oos_end = is_end + pd.DateOffset(years=WF_OOS_YEARS)
        if is_end >= end:
            break
        folds.append(Fold(len(folds), is_start, is_end, min(oos_end, end)))
        if oos_end >= end:
            break
        is_start = is_start + pd.DateOffset(years=WF_STEP_YEARS)
    return folds


def fold_masks(index: pd.DatetimeIndex, folds: list[Fold]) -> list[tuple | None]:
    """Per-fold (is_mask, oos_mask) for one asset, or None where it has too few bars."""
    idx = index.to_numpy()
    out: list[tuple | None] = []
    for f in folds:
        ism = (idx >= np.datetime64(f.is_start)) & (idx < np.datetime64(f.is_end))
        oosm = (idx >= np.datetime64(f.is_end)) & (idx < np.datetime64(f.oos_end))
        ok = ism.sum() >= WF_MIN_IS_BARS and oosm.sum() >= WF_MIN_OOS_BARS
        out.append((ism, oosm) if ok else None)
    return out


def _ir(net: np.ndarray, bench: np.ndarray, mask: np.ndarray, bpy: float) -> float:
    """IR of a rule against buy-and-hold over one window.

    NaN when the difference series has no variance — which happens for real: an
    always-long rule under the `gross` scenario *is* buy-and-hold, so the difference is
    identically zero and its IR is 0/0, not 0. Leaving it NaN keeps such a rule out of
    the champion selection, which is the right outcome: a rule indistinguishable from the
    benchmark is not a strategy.
    """
    return metrics.information_ratio(net[mask], bench[mask], bpy)


def _total_return_pct(net: np.ndarray, mask: np.ndarray) -> float:
    """Compounded return over the evaluated window, in percent.

    Reported alongside IR because a ratio alone does not tell a reader what the rule
    actually earned, and the comparison that matters — against buy-and-hold on the same
    asset over the same bars — is only legible in return terms.
    """
    r = net[mask]
    r = r[np.isfinite(r)]
    return float((np.prod(1.0 + r) - 1.0) * 100.0) if r.size else float("nan")


def _context(asset_class: str, timeframe: str) -> dict | None:
    """Everything Pass A needs, built once per process.

    A pool worker on Windows starts from a bare interpreter and inherits nothing, so it
    rebuilds this. Keeping one builder for both paths is also what stops the serial and
    parallel runs from drifting onto differently-constructed benchmarks or fold masks —
    the kind of difference that would show up as a changed number with no changed code.

    Returns `None` when the sheet has no cached bars, or a dict carrying `skipped` when
    it has too few folds to walk forward over.
    """
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        return None

    start = min(df.index[0] for df in data.values())
    end = max(df.index[-1] for df in data.values())
    folds = generate_folds(start, end)
    if len(folds) < WF_MIN_FOLDS:
        return {"skipped": f"only {len(folds)} folds in {start.date()}..{end.date()}"}

    names = list(get_all_indicator_names())
    if BASELINE_NAME not in names:
        names.append(BASELINE_NAME)
    runnable, skipped = signals.usable_rules(names, asset_class, timeframe)

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

    # Computed once here rather than inside `score_folds` and again inside every `stitch`
    # call. `run_pair` makes 15 of those (IS#1 plus 14 families), so this was 16 identical
    # passes over the fold calendar for every asset on the sheet.
    masks = {s: fold_masks(df.index, folds) for s, df in data.items()}
    union = {s: np.logical_or.reduce([m[1] for m in ms if m is not None])
             for s, ms in masks.items() if any(m is not None for m in ms)}

    return {"data": data, "bench": bench, "free": free, "folds": folds,
            "masks": masks, "union": union, "runnable": runnable, "skipped": skipped,
            "asset_class": asset_class, "timeframe": timeframe,
            "fees": scenarios_for(asset_class, timeframe),
            "cache": signals.sheet_cache(asset_class, timeframe, data)}


def _score_rule(rule: str, ctx: dict) -> list[dict]:
    """Pass A for ONE rule: its per-fold and union rows across every asset.

    Split out of `score_folds` so it can run in a pool worker. The memory contract from
    `sweep.py` still holds and is what the layout protects: positions for one rule across
    all assets are built, scored and dropped before the next rule starts, so a 231-rule
    tensor over a 3.35M-bar minute series (~6 GB) is never materialised. That is the only
    reason the fine timeframes are runnable at all.
    """
    data, bench, union, masks = ctx["data"], ctx["bench"], ctx["union"], ctx["masks"]
    folds, free = ctx["folds"], ctx["free"]

    fold_rows: list[tuple] = []
    union_rows: list[tuple] = []
    positions = signals.rule_positions(rule, data, ctx["asset_class"], ctx["timeframe"],
                                       ctx["cache"], baseline_name=BASELINE_NAME)
    for symbol, df in data.items():
        if symbol not in union:
            continue
        pos = positions.get(symbol)
        if pos is None:
            continue
        b = bench[symbol]
        u = union[symbol]
        # Exposure over the scored window, on the same definitions `combo_wf.py` uses,
        # because the dashboard now ranks singles and pairs in one list and a rule that
        # is long ~100% of the time IS buy-and-hold — its IR converges on 0 from below
        # for that reason alone, which on a leaderboard looks exactly like skill. The
        # position does not depend on the fee schedule, so this is computed once and
        # repeated across the scenario rows.
        expo = (float(np.mean(pos[u] != 0)), float(np.mean(pos[u] > 0)),
                float(np.mean(pos[u] < 0)))
        fees = [free] if rule == BASELINE_NAME else ctx["fees"]
        for fee in fees:
            net = vector.net_returns(pos, b["close"], fee, b["bpy"])
            union_rows.append((
                rule, symbol, fee["key"],
                _ir(net, b["net"], union[symbol], b["bpy"]),
                _total_return_pct(net, union[symbol]),
                _total_return_pct(b["net"], union[symbol]),
                # Per asset, not per sheet. The sheet median is 41 years on 1d
                # equities, but META listed in 2012 and TSLA in 2010 — annualising
                # their returns over 41 years understates them by a factor of three.
                float(union[symbol].sum() / b["bpy"]) if b["bpy"] > 0 else np.nan,
                *expo,
            ))
            for f, m in zip(folds, masks[symbol]):
                if m is None:
                    continue
                fold_rows.append((
                    rule, symbol, fee["key"], f.index,
                    _ir(net, b["net"], m[0], b["bpy"]),
                    _ir(net, b["net"], m[1], b["bpy"]),
                ))
    return [{"fold": fold_rows, "union": union_rows}]


_CTX: dict | None = None


def _init_worker(asset_class: str, timeframe: str) -> None:
    global _CTX
    _CTX = _context(asset_class, timeframe)


def _score_rule_worker(rule: str) -> list[dict]:
    return _score_rule(rule, _CTX)


def score_folds(ctx: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pass A — every rule's per-fold in-sample and out-of-sample IR.

    Returns ``(fold_table, union_table)``. Rules are independent, so the loop runs across
    cores; `parallel.map_rules` preserves submission order, so both tables come out in
    exactly the row sequence the serial version produced.
    """
    chunks = parallel.map_rules(
        ctx["runnable"], _score_rule_worker, _init_worker,
        (ctx["asset_class"], ctx["timeframe"]),
        desc=f"WF {ctx['asset_class']}/{ctx['timeframe']}",
        serial_fn=lambda r: _score_rule(r, ctx))

    fold_rows = [r for c in chunks for r in c["fold"]]
    union_rows = [r for c in chunks for r in c["union"]]
    fold_table = pd.DataFrame(
        fold_rows, columns=["rule", "symbol", "scenario", "fold", "ir_is", "ir_oos"])
    union_table = pd.DataFrame(
        union_rows, columns=["rule", "symbol", "scenario", "ir_union",
                             "ret_pct", "bench_pct", "years_oos",
                             "exposure", "long_frac", "short_frac"])
    return fold_table, union_table


def pick_champions(fold_table: pd.DataFrame, candidates: set[str]) -> pd.DataFrame:
    """Per (symbol, scenario, fold), the candidate with the highest in-sample IR.

    Ties and all-NaN folds resolve to no pick, which drops the fold from that asset's
    stitched path rather than inventing one.
    """
    sub = fold_table[fold_table["rule"].isin(candidates)]
    sub = sub[np.isfinite(sub["ir_is"])]
    if sub.empty:
        return pd.DataFrame(columns=["symbol", "scenario", "fold", "rule",
                                     "ir_is", "ir_oos"])
    best = sub.loc[sub.groupby(["symbol", "scenario", "fold"])["ir_is"].idxmax()]
    return best[["symbol", "scenario", "fold", "rule", "ir_is", "ir_oos"]]


def stitch(data: dict, asset_class: str, timeframe: str, folds: list[Fold],
           bench: dict, picks: pd.DataFrame, label: str,
           position_fn=None, masks: dict | None = None) -> pd.DataFrame:
    """Pass B — write each fold's chosen rule into one position array and backtest once.

    The switch at a fold boundary is a real trade and is charged here, because the
    stitched *position* series jumps. Stitching returns instead would make every
    re-selection free, which over 22 annual folds is not a rounding error.

    `position_fn(rule, symbol, df) -> array | None` overrides how a name becomes a
    position, so `variants.py` can stitch transformed rules (`MAXINDEX|long_only`) that
    `signals.position_for` would not recognise. Default is the plain rule lookup.

    `masks` is an optional pre-computed `{symbol: fold_masks(...)}`. `run_pair` calls this
    fifteen times per sheet and every call rebuilt the identical calendar; passing it in
    is the same arithmetic done once. Omitted, it is computed here as before, which keeps
    the three external callers (`variants`, `prereg`, `strat_wf`) working unchanged.
    """
    if masks is None:
        masks = {s: fold_masks(df.index, folds) for s, df in data.items()}
    rows = []
    for symbol, df in data.items():
        ms = masks[symbol]
        if not any(m is not None for m in ms):
            continue
        mine = picks[picks["symbol"] == symbol]
        if mine.empty:
            continue
        b = bench[symbol]
        pos_cache: dict[str, np.ndarray | None] = {}
        for rule in mine["rule"].unique():
            pos_cache[rule] = (
                position_fn(rule, symbol, df) if position_fn is not None
                else signals.position_for(rule, df, asset_class, timeframe,
                                          baseline_name=BASELINE_NAME))
        for scen, grp in mine.groupby("scenario"):
            stitched = np.zeros(len(df), dtype="float64")
            used = np.zeros(len(df), dtype=bool)
            for r in grp.itertuples():
                m = ms[r.fold]
                p = pos_cache.get(r.rule)
                if m is None or p is None:
                    continue
                stitched[m[1]] = p[m[1]]
                used |= m[1]
            if not used.any():
                continue
            # `next()` with no default raises StopIteration deep inside a generator
            # and kills the run with no usable message — which is exactly what happened
            # when a caller still built picks on the four-scenario grid while this sheet
            # had collapsed to `gross`. Fail with the mismatch spelled out instead.
            avail = scenarios_for(asset_class, timeframe)
            fee = next((f for f in avail if f["key"] == scen), None)
            if fee is None:
                raise ValueError(
                    f"{asset_class}/{timeframe}: picks reference scenario {scen!r} but "
                    f"this sheet runs {[f['key'] for f in avail]}. The caller and "
                    f"`config.scenarios_for` disagree about the fee grid.")
            net = vector.net_returns(stitched, b["close"], fee, b["bpy"])
            years = float(used.sum() / b["bpy"]) if b["bpy"] > 0 else np.nan
            chosen = grp.sort_values("fold")["rule"]
            rows.append({
                "rule": label, "symbol": symbol, "scenario": scen,
                "ir_wf": _ir(net, b["net"], used, b["bpy"]),
                "ret_pct": _total_return_pct(net, used),
                "bench_pct": _total_return_pct(b["net"], used),
                "years_oos": years,
                # Of the stitched path, not of any one fold's rule: what a person following
                # this selection procedure was actually holding. Same definitions as the
                # fixed rows, so the two are comparable in one column.
                "exposure": float(np.mean(stitched[used] != 0)),
                "long_frac": float(np.mean(stitched[used] > 0)),
                "short_frac": float(np.mean(stitched[used] < 0)),
                # Changes *between* folds. The opening selection is an entry, not a
                # switch, and counting it would overstate churn by one on every asset.
                "n_switches": int((chosen != chosen.shift()).sum() - 1),
                "n_folds": int(len(grp)),
            })
    return pd.DataFrame(rows)


def leaderboard_row(per_symbol: pd.DataFrame, label: str, mode: str,
                    asset_class: str, timeframe: str, scen: str,
                    ir_by_scen: pd.Series) -> dict:
    """One gated leaderboard row, using the same aggregation as `sweep.summarise`."""
    per_asset = {r.symbol: {"ir": r.ir_wf} for r in per_symbol.itertuples()}
    years = float(per_symbol["years_oos"].median())
    row = metrics.aggregate(per_asset, years,
                            float(ir_by_scen.get("gross", np.nan)),
                            float(ir_by_scen.get(headline_key(asset_class, timeframe), np.nan)))
    ret = float(per_symbol["ret_pct"].mean()) if "ret_pct" in per_symbol else float("nan")
    bench = float(per_symbol["bench_pct"].mean()) if "bench_pct" in per_symbol else float("nan")
    row.update({
        "net_return_pct": ret,
        "bench_return_pct": bench,
        # The number a non-quant reads first: how many points of return the rule gained or
        # gave up against simply holding the same assets over the same bars.
        "excess_return_pct": ret - bench,
        "class": asset_class, "timeframe": timeframe, "rule": label,
        "scenario": scen, "wf_mode": mode,
        "n_folds": float(per_symbol["n_folds"].median()),
        "n_switches": float(per_symbol["n_switches"].median()),
        "is_baseline": label == BASELINE_NAME,
        "generic_fallback": label in GENERIC_FALLBACK_FUNCTIONS,
    })
    row["rankable"] = metrics.rankable(row, MIN_IR_COVERAGE)
    return row


def ranking_stability(fold_table: pd.DataFrame, scen: str) -> float:
    """Mean Spearman correlation of the rule ranking between consecutive folds.

    1.0 would mean the leaderboard never moves; 0 means each fold reshuffles it, and a
    rule that led one year tells you nothing about the next. Both submissions measured
    0.37-0.62 on a 500-name universe, which is the honest context for any claim that some
    indicator is "the best".

    Spearman is computed as Pearson on ranks rather than through `scipy.stats`, so this
    module stays importable in the shared venv — `metrics.py` avoids scipy for the same
    reason and uses `statistics.NormalDist` for its normal quantiles.
    """
    sub = fold_table[fold_table["scenario"] == scen]
    if sub.empty:
        return float("nan")
    mat = sub.pivot_table(index="fold", columns="rule", values="ir_oos", aggfunc="mean")
    ranks = mat.rank(axis=1)
    corrs = [ranks.iloc[i].corr(ranks.iloc[i + 1]) for i in range(len(ranks) - 1)]
    corrs = [c for c in corrs if np.isfinite(c)]
    return float(np.mean(corrs)) if corrs else float("nan")


def run_pair(asset_class: str, timeframe: str) -> tuple[dict, dict]:
    ctx = _context(asset_class, timeframe)
    if ctx is None:
        return {}, {}
    if "runnable" not in ctx:
        return {}, ctx                       # carries the `skipped` reason

    data, bench, folds = ctx["data"], ctx["bench"], ctx["folds"]
    masks, runnable, skipped = ctx["masks"], ctx["runnable"], ctx["skipped"]

    fold_table, union_table = score_folds(ctx)
    if fold_table.empty:
        return {}, {"skipped": "no scoreable folds"}

    # The baseline is the thing being beaten, so it is never a candidate: letting the
    # selector choose buy-and-hold would answer a different question, and answer it
    # trivially.
    candidates = {r for r in runnable if r != BASELINE_NAME}
    fams: dict[str, set[str]] = {}
    for r in candidates:
        f = family_of(r)
        if f:
            fams.setdefault(f, set()).add(r)
    fams = {f: v for f, v in fams.items() if len(v) > 1}

    stitched = [stitch(data, asset_class, timeframe, folds, bench,
                       pick_champions(fold_table, candidates), "IS#1", masks=masks)]
    for fam, variants in fams.items():
        stitched.append(stitch(data, asset_class, timeframe, folds, bench,
                               pick_champions(fold_table, variants), f"{fam}[WF]",
                               masks=masks))
    wf = pd.concat([s for s in stitched if not s.empty], ignore_index=True)

    # Fixed rules get the same stitched-OOS calendar with no re-selection, so the three
    # modes are read off one column rather than compared across two different spans.
    fixed = union_table.rename(columns={"ir_union": "ir_wf"}).copy()
    yrs = wf.groupby("symbol")["years_oos"].median()
    fixed["years_oos"] = fixed["symbol"].map(yrs)
    fixed["n_folds"] = len(folds)
    fixed["n_switches"] = 0
    fixed = fixed.dropna(subset=["years_oos"])

    allrows = pd.concat([wf, fixed], ignore_index=True)
    ir_by_scen = allrows.groupby(["rule", "scenario"])["ir_wf"].mean().unstack()

    out = []
    for (label, scen), grp in allrows.groupby(["rule", "scenario"]):
        mode = ("is1_selection" if label == "IS#1"
                else "family_reopt" if label.endswith("[WF]") else "fixed")
        row = leaderboard_row(grp, label, mode, asset_class, timeframe, scen,
                              ir_by_scen.loc[label] if label in ir_by_scen.index
                              else pd.Series(dtype=float))
        # Mean across assets, matching how every other column on this row aggregates.
        for col in ("exposure", "long_frac", "short_frac"):
            row[col] = float(grp[col].mean()) if col in grp else float("nan")
        out.append(row)
    summary = pd.DataFrame(out).sort_values(["scenario", "ir_net"],
                                            ascending=[True, False])

    # The scenario this sheet ACTUALLY ran. `HEADLINE_SCENARIO` is `retail`, but at
    # 1d and 4h the grid collapses to `gross`, so filtering on it selects zero rows
    # and every aggregate becomes the mean of an empty set — NaN, which reads as
    # 'not computed' rather than 'asked the wrong question'.
    headline = scenarios_for(asset_class, timeframe)[0]["key"]
    per_rule_folds = (fold_table.groupby(["rule", "scenario", "fold"])
                      [["ir_is", "ir_oos"]].mean().reset_index())
    schedule = pick_champions(fold_table, candidates)

    # Per (rule, symbol) rows are what the breadth gate is actually made of, and the only
    # place the "which assets did it work on" question can be answered. `per_rule_folds`
    # averages the symbol away, so keep the union table too.
    tables = {"summary": summary, "folds": per_rule_folds, "schedule": schedule,
              "per_asset": union_table}
    meta = {
        "class": asset_class, "timeframe": timeframe,
        "n_assets": len(data), "n_rules_run": len(runnable),
        "n_rules_skipped": len(skipped), "n_folds": len(folds),
        "n_families_reoptimised": len(fams),
        "is_years": WF_IS_YEARS, "oos_years": WF_OOS_YEARS, "step_years": WF_STEP_YEARS,
        "oos_start": str(folds[0].is_end.date()), "oos_end": str(folds[-1].oos_end.date()),
        "ranking_stability_spearman": ranking_stability(per_rule_folds, headline),
        "mean_is_ir": float(per_rule_folds[per_rule_folds.scenario == headline]["ir_is"].mean()),
        "mean_oos_ir": float(per_rule_folds[per_rule_folds.scenario == headline]["ir_oos"].mean()),
    }
    meta["wf_drop"] = meta["mean_oos_ir"] - meta["mean_is_ir"]
    return tables, meta


def report(tables: dict, meta: dict) -> None:
    summary = tables["summary"]
    headline = scenarios_for(meta["class"], meta["timeframe"])[0]["key"]
    head = summary[(summary["scenario"] == headline) & summary["rankable"]]
    is1 = head[head["wf_mode"] == "is1_selection"]
    fixed = head[(head["wf_mode"] == "fixed") & ~head["is_baseline"]]
    fam = head[head["wf_mode"] == "family_reopt"]

    print(f"\n=== {meta['class']}_{meta['timeframe']} ({meta['seconds']:.0f}s) ===")
    print(f"  {meta['n_folds']} folds "
          f"({meta['is_years']}y IS / {meta['oos_years']}y OOS / {meta['step_years']}y step)"
          f" | stitched OOS {meta['oos_start']} -> {meta['oos_end']}")
    print(f"  {meta['n_assets']} assets x {meta['n_rules_run']} rules, "
          f"{meta['n_families_reoptimised']} families re-optimised per fold")
    print(f"  ranking stability (consecutive-fold Spearman): "
          f"{meta['ranking_stability_spearman']:.3f}")
    print(f"  mean IS IR {meta['mean_is_ir']:+.3f} -> mean OOS IR "
          f"{meta['mean_oos_ir']:+.3f}  (wf_drop {meta['wf_drop']:+.3f})")

    if not is1.empty:
        r = is1.iloc[0]
        print(f"\n  IS#1 (the rule you could actually have picked): "
              f"IR {r['ir_net']:+.3f}, breadth {r['ir_hit_rate']:.0%}, "
              f"t {r['t_stat']:+.2f}, {r['legacy_passed']}/4 legacy, "
              f"{r['n_switches']:.0f} switches over {r['n_folds']:.0f} folds")
    if not fixed.empty:
        b = fixed.nlargest(1, "ir_net").iloc[0]
        print(f"  best FIXED rule (chosen with hindsight):        "
              f"IR {b['ir_net']:+.3f}  [{b['rule']}]")
        if not is1.empty:
            print(f"  cost of having to choose in real time:          "
                  f"{is1.iloc[0]['ir_net'] - b['ir_net']:+.3f} IR")
    if not fam.empty:
        print("\n  per-fold parameter re-optimisation vs the best fixed period:")
        # Match on the parsed family, not a name prefix: `LINEARREG_` also prefixes
        # `LINEARREG_INTERCEPT_50`, which would compare a family against a sibling's
        # periods and flatter or damn it at random.
        fixed_family = fixed["rule"].map(family_of)
        helped = 0
        for r in fam.sort_values("ir_net", ascending=False).itertuples():
            base = r.rule[:-4]
            variants = fixed[fixed_family == base]
            best = variants["ir_net"].max() if not variants.empty else np.nan
            helped += int(np.isfinite(best) and r.ir_net > best)
            print(f"    {r.rule:<24} {r.ir_net:+.3f}   best fixed {base}_* "
                  f"{best:+.3f}   delta {r.ir_net - best:+.3f}")
        print(f"    -> re-optimising helped {helped} of {len(fam)} families")
    n_clear = int((head[~head["is_baseline"]]["legacy_passed"] == 4).sum())
    print(f"\n  {n_clear} of {len(head[~head['is_baseline']])} rankable rows "
          f"cleared all four gates")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", choices=list(TIMEFRAMES),
                    default=list(TIMEFRAMES))
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    metas = []
    for asset_class in args.classes:
        for timeframe in args.timeframes:
            t0 = time.time()
            tables, meta = run_pair(asset_class, timeframe)
            if not tables:
                why = meta.get("skipped", "no cached data")
                print(f"{asset_class}/{timeframe}: {why}, skipped")
                continue
            tag = f"{asset_class}_{timeframe}"
            tables["summary"].to_csv(RESULTS_DIR / f"wf_summary_{tag}.csv", index=False)
            tables["folds"].to_csv(RESULTS_DIR / f"wf_folds_{tag}.csv", index=False)
            tables["schedule"].to_csv(RESULTS_DIR / f"wf_schedule_{tag}.csv", index=False)
            # Parquet: 77 MB of CSV on us_stocks 1d. The dashboard reads this one, so it
            # goes through `artifacts.read_bulk`, which accepts either spelling.
            write_bulk(tables["per_asset"], RESULTS_DIR / f"wf_per_asset_{tag}.parquet")
            meta["seconds"] = time.time() - t0
            metas.append(meta)
            report(tables, meta)

    if metas:
        # Merge rather than overwrite: this file is the cross-sheet index, and running
        # `--tf 1d` after a full sweep must not erase the ten sheets it did not touch.
        fresh = pd.DataFrame(metas)
        path = RESULTS_DIR / "wf_meta.csv"
        if path.exists():
            old = pd.read_csv(path)
            keys = set(zip(fresh["class"], fresh["timeframe"]))
            old = old[[k not in keys for k in zip(old["class"], old["timeframe"])]]
            fresh = pd.concat([old, fresh], ignore_index=True)
        fresh.sort_values(["class", "timeframe"]).to_csv(path, index=False)
        print(f"\nwrote {RESULTS_DIR}")


if __name__ == "__main__":
    main()
