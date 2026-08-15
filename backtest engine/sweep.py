"""Stage 1: every TA-Lib rule, every asset, every timeframe, every cost level.

Writes `results/per_asset_<class>_<tf>.csv` and `results/summary_<class>_<tf>.csv`.

Two structural choices worth knowing before changing anything here:

**One rule on one asset at a time.** Positions are generated, scored and discarded
before the next rule starts, so peak memory is a single position series — 27 MB on a
3.35M-bar crypto 1-minute series, against ~6 GB for a materialised 231-rule tensor over
the same span. That is the only reason the finest timeframes are runnable at all.

**Gates are measured out of sample.** Each series is split chronologically at
`TRAIN_FRACTION`; every gate metric is computed on the test segment alone, and the
shortlist that feeds the combo stage never sees it. Sorting a results table by a test
column and reading the top rows is selection on test, and it manufactures winners.

Run::

    python sweep.py                          # everything
    python sweep.py --class crypto --tf 1d
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np
import pandas as pd

from config import (BASELINE_NAME, CAPITAL_PER_TICKER, CLASSES, HEADLINE_SCENARIO,
                    MIN_BARS, MIN_IR_COVERAGE, RESULTS_DIR, TIMEFRAMES,
                    TRAIN_FRACTION, headline_key, scenarios_for)
from engines import vector
import metrics
import signals
import td_loader

from stockhunt import parallel
from stockhunt.artifacts import write_bulk

from strategies.talib_signals import GENERIC_FALLBACK_FUNCTIONS, get_all_indicator_names


def split_index(n: int) -> int:
    """First index of the test segment."""
    return max(1, int(n * TRAIN_FRACTION))


CURVE_POINTS = 120


def _finish_curves(acc: dict) -> dict:
    """Running sums -> one downsampled equal-weight excess curve per (rule, scenario).

    Divided by a PER-BAR asset count, not by the final one: a 1970 bar is covered by a
    handful of names and a 2020 bar by all of them, so a scalar divisor would scale the
    early sample by an asset count that did not exist yet.

    Downsampled to `CURVE_POINTS` here rather than in the payload builder so the cost is
    paid once, in the pass that already holds the data.
    """
    out = {}
    for (rule, scenario), a in acc.items():
        if a["sum"] is None or a["count"] is None:
            continue
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(a["count"] > 0, a["sum"] / np.maximum(a["count"], 1.0), 0.0)
        equity = np.cumprod(1.0 + np.nan_to_num(mean, nan=0.0, posinf=0.0, neginf=0.0))
        if not np.isfinite(equity).all():
            continue
        n = len(equity)
        idx = (np.unique(np.linspace(0, n - 1, min(CURVE_POINTS, n)).astype(int))
               if n > CURVE_POINTS else np.arange(n))
        stamps = a["index"][-n:]
        out[f"{rule}|{scenario}"] = {
            "t": [str(stamps[i].date()) for i in idx],
            "v": [round(float(equity[i]), 5) for i in idx],
        }
    return out


def _context(asset_class: str, timeframe: str) -> dict | None:
    """Everything scoring one rule needs, built once per process.

    Split out of `run_pair` so a pool worker can rebuild it after `spawn` — on Windows a
    worker starts from a bare interpreter and inherits nothing. It is also the only
    honest way to keep the serial path and the parallel path identical: both call this,
    so neither can drift into scoring against a differently-built benchmark.
    """
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        return None

    names = list(get_all_indicator_names())
    if BASELINE_NAME not in names:
        names.append(BASELINE_NAME)
    runnable, skipped = signals.usable_rules(names, asset_class, timeframe)

    # Buy-and-hold on the same asset, never charged a cost and never flattened — it is
    # the thing every rule is measured against, and modifying it would make the
    # comparison meaningless.
    # The baseline is charged nothing and gets a single row, tagged "gross"; the report
    # joins it into every scenario panel.
    free = {"key": "gross", "commission_bps": 0.0, "half_spread_bps": 0.0,
            "sell_fee_bps": 0.0, "borrow_annual": 0.0}
    bench = {}
    for symbol, df in data.items():
        close = df["Close"].to_numpy(dtype="float64")
        ones = np.ones(len(df), dtype="float64")
        bpy = vector.bars_per_year(df.index)
        bench[symbol] = {
            # Buy-and-hold pays nothing: one entry, no exit, never short, so none of the
            # four fee components apply to it beyond an opening commission we also omit.
            "net": vector.net_returns(ones, close, free, bpy),
            "bpy": bpy, "close": close,
        }

    return {"data": data, "bench": bench, "free": free, "runnable": runnable,
            "skipped": skipped, "asset_class": asset_class, "timeframe": timeframe,
            "fees": scenarios_for(asset_class, timeframe),
            "cache": signals.sheet_cache(asset_class, timeframe, data)}


def _score_rule(rule: str, ctx: dict) -> list[dict]:
    """Per-asset rows and the finished equity curves for ONE rule.

    Returned as a single-element list because `parallel.map_rules` concatenates what each
    task returns; the caller flattens. Curves are downsampled here, inside the worker,
    so a 14,272-bar float64 accumulator never crosses a process boundary — only the
    120 points that survive it.
    """
    data, bench = ctx["data"], ctx["bench"]
    asset_class, timeframe, free = ctx["asset_class"], ctx["timeframe"], ctx["free"]

    rows: list[dict] = []
    # (rule, scenario) -> running equal-weight excess curve. Filled from the
    # `net` series the sweep already computes, so every rule gets a curve
    # instead of the top 20 per panel.
    curve_acc: dict = {}
    positions = signals.rule_positions(rule, data, asset_class, timeframe,
                                       ctx["cache"], baseline_name=BASELINE_NAME)
    for symbol, df in data.items():
        pos = positions.get(symbol)
        if pos is None:
            continue
        b = bench[symbol]
        cut = split_index(len(df))
        # The baseline is never charged; charging the benchmark would turn it into
        # a different strategy and flatter every rule measured against it.
        fees = [free] if rule == BASELINE_NAME else ctx["fees"]

        for fee in fees:
            # Computed once and handed to `stats`, which used to recompute it. The two
            # are not interchangeable afterwards — `stats` scores the finite subset while
            # this copy stays full length, because `net[cut:]` and `net - b["net"]` below
            # both need the original bar positions.
            net = vector.net_returns(pos, b["close"], fee, b["bpy"])
            stats = vector.stats(pos, b["close"], df.index, fee, CAPITAL_PER_TICKER,
                                 net=net, bpy=b["bpy"])
            if stats is None:
                continue
            ir_test = metrics.information_ratio(net[cut:], b["net"][cut:], b["bpy"])
            ir_train = metrics.information_ratio(net[:cut], b["net"][:cut], b["bpy"])
            rows.append({
                "class": asset_class, "timeframe": timeframe, "symbol": symbol,
                "rule": rule, "scenario": fee["key"],
                "ir_test": ir_test, "ir_train": ir_train,
                "years_test": stats["years"] * (1 - TRAIN_FRACTION),
                **{k: stats[k] for k in
                   ("total_return", "pnl_dollars", "cagr", "sharpe",
                    "max_drawdown", "exposure", "n_trades",
                    "turnover_per_year", "n_bars", "years", "bars_per_year")},
            })
            # Accumulate the equal-weight excess curve while `net` is in hand.
            #
            # This is the whole reason the report could only ever show a curve for
            # the top 20 rows of a panel: `build_payload._pooled_curve` REBUILT the
            # positions in a second pass, so a curve per rule meant re-running the
            # sweep. Here it is a running sum over a series that already exists —
            # the marginal cost is one add per (rule, asset, fee) — and it lets every
            # displayed row have a curve instead of 4.5% of them.
            acc = curve_acc.setdefault((rule, fee["key"]),
                                       {"sum": None, "count": None, "index": df.index})
            excess = net - b["net"]
            # Assets start on different dates, so align on the TAIL: every series
            # ends at the same bar, and the overlap is what a pooled curve can
            # honestly show. When a longer series arrives the buffer grows and the
            # existing sum moves to the tail with it — putting it at the head instead
            # would silently shift one asset's history against the others.
            if acc["sum"] is None or len(acc["sum"]) < len(excess):
                prev, prev_n = acc["sum"], acc["count"]
                acc["sum"] = np.zeros(len(excess))
                acc["count"] = np.zeros(len(excess))
                acc["index"] = df.index
                if prev is not None:
                    acc["sum"][-len(prev):] += prev
                    acc["count"][-len(prev_n):] += prev_n
            acc["sum"][-len(excess):] += excess
            # Per-bar count, not a scalar: early bars are covered by fewer assets, and
            # dividing the whole curve by the final asset count would understate the
            # start of the sample rather than average what was actually there.
            acc["count"][-len(excess):] += 1.0

    return [{"rows": rows, "curves": _finish_curves(curve_acc)}]


_CTX: dict | None = None


def _init_worker(asset_class: str, timeframe: str) -> None:
    global _CTX
    _CTX = _context(asset_class, timeframe)


def _score_rule_worker(rule: str) -> list[dict]:
    return _score_rule(rule, _CTX)


def run_pair(asset_class: str, timeframe: str) -> tuple[pd.DataFrame, dict, dict]:
    ctx = _context(asset_class, timeframe)
    if ctx is None:
        return pd.DataFrame(), {}, {}
    data, runnable, skipped = ctx["data"], ctx["runnable"], ctx["skipped"]

    # Rules are independent of one another, so this is the whole loop parallelised. Order
    # is preserved by `map_rules`, so `per_asset.csv` comes out in exactly the sequence
    # the serial version wrote it.
    chunks = parallel.map_rules(runnable, _score_rule_worker, _init_worker,
                                (asset_class, timeframe),
                                desc=f"{asset_class}/{timeframe}",
                                serial_fn=lambda r: _score_rule(r, ctx))
    rows = [r for c in chunks for r in c["rows"]]
    curves: dict = {}
    for c in chunks:
        curves.update(c["curves"])

    per_asset = pd.DataFrame(rows)
    meta = {
        "class": asset_class, "timeframe": timeframe,
        "n_assets": len(data),
        "n_rules_run": len(runnable), "n_rules_skipped": len(skipped),
        "skipped_rules": sorted(skipped),
        "n_bars_min": int(min(len(d) for d in data.values())),
        "n_bars_max": int(max(len(d) for d in data.values())),
        "start": str(min(d.index[0] for d in data.values())),
        "end": str(max(d.index[-1] for d in data.values())),
        "source": td_loader.describe_source(),
    }
    return per_asset, meta, curves


def _headline_for(asset_class: str, timeframe: str | None,
                  per_asset: pd.DataFrame) -> str:
    """The scenario to report on: the one that was run, not the one assumed.

    Same fix as `config.headline_key` — asking for a timeframe is not the same as that
    timeframe having collapsed to a single scenario, and `scenarios_for(...)[0]` is
    `gross` on every full-grid sheet. Prefer the class headline and fall back only to what
    the frame actually contains, which is what the no-timeframe branch already did right.
    """
    present = set(per_asset["scenario"].unique())
    want = headline_key(asset_class, timeframe)
    return want if want in present else (sorted(present)[0] if present else want)


def summarise(per_asset: pd.DataFrame, asset_class: str,
              timeframe: str | None = None) -> pd.DataFrame:
    """Per-rule leaderboard with the four gates, one row per (rule, fee scenario)."""
    if per_asset.empty:
        return pd.DataFrame()
    # The headline is whichever scenario this sheet ACTUALLY ran. When the grid
    # collapses to `gross`, looking up `HEADLINE_SCENARIO` (retail) matches no rows at
    # all — which printed "0 rankable, 0 cleared all four gates" and read exactly like a
    # null result rather than a lookup miss. Named `head_key`, not `headline_key`, so it
    # cannot shadow the imported `config.headline_key` that `_headline_for` calls.
    head_key = _headline_for(asset_class, timeframe, per_asset)

    # Mean OOS IR with no fees and with the real schedule, per rule. Cost headroom is a
    # property of the rule, not of the scenario being displayed, so it is computed once
    # from those two points and repeated on every scenario row.
    ir_by_scen = (per_asset.groupby(["rule", "scenario"])["ir_test"]
                  .mean().unstack())

    out = []
    for (rule, scen), grp in per_asset.groupby(["rule", "scenario"]):
        per_sym = {r.symbol: {"ir": r.ir_test} for r in grp.itertuples()}
        years_test = float(grp["years_test"].median())
        gross = float(ir_by_scen.loc[rule].get("gross", np.nan)) \
            if rule in ir_by_scen.index else np.nan
        head = float(ir_by_scen.loc[rule].get(head_key, np.nan)) \
            if rule in ir_by_scen.index else np.nan

        row = metrics.aggregate(per_sym, years_test, gross, head)
        row.update({
            "class": asset_class,
            "timeframe": grp["timeframe"].iloc[0],
            "rule": rule, "scenario": scen,
            "ir_train": float(grp["ir_train"].mean()),
            "total_pnl_dollars": float(grp["pnl_dollars"].sum()),
            "avg_cagr": float(grp["cagr"].mean()),
            "avg_sharpe": float(grp["sharpe"].mean()),
            "avg_max_drawdown": float(grp["max_drawdown"].mean()),
            "avg_exposure": float(grp["exposure"].mean()),
            "turnover_per_year": float(grp["turnover_per_year"].mean()),
            "n_trades": int(grp["n_trades"].sum()),
            "is_baseline": rule == BASELINE_NAME,
            "generic_fallback": rule in GENERIC_FALLBACK_FUNCTIONS,
        })
        row["rankable"] = metrics.rankable(row, MIN_IR_COVERAGE)
        out.append(row)

    df = pd.DataFrame(out)
    return df.sort_values(["scenario", "ir_net"], ascending=[True, False])


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
            per_asset, meta, curves = run_pair(asset_class, timeframe)
            if per_asset.empty:
                print(f"{asset_class}/{timeframe}: no cached data, skipped")
                continue
            summary = summarise(per_asset, asset_class, timeframe)

            tag = f"{asset_class}_{timeframe}"
            # Parquet: this is the longest table the engine produces — 166 MB of CSV on
            # us_stocks 4h — and **nothing in the repo reads it**. It is a per-cell
            # diagnostic dump, kept for inspection, so it pays the Parquet price rather
            # than the CSV one. `write_bulk` removes the superseded CSV twin.
            write_bulk(per_asset, RESULTS_DIR / f"per_asset_{tag}.parquet")
            # Every rule's curve, so the report is not limited to a top slice.
            with open(RESULTS_DIR / f"curves_{tag}.json", "w",
                      encoding="utf-8") as fh:
                json.dump(curves, fh, separators=(",", ":"))
            summary.to_csv(RESULTS_DIR / f"summary_{tag}.csv", index=False)
            meta["seconds"] = time.time() - t0
            metas.append(meta)

            head_key = _headline_for(asset_class, timeframe, per_asset)
            head = summary[(summary["scenario"] == head_key)
                           & summary["rankable"] & ~summary["is_baseline"]]
            n_clear = int((head["legacy_passed"] == 4).sum())
            best = head.nlargest(3, "ir_net")
            skipped = meta["skipped_rules"]
            skip_note = f" ({len(skipped)} skipped, no volume: {skipped})" if skipped else ""
            print(f"\n=== {tag} ({meta['seconds']:.0f}s) ===")
            print(f"  {meta['n_assets']} assets x {meta['n_rules_run']} rules{skip_note}"
                  f" | {meta['n_bars_min']:,}-{meta['n_bars_max']:,} bars")
            print(f"  [{head_key}]: {len(head)} rankable, "
                  f"{n_clear} cleared all four gates")
            if len(best):
                print(best[["rule", "ir_net", "ir_hit_rate", "headroom",
                            "t_stat", "legacy_passed"]]
                      .to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    if metas:
        pd.DataFrame(metas).to_csv(RESULTS_DIR / "sweep_meta.csv", index=False)
        print(f"\nwrote {RESULTS_DIR}")


if __name__ == "__main__":
    main()
