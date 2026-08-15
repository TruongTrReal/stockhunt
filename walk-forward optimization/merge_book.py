"""Add a rule to a book sheet without re-scoring the rules already on it.

`run_book.sh` re-runs every rule on every sheet, which is ~28 minutes for one new
candidate. Almost none of that work is required by the arithmetic: a rule's book is built
from its own positions and the price bars, so nothing about `ibs`'s row depends on
`macd_cross` existing. Only four things on the sheet are properties of the PANEL rather
than of the row, and three of them are pure arithmetic over columns already stored:

    vs_random      interpolated from the RANDOM_* rows' measured exposure and Sharpe.
                   A new candidate does not move it at all -- but a `--rules` run has no
                   controls in its panel, so it cannot compute it either. This is the
                   column that makes a scoped run's verdict provisional, and merging into
                   a sheet that HAS the controls is what fixes it.
    t_bar_maxt     20,000 seeded sign-flips over every candidate's `fold_edges`, which is
                   on the sheet, semicolon-separated. Deterministic: `PERM_SEED`.
    n_trials       a count.
    edge_*         `metrics.apply_edge_standard` re-applied to the six inputs, all of
                   which are row columns.

So the honest update is: score the ONE new rule, append its row, re-run those passes over
the merged panel. That is what this does, and it calls `portfolio_wf`'s own functions to
do it -- `_vs_random`, `_t_bar` and `_standard` are imported, never reimplemented, for the
same reason `curves.py` was deleted. There is one book builder and one panel pass.

What it will NOT re-derive, and this is deliberate
--------------------------------------------------
`dsr`, `psr` and `sr_star_ann` on the rows that were already there. `_deflate` needs the
book's per-bar excess return series (`_excess`, which it pops and never writes), and the
sheet stores moments rather than the series. Rather than reimplement the deflation from
stored moments -- a second copy of `metrics.deflated_sharpe`, free to drift -- the old
rows keep the DSR they were given, deflated against the pre-merge trial count.

That is off by the number of rules you are adding. `expected_max_sharpe` grows like
sqrt(2 ln N), so 405 -> 406 moves the bar by ~0.1%. It is reported at the end of every
run rather than left for someone to discover.

**The new rule's DSR is NOT off**, because this passes `--n-trials` and
`--trial-dispersion` measured from the sheet being merged into. That is the "pass both or
neither" pair the root CLAUDE.md requires, and here both come from the full search rather
than from a shortlist -- which is precisely the case the overrides exist for.

The verdict column does not depend on any of that: `_standard` takes `t_bar_maxt` as its
`t_bar_override`, and `t_bar_maxt` IS recomputed.

Before it writes anything it proves it can reproduce the sheet
--------------------------------------------------------------
`--verify` (on by default) runs the three passes over the EXISTING rows alone and requires
every recomputed column to reproduce what is stored. If it cannot, the sheet was written
by a different version of the code or under different flags, merging into it would be the
cross-study comparison this repo forbids, and the run aborts telling you to use
`run_book.sh`. Same idea as `tools/golden.py`: hash it before, require it after.

Run::

    python merge_book.py --class us_stocks --tf 1d --rules my_rule
    python merge_book.py --class us_stocks --tf 1d --rules my_rule --dry-run
    python merge_book.py --class crypto --tf 1d 4h --rules my_rule other_rule

Then rebuild the dashboard. `make_book_rules.py` reads `edge_standard.csv`, so if you want
the label to survive the next full `run_book.sh` it has to be on that sheet too -- this
tool updates the book sheets, not the standard.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import wfo_paths                                                    # noqa: F401
from wfo_paths import RESULTS_DIR

import portfolio_wf as pwf

HERE = Path(__file__).resolve().parent
STARTS = HERE / "book_rules" / "starts.csv"

# Columns each pass owns. Reproducing these is what `--verify` requires, and re-deriving
# them is the entire job. Anything not listed here is a property of the row and is copied
# across untouched.
PANEL_COLS = {
    "_vs_random": ("rand_sharpe", "vs_random"),
    "_t_bar": ("t_bar_maxt", "t_bar_bonferroni", "n_candidates_maxt"),
    "_standard": ("edge_gate_dsharpe", "edge_gate_t", "edge_gate_vs_random",
                  "edge_gate_vs_constant", "edge_gate_wealth", "edge_gate_headroom",
                  "edge_passed", "edge_n", "edge_powered", "edge_rankable",
                  "edge_verdict", "edge_t_bar_corrected", "edge_t_bar_source",
                  "edge_t_uncorrected_pass"),
}
ALL_PANEL_COLS = tuple(c for cols in PANEL_COLS.values() for c in cols)

# How close a recomputed float has to be to the stored one to count as reproduced. The CSV
# round-trips through text, so exact equality is the wrong test; 1e-9 relative is far
# tighter than any real disagreement would be.
RTOL = 1e-9
ATOL = 1e-12


def _rows_from_csv(path: Path) -> tuple[list[dict], list[str], dict]:
    """The sheet twice: as typed rows to compute on, and as RAW TEXT to write back.

    The raw copy is not a convenience, it is the correctness guarantee. Parsing a column
    to float64 and re-serialising it does not round-trip: `0.13319632586727148` comes back
    as `0.1331963258672714`, a last-ulp change on a value nothing in this run touched. Do
    that to a 409-row sheet and 15,388 cells move — none of them meaningfully, all of them
    showing up as a diff on rows that were never re-scored, which makes it impossible to
    see the cells that DID change and impossible to claim the sheet was left alone.

    So every untouched cell is copied across as the exact bytes it was read as, and only
    the cells this tool actually recomputes are re-rendered.

    `fold_edges` gets one fix on the typed copy: it reads back as NaN where a rule never
    scored one, and `float('nan')` is TRUTHY, so `_t_bar`'s `r.get("fold_edges")` filter
    would admit it and parse "nan" into a length-1 edge vector — padding
    `n_candidates_maxt` with rules that have no edges at all.
    """
    df = pd.read_csv(path)
    cols = list(df.columns)
    rows = df.to_dict("records")
    for r in rows:
        if not isinstance(r.get("fold_edges"), str):
            r["fold_edges"] = ""
    with open(path, newline="", encoding="utf-8") as fh:
        raw = {r["rule"]: r for r in csv.DictReader(fh)}
    return rows, cols, raw


def _fmt(v) -> str:
    """One value as pandas' `to_csv` would have written it.

    Only ever applied to a cell this tool recomputed, so the formatting only has to match
    on the columns the panel passes own — bools, small ints, floats and the two string
    verdict columns.
    """
    if v is None:
        return ""
    if isinstance(v, (bool, np.bool_)):
        return str(bool(v))
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        return "" if not np.isfinite(float(v)) else repr(float(v))
    return str(v)


def _apply_panel(raw: dict, row: dict, stored: dict | None,
                 cols: list[str]) -> tuple[dict, list[str]]:
    """Overwrite the panel cells in a raw text row, and only where they actually moved.

    `stored` is the typed row this raw text came from. Where the recomputed value agrees
    with it, the original TEXT is kept rather than re-rendered — so a row whose panel did
    not move is byte-identical to what was there before, and `git diff` on the sheet shows
    exactly the rules that changed.
    """
    changed = []
    for col in ALL_PANEL_COLS:
        # Membership is tested against the SHEET's columns, not this row's. A row that came
        # from the scoped scoring run has no `vs_random`/`t_bar_maxt` column at all — its
        # panel could not compute them — and skipping on that basis wrote the merged rule
        # onto the leaderboard with its five panel cells blank, which is the one row that
        # most needs them.
        if col not in cols:
            continue
        new = row.get(col)
        if stored is not None:
            old = stored.get(col)
            if isinstance(old, (int, float, np.floating)) and not isinstance(old, bool):
                if not np.isfinite(_num(old)) and not np.isfinite(_num(new)):
                    continue
                if np.isclose(_num(old), _num(new), rtol=RTOL, atol=ATOL, equal_nan=True):
                    continue
            elif str(old) == str(new):
                continue
        raw[col] = _fmt(new)
        changed.append(col)
    return raw, changed


def _n_folds(rows: list[dict], cls: str, tf: str) -> int:
    """The sheet's fold count, which `_standard` needs for `fold_coverage`.

    Not stored as a column, and the obvious guess is wrong: `book_rules/starts.csv` has an
    `n_folds` computed over the FULL history, while the run that wrote the sheet passed
    `--start` and generated its folds over the out-of-sample span alone. us_stocks 1d is
    24 in that file and 21 in the sheet.

    So both candidates are tried and the one that reproduces the stored `edge_powered` on
    every row wins. If neither does, `--verify` fails a moment later with the detail.
    """
    scored = [int(r["n_folds_scored"]) for r in rows
              if np.isfinite(r.get("n_folds_scored", np.nan))]
    cands = [max(scored)] if scored else []
    if STARTS.exists():
        s = pd.read_csv(STARTS)
        hit = s[(s["class"] == cls) & (s.tf.astype(str) == tf)]
        if not hit.empty:
            cands.append(int(hit.iloc[0]["n_folds"]))
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out or [0]


def _repanel(rows: list[dict], n_folds: dict) -> list[dict]:
    """The three panel passes, in `portfolio_wf.main`'s order, minus `_deflate`.

    `_deflate` is skipped rather than called: without `_excess` on the row it silently
    `continue`s, so calling it would look like a re-derivation and do nothing.
    """
    return pwf._standard(pwf._t_bar(pwf._vs_random(rows)), n_folds)


def _compare(before: list[dict], after: list[dict]) -> list[str]:
    """Every panel column that moved, as `rule.column stored -> recomputed`."""
    idx = {r["rule"]: r for r in after}
    out = []
    for b in before:
        a = idx.get(b["rule"])
        if a is None:
            continue
        for col in ALL_PANEL_COLS:
            if col not in b and col not in a:
                continue
            x, y = b.get(col), a.get(col)
            if isinstance(x, (int, float, np.floating)) and not isinstance(x, bool):
                if not np.isfinite(x) and not np.isfinite(_num(y)):
                    continue                       # both undefined is agreement
                if np.isclose(float(x), _num(y), rtol=RTOL, atol=ATOL, equal_nan=True):
                    continue
            elif str(x) == str(y):
                continue
            out.append(f"{b['rule']}.{col}: {x!r} -> {y!r}")
    return out


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def verify(rows: list[dict], cls: str, tf: str) -> int:
    """Reproduce the stored panel columns from the stored inputs, or abort.

    Returns the fold count that worked. Raises SystemExit if none does, because merging
    into a sheet this code cannot reproduce would mix two studies on one row -- the exact
    failure `payload.py` drops unscored rows to prevent.
    """
    best, best_diff = None, None
    for n in _n_folds(rows, cls, tf):
        probe = _repanel(copy.deepcopy(rows), {(cls, tf): n})
        diff = _compare(rows, probe)
        if not diff:
            print(f"  verify: reproduced all {len(ALL_PANEL_COLS)} panel columns on "
                  f"{len(rows)} rows (n_folds={n})")
            return n
        if best_diff is None or len(diff) < len(best_diff):
            best, best_diff = n, diff
    print(f"\n  VERIFY FAILED on {cls} {tf}: {len(best_diff)} column(s) do not reproduce "
          f"(closest n_folds={best})")
    for d in best_diff[:12]:
        print(f"    {d}")
    if len(best_diff) > 12:
        print(f"    ... and {len(best_diff) - 12} more")
    raise SystemExit(
        "\nThis sheet was not written by this version of portfolio_wf, or was written\n"
        "under different flags. Appending to it would put two studies on one row.\n"
        "Re-run ./run_book.sh for this sheet, or pass --no-verify if you know why.")


def _start_for(cls: str, tf: str) -> str:
    if not STARTS.exists():
        raise SystemExit(f"{STARTS} missing -- run make_book_rules.py")
    s = pd.read_csv(STARTS)
    hit = s[(s["class"] == cls) & (s.tf.astype(str) == tf)]
    if hit.empty:
        raise SystemExit(f"no --start for {cls} {tf} in {STARTS}")
    return str(hit.iloc[0]["oos_start"])


def score_new(cls: str, tf: str, rules: list[str], rows: list[dict],
              python: str) -> tuple[list[dict], dict, dict]:
    """Run `portfolio_wf` for the new rules only, with run_book.sh's exact flags.

    Two additions to those flags, and they are the reason the new row does not need
    re-deflating later: `--n-trials` and `--trial-dispersion`, both measured from the
    sheet being merged into. The count is what `_deflate` would have computed on the
    merged panel (its own rule, `len(finite sharpes) + ledger`), and the dispersion is the
    spread of book Sharpes across the whole search rather than across the one rule you
    already believe in.

    `--curves` writes `book_curves_<cls>_<tf>.json` for the rules THIS run scored, which
    would replace a 400-rule file with a 1-rule one. The existing file is moved aside
    first and merged back by the caller; it is never left in the scoped state.
    """
    sharpes = np.array([_num(r.get("sharpe")) for r in rows])
    sharpes = sharpes[np.isfinite(sharpes)]
    try:
        from strategies import trials as _trials
        ledger = _trials.count(f"{cls}/{tf}")
    except Exception:
        ledger = 0
    n_trials = max(int(sharpes.size) + len(rules) + ledger, 2)
    dispersion = float(sharpes.std(ddof=1)) if sharpes.size >= 2 else None

    tmp_out = f"book_merge_tmp_{cls}_{tf}.csv"
    cmd = [python, "-u", "portfolio_wf.py",
           "--class", cls, "--tf", tf, "--pit",
           "--start", _start_for(cls, tf), "--cash-rate", "0",
           "--rules", *rules,
           "--n-trials", str(n_trials),
           "--out", tmp_out, "--curves"]
    if dispersion:
        # Full precision, not %.6f: the round trip through this string is the only place
        # the dispersion could pick up a difference from what a full run would have used.
        cmd += ["--trial-dispersion", f"{dispersion:.17g}"]

    curves = RESULTS_DIR / f"book_curves_{cls}_{tf}.json"
    backup = curves.with_suffix(".json.premerge")
    if curves.exists():
        shutil.copy2(curves, backup)

    print(f"  scoring {len(rules)} rule(s): n_trials={n_trials} "
          f"dispersion={dispersion:.4f}" if dispersion else
          f"  scoring {len(rules)} rule(s): n_trials={n_trials}")
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    if proc.returncode != 0:
        if backup.exists():
            shutil.move(str(backup), str(curves))
        print(proc.stdout[-4000:])
        print(proc.stderr[-4000:])
        raise SystemExit(f"portfolio_wf exited {proc.returncode}")
    print(f"  scored in {time.time() - t0:.0f}s")

    new_rows, _, new_raw = _rows_from_csv(RESULTS_DIR / tmp_out)
    new_curves = json.loads(curves.read_text(encoding="utf-8")) if curves.exists() else {}
    if backup.exists():
        shutil.move(str(backup), str(curves))          # put the full file back
    (RESULTS_DIR / tmp_out).unlink(missing_ok=True)
    return new_rows, new_curves, new_raw


def merge_sheet(cls: str, tf: str, rules: list[str], python: str,
                dry_run: bool, do_verify: bool) -> None:
    sheet = RESULTS_DIR / f"book_{cls}_{tf}.csv"
    if not sheet.exists():
        raise SystemExit(f"{sheet} missing -- there is no sheet to merge into. "
                         f"Run ./run_book.sh for {cls} {tf} first.")
    print(f"\n=== {cls} {tf}")
    rows, cols, raw = _rows_from_csv(sheet)
    print(f"  {sheet.name}: {len(rows)} rows")

    n_folds = verify(rows, cls, tf) if do_verify else _n_folds(rows, cls, tf)[0]

    if dry_run:
        merged = copy.deepcopy(rows)
        new_curves: dict = {}
    else:
        new_rows, new_curves, new_raw = score_new(cls, tf, rules, rows, python)
        raw.update(new_raw)
        got = {str(r["rule"]) for r in new_rows}
        missing = [r for r in rules if r not in got]
        if missing:
            print(f"  ! not scored, skipped: {', '.join(missing)}")
        if not new_rows:
            print("  nothing scored, sheet untouched")
            return
        # Replace rather than append: re-merging a rule you have already merged is a
        # correction, not a duplicate row on the leaderboard.
        replaced = sum(1 for r in rows if str(r["rule"]) in got)
        merged = [r for r in rows if str(r["rule"]) not in got] + new_rows
        print(f"  merged: {len(new_rows)} new row(s), {replaced} replaced "
              f"-> {len(merged)} total")

    before = copy.deepcopy(merged)
    merged = _repanel(merged, {(cls, tf): n_folds})

    # Drift is only meaningful on rows that were NOT re-scored. A newly scored row arrives
    # carrying the panel columns its own scoped run computed, and those are provisional by
    # construction -- a `--rules` panel has no RANDOM_* controls, so `_vs_random` leaves
    # criterion R uncomputable and the row lands a criterion short. Merging is what fixes
    # that, so counting it as "the panel moved" would report the repair as a problem.
    moved = _compare(before, merged)
    untouched = {str(r["rule"]) for r in rows} - {str(r) for r in rules}
    on_old = [d for d in moved if d.split(".", 1)[0] in untouched]
    bar = next((r.get("t_bar_maxt") for r in merged
                if np.isfinite(_num(r.get("t_bar_maxt")))), float("nan"))
    old_bar = next((r.get("t_bar_maxt") for r in before
                    if np.isfinite(_num(r.get("t_bar_maxt")))), float("nan"))
    print(f"  t_bar_maxt {_num(old_bar):.4f} -> {_num(bar):.4f}")
    verdicts = [d for d in on_old if ".edge_verdict" in d or ".edge_passed" in d]
    print(f"  panel columns moved on {len({d.split('.', 1)[0] for d in on_old})} of "
          f"{len(untouched)} untouched row(s); {len(verdicts)} verdict change(s)")
    for d in verdicts[:10]:
        print(f"    {d}")
    gained = [d for d in moved
              if d.split(".", 1)[0] not in untouched and ".edge_passed" in d]
    for d in gained:
        print(f"    (merged row, now scored against the full panel) {d}")

    if dry_run:
        print("  --dry-run: nothing written")
        return

    # Written from the RAW text, not from the DataFrame: see `_rows_from_csv`. Every cell
    # this run did not recompute goes back exactly as it came in.
    stored = {str(r["rule"]): r for r in before}
    touched = 0
    for row in merged:
        name = str(row["rule"])
        rr = raw.get(name)
        if rr is None:
            continue
        rr, ch = _apply_panel(rr, row, stored.get(name), cols)
        touched += len(ch)
    # `main` sorts on (class, tf, cashmatch_excess_cagr desc); a sheet is one (class, tf),
    # so the first two keys are constant here. NaN sorts LAST, which is what
    # `pd.sort_values` does and what the original file therefore looks like.
    def _key(r):
        v = _num(r.get("cashmatch_excess_cagr"))
        return (1, 0.0) if not np.isfinite(v) else (0, -v)
    order = sorted(merged, key=_key)
    with open(sheet, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in order:
            rr = raw.get(str(row["rule"]))
            if rr is not None:
                w.writerow({c: rr.get(c, "") for c in cols})
    print(f"  wrote {sheet.name}  ({len(order)} rows, {touched} panel cell(s) rewritten)")
    df = pd.DataFrame(merged)

    curves = RESULTS_DIR / f"book_curves_{cls}_{tf}.json"
    if new_curves:
        payloads = json.loads(curves.read_text(encoding="utf-8")) if curves.exists() else {}
        payloads.update(new_curves)
        # The same check `portfolio_wf.main` runs: the chart is the row, so a curve that
        # disagrees with the row it was written beside is a bug, and it is loud.
        g = df.set_index("rule")
        bad = []
        for rule, p in new_curves.items():
            if rule not in g.index:
                continue
            r, m = g.loc[rule], p["metrics"]
            for key, col, scale in (("cagr_pct", "cagr", 100.0),
                                    ("sharpe", "sharpe", 1.0),
                                    ("max_dd_pct", "dd", 100.0)):
                a, b = m.get(key), _num(r[col]) * scale
                b_ok = np.isfinite(b)
                if (a is None) != (not b_ok):
                    bad.append(f"{rule}.{key} curve={a} csv={b}")
                elif a is not None and b_ok and abs(a - b) > 0.05:
                    bad.append(f"{rule}.{key} curve={a} csv={b:.4f}")
        curves.write_text(json.dumps(payloads, separators=(",", ":")), encoding="utf-8")
        note = f"  MISMATCH vs csv: {len(bad)} ({'; '.join(bad[:3])})" if bad else ""
        print(f"  wrote {curves.name}  ({len(payloads)} rules){note}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--class", dest="classes", nargs="+", required=True)
    ap.add_argument("--tf", nargs="+", required=True)
    ap.add_argument("--rules", nargs="+", required=True,
                    help="labels to score and merge in")
    ap.add_argument("--dry-run", action="store_true",
                    help="verify and re-derive the panel from the sheet as it stands, "
                         "score nothing, write nothing. This is the honest check that "
                         "the sheet is reproducible before you add to it")
    ap.add_argument("--no-verify", dest="verify", action="store_false",
                    help="skip the reproduction check. Only sensible when you already "
                         "know why it fails")
    ap.add_argument("--python", default=str(Path(sys.executable)),
                    help="interpreter for the scoped portfolio_wf run")
    args = ap.parse_args()

    for cls in args.classes:
        for tf in args.tf:
            merge_sheet(cls, tf, args.rules, args.python, args.dry_run, args.verify)

    if not args.dry_run:
        print("\nDone. Two things this did NOT do:")
        print("  * dsr/psr/sr_star_ann on the PRE-EXISTING rows still carry the trial")
        print("    count they were written with -- re-deriving them needs the per-bar")
        print("    excess series, which the sheet does not store. The added rules were")
        print("    deflated against the merged count via --n-trials, and the VERDICT")
        print("    column does not use either: it takes the recomputed t_bar_maxt.")
        print("  * edge_standard.csv is untouched, so make_book_rules.py will not yet")
        print("    carry the new label. Run riskmatch_wf.py when you want it there.")
        print("\nRebuild the dashboard to see it: "
              "cd '../Stockhunt Dashboard' && python build_dashboard.py --serve --dist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
