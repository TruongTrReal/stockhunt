"""Stage 1d: five pre-registered hypotheses, and nothing else.

Stage 1c showed the search is self-defeating — every extra variant raises the noise
ceiling it has to clear. The fix is not a better search, it is a smaller one. This module
tests **exactly five** rules, fixed in advance, with **no free parameters and no
selection**: each is a published strategy taken at its published settings, so there is
nothing to optimise per fold and the multiple-testing count is 5 rather than 96 or 327.

That matters arithmetically. `metrics.noise_ceiling` scales with the number of things
tried: on 41 out-of-sample years the ceiling is +0.36 at 96 trials but only +0.21 at 5.
A rule scoring 0.4 would be noise in stage 1c and evidence here.

The five, each with the prior that justifies it:

    tsmom12       Moskowitz, Ooi & Pedersen (2012), "Time Series Momentum" — long if the
                  trailing 12-month return is positive, else flat. Documented across 58
                  instruments and eight decades.
    faber_gtaa    Faber (2007), "A Quantitative Approach to Tactical Asset Allocation" —
                  long above the 10-month moving average, else flat. The most widely
                  replicated timing rule in retail literature.
    golden_cross  Long while the 50-day SMA is above the 200-day. Folk rather than
                  academic, included precisely because it is what a retail trader would
                  actually reach for.
    volmanaged    Moreira & Muir (2017), "Volatility-Managed Portfolios" — always long,
                  scaled by the inverse of last month's realised variance. Capped at 1.0:
                  the published version levers, our gates are stated unlevered, and
                  leverage would flatter the Sharpe for free.
    st_reversal   Jegadeesh (1990), "Evidence of Predictable Behavior of Security
                  Returns" — long after a down week, flat after an up week.

**Honesty about what "pre-registered" can mean retrospectively.** True pre-registration
requires committing before seeing any result, and this project has already looked. Two of
the five overlap with transforms stage 1c tested (`faber_gtaa` ~ `regime200` long-only,
`volmanaged` ~ `voltgt`), so for those the N=5 ceiling understates the real multiplicity
and they are flagged `contaminated` in the output. The other three are new at these
specifications. The genuinely clean test is forward-only: `--freeze` writes
`results/prereg_manifest.json` fixing the five definitions and the evaluation start date,
so a future run can score them on bars that did not exist when they were chosen.

Lookbacks are specified in **calendar** terms and converted per sheet through the measured
bars-per-year, so "12 months" means the same thing at 1d and 4h.

Run::

    python prereg.py --tf 1d 4h
    python prereg.py --freeze          # stamp the forward-test manifest
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import talib

from wfo_paths import RESULTS_DIR          # noqa: F401  (wires sys.path first)
from config import (headline_key,  # noqa: F401
                    CLASSES, HEADLINE_SCENARIO, MIN_BARS, MIN_IR_COVERAGE,
                    scenarios)
from engines import vector
from strategies._indicators import _causal_median
import metrics
import td_loader
import walkforward as wfmod

DEFAULT_TIMEFRAMES = ("1d", "4h")

# Transforms stage 1c already looked at. Their N=5 ceiling is optimistic and the report
# says so rather than quietly benefiting from it.
CONTAMINATED = {"faber_gtaa", "volmanaged"}


def _bars(bpy: float, years: float) -> int:
    return max(2, int(round(bpy * years)))


def tsmom12(df: pd.DataFrame, close: np.ndarray, bpy: float) -> np.ndarray:
    n = _bars(bpy, 1.0)
    past = np.full(len(close), np.nan)
    past[n:] = close[n:] / close[:-n] - 1.0
    return np.nan_to_num((past > 0).astype("float64"), nan=0.0)


def faber_gtaa(df: pd.DataFrame, close: np.ndarray, bpy: float) -> np.ndarray:
    ma = talib.SMA(close, timeperiod=_bars(bpy, 10.0 / 12.0))
    return np.nan_to_num((close > ma).astype("float64"), nan=0.0)


def golden_cross(df: pd.DataFrame, close: np.ndarray, bpy: float) -> np.ndarray:
    fast = talib.SMA(close, timeperiod=_bars(bpy, 50.0 / 252.0))
    slow = talib.SMA(close, timeperiod=_bars(bpy, 200.0 / 252.0))
    sig = np.where(np.isfinite(fast) & np.isfinite(slow) & (fast > slow), 1.0, 0.0)
    return sig


def volmanaged(df: pd.DataFrame, close: np.ndarray, bpy: float) -> np.ndarray:
    """Always long, scaled by inverse prior-month realised variance, capped at 1.0.

    The target is an EXPANDING median, not `np.nanmedian` over the whole series.

    The whole-series version is look-ahead and it is not marginal: truncation-testing it
    changed **5,696 of 11,005** past values on AAPL — 52% of bars, not the "11 of 93
    cells" this repo previously recorded. One scalar computed from data that had not
    happened yet re-scales every position before it.

    `_causal_median` is the level a trader could have computed by bar t.
    """
    ret = np.empty_like(close)
    ret[0] = 0.0
    ret[1:] = close[1:] / close[:-1] - 1.0
    var = pd.Series(ret).rolling(_bars(bpy, 1.0 / 12.0)).var(ddof=1).to_numpy()
    target = _causal_median(var, _bars(bpy, 1.0 / 12.0))
    ok = np.isfinite(var) & (var > 0) & np.isfinite(target) & (target > 0)
    scale = np.divide(target, var, out=np.ones_like(var), where=ok)
    return np.clip(np.nan_to_num(scale, nan=1.0), 0.0, 1.0)


def st_reversal(df: pd.DataFrame, close: np.ndarray, bpy: float) -> np.ndarray:
    n = _bars(bpy, 1.0 / 52.0)
    past = np.full(len(close), np.nan)
    past[n:] = close[n:] / close[:-n] - 1.0
    return np.nan_to_num((past < 0).astype("float64"), nan=0.0)


HYPOTHESES = {
    "tsmom12": (tsmom12, "Moskowitz/Ooi/Pedersen 2012: long if trailing 12m return > 0"),
    "faber_gtaa": (faber_gtaa, "Faber 2007: long above the 10-month moving average"),
    "golden_cross": (golden_cross, "Long while the 50d SMA is above the 200d SMA"),
    "volmanaged": (volmanaged, "Moreira/Muir 2017: inverse prior-month variance, capped 1.0"),
    "st_reversal": (st_reversal, "Jegadeesh 1990: long after a down week"),
}


def run_pair(asset_class: str, timeframe: str) -> tuple[pd.DataFrame, dict]:
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        return pd.DataFrame(), {}

    start = min(df.index[0] for df in data.values())
    end = max(df.index[-1] for df in data.values())
    folds = wfmod.generate_folds(start, end)
    if len(folds) < 3:
        return pd.DataFrame(), {"skipped": f"only {len(folds)} folds"}

    free = {"key": "gross", "commission_bps": 0.0, "half_spread_bps": 0.0,
            "sell_fee_bps": 0.0, "borrow_annual": 0.0}
    masks = {s: wfmod.fold_masks(df.index, folds) for s, df in data.items()}
    union = {s: np.logical_or.reduce([m[1] for m in ms if m is not None])
             for s, ms in masks.items() if any(m is not None for m in ms)}

    rows = []
    for symbol, df in data.items():
        if symbol not in union:
            continue
        close = df["Close"].to_numpy(dtype="float64")
        bpy = vector.bars_per_year(df.index)
        bench_net = vector.net_returns(np.ones(len(df), dtype="float64"), close, free, bpy)
        u = union[symbol]
        years = float(u.sum() / bpy) if bpy > 0 else np.nan
        for name, (fn, _) in HYPOTHESES.items():
            pos = fn(df, close, bpy)
            for fee in scenarios(asset_class):
                net = vector.net_returns(pos, close, fee, bpy)
                rows.append({
                    "rule": name, "symbol": symbol, "scenario": fee["key"],
                    "ir_wf": wfmod._ir(net, bench_net, u, bpy),
                    "years_oos": years, "n_folds": len(folds), "n_switches": 0,
                })
    per_asset = pd.DataFrame(rows)
    if per_asset.empty:
        return pd.DataFrame(), {"skipped": "no scoreable folds"}

    ir_by_scen = per_asset.groupby(["rule", "scenario"])["ir_wf"].mean().unstack()
    out = []
    for (name, scen), grp in per_asset.groupby(["rule", "scenario"]):
        row = wfmod.leaderboard_row(grp, name, "prereg", asset_class, timeframe, scen,
                                    ir_by_scen.loc[name])
        row["contaminated"] = name in CONTAMINATED
        row["prior"] = HYPOTHESES[name][1]
        row["rankable"] = metrics.rankable(row, MIN_IR_COVERAGE)
        out.append(row)
    summary = pd.DataFrame(out).sort_values(["scenario", "ir_net"], ascending=[True, False])

    years = float(per_asset["years_oos"].median())
    meta = {
        "class": asset_class, "timeframe": timeframe, "n_assets": len(union),
        "n_trials": len(HYPOTHESES), "n_folds": len(folds), "years_oos": years,
        "noise_ceiling_5": metrics.noise_ceiling(len(HYPOTHESES), years),
        "noise_ceiling_96": metrics.noise_ceiling(96, years),
        "oos_start": str(folds[0].is_end.date()),
        "oos_end": str(folds[-1].oos_end.date()),
    }
    return summary, meta


def report(summary: pd.DataFrame, meta: dict) -> None:
    scen = headline_key(meta["class"], meta.get("timeframe"))
    h = summary[summary.scenario == scen]
    print(f"\n=== {meta['class']}_{meta['timeframe']} ({meta['seconds']:.0f}s) ===")
    print(f"  {meta['n_trials']} pre-registered hypotheses | {meta['n_folds']} folds | "
          f"OOS {meta['oos_start']} -> {meta['oos_end']} ({meta['years_oos']:.1f}y median)")
    print(f"  noise ceiling: IR {meta['noise_ceiling_5']:+.3f} at 5 trials "
          f"(vs {meta['noise_ceiling_96']:+.3f} at the 96 of stage 1c)")
    print()
    for r in h.itertuples():
        mark = " [contaminated by stage 1c]" if r.contaminated else ""
        verdict = "ABOVE ceiling" if r.ir_net > meta["noise_ceiling_5"] else "below ceiling"
        print(f"    {r.rule:<14} IR {r.ir_net:+.3f}  breadth {r.ir_hit_rate:.0%}  "
              f"t {r.t_stat:+.2f}  headroom {r.headroom:.2f}  "
              f"{r.legacy_passed}/4 legacy  {verdict}{mark}")
    print(f"\n  legacy 4-gate diagnostic: {int((h.legacy_passed == 4).sum())} of {len(h)}"
          f" — the verdict lives in results/edge_standard.csv, not here")


def freeze(paths: dict) -> None:
    """Stamp the definitions and a forward-test start so a later run is genuinely clean."""
    manifest = {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "forward_test_starts": datetime.now(timezone.utc).date().isoformat(),
        "hypotheses": {k: v[1] for k, v in HYPOTHESES.items()},
        "contaminated": sorted(CONTAMINATED),
        "n_trials": len(HYPOTHESES),
        "note": ("Scored on bars after forward_test_starts, this set carries no selection "
                 "at all: 5 fixed rules, no free parameters, no re-optimisation. That is "
                 "the only version of this test that is genuinely pre-registered."),
        "sheets": paths,
    }
    out = RESULTS_DIR / "prereg_manifest.json"
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nfroze {len(HYPOTHESES)} hypotheses -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", default=list(DEFAULT_TIMEFRAMES))
    ap.add_argument("--freeze", action="store_true",
                    help="write the forward-test manifest after running")
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    metas, paths = [], {}
    for asset_class in args.classes:
        for timeframe in args.timeframes:
            t0 = time.time()
            summary, meta = run_pair(asset_class, timeframe)
            if summary.empty:
                print(f"{asset_class}/{timeframe}: "
                      f"{meta.get('skipped', 'no cached data')}, skipped")
                continue
            tag = f"{asset_class}_{timeframe}"
            summary.to_csv(RESULTS_DIR / f"prereg_{tag}.csv", index=False)
            paths[tag] = {"oos_start": meta["oos_start"], "oos_end": meta["oos_end"],
                          "years_oos": round(meta["years_oos"], 2)}
            meta["seconds"] = time.time() - t0
            metas.append(meta)
            report(summary, meta)

    if metas:
        pd.DataFrame(metas).to_csv(RESULTS_DIR / "prereg_meta.csv", index=False)
        if args.freeze:
            freeze(paths)
        print(f"\nwrote {RESULTS_DIR}")


if __name__ == "__main__":
    main()
