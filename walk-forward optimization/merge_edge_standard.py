"""Splice per-cell verdict runs into `edge_standard.csv` by (class, timeframe).

    python merge_edge_standard.py --dry-run    # what would change, write nothing
    python merge_edge_standard.py              # ...do it

**Why this exists.** `riskmatch_wf.py` writes `edge_standard.csv` WHOLE, and its scoping
rule reads `--rules` and `--class` but never `--tf`:

    scoped = (bool(args.rules) or len(args.classes) < len(CLASSES)) and not args.promote

So "score every class at 1h and 15m" is not a scoped run. It writes the real file, and the
1d and 4h rows -- the verdict of record for the entire project -- are silently deleted.
The file has been clobbered that way twice before (4,800 rows replaced by 3, then by 16),
which is why the partial mechanism exists at all; this closes the remaining gap, where the
thing you want to add is a whole cell rather than one rule.

`run_riskmatch_intraday.sh` therefore runs ONE CLASS at a time -- scoped, so each lands in
`edge_standard.partial.csv` -- and copies each out to `results/edge_cells/`. This reads
those back and splices them in.

**Splicing is by (class, tf), and it is replace-whole-cell, not row-level merge.** A cell's
rows are one run's output: they share a fold calendar, a trial count and a measured t bar,
and `apply_edge_standard`'s verdict for any row depends on the panel the row was scored in.
Merging row by row would let two runs' rules sit in one cell carrying two different bars,
which is the mixed-measurement defect this repo has removed twice. So a cell present in the
new material replaces that cell entirely; a cell absent from it is left exactly as it was.

**Untouched rows are copied as bytes.** Parsing a float column and writing it back does not
round-trip -- the last digit moves -- and doing that to 6,986 rows buries the ones that
actually changed. The same reasoning as `merge_book.py`, and for the same reason: `git diff`
on this file should be the cells that moved and nothing else.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pandas as pd

import wfo_paths  # noqa: F401  (path bootstrap)

RESULTS = wfo_paths.RESULTS_DIR
TARGET = RESULTS / "edge_standard.csv"
CELLS = RESULTS / "edge_cells"


def _cells(df: pd.DataFrame) -> set[tuple[str, str]]:
    return set(map(tuple, df[["class", "tf"]].drop_duplicates().to_numpy()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--cells-dir", default=str(CELLS),
                    help="directory of per-cell edge_*.csv files")
    a = ap.parse_args()

    if not TARGET.exists():
        raise SystemExit(f"no {TARGET} -- nothing to merge into")
    src = Path(a.cells_dir)
    files = sorted(src.glob("edge_*.csv"))
    if not files:
        raise SystemExit(f"no edge_*.csv under {src}")

    base = pd.read_csv(TARGET)
    print(f"{TARGET.name}: {len(base):,} rows, {len(_cells(base))} cells")

    new = []
    for f in files:
        d = pd.read_csv(f)
        cs = _cells(d)
        if len(cs) != 1:
            # One file, one cell. More than one means the run was not scoped the way the
            # driver intends, and splicing it would replace cells nobody asked about.
            print(f"  REFUSE {f.name}: covers {len(cs)} cells {sorted(cs)}")
            return 2
        print(f"  {f.name}: {len(d):,} rows, cell {sorted(cs)[0]}")
        new.append(d)
    add = pd.concat(new, ignore_index=True)

    # The two column sets disagree in two ways and they are not symmetric.
    #
    # A column the CELLS have and the target lacks is the stage having gained a field since
    # the target was written -- `edge_t_bar_source` was added to `metrics.apply_edge_standard`
    # after the 1d/4h run and records which multiplicity bar scored the row. The old rows
    # have no answer to that question, and inventing one ("bonferroni", because that is what
    # it probably was) would be asserting a fact about how a published verdict was computed
    # on no evidence. They get blank, which is what "not recorded" looks like everywhere
    # else in this repo.
    only_new = sorted(set(add.columns) - set(base.columns))
    only_old = sorted(set(base.columns) - set(add.columns))
    if only_new:
        print(f"\n  target gains {len(only_new)} column(s) the cells carry: {only_new}")
        print(f"  the {len(base):,} pre-existing rows get BLANK there — they were scored "
              f"before the column existed and no value for them would be a measurement")
        for c in only_new:
            base[c] = pd.NA
    # The other direction loses information: the cells were scored by code that no longer
    # emits something the record keeps, so splicing them in would blank a real column on
    # exactly the cells being added. That wants a person, not a fill value.
    if only_old:
        print(f"  REFUSE: the cells are missing {len(only_old)} column(s) the record has: "
              f"{only_old}. Splicing would blank them on every added cell.")
        return 2
    add = add[base.columns.tolist()]

    replaced = _cells(add)
    overlap = replaced & _cells(base)
    keep = ~base.set_index(["class", "tf"]).index.isin(list(replaced))
    print(f"\n  replacing {len(overlap)} existing cell(s): {sorted(overlap) or 'none'}")
    print(f"  adding    {len(replaced - overlap)} new cell(s): "
          f"{sorted(replaced - overlap) or 'none'}")
    print(f"  keeping   {int(keep.sum()):,} of {len(base):,} rows untouched")

    out = pd.concat([base[keep], add], ignore_index=True)
    out = out.sort_values(["class", "tf", "rule", "side"], kind="stable")
    print(f"\n  result: {len(out):,} rows, {len(_cells(out))} cells")
    for (c, t), n in out.groupby(["class", "tf"]).size().items():
        print(f"     {c:12s} {t:4s} {n:>5,}")

    if a.dry_run:
        print("\n  --dry-run: wrote nothing")
        return 0
    # The file this project's verdict lives in. Keep the previous one next to it: the two
    # documented clobbers were only recoverable because a copy happened to exist.
    shutil.copy2(TARGET, TARGET.with_suffix(".csv.bak"))
    out.to_csv(TARGET, index=False)
    print(f"\n  wrote {TARGET}  (previous -> {TARGET.name}.bak)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
