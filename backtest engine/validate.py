"""Re-run survivors in NautilusTrader with the realism the sweep cannot represent.

The vectorised sweep is exact for the model it implements — `parity.py` proves it against
two independent derivations. What it cannot represent is the part that only exists once
an order reaches a venue:

* **whole-share rounding.** A real `Equity` instrument has `size_precision=0`. At $10,000
  a side, a $500 stock cannot be held in the fraction the sweep assumes.
* **an explicit commission**, charged on traded notional at the fill, rather than this
  project's `|d target| x equity` turnover charge.
* **slippage**, as an explicit assumption rather than an implicit zero.

This is the one place in the project where a number is *allowed* to differ from the
sweep, because here the difference is the finding: if a candidate's edge does not survive
whole shares and a real fee, it was never an edge.

Only candidates that clear all four gates are worth this — Nautilus runs ~1.5 ms/bar
under constant-fraction rebalancing. If nothing clears, this script says so and exits,
which is the expected outcome.

Run::

    python validate.py                     # every survivor found by the sweep
    python validate.py --rule "AD and OBV" --class us_stocks --tf 1d
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from config import CAPITAL_PER_TICKER, CLASSES, MIN_BARS, RESULTS_DIR, TIMEFRAMES
from engines import nautilus, vector
import signals
import td_loader

# Charged inside Nautilus on traded notional, unlike the sweep's turnover model.
COMMISSION_BPS = {"us_stocks": 1.0, "crypto": 10.0}
SLIPPAGE_BPS = {"us_stocks": 1.0, "crypto": 5.0}


EDGE_STANDARD_CSV = (RESULTS_DIR.parent.parent / "walk-forward optimization"
                     / "results" / "edge_standard.csv")


def survivors() -> pd.DataFrame:
    """Every (class, timeframe, rule) that PASSED THE EDGE STANDARD.

    Re-pointed 2026-08-08. This used to select `gates_passed == 4` out of this folder's
    own single-split sweeps, which is wrong twice over: those gates were retired, and a
    single split scores a rule without scoring the act of choosing it. Nautilus validation
    is expensive and exists to re-price things that already look real, so it should run on
    the one list of things that actually cleared the standard.

    That list is written by `walk-forward optimization/riskmatch_wf.py` — the only module
    that can compute the six criteria. This is the sole read this folder makes into the
    walk-forward results, and it exists because the verdict genuinely lives there.
    """
    if not EDGE_STANDARD_CSV.exists():
        print(f"no {EDGE_STANDARD_CSV.name} — run `riskmatch_wf.py` in the walk-forward "
              f"folder first; there is nothing to validate until something passes")
        return pd.DataFrame()
    df = pd.read_csv(EDGE_STANDARD_CSV)
    keep = df[df["edge_verdict"] == "PASS"].copy()
    if keep.empty:
        return pd.DataFrame()
    keep["stage"] = "edge_standard"
    keep = keep.rename(columns={"tf": "timeframe"})
    return keep


def validate_one(asset_class: str, timeframe: str, rule: str) -> list[dict]:
    data = td_loader.load(asset_class, timeframe)
    data = {s: d for s, d in data.items() if len(d) >= MIN_BARS}
    comm = COMMISSION_BPS[asset_class]
    slip = SLIPPAGE_BPS[asset_class]
    headline = CLASSES[asset_class]["headline_cost"]

    rows = []
    for symbol, df in data.items():
        pos = signals.position_for(rule, df, asset_class, timeframe)
        if pos is None:
            continue
        close = df["Close"].to_numpy(dtype="float64")

        swept = vector.final_equity(pos, close, headline, CAPITAL_PER_TICKER)
        # Whole shares: the realism the sweep cannot express.
        nt = nautilus.run(symbol, df, pos, CAPITAL_PER_TICKER, timeframe,
                          fractional=False)
        # Commission and slippage are charged outside the engine, on the same traded
        # notional Nautilus filled, so the two effects stay separable in the output.
        turnover = float(np.abs(np.diff(pos)).sum() + abs(pos[0]))
        drag = turnover * (comm + slip) / 10_000.0
        realistic = nt["final_equity"] * (1.0 - drag)

        rows.append({
            "class": asset_class, "timeframe": timeframe, "rule": rule,
            "symbol": symbol, "n_bars": len(df),
            "swept_equity": swept,
            "nautilus_whole_share_equity": nt["final_equity"],
            "after_commission_slippage": realistic,
            "n_fills": nt["n_fills"],
            "turnover": turnover,
            "rounding_cost_pct": (nt["final_equity"] / swept - 1.0) * 100.0,
            "total_cost_pct": (realistic / swept - 1.0) * 100.0,
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rule")
    ap.add_argument("--class", dest="asset_class", choices=list(CLASSES))
    ap.add_argument("--tf", dest="timeframe", choices=list(TIMEFRAMES))
    args = ap.parse_args()

    if args.rule:
        targets = [(args.asset_class, args.timeframe, args.rule)]
    else:
        surv = survivors()
        if surv.empty:
            print("No candidate cleared all four gates - nothing to validate.\n"
                  "That is the expected outcome; see results/summary_*.csv for how "
                  "close anything came.")
            return
        targets = list(surv[["class", "timeframe", "rule"]]
                       .drop_duplicates().itertuples(index=False, name=None))
        print(f"{len(targets)} survivor(s) to validate")

    rows = []
    for asset_class, timeframe, rule in targets:
        print(f"  {asset_class}/{timeframe}: {rule}")
        rows.extend(validate_one(asset_class, timeframe, rule))

    if not rows:
        print("nothing produced")
        return
    df = pd.DataFrame(rows)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(RESULTS_DIR / "validation.csv", index=False)

    agg = df.groupby(["class", "timeframe", "rule"]).agg(
        assets=("symbol", "size"),
        median_rounding_pct=("rounding_cost_pct", "median"),
        median_total_pct=("total_cost_pct", "median"),
    ).reset_index()
    print("\n=== Nautilus validation (whole shares + commission + slippage) ===")
    print(agg.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print(f"\nwrote {RESULTS_DIR / 'validation.csv'}")


if __name__ == "__main__":
    main()
