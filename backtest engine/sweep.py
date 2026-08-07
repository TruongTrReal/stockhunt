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
import time

import numpy as np
import pandas as pd
from tqdm import tqdm

from config import (BASELINE_NAME, CAPITAL_PER_TICKER, CLASSES, HEADLINE_SCENARIO,
                    MIN_BARS, MIN_IR_COVERAGE, RESULTS_DIR, TIMEFRAMES,
                    TRAIN_FRACTION, scenarios)
from engines import vector
import metrics
import signals
import td_loader

from strategies.talib_signals import GENERIC_FALLBACK_FUNCTIONS, get_all_indicator_names


def split_index(n: int) -> int:
    """First index of the test segment."""
    return max(1, int(n * TRAIN_FRACTION))


def run_pair(asset_class: str, timeframe: str) -> tuple[pd.DataFrame, dict]:
    spec = CLASSES[asset_class]
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        return pd.DataFrame(), {}

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

    rows = []
    for rule in tqdm(runnable, desc=f"{asset_class}/{timeframe}"):
        for symbol, df in data.items():
            pos = signals.position_for(rule, df, asset_class, timeframe,
                                       baseline_name=BASELINE_NAME)
            if pos is None:
                continue
            b = bench[symbol]
            cut = split_index(len(df))
            # The baseline is never charged; charging the benchmark would turn it into
            # a different strategy and flatter every rule measured against it.
            fees = ([free] if rule == BASELINE_NAME
                    else scenarios(asset_class))

            for fee in fees:
                net = vector.net_returns(pos, b["close"], fee, b["bpy"])
                stats = vector.stats(pos, b["close"], df.index, fee,
                                     CAPITAL_PER_TICKER)
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
    return per_asset, meta


def summarise(per_asset: pd.DataFrame, asset_class: str) -> pd.DataFrame:
    """Per-rule leaderboard with the four gates, one row per (rule, fee scenario)."""
    if per_asset.empty:
        return pd.DataFrame()
    headline_key = HEADLINE_SCENARIO[asset_class]

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
        head = float(ir_by_scen.loc[rule].get(headline_key, np.nan)) \
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
            per_asset, meta = run_pair(asset_class, timeframe)
            if per_asset.empty:
                print(f"{asset_class}/{timeframe}: no cached data, skipped")
                continue
            summary = summarise(per_asset, asset_class)

            tag = f"{asset_class}_{timeframe}"
            per_asset.to_csv(RESULTS_DIR / f"per_asset_{tag}.csv", index=False)
            summary.to_csv(RESULTS_DIR / f"summary_{tag}.csv", index=False)
            meta["seconds"] = time.time() - t0
            metas.append(meta)

            headline_key = HEADLINE_SCENARIO[asset_class]
            head = summary[(summary["scenario"] == headline_key)
                           & summary["rankable"] & ~summary["is_baseline"]]
            n_clear = int((head["gates_passed"] == 4).sum())
            best = head.nlargest(3, "ir_net")
            skipped = meta["skipped_rules"]
            skip_note = f" ({len(skipped)} skipped, no volume: {skipped})" if skipped else ""
            print(f"\n=== {tag} ({meta['seconds']:.0f}s) ===")
            print(f"  {meta['n_assets']} assets x {meta['n_rules_run']} rules{skip_note}"
                  f" | {meta['n_bars_min']:,}-{meta['n_bars_max']:,} bars")
            print(f"  [{headline_key}]: {len(head)} rankable, "
                  f"{n_clear} cleared all four gates")
            if len(best):
                print(best[["rule", "ir_net", "ir_hit_rate", "headroom",
                            "t_stat", "gates_passed"]]
                      .to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    if metas:
        pd.DataFrame(metas).to_csv(RESULTS_DIR / "sweep_meta.csv", index=False)
        print(f"\nwrote {RESULTS_DIR}")


if __name__ == "__main__":
    main()
