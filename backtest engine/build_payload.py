"""results/*.csv -> data/report_payload.json, the contract `report.js` reads.

Two passes, the same split `test research/build_report_data.py` uses and for the same
reason: pass 1 (in `sweep.py`) computes scalars for every rule on every asset and keeps
no per-bar detail; pass 2 (here) recomputes curves and trade records only for the top
slice of each panel. With 2 classes x 7 timeframes x 4 cost levels there are 56 panels,
so retaining a curve per rule per panel would run to hundreds of MB in a file that has
to load as a single page.

Every gate decision is made here in Python and shipped as a boolean. `report.js` never
re-derives one, so the page and the CSVs cannot disagree about whether a rule passed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from config import (BASELINE_NAME, CAPITAL_PER_TICKER, CLASSES, GATES,
                    HEADLINE_SCENARIO, MIN_BARS, REPORT_DIR, RESULTS_DIR, TIMEFRAMES,
                    scenarios)
from engines import vector
import metrics
import report_schema
import rule_logic
import signals
import td_loader

from strategies.talib_signals import (GENERIC_FALLBACK_FUNCTIONS,
                                      describe_signal)

PAYLOAD_PATH = REPORT_DIR / "report_payload.json"

# Buy-and-hold pays nothing anywhere in this project.
FREE = {"key": "gross", "commission_bps": 0.0, "half_spread_bps": 0.0,
        "sell_fee_bps": 0.0, "borrow_annual": 0.0}

# Budget: the page must load as one file, and the artifact ceiling is 16 MB. With 52
# panels the leaderboard rows alone are ~12 MB even columnar, so the per-candidate detail
# is what gets trimmed. Trade records are the cheapest thing to lose — they are a
# drill-down for the handful of leaders, not evidence anything is ranked on.
CURVE_POINTS = 80
# Was 20, which meant only 1,176 of 26,304 candidates (4.5%) had a curve and clicking any
# of the other 25,128 showed an empty drill-down. Curves are pooled across assets, so a
# curve costs the same whether the panel holds 20 names or 722 — raising this is ~12 MB
# of payload and nothing else. 100 covers the rows anyone actually opens; it does NOT
# cover all of them, and rows below the cut still have no curve by design.
CURVES_KEPT = 50

# The page must load as one file under a 16 MB ceiling, and with 56 panels every choice
# here is a trade against that. Priority order, set deliberately:
#
#   1. Every single rule stays on the leaderboard. The 231 singles are the study, and a
#      row is ~170 bytes, so this is cheap and non-negotiable.
#   2. Combos are capped to the top COMBOS_KEPT by IR. All ~4,800 remain in
#      results/combo_summary_*.csv; only the *page* is truncated, and it says so.
#   3. Curves next, because they do not scale with universe size.
#   4. Per-asset trade detail last, because it is the only block that does.
#
# `ASSETS_PER_RULE = None` (all assets) was correct at 20 stocks and is not survivable at
# 722.
#
# Per-asset trade detail is the one block that scales with universe size, and it was
# already 3.31 MB — 28% of an 11.9 MB payload — for a *single* rule per panel across 20
# names. At 722 names the same setting is ~120 MB before any other change, which is not a
# tuning problem, it is a different artifact. So the drill-down is now capped per rule.
#
# The old comment called all-assets "not negotiable" because breadth is a gate. That
# reasoning does not transfer: breadth is computed in `results/*.csv` from every asset and
# is unaffected by what the page renders. This cap truncates a *display* table that nobody
# can read 722 rows of anyway, and `n_assets` on the row still reports the true count.
ASSETS_PER_RULE = 10
TOP_N_TRADES = 2                # was 1 — 0.21% of candidates had any trade detail
COMBOS_KEPT = 250
MAX_TRADES_SHOWN = 12
TICKER_CURVE_POINTS = 60

TIMEFRAME_TLDR = {
    "1d": "Daily bars, the deepest history available - US equities back to 2000, crypto "
          "to 2017. That depth is the single biggest lever on significance, since "
          "t = IR x sqrt(years).",
    "4h": "Four-hour bars. Session-aligned for equities, so a US trading day is one 4h "
          "bar plus a 2.5h stub; annualisation is measured from the index rather than "
          "assumed. Crypto 4h is a uniform 24/7 grid.",
    "2h": "Two-hour bars, same session-alignment caveat as 4h.",
    "1h": "Hourly bars. The only timeframe where the earlier top-20 study found anything "
          "above parity - two rules at zero cost, both gone by 1bp.",
    "30m": "Thirty-minute bars. A vendor-native interval (added 2026-08-22), with the "
           "same late-2019/2020 history floor as the other intraday grids.",
    "15m": "Fifteen-minute bars.",
    "3m": "Three-minute bars, resampled from the cached 1m - the vendor sells no such "
          "interval. End-of-day flattened for equities, like 1m and 5m.",
    "2m": "Two-minute bars, resampled from the cached 1m, same conventions as 3m.",
    "5m": "Five-minute bars, end-of-day flattened for equities so the result is genuine "
          "day-trading and does not quietly collect the overnight drift buy-and-hold "
          "already earns. The benchmark is never flattened. Crypto trades 24/7 and is "
          "not flattened at all.",
    "1m": "One-minute bars - up to 3.3M per crypto pair. Turnover here runs to thousands "
          "of round trips a year, so the rule that scores best after costs is usually "
          "the one that most nearly does nothing.",
}

CLASS_TLDR = {
    "us_stocks": "20 US mega-caps, the same universe as the top-20 study so results stay "
                 "comparable. Fees are the real schedule for a zero-commission US broker: "
                 "half the NBBO spread per side, SEC Section 31 and FINRA TAF on sells "
                 "only, and 0.30%/yr stock borrow while short. SPY is cached as the "
                 "benchmark input for BETA and CORREL but is not in the universe.",
    "crypto": "10 USD pairs, priced against the actual taker fees of three venues - "
              "Binance 0.10%, Kraken 0.26%, Coinbase Advanced 0.60% - plus the quoted "
              "spread. Spot shorting is not available retail; via perpetuals funding has "
              "historically flowed from longs to shorts, so charging zero borrow is the "
              "conservative choice. Twelve Data serves no volume for crypto, so AD, "
              "ADOSC, MFI and OBV cannot be evaluated on this class at all. Survivorship "
              "is severe (dead coins are absent) but largely cancels in the IR, since "
              "every asset is measured against buy-and-hold on itself.",
}



def _rule_logic_block() -> dict:
    """Long-form explanation per TA-Lib rule: mechanics, family, exposure, failure mode."""
    from strategies.talib_signals import get_all_indicator_names
    out = {}
    for name in get_all_indicator_names():
        text = rule_logic.explain(name, describe_signal, GENERIC_FALLBACK_FUNCTIONS)
        if text:
            out[name] = text
    return out


def _strategy_logic_block() -> dict:
    """The published catalog's own LOGIC blocks, with provenance."""
    try:
        from strategies.registry import CATALOG
    except Exception:
        return {}
    return {name: {"rule": s.rule, "source": s.source, "family": s.family,
                   "logic": s.logic, "note": s.note}
            for name, s in CATALOG.items() if s.logic or s.note}


def _sanitize(obj):
    """Replace non-finite floats with None, recursively.

    Required because `json.dump(..., allow_nan=False)` refuses NaN/Infinity — they are
    not valid JSON and would break `JSON.parse` in the browser. They arise here for a
    real reason, not a bug: see `_finite_pnl`.
    """
    if isinstance(obj, float):
        return obj if np.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _finite_pnl(net: np.ndarray) -> np.ndarray | None:
    """Cumulative dollar PnL, or None if per-bar compounding overflows float64.

    Constant-fraction compounding over a 3.3M-bar crypto minute series is not safe in
    double precision: an average net return of only +0.1% per bar gives e^3300, which is
    `inf`. That is a limitation of the *model* (it assumes you can re-size to a fixed
    fraction of equity every minute for six years, with unlimited liquidity), not an
    arithmetic slip — so the curve is dropped rather than clipped to a fake finite value.

    Note this touches display quantities only. The IR the gates are decided on is the
    mean/std of a per-bar difference series and never compounds, which is one more
    reason it is what this study ranks on.
    """
    with np.errstate(over="ignore", invalid="ignore"):
        pnl = CAPITAL_PER_TICKER * (np.cumprod(1.0 + net) - 1.0)
    return pnl if np.isfinite(pnl).all() else None


def _resample(values: np.ndarray, index, n: int) -> list:
    """Downsample a curve to at most `n` points, keeping first and last."""
    if len(values) <= n:
        picks = np.arange(len(values))
    else:
        picks = np.unique(np.linspace(0, len(values) - 1, n).astype(int))
    return [[str(index[i])[:19], round(float(values[i]), 2)] for i in picks]


def _excess_curve(row, data: dict, asset_class: str, timeframe: str,
                  cost: dict, n_points: int):
    """Cumulative excess return vs buy-and-hold, equal-weighted across assets, in %.

    This is the curve the ranking is actually computed on. IR is
    `mean(r - r_bh) / std(r - r_bh) * sqrt(ppy)` per asset, averaged across assets — so
    the honest picture of it is the running sum of that same difference series, weighted
    equally. Buy-and-hold is the zero line by construction: a rule that beats it slopes
    up, one that trails slopes down, and the slope-to-wobble ratio *is* the IR.

    It replaces a pooled dollar-PnL curve, which could disagree with the ranking in a way
    that looked like a contradiction: pooled dollars are dominated by whichever asset the
    rule happened to catch (one NVDA-sized winner carries the total), while IR gives every
    asset equal say. On daily equities some rules showed higher pooled PnL than
    buy-and-hold while still having a negative IR.

    Two further benefits: it cannot overflow (a sum of arithmetic differences, not a
    product — the 3.3M-bar crypto minute curves are representable again), and assets with
    different histories are handled by weighting only the ones trading at each point.
    """
    ref_start = min(d.index[0] for d in data.values())
    ref_end = max(d.index[-1] for d in data.values())
    grid = pd.date_range(ref_start, ref_end, periods=n_points)

    total = np.zeros(n_points, dtype="float64")
    count = np.zeros(n_points, dtype="float64")

    for symbol, df in data.items():
        pos = signals.position_for_row(row, df, asset_class, timeframe,
                                       baseline_name=BASELINE_NAME)
        if pos is None:
            continue
        close = df["Close"].to_numpy(dtype="float64")
        charge = 0.0 if row["rule"] == BASELINE_NAME else cost
        bpy = vector.bars_per_year(df.index)
        net = vector.net_returns(pos, close, charge, bpy)
        bench = vector.net_returns(np.ones(len(df), dtype="float64"), close, FREE, bpy)
        cum = np.cumsum(net - bench)
        if not np.isfinite(cum).all():
            continue

        # Sample this asset's running excess at the shared grid. Before it starts
        # trading it contributes nothing and is not counted, so early points are the
        # mean over the assets that actually existed then.
        idx = np.searchsorted(df.index.to_numpy(), grid.to_numpy(), side="right") - 1
        live = idx >= 0
        total[live] += cum[idx[live]]
        count[live] += 1.0

    if count.max() == 0:
        return None
    with np.errstate(invalid="ignore"):
        mean_excess = np.where(count > 0, total / np.maximum(count, 1e-9), 0.0) * 100.0
    return [[str(g)[:19], round(float(v), 4)] for g, v in zip(grid, mean_excess)]


def _pooled_curve(row, data: dict, asset_class: str, timeframe: str,
                  cost: dict, n_points: int):
    """Cumulative dollar PnL summed across assets, $10k notional each.

    Takes the leaderboard row rather than a name so combos can be rebuilt from their
    legs — their label is not something `generate_position` can parse.
    """
    rule = row["rule"] if not isinstance(row, str) else row
    if isinstance(row, str):
        row = {"rule": row}
    total = None
    idx = None
    for symbol, df in data.items():
        pos = signals.position_for_row(row, df, asset_class, timeframe,
                                       baseline_name=BASELINE_NAME)
        if pos is None:
            continue
        charge = FREE if rule == BASELINE_NAME else cost
        net = vector.net_returns(pos, df["Close"].to_numpy(dtype="float64"), charge)
        pnl = _finite_pnl(net)
        if pnl is None:
            return None                       # whole curve is unrepresentable
        if total is None:
            total, idx = pnl.copy(), df.index
        elif len(pnl) == len(total):
            total += pnl
        else:
            # Assets with different bar counts (a later listing) are aligned on the
            # longest index by padding the front with zero PnL rather than dropped.
            m = min(len(pnl), len(total))
            total[-m:] += pnl[-m:]
    if total is None:
        return None
    return _resample(total, idx, n_points)


def _trades(row, df: pd.DataFrame, asset_class: str, timeframe: str,
            cost: dict) -> dict | None:
    """Trade records for one rule on one asset, plus that asset's own stats."""
    rule = row["rule"]
    pos = signals.position_for_row(row, df, asset_class, timeframe,
                                   baseline_name=BASELINE_NAME)
    if pos is None:
        return None
    close = df["Close"].to_numpy(dtype="float64")
    charge = FREE if rule == BASELINE_NAME else cost
    stats = vector.stats(pos, close, df.index, charge, CAPITAL_PER_TICKER)
    if stats is None:
        return None
    bpy = vector.bars_per_year(df.index)
    net = vector.net_returns(pos, close, charge, bpy)
    pnl_curve = _finite_pnl(net)
    if pnl_curve is None:
        return None

    # A trade is a maximal run of constant non-zero position. Entry is the bar the
    # position was established on; exit the bar it changed.
    trades = []
    wins = losses = 0
    win_sum = loss_sum = 0.0
    i = 0
    n = len(pos)
    while i < n:
        if pos[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < n and pos[j + 1] == pos[i]:
            j += 1
        exit_i = min(j + 1, n - 1)
        if exit_i > i:
            direction = "long" if pos[i] > 0 else "short"
            entry_p, exit_p = close[i], close[exit_i]
            ret = (exit_p / entry_p - 1.0) * (1.0 if pos[i] > 0 else -1.0)
            dollars = CAPITAL_PER_TICKER * ret
            if dollars >= 0:
                wins += 1
                win_sum += dollars
            else:
                losses += 1
                loss_sum += abs(dollars)
            trades.append({
                "entry_date": str(df.index[i])[:19], "exit_date": str(df.index[exit_i])[:19],
                "direction": direction,
                "entry_price": round(float(entry_p), 4),
                "exit_price": round(float(exit_p), 4),
                "pnl_pct": round(float(ret * 100), 3),
                "pnl_dollars": round(float(dollars), 2),
                "holding_days": round(
                    (df.index[exit_i] - df.index[i]).total_seconds() / 86400.0, 4),
            })
        i = j + 1

    n_trades = len(trades)
    return {
        "stats": {
            "total_pnl_dollars": round(stats["pnl_dollars"], 2),
            "cagr": round(stats["cagr"], 4) if np.isfinite(stats["cagr"]) else None,
            "sharpe": round(stats["sharpe"], 3) if np.isfinite(stats["sharpe"]) else None,
            "profit_factor": round(win_sum / loss_sum, 3) if loss_sum > 0 else None,
            "win_rate": round(wins / n_trades, 4) if n_trades else None,
            "avg_win_dollars": round(win_sum / wins, 2) if wins else 0.0,
            "avg_loss_dollars": round(-loss_sum / losses, 2) if losses else 0.0,
            "max_drawdown": round(stats["max_drawdown"], 4),
            "n_trades": n_trades,
        },
        "curve": _resample(pnl_curve, df.index, TICKER_CURVE_POINTS),
        # Most recent only; the stats above reflect every trade, and report.js says so.
        "trades": trades[-MAX_TRADES_SHOWN:],
    }


OP_TEXT = {
    "vote": "Sum the two positions and take the sign: they double down when they agree "
            "and cancel to flat when they disagree.",
    "and": "Trade only where both legs agree on direction, flat otherwise. The strictest "
           "operator and the one that cuts turnover most, which is what the cost gate "
           "actually rewards.",
    "or": "Take either signal, preferring the first leg when both are active and "
          "disagree.",
    "gate": "The first leg supplies the direction; the second only permits or blocks it.",
}


def _describe(r) -> str:
    """Human-readable rule text. Combos are described from their legs and operator."""
    op = r.get("op")
    if isinstance(op, str) and op in OP_TEXT:
        return (f"Combination of {r.get('leg_a')} and {r.get('leg_b')}. {OP_TEXT[op]} "
                f"Both legs were shortlisted on train-period IR only.")
    try:
        return describe_signal(r["rule"])
    except Exception:
        return "Rule description unavailable."


# Leaderboard rows ship as arrays against a shared field list, not as objects. With
# ~1,300 rows x many panels the repeated JSON key names alone were ~400 bytes a row —
# roughly two thirds of the payload. `report.js` hydrates them back into objects.
#
# The list, the extraction, the rounding and the column header now all come from ONE
# declaration per metric in `report_schema.REGISTRY`, because keeping them in two
# parallel positional structures meant a mis-indexed edit shifted every later column and
# rendered a plausible wrong number rather than failing.
ROW_FIELDS = report_schema.ROW_FIELDS


def _row_for_report(r, rank: int) -> list:
    return _check_row_contract(
        report_schema.build_row(r, {"rank": rank, "operators": signals.OPERATORS}))


def _check_row_contract(row: list) -> list:
    """The wire format is positional, so a mismatch corrupts data instead of failing.

    `ROW_FIELDS` and the list `_row_for_report` builds are zipped together. Adding a
    metric to one and not the other — or inserting it at a different index — shifts every
    later column by one, so `ir_net` renders `headroom`'s number and the page is wrong in
    a way that looks entirely plausible. `zip` truncates silently, so nothing raises.

    This is the same failure shape as the IR-by-float-noise and empty-scenario bugs: it
    does not crash, it produces a believable wrong answer. One assertion removes the whole
    class, and it is the first thing to keep when adding a column here.
    """
    if len(row) != len(ROW_FIELDS):
        raise ValueError(
            f"payload row has {len(row)} values but ROW_FIELDS declares "
            f"{len(ROW_FIELDS)}. Both are derived from `report_schema.REGISTRY`, so this "
            f"means something appended to ROW_FIELDS directly instead of declaring a "
            f"Metric. Add the metric to REGISTRY — appending is safe, inserting or "
            f"reordering is not, because the wire format is positional and zip() "
            f"truncates silently.")
    return row


def _as_dict(row: list) -> dict:
    return dict(zip(ROW_FIELDS, row))


def build(classes=None, timeframes=None) -> dict:
    classes = classes or list(CLASSES)
    timeframes = timeframes or list(TIMEFRAMES)
    panels = {}
    tldr_cache: dict[str, str] = {}

    for asset_class in classes:
        for timeframe in timeframes:
            tag = f"{asset_class}_{timeframe}"
            path = RESULTS_DIR / f"summary_{tag}.csv"
            if not path.exists():
                continue
            summary = pd.read_csv(path)

            # Refuse results written by an older schema rather than half-merging them.
            # `data/` artifacts in this repo go stale silently whenever the cost model
            # or the signal layer changes, and a mixed-schema payload would report some
            # panels under one fee model and some under another with nothing to show it.
            if "scenario" not in summary.columns:
                print(f"  {tag}: SKIPPED - stale results (pre-fee-model schema); re-run sweep.py")
                continue

            # Combos share the leaderboard with the singles they were built from —
            # they are competing for the same four gates on the same assets, so
            # ranking them separately would let a combo look like a winner while a
            # single rule beat it.
            combo_path = RESULTS_DIR / f"combo_summary_{tag}.csv"
            if combo_path.exists():
                combos = pd.read_csv(combo_path)
                if not combos.empty and "scenario" in combos.columns:
                    summary = pd.concat([summary, combos], ignore_index=True)
                elif not combos.empty:
                    print(f"  {tag}: combo results are stale, excluded")
            data = td_loader.load(asset_class, timeframe)
            data = {s: d for s, d in data.items() if len(d) >= MIN_BARS}
            if not data or summary.empty:
                continue
            print(f"  {tag}: {len(summary)} rows, {len(data)} assets")

            for fee in scenarios(asset_class):
                cost = fee["key"]
                sub = summary[(summary["scenario"] == cost) | summary["is_baseline"]]
                sub = sub.sort_values("ir_net", ascending=False)
                ranked = sub[~sub["is_baseline"] & sub["rankable"]]
                baseline = sub[sub["is_baseline"]]

                # Keep every single rule; cap combos to the strongest by IR. The page
                # states the truncation rather than silently showing a subset.
                recs = ranked.to_dict("records")
                singles = [r for r in recs if not isinstance(r.get("op"), str)]
                combos_all = [r for r in recs if isinstance(r.get("op"), str)]
                n_combos_total = len(combos_all)
                combos_kept = combos_all[:COMBOS_KEPT]
                recs = sorted(singles + combos_kept,
                              key=lambda r: (r["ir_net"] is None, -(r["ir_net"] or -1e9)))

                rows = []
                for rank, r in enumerate(recs, start=1):
                    # Descriptions live in one shared table: the same rule appears in
                    # 4 cost panels x 7 timeframes x 2 classes, and a ~150-char string
                    # repeated 56 times was several MB on its own.
                    if r["rule"] not in tldr_cache:
                        tldr_cache[r["rule"]] = _describe(r)
                    rows.append(_row_for_report(r, rank))

                for r in baseline.to_dict("records"):
                    tldr_cache.setdefault(
                        r["rule"],
                        "Buy on the first bar, hold to the last. Never flattened, never "
                        "charged a cost - the thing every rule here is trying to beat.")
                    rows.append(_row_for_report(r, 0))

                curves = {}
                for row in rows[:CURVES_KEPT]:
                    r = _as_dict(row)
                    r["rule"] = r["indicator"]
                    c = _pooled_curve(r, data, asset_class, timeframe,
                                      fee, CURVE_POINTS)
                    if c:
                        curves[r["indicator"]] = c
                bench = _pooled_curve(BASELINE_NAME, data, asset_class, timeframe,
                                      FREE, CURVE_POINTS)
                if bench:
                    curves[BASELINE_NAME] = bench

                trades = {}
                for row in rows[:TOP_N_TRADES]:
                    r = _as_dict(row)
                    r["rule"] = r["indicator"]
                    per_asset = {}
                    picks = list(data) if ASSETS_PER_RULE is None else list(data)[:ASSETS_PER_RULE]
                    for symbol in picks:
                        t = _trades(r, data[symbol], asset_class, timeframe, fee)
                        if t:
                            per_asset[symbol] = t
                    if per_asset:
                        trades[r["indicator"]] = per_asset

                first = next(iter(data.values()))
                # The maximum of N candidates is an extreme-value statistic. Shipping it
                # without the level noise alone would reach lets the shortest, noisiest
                # sample look like the most promising one.
                import statistics as _st
                _yrs = [r["years"] for r in recs if r.get("years") is not None]
                years_test = float(_st.median(_yrs)) if _yrs else float("nan")
                ceiling = metrics.noise_ceiling(len(recs), years_test)
                panels[f"{asset_class}|{timeframe}|{cost}"] = {
                    "meta": {
                        "start_date": str(min(d.index[0] for d in data.values()))[:10],
                        "end_date": str(max(d.index[-1] for d in data.values()))[:10],
                        "n_tickers": len(data),
                        "n_bars": int(len(first)),
                        "capital_per_ticker": int(CAPITAL_PER_TICKER),
                        "n_indicators": len(recs),
                        "n_singles": len(singles),
                        "n_combos_total": n_combos_total,
                        "n_combos_shown": len(combos_kept),
                        "n_curves": CURVES_KEPT,
                        "n_generic_fallback": int(sum(bool(r["generic_fallback"]) for r in recs)),
                        "years_test": round(years_test, 2) if np.isfinite(years_test) else None,
                        "se_ir": round(metrics.se_ir(years_test), 4)
                                 if np.isfinite(years_test) else None,
                        "noise_ceiling_ir": round(ceiling, 4) if np.isfinite(ceiling) else None,
                        "bonferroni_t": round(metrics.bonferroni_t(len(ranked)), 2),
                    },
                    "leaderboard": rows,
                    "curves": curves,
                    "benchmark_curve": bench or [],
                    "trades": trades,
                }

    classes_meta = []
    for key in classes:
        spec = CLASSES[key]
        classes_meta.append({
            "key": key, "label": spec["label"], "noun": spec["noun"],
            "n_assets": len(spec["symbols"]),
            "cost_grid": [s2["key"] for s2 in scenarios(key)],
            "cost_labels": {s2["key"]: s2["label"] for s2 in scenarios(key)},
            "cost_notes": {s2["key"]: s2["note"] for s2 in scenarios(key)},
            "headline_cost": HEADLINE_SCENARIO[key],
            "tldr": CLASS_TLDR.get(key, ""),
        })

    return {
        "demo": False,
        "row_fields": ROW_FIELDS,
        "field_meta": report_schema.FIELD_META,
        # Keyed by rule name and shared across every panel rather than repeated per row:
        # the same rule appears in all 8 panels, and a one-line description is not enough
        # to read a result. `MAXINDEX` tops us_stocks 1d and is a null-establishing row
        # with no economic content — a reader who does not know that draws the opposite
        # conclusion from the one the number supports.
        "rule_logic": _rule_logic_block(),
        "strategy_logic": _strategy_logic_block(),
        "tldr": tldr_cache,
        "classes": [c for c in classes_meta
                    if any(k.startswith(c["key"] + "|") for k in panels)],
        "timeframes": [t for t in timeframes
                       if any(f"|{t}|" in k for k in panels)],
        "timeframe_tldr": TIMEFRAME_TLDR,
        "gates": [{"key": g["key"], "label": g["label"], "letter": g["letter"],
                   "target": g["target"]} for g in GATES],
        "panels": panels,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", choices=list(TIMEFRAMES))
    args = ap.parse_args()

    payload = build(args.classes, args.timeframes)
    if not payload["panels"]:
        raise SystemExit("no summary CSVs found - run sweep.py first")
    # Sanitise here, not only in build_report: a payload on disk that cannot be
    # serialised is a trap for anything else that reads it.
    payload = _sanitize(payload)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    PAYLOAD_PATH.write_text(
        json.dumps(payload, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    mb = PAYLOAD_PATH.stat().st_size / 1e6
    print(f"wrote {PAYLOAD_PATH} ({mb:.1f} MB, {len(payload['panels'])} panels)")


if __name__ == "__main__":
    main()
