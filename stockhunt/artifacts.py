"""How a long diagnostic table gets written.

The rule: **a table nothing reads by literal name is written as Parquet, not CSV.**

The long tables in this repo are `rule x symbol x scenario x fold`. On us_stocks 1d that
is over ten million rows, and as CSV roughly half of every row is the full float64 repr
of two information ratios that mean nothing past three significant figures
(`-0.7822978734787989`). Parquet suits the shape rather than merely shrinking it: `rule`,
`symbol` and `scenario` are three low-cardinality strings repeating ten million times,
and dictionary encoding stores each distinct value once where CSV re-spells every one.
Measured on the real table it is **5.3x smaller and 19x faster to read back**
(7.6s -> 0.4s), bit-exact including the float64 mantissas — the decimal spelling goes,
the precision does not.

Not 20x, which is what an estimate from the string columns alone suggests: once those
collapse, two high-entropy float columns are most of what is left and they barely
compress. Worth knowing before promising a ratio on the next table.

**The superseded CSV is removed, not left beside the Parquet.** A stale twin of a
generated file is a failure this repo already documents for `report/index.html` and
`web/data.js` — a reader picking the wrong one gets last month's numbers and nothing
anywhere says so. The removal only happens after the Parquet is on disk.

This lived in `walk-forward optimization/wfo_paths.py`, where `backtest engine/` could
not reach it — so `sweep.py` went on writing a 166 MB `per_asset_us_stocks_4h.csv` that
no code in the repo reads. It moved here so both sides of the pipeline can use it;
`wfo_paths.write_bulk` still exists and delegates.

Reserved for tables NOTHING resolves by literal name. `edge_standard.csv` (read by
`validate.py` and the dashboard) and `var_summary_*.csv` (globbed by
`gate_calibration.py`) stay CSV — a reader that opens a path by name does not survive
the extension changing under it.
"""

from __future__ import annotations

from pathlib import Path


def write_bulk(frame, path) -> None:
    """Write a per-fold / per-cell diagnostic table as Parquet and drop the CSV twin."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False, compression="zstd")
    path.with_suffix(".csv").unlink(missing_ok=True)


def read_bulk(path):
    """Read a bulk table, accepting either spelling.

    Prefers Parquet and falls back to the CSV of the same stem, so a reader keeps working
    across the migration and against sheets written before it. Returns `None` when
    neither exists, which callers already treat as "this sheet was never produced".
    """
    import pandas as pd

    path = Path(path)
    parquet = path.with_suffix(".parquet")
    if parquet.exists():
        return pd.read_parquet(parquet)
    csv = path.with_suffix(".csv")
    if csv.exists():
        return pd.read_csv(csv)
    return None
