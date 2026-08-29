"""Concatenate a cell's bisection slices into one CSV, ALIGNING COLUMNS.

`tail -n +2` is the obvious way to do this and it is wrong, because riskmatch_wf does not
emit a fixed schema. Three columns -- `edge_t_bar_corrected`, `edge_t_uncorrected_pass`,
`edge_t_bar_source` -- appear only on rows where the corrected t-bar could be computed, so a
slice containing such a row has 57 fields while its neighbours have 54. Concatenating them
byte-wise produces a file where four lines out of 759 carry three extra fields, and pandas
refuses the whole thing:

    ParserError: Expected 54 fields in line 702, saw 57

It is worse than an error, because the failure is silent until something reads the file --
which for a cell fetched off a destroyed box can be long after the evidence is gone.

Reading each slice as its own frame and letting pandas union the columns fixes it: a row that
never had `edge_t_bar_source` gets NaN there, which is what "this row has no corrected t bar"
already means everywhere else in the pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


def main() -> int:
    slice_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    frames = []
    for f in sorted(slice_dir.glob("*.csv")):
        try:
            df = pd.read_csv(f)
        except Exception as exc:                       # a truncated slice must not kill the cell
            print(f"  SKIP {f.name}: {exc}", file=sys.stderr)
            continue
        if len(df):
            frames.append(df)
    if not frames:
        print("no readable slices", file=sys.stderr)
        return 1
    merged = pd.concat(frames, ignore_index=True, sort=False)
    # Two slices can legitimately hold the same (rule, side) if a bisection retried a range;
    # the later scoring is the one that completed, so keep it.
    if {"rule", "side"} <= set(merged.columns):
        merged = merged.drop_duplicates(subset=["class", "tf", "side", "rule"], keep="last")
    merged.to_csv(out, index=False)
    print(f"{len(frames)} slices -> {len(merged)} rows, {len(merged.columns)} columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
