"""The results store: one row per scored rule, addressable, so the board can be queried.

Every number on the research leaderboard is computed by a stage in
`walk-forward optimization/` and written to a CSV. That works for a study and fails for a
board, because **a CSV cannot take an insert**. Each stage rewrites its sheet whole —
`strat_wf.py --promote` says so in as many words: *"This REPLACES the sheet of record for
every class and timeframe in scope."* That is why a scoped `--rules` run has to land as
`*.partial.csv`: there is nowhere to put one row.

The consequence is the thing this module exists to remove. A new strategy cannot appear
on the board; somebody has to re-run a stage, then re-run `build_dashboard.py`, and until
they do the page is a snapshot. Scoring a rule is genuinely slow (~32s for one strategy
on us_stocks 1d) and always will be. *Ranking* one is not — it is a join and a sort — and
it was only slow because it had to be done over 131 files by a builder.

So: scoring becomes a job that inserts rows here, and ranking becomes a query. Same seam
`deskdb` already draws between `paper api/` and the desk, for the same reason.

This lives in `stockhunt/` and imports **nothing but the standard library**, so the HTTP
layer can read the board without the trading stack, the walk-forward stages, or pandas
coming along behind it. `stockhunt/deskdb.py` is the file this one is patterned on, down
to the connection machinery; read its notes before changing anything here.

What is in the store and what is still in the sheets
----------------------------------------------------
The stages are **not modified**. They keep writing their CSVs exactly as they always
have, and `tools/ingest_results.py` reads those and upserts rows here. That is what makes
the switch provably safe: the same numbers reach the board by a second route, and
`tools/test_board_equivalence.py` asserts the two routes produce an identical document.

Five tables carry the leaderboard, and they are five because the board is a join across
five different measurements that must never be read as versions of one number:

    rules       what a label IS -- kind, family, provenance, who submitted it
    wf          the walk-forward row: IR, exposure, folds        (wf_/strat_/cwf_summary)
    edge        the six acceptance criteria, per SIDE            (edge_standard.csv)
    book        one account holding the whole universe           (book_<cls>_<tf>.csv)
    per_asset   the rule detail page, name by name               (riskmatch.parquet)

Promoted columns plus a JSON blob, not a column per metric
-----------------------------------------------------------
`book_us_stocks_1d.csv` carries 120 columns and the stages add more every month. A schema
mirroring them needs a migration per stage, and `deskdb._add_late_columns` exists to show
how quickly that becomes a tax. So only the columns that get **filtered or sorted on** are
real columns; the whole row travels as JSON in `doc` beside them.

That is not a shortcut. The promoted set is exactly the ranking key — `edge_passed` then
`cashmatch_excess_cagr` — plus what the query has to filter on, and adding a metric to the
board later costs nothing because it was already stored.

Population statistics are NOT stored, and that is the point
------------------------------------------------------------
`noise_ceiling`, the trial count, `exposure_corr`, ranking stability: every one of them is
defined over the whole population of candidates, so adding a single rule changes them for
every existing row. Baking them into a payload means they go stale silently the moment a
file lands in `strategies/published/` and nobody rebuilds. They are recomputed by the
ranker on every query, from whatever is in the store at that moment, which is the only
construction under which they cannot drift.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from stockhunt import paths

# Beside the sheets it is built from, because that is where anyone looking for a number
# already goes, and because the WFO stages are the processes that write it. Gitignored:
# it is generated from the CSVs and regenerable by `tools/ingest_results.py`, so tracking
# it would put a binary rebuild of committed data into every diff.
DB_PATH = Path(os.environ.get("STOCKHUNT_RESULTS_DB")
               or (paths.WFO_RESULTS / "results.db"))

_lock = threading.RLock()

# ONE CONNECTION PER THREAD. The reasoning is `deskdb`'s and it is not repeated here, but
# it applies with full force: FastAPI runs every sync endpoint on a threadpool worker, so
# the moment the board is polled the interleaving is continuous rather than theoretical,
# and a statement reset mid-read returns a row with empty columns instead of raising.
_local = threading.local()
_open: dict[int, tuple[threading.Thread, sqlite3.Connection]] = {}
_generation = 0

JOB_STATES = ("queued", "running", "scored", "rejected", "failed")
JOB_DONE = ("scored", "rejected", "failed")

SCHEMA = """
-- What a label IS, independent of how it scored. Kept apart from `wf` because a rule
-- keeps its provenance across re-scores, and because a submitted strategy has an author
-- and a code hash that a talib rule does not.
--
--   'single'     one of the 231 TA-Lib rules
--   'pair'       a combo, `MININDEX~SAREXT|and`
--   'published'  a file in strategies/published/
--   'submitted'  arrived through the API and was scored by research_worker
CREATE TABLE IF NOT EXISTS rules (
    cls          TEXT NOT NULL,
    tf           TEXT NOT NULL,
    rule         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    family       TEXT,
    source       TEXT,
    strategy     TEXT,
    submitted_by TEXT,
    submitted_at TEXT,
    code_sha     TEXT,
    PRIMARY KEY (cls, tf, rule)
);

-- The walk-forward leaderboard row, from whichever of the three summary sheets produced
-- it. Scenario is part of the key: a rule is scored on every fee grid and the board reads
-- one of them (`HEADLINE[cls]`), but the others are the cost-headroom evidence.
CREATE TABLE IF NOT EXISTS wf (
    cls         TEXT NOT NULL,
    tf          TEXT NOT NULL,
    rule        TEXT NOT NULL,
    scenario    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    ir_net      REAL,
    long_frac   REAL,
    exposure    REAL,
    years       REAL,
    n_folds     INTEGER,
    rankable    INTEGER,
    is_baseline INTEGER,
    doc         TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (cls, tf, rule, scenario)
);

-- The acceptance standard. BOTH sides are stored and the ranker picks the stronger, which
-- is "short is optional" made concrete -- see `board_rank._edge_index`. Storing only the
-- winning side here would move that decision into ingest, where it cannot be revisited.
CREATE TABLE IF NOT EXISTS edge (
    cls          TEXT NOT NULL,
    tf           TEXT NOT NULL,
    rule         TEXT NOT NULL,
    side         TEXT NOT NULL,
    edge_passed  INTEGER,
    edge_verdict TEXT,
    edge_dsharpe REAL,
    sharpe       REAL,
    years        REAL,
    n_assets     INTEGER,
    n_trials     INTEGER,
    doc          TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (cls, tf, rule, side)
);

-- One account holding the whole point-in-time universe, equal-weighted, over the sheet's
-- out-of-sample span. A SECOND, DIFFERENT measurement from `edge` -- that one is a median
-- across assets -- and the two are not comparable. `cashmatch_excess_cagr` is the
-- leaderboard's tiebreak and `n_trades` is what filters out rules that never open a
-- position, so both are promoted.
CREATE TABLE IF NOT EXISTS book (
    cls                   TEXT NOT NULL,
    tf                    TEXT NOT NULL,
    rule                  TEXT NOT NULL,
    cashmatch_excess_cagr REAL,
    n_trades              INTEGER,
    edge_passed           INTEGER,
    edge_verdict          TEXT,
    edge_powered          INTEGER,
    n_folds_scored        INTEGER,
    doc                   TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    PRIMARY KEY (cls, tf, rule)
);

-- The rule detail page: what each name did.
--
-- BOTH sides are stored, exactly as in `edge`, and the ranker collapses to one. Choosing
-- at ingest would bake in a decision `edge` is allowed to revise -- and the choice must be
-- made at RULE level and never per symbol, because picking the better side per name is
-- selection on the test set. It moved a published median once already: 96 short rows
-- mixed into 518 long ones took `ibs` on us_stocks 1d from $99,735 to $103,670, so the
-- detail page and the leaderboard row above it disagreed about the same rule.
--
-- `src` records which stage supplied the row. `riskmatch` is the measurement the VERDICT
-- was computed in and is authoritative; `wf` and `strat` are a separate, IR-based
-- computation of the same quantity and are the gap-filler for rules the standard never
-- scored. The ranker applies that precedence, so it has to survive ingest.
CREATE TABLE IF NOT EXISTS per_asset (
    cls       TEXT NOT NULL,
    tf        TEXT NOT NULL,
    rule      TEXT NOT NULL,
    side      TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    src       TEXT NOT NULL,
    ir        REAL,
    years     REAL,
    net_cagr  REAL,
    bh_cagr   REAL,
    net_pct   REAL,
    bench_pct REAL,
    PRIMARY KEY (cls, tf, rule, side, symbol)
);

-- Submissions. `seq` is monotonic and the worker drains in `seq` order, so two rules
-- submitted a second apart are scored in the order they were asked for.
--
-- `state` is the WORKER's and only the worker writes it, exactly as `deskdb` splits the
-- API's `want` from the desk's `state`. The API writes the request and then reads.
CREATE TABLE IF NOT EXISTS jobs (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL UNIQUE,
    account      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    label        TEXT NOT NULL,
    cls          TEXT NOT NULL,
    tf           TEXT NOT NULL,
    code         TEXT,
    state        TEXT NOT NULL DEFAULT 'queued',
    stage        TEXT,
    reason       TEXT,
    submitted_at TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);

-- Everything the ranker needs that is not a rule: the gate definitions, each sheet's
-- universe, fold-0 start dates. The gates live here rather than being imported from
-- `backtest engine/config.py` on purpose -- `api_paths` is the one bootstrap that pulls in
-- no trading code, and the HTTP layer starting without the engine is a property worth
-- keeping.
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    doc        TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_wf_sheet    ON wf(cls, tf, scenario);
CREATE INDEX IF NOT EXISTS ix_edge_sheet  ON edge(cls, tf);
CREATE INDEX IF NOT EXISTS ix_book_sheet  ON book(cls, tf);
CREATE INDEX IF NOT EXISTS ix_pa_rule     ON per_asset(cls, tf, rule);
CREATE INDEX IF NOT EXISTS ix_jobs_state  ON jobs(state, seq);
CREATE INDEX IF NOT EXISTS ix_jobs_acct   ON jobs(account, seq);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    """Open the store. Safe to call repeatedly, from either process OR THREAD."""
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "generation", None) == _generation:
        return conn
    with _lock:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None                  # autocommit; `_many` opens its own
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # The worker writes while the API reads, and an ingest run writes a few hundred
        # thousand per-asset rows in one go. Without a busy timeout a read landing inside
        # that commit raises `database is locked` at whoever asked for the board.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        _reap()
        ident = threading.get_ident()
        previous = _open.get(ident)
        if previous is not None:
            try:
                previous[1].close()
            except Exception:
                pass
        _open[ident] = (threading.current_thread(), conn)
        _local.conn = conn
        _local.generation = _generation
        return conn


def _reap() -> None:
    """Close the connections of threads that have exited. Caller must hold `_lock`."""
    for ident, (thread, conn) in list(_open.items()):
        if thread.is_alive():
            continue
        try:
            conn.close()
        except Exception:
            pass
        _open.pop(ident, None)


def use(path: Path | str) -> None:
    """Repoint at another file. For tests, and for nothing else."""
    global DB_PATH
    close()
    with _lock:
        DB_PATH = Path(path)


def close() -> None:
    """Close every thread's connection, not just this one's. See `deskdb.close`."""
    global _generation
    with _lock:
        for _thread, conn in list(_open.values()):
            try:
                conn.close()
            except Exception:
                pass
        _open.clear()
        _generation += 1
    _local.conn = None


# ============================================================ encoding

def jsonable(value):
    """Make a stage's row safe to store, without importing numpy to do it.

    Three things arrive from pandas that `json.dumps` refuses or mangles, and all three
    reach here on every ingest:

    * numpy scalars (`np.float64`, `np.bool_`) -- unwrapped through `.item()`, which every
      one of them has and no builtin needs;
    * `NaN`, which `json.dumps` emits as bare `NaN` and no JSON parser will read back.
      It becomes `None`, which is what a missing metric already means everywhere above;
    * infinities, same treatment and the same reason.

    Duck-typed rather than `isinstance(np.generic)` so this module keeps its promise of
    importing nothing outside the standard library.
    """
    if value is None:
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            return str(value)
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return str(value)


def _doc(row: dict) -> str:
    return json.dumps({str(k): jsonable(v) for k, v in row.items()},
                      separators=(",", ":"))


def _num(row: dict, key: str):
    return jsonable(row.get(key))


def _int(row: dict, key: str):
    v = jsonable(row.get(key))
    return None if v is None else int(v)


def _shape(row: dict | None) -> dict | None:
    """A stored row, as the flat dict the stage wrote.

    **`doc` wins over the promoted columns**, which looks backwards and is not. The doc is
    the stage's row verbatim, and the ranker rebuilds a DataFrame from these dicts and runs
    the same filters it ran against `pd.read_csv` — `df[df.rankable]`, `~df.is_baseline`.
    Those need the column to still be a bool. SQLite has no boolean type, so a promoted
    copy comes back as 0/1 and boolean masking on an integer column raises rather than
    filtering. Preferring the doc keeps every column the dtype `read_csv` gave it, which is
    the whole basis for the equivalence test.

    The promoted columns fill in only where the doc lacks the key, which is what keeps a
    row written before a column was promoted readable.
    """
    if row is None:
        return None
    out = dict(row)
    doc = out.pop("doc", None)
    flat = json.loads(doc) if doc else {}
    for k, v in out.items():
        flat.setdefault(k, v)
    return flat


# The key that makes a cached read safe. `board_rank` memoises a sheet's edge, book and
# per-asset tables — they are read several times while one sheet is ranked — and those
# caches were written for a builder that ran once and exited. Behind an endpoint they are
# a board frozen at the first request, which is the exact failure this whole layer exists
# to remove: a rule is scored, the store has it, and the page goes on showing yesterday.
#
# A counter rather than a timestamp: two writes inside one second are ordinary here, and a
# cache keyed on a second-resolution clock would miss the second one.
_REVISION_KEY = "__revision__"


def _bump() -> None:
    """Invalidate every reader's cache. Called by each write; never by a read."""
    connect().execute(
        f"INSERT INTO meta (key, doc, updated_at) VALUES ('{_REVISION_KEY}', '1', ?)"
        " ON CONFLICT(key) DO UPDATE SET"
        "   doc = CAST(CAST(meta.doc AS INTEGER) + 1 AS TEXT),"
        "   updated_at = excluded.updated_at", (utcnow(),))


def revision() -> int:
    """A number that changes whenever anything in the store does.

    Readers key their caches on it. It is deliberately global rather than per sheet: a
    per-sheet counter is more precise and would have to be right about which sheets a
    write touched, and being wrong about that is silent.
    """
    r = _row(f"SELECT doc FROM meta WHERE key='{_REVISION_KEY}'")
    return int(r["doc"]) if r else 0


def _many(sql: str, rows: list[tuple]) -> int:
    """One transaction for a batch. Ingest writes ~264k per-asset rows and committing per
    row takes minutes rather than seconds."""
    if not rows:
        return 0
    conn = connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(sql, rows)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    _bump()
    return len(rows)


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in connect().execute(sql, args).fetchall()]


def _row(sql: str, args: tuple = ()) -> dict | None:
    r = connect().execute(sql, args).fetchone()
    return dict(r) if r else None


# ============================================================ writes: ingest and worker

def put_rules(rows: list[dict]) -> int:
    """Upsert `rules`. `rows` carry cls/tf/rule/kind and optional provenance."""
    now = utcnow()
    return _many(
        "INSERT INTO rules (cls, tf, rule, kind, family, source, strategy,"
        "                   submitted_by, submitted_at, code_sha)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(cls, tf, rule) DO UPDATE SET"
        "   kind=excluded.kind, family=excluded.family, source=excluded.source,"
        "   strategy=excluded.strategy,"
        # Provenance is only overwritten when the new row HAS it. Re-ingesting the sheets
        # must not blank the author of a rule that arrived through the API.
        "   submitted_by=COALESCE(excluded.submitted_by, rules.submitted_by),"
        "   submitted_at=COALESCE(excluded.submitted_at, rules.submitted_at),"
        "   code_sha=COALESCE(excluded.code_sha, rules.code_sha)",
        [(r["cls"], r["tf"], r["rule"], r.get("kind", "single"),
          r.get("family"), r.get("source"), r.get("strategy"),
          r.get("submitted_by"), r.get("submitted_at") or (now if r.get("submitted_by") else None),
          r.get("code_sha"))
         for r in rows])


def put_wf(rows: list[dict], kind: str) -> int:
    """Upsert walk-forward summary rows. `kind` labels where they came from."""
    now = utcnow()
    return _many(
        "INSERT INTO wf (cls, tf, rule, scenario, kind, ir_net, long_frac, exposure,"
        "                years, n_folds, rankable, is_baseline, doc, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(cls, tf, rule, scenario) DO UPDATE SET"
        "   kind=excluded.kind, ir_net=excluded.ir_net, long_frac=excluded.long_frac,"
        "   exposure=excluded.exposure, years=excluded.years, n_folds=excluded.n_folds,"
        "   rankable=excluded.rankable, is_baseline=excluded.is_baseline,"
        "   doc=excluded.doc, updated_at=excluded.updated_at",
        [(str(r["class"]), str(r["timeframe"]), str(r["rule"]), str(r["scenario"]), kind,
          _num(r, "ir_net"), _num(r, "long_frac"), _num(r, "exposure"), _num(r, "years"),
          _int(r, "n_folds"), _int(r, "rankable"), _int(r, "is_baseline"),
          _doc(r), now)
         for r in rows])


def put_edge(rows: list[dict]) -> int:
    """Upsert `edge_standard` rows. The `tf` column is spelled `tf` in that sheet."""
    now = utcnow()
    return _many(
        "INSERT INTO edge (cls, tf, rule, side, edge_passed, edge_verdict, edge_dsharpe,"
        "                  sharpe, years, n_assets, n_trials, doc, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(cls, tf, rule, side) DO UPDATE SET"
        "   edge_passed=excluded.edge_passed, edge_verdict=excluded.edge_verdict,"
        "   edge_dsharpe=excluded.edge_dsharpe, sharpe=excluded.sharpe,"
        "   years=excluded.years, n_assets=excluded.n_assets, n_trials=excluded.n_trials,"
        "   doc=excluded.doc, updated_at=excluded.updated_at",
        [(str(r["class"]), str(r.get("tf") or r.get("timeframe")), str(r["rule"]),
          str(r["side"]), _int(r, "edge_passed"), str(r.get("edge_verdict") or ""),
          _num(r, "edge_dsharpe"), _num(r, "sharpe"), _num(r, "years"),
          _int(r, "n_assets"), _int(r, "n_trials"), _doc(r), now)
         for r in rows])


def put_book(rows: list[dict]) -> int:
    """Upsert `book_<cls>_<tf>` rows -- the account-level score."""
    now = utcnow()
    return _many(
        "INSERT INTO book (cls, tf, rule, cashmatch_excess_cagr, n_trades, edge_passed,"
        "                  edge_verdict, edge_powered, n_folds_scored, doc, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(cls, tf, rule) DO UPDATE SET"
        "   cashmatch_excess_cagr=excluded.cashmatch_excess_cagr,"
        "   n_trades=excluded.n_trades, edge_passed=excluded.edge_passed,"
        "   edge_verdict=excluded.edge_verdict, edge_powered=excluded.edge_powered,"
        "   n_folds_scored=excluded.n_folds_scored, doc=excluded.doc,"
        "   updated_at=excluded.updated_at",
        [(str(r["class"]), str(r.get("tf") or r.get("timeframe")), str(r["rule"]),
          _num(r, "cashmatch_excess_cagr"), _int(r, "n_trades"), _int(r, "edge_passed"),
          str(r.get("edge_verdict") or ""), _int(r, "edge_powered"),
          _int(r, "n_folds_scored"), _doc(r), now)
         for r in rows])


def put_per_asset(rows: list[dict]) -> int:
    """Upsert the per-name table behind a rule's detail page."""
    return _many(
        "INSERT INTO per_asset (cls, tf, rule, side, symbol, src, ir, years, net_cagr,"
        "                       bh_cagr, net_pct, bench_pct)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(cls, tf, rule, side, symbol) DO UPDATE SET"
        "   src=excluded.src, ir=excluded.ir, years=excluded.years,"
        "   net_cagr=excluded.net_cagr, bh_cagr=excluded.bh_cagr,"
        "   net_pct=excluded.net_pct, bench_pct=excluded.bench_pct",
        [(r["cls"], r["tf"], r["rule"], r.get("side") or "", r["symbol"],
          r.get("src", "riskmatch"), _num(r, "ir"), _num(r, "years"),
          _num(r, "net_cagr"), _num(r, "bh_cagr"),
          _num(r, "net_pct"), _num(r, "bench_pct"))
         for r in rows])


def drop_rule(cls: str, tf: str, rule: str) -> None:
    """Remove every trace of one rule from one sheet.

    The worker calls this before re-scoring, so a rule that used to have a `book` row and
    now does not cannot keep the old one and be ranked on a number nothing recomputed.
    """
    conn = connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in ("wf", "edge", "book", "per_asset", "rules"):
                conn.execute(f"DELETE FROM {table} WHERE cls=? AND tf=? AND rule=?",
                             (cls, tf, rule))
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    _bump()


# ============================================================ reads: the ranker

def sheets() -> list[dict]:
    """Every (class, timeframe) the store has a walk-forward row for."""
    return _rows("SELECT cls, tf, COUNT(*) AS n FROM wf GROUP BY cls, tf ORDER BY cls, tf")


def wf_rows(cls: str, tf: str, scenario: str | None = None) -> list[dict]:
    if scenario is None:
        return [_shape(r) for r in
                _rows("SELECT * FROM wf WHERE cls=? AND tf=?", (cls, tf))]
    return [_shape(r) for r in
            _rows("SELECT * FROM wf WHERE cls=? AND tf=? AND scenario=?",
                  (cls, tf, scenario))]


def edge_rows(cls: str, tf: str) -> list[dict]:
    return [_shape(r) for r in
            _rows("SELECT * FROM edge WHERE cls=? AND tf=?", (cls, tf))]


def book_rows(cls: str, tf: str) -> list[dict]:
    return [_shape(r) for r in
            _rows("SELECT * FROM book WHERE cls=? AND tf=?", (cls, tf))]


def per_asset_rows(cls: str, tf: str, rule: str | None = None) -> list[dict]:
    if rule is None:
        return _rows("SELECT * FROM per_asset WHERE cls=? AND tf=?", (cls, tf))
    return _rows("SELECT * FROM per_asset WHERE cls=? AND tf=? AND rule=?",
                 (cls, tf, rule))


def rule_rows(cls: str, tf: str) -> list[dict]:
    return _rows("SELECT * FROM rules WHERE cls=? AND tf=?", (cls, tf))


def scenarios(cls: str, tf: str) -> list[str]:
    return [r["scenario"] for r in
            _rows("SELECT DISTINCT scenario FROM wf WHERE cls=? AND tf=? ORDER BY scenario",
                  (cls, tf))]


# ============================================================ meta

def set_meta(key: str, doc) -> None:
    connect().execute(
        "INSERT INTO meta (key, doc, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET doc=excluded.doc, updated_at=excluded.updated_at",
        (key, json.dumps(jsonable(doc), separators=(",", ":")), utcnow()))
    # The gates and the headline scenario are read by the ranker, so a change here has to
    # invalidate a cached sheet exactly as a new row does.
    if key != _REVISION_KEY:
        _bump()


def get_meta(key: str, default=None):
    r = _row("SELECT doc FROM meta WHERE key=?", (key,))
    return default if r is None else json.loads(r["doc"])


# ============================================================ jobs

def submit_job(account: str, kind: str, label: str, cls: str, tf: str,
               code: str | None = None) -> dict:
    """Queue a scoring job. Returns the row.

    `kind` is 'label' (a variant in the existing grammar, no new code) or 'code' (a module
    for `strategies/published/`, which the worker gates on causality before it will score).
    """
    if kind not in ("label", "code"):
        raise ValueError(f"kind must be 'label' or 'code', not {kind!r}")
    job_id = uuid.uuid4().hex[:16]
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT INTO jobs (job_id, account, kind, label, cls, tf, code, state,"
            "                  submitted_at) VALUES (?,?,?,?,?,?,?, 'queued', ?)",
            (job_id, account, kind, label, cls, tf, code, utcnow()))
    return job(job_id)                                       # type: ignore[return-value]


def job(job_id: str) -> dict | None:
    return _row("SELECT * FROM jobs WHERE job_id=?", (job_id,))


def jobs(account: str | None = None, state: str | None = None,
         limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM jobs WHERE 1=1"
    args: list = []
    if account is not None:
        sql += " AND account=?"
        args.append(account)
    if state is not None:
        sql += " AND state=?"
        args.append(state)
    return _rows(sql + " ORDER BY seq DESC LIMIT ?", tuple(args + [limit]))


def claim_job() -> dict | None:
    """Take the oldest queued job and mark it running, atomically.

    `BEGIN IMMEDIATE` around the read and the update is what makes two workers safe. It
    is the same construction `deskdb.submit_order` uses and the same reason: a SELECT
    followed by an UPDATE on an autocommit connection is not a transaction, it is a race
    with a comfortable window.
    """
    conn = connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute("UPDATE jobs SET state='running', started_at=? WHERE seq=?",
                         (utcnow(), row["seq"]))
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    return job(row["job_id"])


def mark_job(job_id: str, state: str, *, stage: str | None = None,
             reason: str | None = None) -> None:
    if state not in JOB_STATES:
        raise ValueError(f"unknown job state {state!r}")
    finished = utcnow() if state in JOB_DONE else None
    connect().execute(
        "UPDATE jobs SET state=?, stage=COALESCE(?, stage), reason=COALESCE(?, reason),"
        "                finished_at=COALESCE(?, finished_at) WHERE job_id=?",
        (state, stage, reason, finished, job_id))


def jobs_since(account: str, since_iso: str) -> int:
    """How many jobs an account has queued since `since_iso`. For the rate limit."""
    r = _row("SELECT COUNT(*) AS n FROM jobs WHERE account=? AND submitted_at >= ?",
             (account, since_iso))
    return int(r["n"]) if r else 0


# ============================================================ diagnostics

def summary() -> dict:
    """Row counts per table. `tools/ingest_results.py` prints this when it finishes."""
    out = {"db": str(DB_PATH)}
    for table in ("rules", "wf", "edge", "book", "per_asset", "jobs", "meta"):
        r = _row(f"SELECT COUNT(*) AS n FROM {table}")
        out[table] = int(r["n"]) if r else 0
    return out
