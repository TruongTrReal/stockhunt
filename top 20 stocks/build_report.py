"""Assemble the sweep results into a single self-contained HTML report.

Reads results/summary_*.csv + results/per_ticker_*.csv and writes report.html with
the data embedded as JSON — no backend, no build step, no external requests.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from config import (
    BASELINE_NAME, COST_BPS_GRID, HEADLINE_COST_BPS, HERE, RESULTS_DIR,
    TIMEFRAMES, UNIVERSE,
)

OUT_HTML = HERE / "report.html"
TOP_N = 15


def _clean(value):
    """JSON has no NaN/Infinity literals; anything non-finite becomes null."""
    if isinstance(value, float):
        return None if not math.isfinite(value) else round(value, 6)
    if isinstance(value, (pd.Timestamp,)):
        return str(value)
    return value


def _records(df: pd.DataFrame, columns: list[str]) -> list[dict]:
    return [{c: _clean(row[c]) for c in columns} for _, row in df.iterrows()]


def build_payload() -> dict:
    payload = {
        "universe": UNIVERSE,
        "costGrid": COST_BPS_GRID,
        "headlineCost": HEADLINE_COST_BPS,
        "timeframes": {},
    }

    for timeframe in TIMEFRAMES:
        summary_path = RESULTS_DIR / f"summary_{timeframe}.csv"
        detail_path = RESULTS_DIR / f"per_ticker_{timeframe}.csv"
        if not summary_path.exists():
            continue
        summary = pd.read_csv(summary_path)
        detail = pd.read_csv(detail_path)

        head = summary[(summary["cost_bps"] == HEADLINE_COST_BPS)
                       & summary["rankable"] & ~summary["is_baseline"]]
        head = head.sort_values("avg_excess_sharpe", ascending=False)

        baseline = summary[(summary["cost_bps"] == HEADLINE_COST_BPS)
                           & summary["is_baseline"]]
        baseline_row = _records(baseline, [
            "indicator", "avg_sharpe", "avg_cagr", "avg_max_drawdown",
            "avg_total_return", "avg_exposure"])

        # How the whole field degrades as cost rises — the most informative single
        # view here, because it separates "no signal" from "signal eaten by costs".
        decay = []
        for cost in COST_BPS_GRID:
            sub = summary[(summary["cost_bps"] == cost)
                          & summary["rankable"] & ~summary["is_baseline"]]
            if sub.empty:
                continue
            best = sub.sort_values("avg_excess_sharpe", ascending=False).iloc[0]
            decay.append({
                "costBps": _clean(cost),
                "nBeat": int((sub["avg_excess_sharpe"] > 0).sum()),
                "nRankable": int(len(sub)),
                "bestIndicator": best["indicator"],
                "bestExcessSharpe": _clean(best["avg_excess_sharpe"]),
                "medianExcessSharpe": _clean(sub["avg_excess_sharpe"].median()),
                "bestBeatRate": _clean(best["beat_buyhold_rate"]),
            })

        bars = int(detail["n_bars"].max()) if "n_bars" in detail else None
        payload["timeframes"][timeframe] = {
            "label": timeframe,
            "nRankable": int(len(head)),
            "nBeat": int((head["avg_excess_sharpe"] > 0).sum()),
            "bars": bars,
            "start": TIMEFRAMES[timeframe]["start"],
            "baseline": baseline_row[0] if baseline_row else None,
            "leaders": _records(head.head(TOP_N), [
                "indicator", "avg_excess_sharpe", "avg_sharpe", "avg_cagr",
                "avg_max_drawdown", "beat_buyhold_rate", "avg_exposure",
                "avg_turnover", "avg_trades", "generic_fallback",
                "total_pnl_dollars"]),
            "costDecay": decay,
        }

    return payload


def main() -> None:
    payload = build_payload()
    template = (HERE / "template.html").read_text(encoding="utf-8")
    blob = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    OUT_HTML.write_text(template.replace("__REPORT_DATA__", blob), encoding="utf-8")

    print(f"wrote {OUT_HTML} ({OUT_HTML.stat().st_size / 1024:.0f} KB)")
    for name, tf in payload["timeframes"].items():
        print(f"  {name}: {tf['nBeat']}/{tf['nRankable']} beat B&H "
              f"@ {payload['headlineCost']:g}bps, {tf['bars']:,} bars")


if __name__ == "__main__":
    main()
