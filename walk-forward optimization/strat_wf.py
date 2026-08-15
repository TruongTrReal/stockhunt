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

Speed, and why a new strategy is no longer a full re-run
--------------------------------------------------------
Pass A — every label scored on every asset — is an embarrassingly parallel loop over
independent labels, so it runs across cores through `stockhunt.parallel` and reads its
positions from the on-disk cache, exactly as `walkforward.py` has. The stitching phase
that follows it prefetches each job's picked positions rule-outer and hands the same
arrays to `stitch` and `_stitched_diagnostics`, which used to generate every one of them
twice.

`--rules` is the other half. Adding one strategy to `published/` used to mean rescoring
all 31, because there was no way to ask for less. There is now, and it stays honest
about what it costs: the numbers that are defined over the *whole* catalog — `IS#1`, the
noise ceilings, ranking stability — cannot be computed from a subset, so a `--rules` run
writes `*.partial.csv` and leaves the sheet of record alone. `--promote` is the explicit
opt-in, mirroring `riskmatch_wf.py`.

Run::

    python strat_wf.py                                    # all classes, 1d and 4h
    python strat_wf.py --class us_etfs --tf 1d
    python strat_wf.py --list                             # print the catalog and exit
    python strat_wf.py --tf 1d --rules ibs                # one strategy -> *.partial
    python strat_wf.py --tf 1d --rules ibs --promote      # ...as the sheet of record
"""

from __future__ import annotations

import argparse
import time

import numpy as np
import pandas as pd
from tqdm import tqdm

from wfo_paths import RESULTS_DIR, write_bulk   # noqa: F401  (wires sys.path first)
from config import (scenarios_for,  # noqa: F401
                    headline_key,  # noqa: F401
                    CLASSES, HEADLINE_SCENARIO, MIN_BARS, MIN_IR_COVERAGE,
                    TIMEFRAMES, WF_MIN_FOLDS, scenarios)
from engines import vector
import metrics
import signals
import td_loader
import walkforward as wfmod
from stockhunt import parallel

# The catalog itself is repo-level and engine-free; this module is the half that needs
# the fee model and the fold machinery. `config` is imported first on purpose -- it is
# what puts the repo root on sys.path so `strategies` resolves.
from strategies.catalog import (BASELINE, CATALOG, CONTROLS, RANDOM_DRAWS, SEP,
                                build, cells, decode, skipped_for)

DEFAULT_TIMEFRAMES = ("1d", "4h")

FREE_FEE = {"key": "gross", "commission_bps": 0.0, "half_spread_bps": 0.0,
            "sell_fee_bps": 0.0, "borrow_annual": 0.0}

# Position-cache keyspace. The cache is one npz per (class, timeframe, rule NAME), and
# the TA-Lib sweep is already writing into it — so a published strategy called `ibs`
# and a TA-Lib rule called `IBS` would be the same file on this box, because NTFS is
# case-insensitive. They are not the same series either: `signals.position_for` applies
# end-of-day flattening on the intraday sheets and `registry.build` does not. A prefix
# is one line and makes the collision impossible rather than merely unlikely.
CACHE_NS = "strat."


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
                          bench: dict, label: str, positions: dict) -> pd.DataFrame:
    """`long_frac` / exposure / turnover for a stitched path.

    `walkforward.stitch` returns scores, not positions, so the same fold-by-fold write
    is repeated here to measure what the stitched path actually *held*. Without this the
    diagnostic column is blank on exactly the rows that matter — IS#1 and the WFO paths
    — and `long_frac` is the column that tells an equity IR improvement apart from a
    rule that simply spent more time in the market.

    `positions` is `{(label, symbol): array}` for exactly the pairs this job picked,
    built once by `_picked_positions` and shared with the `stitch` call above. It used
    to rebuild them here, which meant every picked position on the sheet was generated
    twice — and with ~27 jobs per sheet that duplicate was larger than pass A.
    """
    rows = []
    for symbol, df in data.items():
        ms = masks[symbol]
        mine = picks[picks["symbol"] == symbol]
        if mine.empty:
            continue
        b = bench[symbol]
        cache = {r: positions.get((r, symbol)) for r in mine["rule"].unique()}
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


def select_cells(labels: list[str], rules: list[str] | None) -> list[str]:
    """The cells `--rules` asks for. A bare strategy name brings its whole grid.

    Naming `ibs` and getting only `ibs` would silently drop `WFO[ibs]`, which needs the
    grid it re-selects from — so a name expands to every cell of that strategy and an
    explicit cell label (`ibs@buy=0.1`) is taken literally.

    An unknown name is fatal; a name that is simply not runnable on THIS class is not.
    `monday_effect` is undefined on crypto, and a `--class crypto us_stocks` run naming
    it should score it where it exists rather than refuse to start.
    """
    if not rules:
        return labels
    want = set(rules)
    unknown = {r for r in want if r not in CATALOG and r not in set(labels)}
    if unknown:
        raise SystemExit(f"not in the catalog: {sorted(unknown)} — "
                         f"`python strat_wf.py --list` prints what is")
    return [c for c in labels if c in want or decode(c)[0] in want]


def _context(asset_class: str, timeframe: str) -> dict | None:
    """Everything a label needs to be scored on this sheet. Rebuilt inside each worker.

    Split out of `run_pair` so it can be a pool initializer — `parallel.map_rules` maps a
    module-level function over rules and that function reads its context from a module
    global, because a closure cannot be pickled. Windows spawns, so each worker pays this
    once and amortises it over its share of the labels.
    """
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        return None

    start = min(df.index[0] for df in data.values())
    end = max(df.index[-1] for df in data.values())
    folds = wfmod.generate_folds(start, end)
    if len(folds) < WF_MIN_FOLDS:
        return {"skipped": f"only {len(folds)} folds in {start.date()}..{end.date()}"}

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
        return {"skipped": "no scoreable folds"}

    return {
        "data": data, "bench": bench, "folds": folds, "masks": masks, "union": union,
        "asset_class": asset_class, "timeframe": timeframe,
        # One scenario at 1d/4h, per `SINGLE_SCENARIO_TIMEFRAMES`. This ran the full
        # four-scenario grid on sheets that only report `gross`, so three quarters of
        # the work was discarded — 617 rows where 155 were read.
        "fees": scenarios_for(asset_class, timeframe),
        "cache": signals.sheet_cache(asset_class, timeframe, data),
    }


def _cacheable(label: str) -> bool:
    """Controls are not cached: they are microseconds to build and `RANDOM_*` is twelve
    independent draws, which the cache keys on rule name alone cannot tell apart."""
    return label not in (BASELINE, *CONTROLS)


def _positions(label: str, ctx: dict) -> dict[str, np.ndarray | None]:
    """One label across every symbol, served from the position cache where possible.

    Rule-outer, symbol-inner — the cache is one file per rule, so this opens it once and
    reads every symbol out of it. The same shape as `signals.rule_positions`, which
    cannot be reused directly: it builds through `signals.position_for`, which knows the
    231 TA-Lib rules and nothing about `strategies/published/`.

    A `None` is not cached. It can mean a rule genuinely undefined on this asset or a
    transient failure, and persisting the second kind would make it permanent.
    """
    data, bench, cache = ctx["data"], ctx["bench"], ctx["cache"]

    def make(symbol, df):
        b = bench[symbol]
        return build(label, df, b["close"], b["bpy"], symbol)

    if cache is None or not cache.enabled or not _cacheable(label):
        return {s: make(s, df) for s, df in data.items()}

    out: dict[str, np.ndarray | None] = {}
    with cache.rule(CACHE_NS + label) as rc:
        for symbol, df in data.items():
            pos = rc.get(symbol)
            if pos is None:
                pos = make(symbol, df)
                rc.put(symbol, pos)
            out[symbol] = pos
    return out


def _picked_positions(picks: pd.DataFrame, ctx: dict) -> dict:
    """`{(label, symbol): position}` for exactly the pairs one stitching job picked.

    Rule-outer for the same reason as `_positions`: `stitch` and `_stitched_diagnostics`
    both loop symbol-outer, which against a one-file-per-rule cache would reopen and
    decompress each rule's file once per symbol. Held only for the job in flight — on
    us_stocks 1d that is ~9 distinct rules per symbol for IS#1, not the full
    label x symbol tensor the memory contract exists to avoid.
    """
    out = {}
    for label, grp in picks.groupby("rule", sort=False):
        want = set(grp["symbol"])
        for symbol, pos in _positions(label, ctx).items():
            if pos is not None and symbol in want:
                out[(label, symbol)] = pos
    return out


def _score_label(label: str, ctx: dict) -> list[dict]:
    """Pass A for ONE label: its per-fold and union rows across every asset.

    Split out of `run_pair` so it can run in a pool worker. Labels are independent of
    each other, which is what makes this the same embarrassingly parallel loop every
    other stage in the repo already runs across cores.
    """
    data, bench, union, masks = ctx["data"], ctx["bench"], ctx["union"], ctx["masks"]
    folds, fee_grid = ctx["folds"], ctx["fees"]

    # One draw for everything except the random controls, whose score is the mean over
    # independent draws — see RANDOM_DRAWS. The single-draw case is loaded rule-outer so
    # it comes off the cache; the twelve-draw case stays per symbol, where its peak
    # memory is one array rather than twelve times the sheet.
    n_draws = RANDOM_DRAWS if label.startswith("RANDOM_") else 1
    by_symbol = _positions(label, ctx) if n_draws == 1 else None

    fold_rows, union_rows = [], []
    for symbol, df in data.items():
        if symbol not in union:
            continue
        b = bench[symbol]
        u = union[symbol]
        if by_symbol is not None:
            positions = [by_symbol.get(symbol)]
        else:
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
    return [{"fold": fold_rows, "union": union_rows}]


_CTX: dict | None = None


def _init_worker(asset_class: str, timeframe: str) -> None:
    global _CTX
    _CTX = _context(asset_class, timeframe)


def _score_label_worker(label: str) -> list[dict]:
    return _score_label(label, _CTX)


def run_pair(asset_class: str, timeframe: str,
             rules: list[str] | None = None) -> tuple[dict, dict]:
    ctx = _context(asset_class, timeframe)
    if ctx is None:
        return {}, {}
    if "skipped" in ctx:
        return {}, ctx

    data, bench, folds = ctx["data"], ctx["bench"], ctx["folds"]
    masks, union = ctx["masks"], ctx["union"]

    labels = select_cells(cells(asset_class), rules)
    if not labels:
        return {}, {"skipped": f"none of {rules} is runnable on {asset_class}"}

    # Rules are independent, so pass A runs across cores. `parallel.map_rules` preserves
    # submission order, so the tables come out in exactly the row sequence the serial
    # loop produced — several result CSVs are written in loop order and a reshuffle would
    # read as a change in a diff without being one. Below `MIN_TASKS` it stays serial and
    # does not pay for spawning, which is what a `--rules ibs` run gets.
    chunks = parallel.map_rules(
        labels + [BASELINE, *CONTROLS], _score_label_worker, _init_worker,
        (asset_class, timeframe),
        desc=f"strat {asset_class}/{timeframe}",
        serial_fn=lambda lab: _score_label(lab, ctx))

    fold_rows = [r for c in chunks for r in c["fold"]]
    union_rows = [r for c in chunks for r in c["union"]]

    fold_table = pd.DataFrame(
        fold_rows, columns=["rule", "symbol", "scenario", "fold", "ir_is", "ir_oos"])
    fixed = pd.DataFrame(union_rows)
    if fold_table.empty:
        return {}, {"skipped": "no scoreable folds"}

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
    for label, pool in tqdm(jobs, desc=f"stitch {asset_class}/{timeframe}"):
        picks = wfmod.pick_champions(fold_table, pool)
        if picks.empty:
            continue
        job_pos = _picked_positions(picks, ctx)
        # `masks` is passed in rather than recomputed. `stitch` rebuilt the identical
        # fold calendar for every asset on every one of these ~27 jobs, which is the same
        # arithmetic the caller is already holding.
        out = wfmod.stitch(data, asset_class, timeframe, folds, bench, picks, label,
                           position_fn=lambda r, s, d: job_pos.get((r, s)),
                           masks=masks)
        if out.empty:
            continue
        stitched.append(out)
        diags.append(_stitched_diagnostics(data, folds, masks, picks, bench, label,
                                           job_pos))

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

    headline = headline_key(asset_class, timeframe)
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
    scen = headline_key(meta["class"], meta.get("timeframe"))
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
              f"t {r['t_stat']:+.2f}  {r['legacy_passed']}/4 legacy  "
              f"long {r['long_frac']:.0%}  {r['n_switches']:.0f} switches"
              f"  | vs random at that exposure {r['ir_vs_random']:+.3f}")

    print("\n  published parameters, best 8 by raw IR:")
    for r in pub.nlargest(8, "ir_net").itertuples():
        mark = " [anchor]" if r.anchor else ""
        verdict = "ABOVE" if r.ir_net > meta["ceiling_published"] else "below"
        print(f"    {r.rule:<18} IR {r.ir_net:+.3f}  breadth {r.ir_hit_rate:>4.0%}  "
              f"t {r.t_stat:+.2f}  head {r.headroom:>5.2f}  long {r.long_frac:>4.0%}  "
              f"{r.legacy_passed}/4  {verdict} ceiling{mark}")

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
    gate_counts = {g: int(body[f"legacy_gate_{g}"].sum())
                   for g in ("ir", "breadth", "headroom", "t")}
    print(f"\n  legacy per-gate diagnostic: {gate_counts}")
    print(f"  legacy 4-gate diagnostic: {int((body.legacy_passed == 4).sum())} of "
          f"{len(body)} — the verdict is riskmatch_wf.py / edge_standard.csv")


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
    ap.add_argument("--rules", nargs="+", default=None, metavar="NAME",
                    help="score only these strategies. A bare name brings its whole "
                         "grid, so WFO[name] still has something to re-select from. "
                         "Writes *.partial.csv unless --promote — see below.")
    ap.add_argument("--promote", action="store_true",
                    help="write the real strat_* sheets from a --rules run. Off by "
                         "default: IS#1 and the noise ceilings are computed over the "
                         "catalog in the run, so a narrowed one answers a different "
                         "question under the same filename.")
    args = ap.parse_args()

    if args.list:
        print_catalog()
        return

    # A `--rules` run is SCOPED and must not become the sheet of record.
    #
    # This is not the same caution as `--class`/`--tf`, which narrow *which sheets* get
    # written and leave each written sheet complete. `--rules` narrows the catalog
    # *within* a sheet, and three numbers on it are defined over that catalog: `IS#1` is
    # per fold the best cell in the whole of it, `ceiling_published`/`ceiling_cells` are
    # noise ceilings for a search of that size, and `ranking_stability_spearman` is
    # measured across it. Two rules picked because you already like them produce an IS#1
    # that had nothing to choose between and a ceiling low enough for anything to clear —
    # under the same filename the dashboard reads. So it lands as `.partial` unless the
    # scoped set IS the study, which is what `--promote` says.
    scoped = bool(args.rules) and not args.promote
    suffix = ".partial" if scoped else ""
    if args.rules:
        # Checked here, before the first sheet is loaded. `select_cells` catches it too,
        # but only after `_context` has read every bar of the first class — a typo should
        # cost a second, not a sheet load.
        typos = [r for r in args.rules if r not in CATALOG and SEP not in r]
        if typos:
            raise SystemExit(f"not in the catalog: {sorted(typos)} — "
                             f"`python strat_wf.py --list` prints what is")
        print(f"scoped to {len(args.rules)} strateg(ies): {' '.join(args.rules)}")
        print("  IS#1, the noise ceilings and ranking stability are computed over THIS "
              "set, not the catalog — they are not comparable to a full run's.")
    if args.promote and args.rules:
        print("--promote: writing the REAL strat_* sheets from a scoped run. This "
              "REPLACES the sheet of record for every class and timeframe in scope.")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    metas = []
    for asset_class in args.classes:
        for timeframe in args.timeframes:
            t0 = time.time()
            tables, meta = run_pair(asset_class, timeframe, args.rules)
            if not tables:
                print(f"{asset_class}/{timeframe}: "
                      f"{meta.get('skipped', 'no cached data')}, skipped")
                continue
            tag = f"{asset_class}_{timeframe}{suffix}"
            for key, name in (("summary", "strat_summary"),
                              ("schedule", "strat_schedule")):
                tables[key].to_csv(RESULTS_DIR / f"{name}_{tag}.csv", index=False)
            write_bulk(tables["folds"], RESULTS_DIR / f"strat_folds_{tag}.parquet")
            # 53 MB of CSV on us_stocks 1d, and the dashboard's fallback source for rules
            # the edge standard never scored — so Parquet, read via `artifacts.read_bulk`.
            write_bulk(tables["per_asset"], RESULTS_DIR / f"strat_per_asset_{tag}.parquet")
            meta["seconds"] = time.time() - t0
            metas.append(meta)
            report(tables, meta)

    if metas and not scoped:
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
    if metas:
        print(f"\nwrote {RESULTS_DIR}")
    if scoped:
        # `strat_meta.csv` indexes the sheets and carries the ceilings; a scoped run's
        # would be a ceiling for a search of two, filed as if it were the catalog's.
        print(f"  SCOPED RUN — wrote *{suffix}.csv only. The full sheets and "
              f"strat_meta.csv are untouched.")


if __name__ == "__main__":
    main()
