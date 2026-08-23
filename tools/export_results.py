"""Write the store back out as CSVs, for everything that still reads a file by name.

`tools/ingest_results.py` runs one way — sheets into `results.db` — and for the pipeline's
own output that is the only direction needed, because the stages keep writing their CSVs
exactly as they always have. This is the other direction, and it exists for one case: a
rule that was **submitted** through `/v1/research` and scored by `research_worker.py`.

That worker deliberately leaves the sheets of record alone. `strat_wf.py` and
`riskmatch_wf.py` are run without their promote flags, so they write `*.partial` — the
committed sheets cannot be rewritten by a one-rule run, because `IS#1`, the noise ceilings
and ranking stability are all defined over the catalogue in the run and a narrowed run's
versions of them are a different quantity under the same filename.

The consequence is a real and deliberate gap: **a submitted rule is on the board and not in
`edge_standard.csv`.** The board is a query, so it sees the store; anything that opens a
file by name does not. `make_book_rules.py` and `validate.py` are the two that matter, and
neither would carry the label until somebody re-runs the full stage.

So this prints the gap by default and only writes when told to. Reporting first is the
point — materialising rows into a sheet of record is a decision about what the study
contains, and it should be made deliberately rather than as a side effect of a tool run.

The book sheets are NOT in the gap, and that is worth knowing: `research_worker` merges
through `merge_book.py`, which appends to `book_<cls>_<tf>.csv` and re-derives the panel
columns, so those stay in step by themselves.

Run::

    python tools/export_results.py                    # what the store has and the CSVs do not
    python tools/export_results.py --write edge       # merge those rows into edge_standard.csv
    python tools/export_results.py --write wf         # ...and into the strat_summary sheets
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO), str(REPO / "Stockhunt Dashboard")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dash_config import WFO_RESULTS                                   # noqa: E402
from stockhunt import resultsdb                                       # noqa: E402

BM = WFO_RESULTS


def _submitted(cls: str, tf: str) -> set[str]:
    """Labels the store knows arrived through the API rather than out of a sweep."""
    return {r["rule"] for r in resultsdb.rule_rows(cls, tf) if r["kind"] == "submitted"}


def gap() -> list[dict]:
    """Rows the store holds that the sheet of record does not.

    Compared on the label alone. A rule whose numbers were re-scored is NOT reported: the
    stage that re-scored it wrote the sheet, so the two agreeing is the normal case and
    diffing every cell would turn a useful report into a float-formatting exercise.
    """
    out = []
    edge = _read(BM / "edge_standard.csv")
    for sheet in resultsdb.sheets():
        cls, tf = sheet["cls"], sheet["tf"]
        submitted = _submitted(cls, tf)
        if not submitted:
            continue
        have_edge = set()
        if not edge.empty:
            col_tf = "timeframe" if "timeframe" in edge.columns else "tf"
            g = edge[(edge["class"] == cls) & (edge[col_tf] == tf)]
            have_edge = set(g["rule"].astype(str))
        strat = _read(BM / f"strat_summary_{cls}_{tf}.csv")
        have_wf = set(strat["rule"].astype(str)) if not strat.empty else set()
        book = _read(BM / f"book_{cls}_{tf}.csv")
        have_book = set(book["rule"].astype(str)) if not book.empty else set()
        for rule in sorted(submitted):
            out.append({"cls": cls, "tf": tf, "rule": rule,
                        "in_edge_csv": rule in have_edge,
                        "in_strat_csv": rule in have_wf,
                        "in_book_csv": rule in have_book})
    return out


def _read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def _backup(path: Path) -> None:
    """Keep the previous sheet beside the new one.

    These are results files, and this is the only tool in the repo that writes one from
    somewhere other than the stage that computed it. A stamped copy costs nothing and is
    the difference between a mistake and a lost study.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    shutil.copy2(path, path.with_suffix(f".before-export-{stamp}.csv"))


def _merge(path: Path, rows: list[dict], key: list[str]) -> int:
    """Append the store's rows to a sheet, replacing any row with the same key."""
    if not rows:
        return 0
    fresh = pd.DataFrame(rows)
    old = _read(path)
    if not old.empty:
        _backup(path)
        drop = set(map(tuple, fresh[key].astype(str).values.tolist()))
        keep = [tuple(map(str, t)) not in drop
                for t in old[key].values.tolist()]
        # Columns the sheet has and the store's rows do not become NaN, which is what a
        # missing metric already means everywhere downstream. The reverse — a new column —
        # widens the sheet, which is why the backup above is not optional.
        fresh = pd.concat([old[keep], fresh], ignore_index=True)
    fresh.to_csv(path, index=False)
    return len(rows)


def write_edge(rules: list[dict]) -> int:
    rows = []
    for r in rules:
        if r["in_edge_csv"]:
            continue
        rows += [e for e in resultsdb.edge_rows(r["cls"], r["tf"])
                 if e["rule"] == r["rule"]]
    # `_shape` returns the stage's own row, so the columns are `edge_standard.csv`'s
    # already. The store's own key columns ride along and are dropped here.
    rows = [{k: v for k, v in e.items()
             if k not in ("cls", "updated_at")} for e in rows]
    return _merge(BM / "edge_standard.csv", rows, ["class", "tf", "rule", "side"])


def write_wf(rules: list[dict]) -> int:
    total = 0
    by_sheet: dict[tuple, list] = {}
    for r in rules:
        if r["in_strat_csv"]:
            continue
        by_sheet.setdefault((r["cls"], r["tf"]), []).append(r["rule"])
    for (cls, tf), labels in by_sheet.items():
        rows = [{k: v for k, v in w.items()
                 if k not in ("cls", "tf", "kind", "updated_at")}
                for w in resultsdb.wf_rows(cls, tf) if w["rule"] in set(labels)]
        total += _merge(BM / f"strat_summary_{cls}_{tf}.csv", rows,
                        ["class", "timeframe", "rule", "scenario"])
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", choices=["edge", "wf", "both"], default=None,
                    help="materialise the missing rows into the sheet of record. "
                         "Backs the sheet up first")
    args = ap.parse_args()

    rules = gap()
    if not rules:
        print("nothing submitted through the API; the sheets and the store agree")
        return 0

    print(f"{len(rules)} submitted rule(s):\n")
    print(f"  {'sheet':<18} {'rule':<28} {'edge':>6} {'strat':>6} {'book':>6}")
    for r in rules:
        print(f"  {r['cls'] + '/' + r['tf']:<18} {r['rule']:<28} "
              f"{'yes' if r['in_edge_csv'] else 'NO':>6} "
              f"{'yes' if r['in_strat_csv'] else 'NO':>6} "
              f"{'yes' if r['in_book_csv'] else 'NO':>6}")

    if args.write is None:
        print("\nThe board reads the store and shows these already. A `NO` above means a "
              "\ntool that opens the file by name — `make_book_rules.py`, `validate.py` — "
              "\ndoes not. Pass --write to change that, deliberately.")
        return 0

    if args.write in ("edge", "both"):
        print(f"\nedge_standard.csv: +{write_edge(rules)} rows")
    if args.write in ("wf", "both"):
        print(f"strat_summary_*.csv: +{write_wf(rules)} rows")
    print("\nRe-run `python tools/ingest_results.py` so the store and the sheets agree, "
          "then `python tools/test_board_equivalence.py verify`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
