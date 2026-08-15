"""Convert the long diagnostic result tables from CSV to Parquet, in place.

`sweep.py`, `walkforward.py` and `strat_wf.py` now write these as Parquet, but the
tables already on disk are CSV and would stay that way until each stage is re-run —
which for us_stocks 1d walk-forward is four minutes and for the full grid is hours.
This converts what is already there.

**Nothing is deleted until its replacement is verified.** Each file is read, written as
Parquet, read back, and compared to the original with `assert_frame_equal(check_exact=True)`
— dtypes, column order, every float64 mantissa. Only then is the CSV removed. A
mismatch leaves both files in place and reports it, because a lossy "optimisation" of a
result table is exactly the kind of silent corruption this repo cannot afford.

    python tools/migrate_bulk_csv.py --dry-run     # what would change, and by how much
    python tools/migrate_bulk_csv.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stockhunt import paths                                     # noqa: E402

# Only tables nothing resolves by literal name. `edge_standard.csv`, `var_summary_*.csv`,
# `wf_summary_*`, `cwf_*` and the paper-desk CSVs are all read by name and stay CSV.
PATTERNS = [
    (paths.ENGINE_RESULTS, "per_asset_*.csv"),
    (paths.ENGINE_RESULTS, "combo_per_asset_*.csv"),
    (paths.WFO_RESULTS, "wf_per_asset_*.csv"),
    (paths.WFO_RESULTS, "strat_per_asset_*.csv"),
]

# Guard against the glob catching something a reader opens by name. `_best` is an
# orphan that matches the `strat_per_asset_*` glob but not the `<class>_<tf>` shape any
# writer here produces, so its provenance is unknown and it is left alone — it is 4 KB.
NEVER = ("edge_standard", "var_summary", "wf_summary", "wf_meta", "parity", "_best")


def targets() -> list[Path]:
    out = []
    for root, pattern in PATTERNS:
        if not root.exists():
            continue
        for p in sorted(root.glob(pattern)):
            if any(n in p.name for n in NEVER):
                continue
            out.append(p)
    return out


def convert(csv: Path, dry_run: bool) -> tuple[bool, str]:
    parquet = csv.with_suffix(".parquet")
    before = csv.stat().st_size
    if dry_run:
        return True, f"{before / 1e6:8.1f} MB  {csv.name}"

    original = pd.read_csv(csv)
    original.to_parquet(parquet, index=False, compression="zstd")

    # Read it back and require the frames to be indistinguishable before removing the
    # source. Parquet round-trips float64 bit-exactly; this proves it did.
    back = pd.read_parquet(parquet)
    try:
        pd.testing.assert_frame_equal(original, back, check_exact=True)
    except AssertionError as exc:
        parquet.unlink(missing_ok=True)
        return False, f"MISMATCH, kept CSV: {csv.name} -> {str(exc).splitlines()[0]}"

    after = parquet.stat().st_size
    csv.unlink()
    return True, (f"{before / 1e6:8.1f} MB -> {after / 1e6:6.1f} MB "
                  f"({before / max(after, 1):4.1f}x)  {csv.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = targets()
    if not files:
        print("nothing to convert")
        return

    total_before = sum(f.stat().st_size for f in files)
    print(f"{len(files)} tables, {total_before / 1e6:.0f} MB of CSV"
          f"{' (dry run)' if args.dry_run else ''}\n")

    failures = 0
    for f in files:
        ok, msg = convert(f, args.dry_run)
        failures += (not ok)
        print(f"  {msg}")

    if args.dry_run:
        return
    after = sum(p.stat().st_size for root, pat in PATTERNS if root.exists()
                for p in root.glob(pat.replace(".csv", ".parquet")))
    print(f"\n{total_before / 1e6:.0f} MB -> {after / 1e6:.0f} MB")
    if failures:
        print(f"{failures} file(s) did NOT round-trip and were left as CSV")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
