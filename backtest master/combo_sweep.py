"""Stage 2/3: combinations of the shortlisted single rules.

Exhaustive 231-choose-2 is 26,565 pairs before any operator, times 30 assets times 7
timeframes. That is not merely expensive — it is self-defeating: more candidates raise
the significance bar rather than lowering it. Bonferroni at 26k candidates needs t > 4.4
for p<0.05, and `t = IR x sqrt(years)` on a 7-year intraday sample would demand an IR
above 1.7. Nothing in this repo has ever come close.

So the search is staged:

1. rank the single rules by **train-period IR only** and keep the top N;
2. pair them under several combination operators;
3. score every candidate out of sample against the four gates.

The shortlist never sees the test segment. Sorting by a test column and reading the top
rows is selection on test, and it manufactures winners — this project has done it before
and had to retract the result.

The shortlist shrinks on finer timeframes, where a candidate is 40x more bars and the
prior from both earlier studies is that there is nothing there to find.

Run::

    python combo_sweep.py --class us_stocks --tf 1d
"""

from __future__ import annotations

import argparse
import itertools
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

FREE = {"key": "gross", "commission_bps": 0.0, "half_spread_bps": 0.0,
        "sell_fee_bps": 0.0, "borrow_annual": 0.0}

# Fewer candidates where each one costs the most and the prior is weakest.
SHORTLIST = {"1d": 24, "4h": 24, "2h": 20, "1h": 20, "15m": 16, "5m": 12, "1m": 10}


# Defined in signals.py so the search and the report cannot disagree about what an
# operator means:
#   vote  sum then sign - agreement doubles, disagreement cancels to flat
#   and   trade only where both agree on direction; the strictest, and the one that
#         most reduces turnover, which is what the cost gate actually rewards
#   or    take either signal, preferring `a` when both are active and disagree
#   gate  `a` supplies the direction, `b` only permits or blocks it
from signals import OPERATORS, combine  # noqa: E402


def run_pair(asset_class: str, timeframe: str, shortlist_n: int | None = None
             ) -> tuple[pd.DataFrame, dict]:
    spec = CLASSES[asset_class]
    tag = f"{asset_class}_{timeframe}"
    summary_path = RESULTS_DIR / f"summary_{tag}.csv"
    if not summary_path.exists():
        return pd.DataFrame(), {}

    summary = pd.read_csv(summary_path)
    headline = HEADLINE_SCENARIO[asset_class]
    pool = summary[(summary["scenario"] == headline) & summary["rankable"]
                   & ~summary["is_baseline"]]
    if pool.empty:
        return pd.DataFrame(), {}

    n = shortlist_n or SHORTLIST.get(timeframe, 16)
    # TRAIN IR only. This is the line that keeps the combo stage honest.
    short = pool.nlargest(n, "ir_train")["rule"].tolist()

    data = td_loader.load(asset_class, timeframe)
    data = {s: d for s, d in data.items() if len(d) >= MIN_BARS}
    if not data:
        return pd.DataFrame(), {}

    # Generate each shortlisted rule's position once per asset and keep it. int8 keeps
    # a 3.35M-bar crypto minute series at 3.3 MB, so 10 rules x 10 assets is ~330 MB
    # at the worst timeframe rather than the ~6 GB a full 231-rule tensor would need.
    positions: dict[str, dict[str, np.ndarray]] = {}
    bench: dict[str, dict] = {}
    for symbol, df in data.items():
        close = df["Close"].to_numpy(dtype="float64")
        bench[symbol] = {
            "net": vector.net_returns(np.ones(len(df)), close, FREE,
                                      vector.bars_per_year(df.index)),
            "bpy": vector.bars_per_year(df.index),
            "close": close, "index": df.index, "cut": max(1, int(len(df) * TRAIN_FRACTION)),
        }
        per_rule = {}
        for rule in short:
            pos = signals.position_for(rule, df, asset_class, timeframe,
                                       baseline_name=BASELINE_NAME)
            if pos is not None:
                per_rule[rule] = pos.astype("int8")
        positions[symbol] = per_rule

    candidates = [(a, b, op) for a, b in itertools.combinations(short, 2)
                  for op in OPERATORS]

    rows = []
    for a, b, op in tqdm(candidates, desc=f"combo {tag} ({len(short)} shortlisted)"):
        name = f"{a} {op} {b}"
        for symbol, df in data.items():
            pa = positions[symbol].get(a)
            pb = positions[symbol].get(b)
            if pa is None or pb is None:
                continue
            pos = combine(pa.astype("float64"), pb.astype("float64"), op)
            bs = bench[symbol]
            for fee in scenarios(asset_class):
                net = vector.net_returns(pos, bs["close"], fee, bs["bpy"])
                stats = vector.stats(pos, bs["close"], bs["index"], fee,
                                     CAPITAL_PER_TICKER)
                if stats is None:
                    continue
                cut = bs["cut"]
                rows.append({
                    "class": asset_class, "timeframe": timeframe, "symbol": symbol,
                    "rule": name, "op": op, "leg_a": a, "leg_b": b, "scenario": fee["key"],
                    "ir_test": metrics.information_ratio(net[cut:], bs["net"][cut:], bs["bpy"]),
                    "ir_train": metrics.information_ratio(net[:cut], bs["net"][:cut], bs["bpy"]),
                    "years_test": stats["years"] * (1 - TRAIN_FRACTION),
                    **{k: stats[k] for k in
                       ("total_return", "pnl_dollars", "cagr", "sharpe", "max_drawdown",
                        "exposure", "n_trades", "turnover_per_year", "n_bars", "years",
                        "bars_per_year")},
                })

    meta = {"class": asset_class, "timeframe": timeframe,
            "shortlist_n": len(short), "shortlist": short,
            "n_candidates": len(candidates), "operators": list(OPERATORS),
            "selection": "train-period IR only"}
    return pd.DataFrame(rows), meta


def summarise(per_asset: pd.DataFrame, asset_class: str) -> pd.DataFrame:
    if per_asset.empty:
        return pd.DataFrame()
    headline_key = HEADLINE_SCENARIO[asset_class]
    ir_by_scen = (per_asset.groupby(["rule", "scenario"])["ir_test"]
                  .mean().unstack())

    out = []
    for (rule, scen), grp in per_asset.groupby(["rule", "scenario"]):
        per_sym = {r.symbol: {"ir": r.ir_test} for r in grp.itertuples()}
        gross = float(ir_by_scen.loc[rule].get("gross", np.nan)) if rule in ir_by_scen.index else np.nan
        head = float(ir_by_scen.loc[rule].get(headline_key, np.nan)) if rule in ir_by_scen.index else np.nan
        row = metrics.aggregate(per_sym, float(grp["years_test"].median()),
                                gross, head)
        row.update({
            "class": asset_class, "timeframe": grp["timeframe"].iloc[0],
            "rule": rule, "op": grp["op"].iloc[0],
            "leg_a": grp["leg_a"].iloc[0], "leg_b": grp["leg_b"].iloc[0],
            "scenario": scen,
            "ir_train": float(grp["ir_train"].mean()),
            "total_pnl_dollars": float(grp["pnl_dollars"].sum()),
            "avg_cagr": float(grp["cagr"].mean()),
            "avg_sharpe": float(grp["sharpe"].mean()),
            "avg_max_drawdown": float(grp["max_drawdown"].mean()),
            "avg_exposure": float(grp["exposure"].mean()),
            "turnover_per_year": float(grp["turnover_per_year"].mean()),
            "n_trades": int(grp["n_trades"].sum()),
            "is_baseline": False, "generic_fallback": False,
        })
        row["rankable"] = metrics.rankable(row, MIN_IR_COVERAGE)
        out.append(row)
    return pd.DataFrame(out).sort_values(["scenario", "ir_net"], ascending=[True, False])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", choices=list(TIMEFRAMES),
                    default=list(TIMEFRAMES))
    ap.add_argument("--shortlist", type=int, default=None)
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    metas = []
    for asset_class in args.classes:
        for timeframe in args.timeframes:
            t0 = time.time()
            per_asset, meta = run_pair(asset_class, timeframe, args.shortlist)
            if per_asset.empty:
                print(f"{asset_class}/{timeframe}: no stage-1 summary, skipped")
                continue
            summary = summarise(per_asset, asset_class)
            tag = f"{asset_class}_{timeframe}"
            per_asset.to_csv(RESULTS_DIR / f"combo_per_asset_{tag}.csv", index=False)
            summary.to_csv(RESULTS_DIR / f"combo_summary_{tag}.csv", index=False)
            meta["seconds"] = time.time() - t0
            metas.append(meta)

            headline_key = HEADLINE_SCENARIO[asset_class]
            head = summary[(summary["scenario"] == headline_key) & summary["rankable"]]
            print(f"\n=== combo {tag} ({meta['seconds']:.0f}s) ===")
            print(f"  {meta['shortlist_n']} shortlisted -> {meta['n_candidates']} "
                  f"candidates | {int((head['gates_passed'] == 4).sum())} cleared all four")
            if len(head):
                print(head.nlargest(3, "ir_net")[
                    ["rule", "ir_net", "ir_hit_rate", "headroom", "t_stat", "gates_passed"]]
                    .to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    if metas:
        pd.DataFrame(metas).to_csv(RESULTS_DIR / "combo_meta.csv", index=False)


if __name__ == "__main__":
    main()
