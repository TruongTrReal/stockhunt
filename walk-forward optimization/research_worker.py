"""Drain the research queue: score a submitted strategy and put its row on the board.

`paper api/api_research.py` writes a job; this reads it, runs the same three stages a
person would run by hand, and inserts the resulting rows into `results.db`. The board is a
query, so the rule is on it at the next request — nothing is rebuilt and nothing is
published.

This is `desk_control` draining `deskdb.orders`, one pipeline over. Neither process calls
the other, neither imports the other, and if the API is down, wedged or compromised this
keeps scoring whatever was already queued.

    claim (BEGIN IMMEDIATE)  ->  causality gate  ->  register the trial
                             ->  strat_wf  ->  riskmatch_wf  ->  portfolio_wf
                             ->  insert rows  ->  scored

Three properties this file is responsible for
----------------------------------------------
**Nothing is scored before it is proved causal.** A code submission is written into
`strategies/published/` and then `strategies/tests/test_causality.py --rules <name>` is
run against it. A nonzero exit rejects the job with the failure attached and the file is
removed again. The gate tests by TRUNCATION rather than by reading the code, which is the
only method that catches the class of defect that matters here — a whole-series
`np.nanmedian` survived review in this repo long enough to contaminate two published
stages, and truncation is what found it. An agent submitting a rule that peeks at the
future would otherwise top this leaderboard instantly, and it would look like the best
result the project has ever produced.

**Nothing is scored before it is registered as a trial.** `strategies.trials.register`
writes the label into `data/reference/trials.csv` before the first stage runs. An open
leaderboard *is* a search: the best of N worthless candidates reaches about
`sigma*sqrt(2 ln N)` by luck, so a result is only interpretable against an honest N, and a
count taken from whatever files happen to be on disk is wrong in the flattering direction
every time. This repo has retracted two findings; both would have been caught by an honest
N. Registering before scoring is the only construction under which the count cannot shrink.

**A scoped run never becomes the sheet of record.** Every stage is invoked WITHOUT its
promote flag, so it writes `*.partial` and the committed sheets are untouched. That is not
timidity: `IS#1`, the noise ceilings and ranking stability are defined over the catalogue
in the run, so a two-rule run's versions of them are a different quantity under the same
filename. The store takes the partial's rows for THIS rule and nothing else.

Known gap: the equity curve
----------------------------
`portfolio_wf --curves` writes one JSON per sheet, so running it here would overwrite the
whole sheet's curves with a single rule's. A newly scored rule therefore has a leaderboard
row and no chart until the next `./run_book.sh` and dashboard build. The row says so
(`curve: false`); it does not draw an invented one.

Run::

    python research_worker.py                 # drain, then exit
    python research_worker.py --watch         # keep draining, one job at a time
    python research_worker.py --once          # exactly one job, for testing
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

from wfo_paths import RESULTS_DIR                    # noqa: F401  (wires sys.path first)
# The second hop, and it is not optional here. `wfo_paths` puts `backtest engine/` on the
# path; importing its `config` is what puts the REPO ROOT there, which is the only reason
# `stockhunt` and `strategies` resolve below. Three hops, and the order matters — see the
# table in ../CLAUDE.md.
import config                                        # noqa: E402,F401

REPO = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
PUBLISHED = REPO / "strategies" / "published"
LOGS = HERE / "logs"
# The interpreter running this one, so a worker started from the repo venv stays in it.
# Resolving `python` off PATH is how a stage ends up running under a different numpy.
PY = sys.executable

from stockhunt import resultsdb                                          # noqa: E402
from strategies import trials                                            # noqa: E402

# How long any one stage may run before it is killed. A walk-forward pass over the biggest
# sheet is ~4 minutes; twenty is wide enough that a cold position cache never trips it and
# narrow enough that a submitted rule which loops forever does not hold the queue.
STAGE_TIMEOUT = 20 * 60

BOOK_STARTS = HERE / "book_rules" / "starts.csv"

# Every stage here prints em-dashes and arrows, and a rejection reason is assembled from a
# stage's own log tail. Windows gives a redirected stdout cp1252, which raises on the first
# one — so the worker died reporting a failure rather than recording it, which is the worst
# possible place to lose an exception. Read with `errors="replace"` and print through UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):        # already wrapped, or not a text stream
        pass


def _log_path(job_id: str) -> Path:
    LOGS.mkdir(parents=True, exist_ok=True)
    return LOGS / f"research_{job_id}.log"


def run_stage(job: dict, name: str, args: list[str]) -> tuple[bool, str]:
    """One pipeline stage, as a subprocess, with its output appended to the job's log.

    A subprocess and not an import, for the same three reasons the pipeline is launched
    from bash: these are `__main__` scripts with their own path bootstraps, they spawn
    multiprocessing pools, and a stage that dies must not take the worker with it.
    """
    resultsdb.mark_job(job["job_id"], "running", stage=name)
    log = _log_path(job["job_id"])
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {name}: {' '.join(args)}\n")
        fh.flush()
        try:
            code = subprocess.call([PY, "-u"] + args, cwd=str(HERE),
                                   stdout=fh, stderr=subprocess.STDOUT,
                                   timeout=STAGE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"{name} exceeded {STAGE_TIMEOUT // 60} minutes and was killed"
    if code != 0:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]
        return False, f"{name} exited {code}: " + " / ".join(t.strip() for t in tail if t.strip())
    return True, ""


# ============================================================ the causality gate

def gate_causality(job: dict, name: str) -> tuple[bool, str]:
    """Prove a submitted module is causal, by truncation, before it may be scored."""
    ok, why = run_stage(job, "causality",
                        [str(REPO / "strategies" / "tests" / "test_causality.py"),
                         "--rules", name])
    if not ok:
        return False, ("rejected by the causality gate — positions on the truncated "
                       "series differ from the full one, so the rule reads bars it could "
                       "not have seen. " + why)
    return True, ""


def install_code(job: dict) -> tuple[bool, str, Path | None]:
    """Write the submitted module into `strategies/published/`.

    Refuses to overwrite an existing file. A strategy name is the primary key of every
    result in this repo — every CSV going back three studies is keyed on the label — so
    silently replacing `ibs` with somebody else's module would rewrite the meaning of
    published numbers rather than adding a candidate.
    """
    name = job["label"]
    path = PUBLISHED / f"{name}.py"
    if path.exists():
        return False, (f"`{name}` already exists in strategies/published/. Pick another "
                       f"name: a label is the key every result in this repo is stored "
                       f"under, so reusing one rewrites history rather than adding to it."), None
    path.write_text(job["code"] or "", encoding="utf-8")
    return True, "", path


# ============================================================ ingesting one rule's rows

def _rows_for(df: pd.DataFrame, label: str, col: str = "rule") -> list[dict]:
    if df is None or df.empty or col not in df.columns:
        return []
    return df[df[col].astype(str) == label].to_dict("records")


def ingest_wf(job: dict, label: str) -> int:
    path = RESULTS_DIR / f"strat_summary_{job['cls']}_{job['tf']}.partial.csv"
    if not path.exists():
        return 0
    rows = _rows_for(pd.read_csv(path), label)
    if rows:
        resultsdb.put_wf(rows, "published")
        resultsdb.put_rules([{"cls": job["cls"], "tf": job["tf"], "rule": label,
                              "kind": "submitted",
                              "family": rows[0].get("family"),
                              "source": rows[0].get("source"),
                              "strategy": rows[0].get("strategy"),
                              "submitted_by": job["account"],
                              "submitted_at": job["submitted_at"],
                              "code_sha": job.get("code_sha")}])
    return len(rows)


def ingest_edge(job: dict, label: str) -> tuple[int, int]:
    """`edge_standard.partial.csv` plus the per-symbol layer behind it.

    Both sides are taken. Which one the board shows is `board_rank`'s decision and it makes
    it on delta-Sharpe; collapsing here would move it somewhere it cannot be revisited.
    """
    n_edge = n_pa = 0
    std = RESULTS_DIR / "edge_standard.partial.csv"
    if std.exists():
        rows = _rows_for(pd.read_csv(std), label)
        rows = [r for r in rows
                if str(r.get("class")) == job["cls"]
                and str(r.get("tf") or r.get("timeframe")) == job["tf"]]
        n_edge = resultsdb.put_edge(rows) if rows else 0

    rm = RESULTS_DIR / "riskmatch.partial.parquet"
    if rm.exists():
        df = pd.read_parquet(rm)
        df = df[(df["class"] == job["cls"]) & (df["tf"] == job["tf"])
                & (df["rule"].astype(str) == label)]
        if not df.empty:
            # Unrounded, exactly as `tools/ingest_results.py` writes them: rounding in the
            # store loses the sign of a negative zero, which the page prints.
            cap = 10_000.0
            n_pa = resultsdb.put_per_asset(pd.DataFrame({
                "cls": df["class"].astype(str), "tf": df["tf"].astype(str),
                "rule": df["rule"].astype(str), "side": df["side"].astype(str),
                "symbol": df["symbol"].astype(str), "src": "riskmatch",
                "ir": df["sharpe_edge"], "years": df["years"],
                "net_cagr": df["causal_cagr"] * 100.0,
                "bh_cagr": df["bench_cagr"] * 100.0,
                "net_pct": (df["causal_wealth"] / cap - 1.0) * 100.0,
                "bench_pct": (df["bench_wealth"] / cap - 1.0) * 100.0,
            }).to_dict("records"))
    return n_edge, n_pa


def ingest_book(job: dict, label: str) -> int:
    """The merged row, read back off the sheet of record.

    `merge_book.py` appends to `book_<cls>_<tf>.csv` and re-runs the panel passes, so the
    row that lands there is the one with a complete verdict. Reading it back rather than
    parsing the merge's output keeps the CSV and the store saying the same thing about the
    same rule — which is the property `tools/test_board_equivalence.py` checks.
    """
    path = RESULTS_DIR / f"book_{job['cls']}_{job['tf']}.csv"
    if not path.exists():
        return 0
    rows = _rows_for(pd.read_csv(path), label)
    return resultsdb.put_book(rows) if rows else 0


def reingest_panel(job: dict) -> int:
    """Re-read the WHOLE book sheet after a merge, not just the new row.

    `merge_book` re-derives four columns that are properties of the panel rather than of a
    row — `vs_random`, `t_bar_maxt`, `n_trials` and the six `edge_*` gates — so adding one
    rule can move the verdict on rules already there. Ingesting only the new row would
    leave the store holding a stale `edge_passed` for everything else, and the board ranks
    on exactly that column.
    """
    path = RESULTS_DIR / f"book_{job['cls']}_{job['tf']}.csv"
    if not path.exists():
        return 0
    return resultsdb.put_book(pd.read_csv(path).to_dict("records"))


# ============================================================ one job

def sheet_trials(cls: str, tf: str) -> int | None:
    """How many candidates this sheet's verdict was scored against.

    Read off the store's own `edge` rows rather than recounted, because it has to be the
    SAME number the rest of the sheet carries — a row deflated against a different N is
    not comparable to the rows it is ranked beside. The maximum, not the median: `n_trials`
    is a property of the search and every row of one sheet should already agree, so a
    disagreement means some rows predate a widening and the larger count is the honest one.
    """
    values = [r.get("n_trials") for r in resultsdb.edge_rows(cls, tf)]
    values = [int(v) for v in values if v is not None and v == v]
    return max(values) if values else None


def book_start(cls: str, tf: str) -> str | None:
    """Fold 0's `is_end` for this sheet — the first bar that was ever out of sample.

    Without it the book is scored over the bars the rules were SELECTED on, and ranking on
    that is ranking on in-sample fit. It comes from `make_book_rules.py`, so a sheet whose
    folds were regenerated without re-running that script has no honest start and this
    refuses rather than guessing one.
    """
    if not BOOK_STARTS.exists():
        return None
    df = pd.read_csv(BOOK_STARTS)
    hit = df[(df["class"] == cls) & (df["tf"].astype(str) == tf)]
    return None if hit.empty else str(hit.iloc[0]["oos_start"])


def process(job: dict) -> tuple[bool, str]:
    """Score one submission. Returns `(ok, reason)`; the reason reaches the submitter."""
    cls, tf, label = job["cls"], job["tf"], job["label"]
    scope = f"{cls}/{tf}"
    installed: Path | None = None
    # Local, not read back off the job row: `finally` runs before the caller marks the job,
    # so asking the store here would see `running` and delete a module that had just passed
    # every stage.
    scored = False

    try:
        if job["kind"] == "code":
            ok, why, installed = install_code(job)
            if not ok:
                return False, why
            job["code_sha"] = hashlib.sha256(
                (job["code"] or "").encode("utf-8")).hexdigest()[:16]
            ok, why = gate_causality(job, label)
            if not ok:
                return False, why

        # The authoritative label check, which the API deliberately does not make: it would
        # cost the HTTP layer a TA-Lib build. Its checks give a caller a fast error; these
        # bind.
        from strategies.registry import CATALOG, SEP
        base = label.split(SEP)[0]
        if base not in CATALOG:
            return False, (f"`{base}` is not in the strategy catalogue. "
                           f"`python strat_wf.py --list` prints what is. Combos "
                           f"(`A~B|and`) come from `combo_wf.py` and are not scored here.")

        # BEFORE the first stage. The whole value of the ledger is that it records intent
        # while the result does not yet exist.
        trials.register(label, scope, author=job["account"],
                        why=f"submitted through /v1/research by {job['account']}",
                        hypothesis="null: submitted candidates are scored against the same "
                                   "matched benchmark as everything else on the sheet and "
                                   "the prior is that none clears the standard")

        ok, why = run_stage(job, "strat_wf",
                            ["strat_wf.py", "--class", cls, "--tf", tf, "--rules", label])
        if not ok:
            return False, why
        n_wf = ingest_wf(job, label)
        if not n_wf:
            return False, (f"strat_wf produced no rankable row for `{label}` on {scope}. "
                           f"Usually the sheet has too few bars for this strategy's "
                           f"warm-up; see the job log.")

        # `--n-trials` is not optional on a scoped run. Left off, the multiplicity
        # correction counts the rules on the command line — one — so every t bar and noise
        # ceiling on the row is understated and the rule is deflated against a search of
        # itself. The honest count is the one the sheet was scored under, which is on
        # `edge_standard`'s own rows.
        args = ["riskmatch_wf.py", "--class", cls, "--tf", tf, "--rules", label]
        n_trials = sheet_trials(cls, tf)
        if n_trials:
            args += ["--n-trials", str(n_trials)]
        ok, why = run_stage(job, "riskmatch_wf", args)
        if not ok:
            return False, why
        n_edge, n_pa = ingest_edge(job, label)
        if not n_edge:
            return False, (f"riskmatch_wf scored no edge row for `{label}` on {scope}. "
                           f"Without one the board drops the rule rather than printing a "
                           f"stale diagnostic beside a missing verdict; see the job log.")

        # `merge_book.py`, not `portfolio_wf.py` directly, and the difference decides
        # whether the row gets a verdict at all. A `--rules` shortlist has no `RANDOM_*`
        # controls in its panel, so `_vs_random` cannot compute criterion R and the row
        # arrives one gate short; merging into a sheet that HAS the controls is what
        # repairs it. It also refuses to write unless it can first reproduce every panel
        # column already on the sheet, which is the check that stops two studies ending up
        # on one row.
        #
        # `book_rules/starts.csv` is still read first — not to pass `--start`, which
        # `merge_book` derives itself, but because its absence means the fold calendar was
        # regenerated without re-running `make_book_rules.py`, and a book scored on the
        # wrong span is ranked on in-sample fit.
        if book_start(cls, tf) is None:
            return False, (f"no out-of-sample start date for {scope} in "
                           f"book_rules/starts.csv — run `make_book_rules.py`. Scoring "
                           f"the book without it would score it on the bars the rule was "
                           f"selected on.")
        ok, why = run_stage(job, "merge_book",
                            ["merge_book.py", "--class", cls, "--tf", tf,
                             "--rules", label])
        if not ok:
            return False, why
        n_book = ingest_book(job, label)
        reingest_panel(job)

        trials.mark_scored(label, scope, outcome="scored through /v1/research")
        scored = True
        return True, (f"{n_wf} walk-forward row, {n_edge} edge row(s), {n_pa} per-asset "
                      f"rows, {n_book} book row")
    except Exception as exc:                        # a stage's own failure is not a crash
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        # A rejected module does not stay in `published/`. Leaving it there would put it in
        # the next full `strat_wf.py` run and on the board by the back door -- and, because
        # the position cache keys on a hash of `strategies/**`, it would also invalidate
        # every cached entry for every sheet.
        if installed is not None and not scored and installed.exists():
            installed.unlink(missing_ok=True)


def drain(limit: int | None = None) -> int:
    """Take jobs until the queue is empty. Returns how many were processed."""
    done = 0
    while limit is None or done < limit:
        job = resultsdb.claim_job()
        if job is None:
            return done
        t0 = time.time()
        print(f"[{job['seq']}] {job['kind']} {job['label']!r} {job['cls']}/{job['tf']} "
              f"for {job['account']}", flush=True)
        ok, why = process(job)
        resultsdb.mark_job(job["job_id"], "scored" if ok else "rejected",
                           stage="done", reason=why)
        print(f"    -> {'scored' if ok else 'REJECTED'} in {time.time() - t0:.0f}s: {why}",
              flush=True)
        done += 1
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--watch", action="store_true",
                    help="keep draining; poll every --every seconds when idle")
    ap.add_argument("--once", action="store_true", help="exactly one job, then exit")
    ap.add_argument("--every", type=float, default=10.0, metavar="SECONDS")
    args = ap.parse_args()

    if args.once:
        return 0 if drain(limit=1) else 0
    if not args.watch:
        n = drain()
        print(f"drained {n} job(s)")
        return 0

    print(f"watching {resultsdb.DB_PATH} every {args.every:.0f}s", flush=True)
    while True:
        try:
            drain()
        except KeyboardInterrupt:
            return 0
        except Exception as exc:                # a bad job must not stop the worker
            print(f"drain failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(args.every)


if __name__ == "__main__":
    raise SystemExit(main())
