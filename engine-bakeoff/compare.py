"""Score both engines against the analytic reference and print the verdict table.

Accuracy is measured as relative error in final equity against the reference run
for the *same* (ticker, rule, execution convention) — never against the other
engine, so an engine is never penalised for a difference the reference agrees
with. Trade counts are compared separately: an engine can land on the right
equity with the wrong number of round trips, and that would still be a bug.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import RESULTS_DIR

KEY = ["convention", "ticker", "rule"]
# Below this, a difference is float/quantisation noise (Nautilus rounds every
# quantity to 6 dp and every cash balance to the cent), not a modelling gap.
NOISE = 1e-5


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    ref = pd.read_csv(RESULTS_DIR / "reference.csv")
    engines = pd.concat([
        pd.read_csv(RESULTS_DIR / "manifoldbt.csv"),
        pd.read_csv(RESULTS_DIR / "manifoldbt_batch.csv"),
        pd.read_csv(RESULTS_DIR / "nautilus.csv"),
    ], ignore_index=True)
    return ref, engines


def score(ref: pd.DataFrame, engines: pd.DataFrame) -> pd.DataFrame:
    merged = engines.merge(
        ref[KEY + ["final_equity", "n_trades"]].rename(columns={
            "final_equity": "ref_equity", "n_trades": "ref_trades"}),
        on=KEY, how="left", validate="many_to_one",
    )
    merged["rel_error"] = np.abs(merged["final_equity"] / merged["ref_equity"] - 1.0)
    merged["trade_diff"] = merged["n_trades"] - merged["ref_trades"]
    return merged


def main() -> None:
    ref, engines = load()
    merged = score(ref, engines)
    merged.to_csv(RESULTS_DIR / "comparison.csv", index=False)

    timing = pd.concat([
        pd.read_csv(RESULTS_DIR / f"timing_{n}.csv")
        for n in ("reference", "manifoldbt", "manifoldbt_batch", "nautilus")
    ], ignore_index=True)

    print("=" * 78)
    print("ACCURACY vs analytic reference (relative error in final equity)")
    print("=" * 78)
    acc = merged.groupby(["engine", "convention"]).agg(
        runs=("rel_error", "size"),
        exact=("rel_error", lambda s: int((s == 0).sum())),
        within_noise=("rel_error", lambda s: int((s <= NOISE).sum())),
        median_rel_err=("rel_error", "median"),
        max_rel_err=("rel_error", "max"),
        trade_count_mismatches=("trade_diff", lambda s: int((s != 0).sum())),
    ).reset_index()
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(acc.to_string(index=False, float_format=lambda v: f"{v:.3e}"))

    print()
    print("=" * 78)
    print("SPEED (wall clock over this bake-off's run set)")
    print("=" * 78)
    timing["ms_per_run"] = timing["engine_seconds"] / timing["n_runs"] * 1000
    cols = ["engine", "n_runs", "engine_seconds", "ms_per_run", "total_seconds"]
    with pd.option_context("display.width", 200):
        print(timing[cols].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    base = timing.loc[timing["engine"] == "manifoldbt", "ms_per_run"].iloc[0]
    print(f"\nrelative to manifoldbt ({base:.1f} ms/run):")
    for _, row in timing.iterrows():
        if row["engine"] == "manifoldbt":
            continue
        print(f"  {row['engine']:20} {row['ms_per_run'] / base:8.1f}x")

    print()
    print("=" * 78)
    print("WORST DISAGREEMENTS")
    print("=" * 78)
    worst = merged.sort_values("rel_error", ascending=False).head(8)
    with pd.option_context("display.width", 200):
        print(worst[["engine", "convention", "ticker", "rule", "final_equity",
                     "ref_equity", "rel_error", "trade_diff"]].to_string(
            index=False, float_format=lambda v: f"{v:,.4f}"))

    # Extrapolation to the real workload this project actually runs.
    print()
    print("=" * 78)
    print("EXTRAPOLATION to 501 tickers x 231 indicators (115,731 runs)")
    print("=" * 78)
    for _, row in timing.iterrows():
        hours = row["ms_per_run"] / 1000 * 115_731 / 3600
        print(f"  {row['engine']:20} {hours:10.2f} h")


if __name__ == "__main__":
    main()
