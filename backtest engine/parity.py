"""Cross-check the three engines on sampled cells. Disagreement fails the build.

The premise of this project's accuracy claim: the risk is not which engine is used, it
is an implementation bug, and **a single engine cannot detect its own**. So the same
backtest is derived three times — vectorised array math, an explicit share-level loop,
and an event-driven order/fill simulator — and they are made to agree.

Two tiers, because the three engines are not testing the same thing:

* `vector` vs `reference` runs across the **whole cost grid** and must agree to
  floating-point precision (1e-9 relative). Both implement the identical model, so any
  gap here is a bug, full stop.
* `nautilus` runs at **zero cost only** and is allowed the tolerance its own
  quantisation imposes — positions round to 6dp and cash to the cent, many times per
  run. See `engines/nautilus.py` for why a venue fee is not the same quantity as this
  project's turnover charge.

Nautilus costs ~1.5 ms/bar under constant-fraction rebalancing, so a full 3.35M-bar
crypto 1-minute series would be ~85 minutes for a single cell. Sampling on the fine
timeframes is therefore **windowed** — a fixed slice, not the whole series — and the
report says which cells were checked at full length and which were windowed. A windowed
check is still a real check; calling it full-length would not be.

Run::

    python parity.py                 # sample across everything cached
    python parity.py --n 40          # more cells
    python parity.py --no-nautilus   # vector vs reference only (fast)
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np
import pandas as pd

from config import CAPITAL_PER_TICKER, CLASSES, RESULTS_DIR, TIMEFRAMES, scenarios
from engines import reference, vector
import signals
import td_loader

from strategies.talib_signals import get_all_indicator_names

# Relative tolerance for two implementations of the identical model.
EXACT_TOL = 1e-9

# Nautilus tolerance has three terms and the third is the important one.
#
# Every fill rounds cash to the cent, so each is a +/-0.005 quantisation event with no
# systematic sign. Accumulated error therefore grows as sqrt(n_fills), not n_fills, and
# a fixed tolerance is simply the wrong shape: a daily cell has ~70 fills while a
# 2-hourly cell that holds shorts re-sizes every bar and had 5,355. Measured directly:
# raising Nautilus's price/size precision from 6dp to 8dp shrank the worst gap from
# 1.07e-5 to 7.12e-6, which is what quantisation does and what a modelling bug does not.
#
# 3 sigma of accumulated cent-rounding is the bound. It stays tight (~1e-5 relative on a
# 5,000-fill cell) while a genuine modelling difference — which scales with equity, not
# with sqrt(fills) — would still blow straight through it.
NT_REL_TOL = 1e-5
NT_ABS_TOL = 0.05
CENT = 0.005
NT_SIGMA = 3.0


def nautilus_tolerance(value: float, n_fills: int) -> float:
    """Absolute tolerance for one Nautilus cell, given how many times it rounded."""
    return max(NT_REL_TOL * abs(value), NT_ABS_TOL,
               NT_SIGMA * CENT * np.sqrt(max(n_fills, 1)))
# Bars beyond which a Nautilus parity cell is windowed rather than run in full.
NT_WINDOW_BARS = 20_000


def sample_cells(rng, n: int) -> list[tuple[str, str, str, str]]:
    """(class, timeframe, symbol, rule) cells that actually have cached data."""
    names = get_all_indicator_names()
    cells = []
    for asset_class, spec in CLASSES.items():
        for timeframe in TIMEFRAMES:
            cached = td_loader.load(asset_class, timeframe)
            symbols = [s for s in cached if len(cached[s]) >= 200]
            if not symbols:
                continue
            usable, _skipped = signals.usable_rules(names, asset_class, timeframe)
            if not usable:
                continue
            for _ in range(n):
                cells.append((asset_class, timeframe,
                              str(rng.choice(symbols)), str(rng.choice(usable))))
    return cells


def check_cell(asset_class, timeframe, symbol, rule, use_nautilus: bool) -> list[dict]:
    df = td_loader.load(asset_class, timeframe, [symbol])[symbol]
    if len(df) < 200:
        return []
    pos = signals.position_for(rule, df, asset_class, timeframe)
    if pos is None:
        return [{"class": asset_class, "timeframe": timeframe, "symbol": symbol,
                 "rule": rule, "engine_pair": "signal", "ok": False,
                 "detail": "position_for returned None (rule unavailable here)"}]

    close = df["Close"].to_numpy(dtype="float64")
    rows = []

    bpy = vector.bars_per_year(df.index)
    for fee in scenarios(asset_class):
        v = vector.final_equity(pos, close, fee, CAPITAL_PER_TICKER, bpy)
        r = reference.final_equity(pos, close, fee, CAPITAL_PER_TICKER, bpy)
        rel = abs(v - r) / max(abs(r), 1e-12)
        rows.append({"class": asset_class, "timeframe": timeframe, "symbol": symbol,
                     "rule": rule, "engine_pair": "vector-reference", "scenario": fee["key"],
                     "n_bars": len(df), "windowed": False,
                     "vector": v, "other": r, "rel_diff": rel,
                     "ok": bool(rel <= EXACT_TOL)})

    if use_nautilus:
        from engines import nautilus
        windowed = len(df) > NT_WINDOW_BARS
        sub = df.iloc[:NT_WINDOW_BARS] if windowed else df
        sub_pos = pos[:len(sub)]
        sub_close = sub["Close"].to_numpy(dtype="float64")
        t0 = time.perf_counter()
        try:
            res = nautilus.run(symbol, sub, sub_pos, CAPITAL_PER_TICKER,
                               timeframe, fractional=True)
            nq = res["final_equity"]
            v0 = vector.final_equity(sub_pos, sub_close, 0.0, CAPITAL_PER_TICKER)
            diff = abs(nq - v0)
            tol = nautilus_tolerance(v0, res["n_fills"])
            rows.append({"class": asset_class, "timeframe": timeframe, "symbol": symbol,
                         "rule": rule, "engine_pair": "vector-nautilus", "scenario": "gross",
                         "n_bars": len(sub), "windowed": windowed,
                         "vector": v0, "other": nq, "n_fills": res["n_fills"],
                         "abs_diff": diff, "tolerance": tol,
                         "rel_diff": diff / max(abs(v0), 1e-12),
                         "ok": bool(diff <= tol),
                         "seconds": time.perf_counter() - t0})
        except Exception as exc:
            rows.append({"class": asset_class, "timeframe": timeframe, "symbol": symbol,
                         "rule": rule, "engine_pair": "vector-nautilus", "ok": False,
                         "detail": f"nautilus raised: {exc}"})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=6,
                    help="cells sampled per (class, timeframe)")
    ap.add_argument("--no-nautilus", action="store_true")
    ap.add_argument("--seed", type=int, default=20260803)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    cells = sample_cells(rng, args.n)
    if not cells:
        print("no cached data to check - run td_loader.py first")
        return 1

    print(f"checking {len(cells)} cells "
          f"({'vector/reference/nautilus' if not args.no_nautilus else 'vector/reference'})")
    rows = []
    for i, (c, tf, sym, rule) in enumerate(cells, 1):
        rows.extend(check_cell(c, tf, sym, rule, not args.no_nautilus))
        if i % 10 == 0 or i == len(cells):
            print(f"  {i}/{len(cells)}")

    df = pd.DataFrame(rows)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(RESULTS_DIR / "parity.csv", index=False)

    failures = df[~df["ok"]]
    summary = {}
    for pair, grp in df.groupby("engine_pair"):
        finite = grp["rel_diff"].dropna()
        summary[pair] = {
            "cells": int(len(grp)),
            "failures": int((~grp["ok"]).sum()),
            "max_rel_diff": float(finite.max()) if len(finite) else None,
            "median_rel_diff": float(finite.median()) if len(finite) else None,
            "windowed": int(grp.get("windowed", pd.Series(dtype=bool)).sum()),
        }
    (RESULTS_DIR / "parity_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")

    print("\n=== parity ===")
    for pair, s in summary.items():
        mx = f"{s['max_rel_diff']:.2e}" if s["max_rel_diff"] is not None else "n/a"
        print(f"  {pair:20s} {s['cells']:4d} cells | {s['failures']} failed | "
              f"max rel {mx} | {s['windowed']} windowed")

    if len(failures):
        print(f"\nFAILED - {len(failures)} disagreements:")
        cols = [c for c in ("class", "timeframe", "symbol", "rule", "engine_pair",
                            "scenario", "rel_diff", "detail") if c in failures.columns]
        print(failures[cols].head(15).to_string(index=False))
        return 1

    print("\nPASS - all three engines agree within tolerance")
    return 0


if __name__ == "__main__":
    sys.exit(main())
