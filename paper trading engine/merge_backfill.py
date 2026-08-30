r"""Fold per-leg reconstructions into one record.

    python merge_backfill.py --from-dir /path/to/bf --into /path/to/paper.db
    python merge_backfill.py --from-dir /path/to/bf --into paper.db --dry-run

`backfill_books.py` writes one database per leg, because forty-five concurrent writers on
one SQLite file is a lock storm and because a failed leg should not be entangled with the
rest. This puts them together.

Three things it has to get right, and each of them is a way to corrupt a record rather
than merely to produce a wrong number:

**Session ids are local to their file.** Every per-leg database numbered its own session
1, and `curve.session_id` points at it. Copied across unchanged they would all claim the
target's session 1 — some other day's run. The whole merge lands under ONE new session
row, and every point is remapped onto it.

**Nothing is overwritten.** Both tables carry a natural-key UNIQUE — `(sid, ts)` for a
curve point, `(sid, ts, symbol, side, qty, price, ref)` for a fill — and every insert is
`OR IGNORE`. So a window that overlaps what the desk already traded keeps the DESK's row
and drops the reconstruction's. Re-running the merge changes nothing the second time.

**`first_seen` has to move, or the page still says the record starts today.** It is what
the board prints as `since`, and a reconstruction that does not move it is invisible to
the one line a reader checks first.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

CURVE_COLS = "sid, session_id, ts, equity_pct, bench_pct"
FILL_COLS = ("sid, ts, symbol, side, qty, price, book_pnl, realised_pnl, ref"
             if True else "")


def _fill_columns(conn: sqlite3.Connection) -> list[str]:
    """Whatever this database's `fills` actually has.

    The table gained `symbol`, `ref` and `realised_pnl` across two migrations, and a merge
    that names columns a file does not carry fails on the first row. Read them.
    """
    return [r[1] for r in conn.execute("PRAGMA table_info(fills)")]


def merge_one(src: Path, dst: sqlite3.Connection, session_id: int,
              dry: bool) -> dict:
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    s.row_factory = sqlite3.Row
    try:
        cols = [c for c in _fill_columns(s) if c in set(_fill_columns_of(dst))]
        rows = s.execute("SELECT sid, ts, equity_pct, bench_pct FROM curve "
                         "ORDER BY ts").fetchall()
        fills = s.execute(f"SELECT {', '.join(cols)} FROM fills ORDER BY ts").fetchall()
        strat = s.execute("SELECT * FROM strategies").fetchall()
    finally:
        s.close()

    if dry:
        return {"curve": len(rows), "fills": len(fills),
                "first": rows[0]["ts"] if rows else None,
                "last": rows[-1]["ts"] if rows else None}

    added_c = added_f = 0
    for r in rows:
        cur = dst.execute(
            f"INSERT OR IGNORE INTO curve ({CURVE_COLS}) VALUES (?,?,?,?,?)",
            (r["sid"], session_id, r["ts"], r["equity_pct"], r["bench_pct"]))
        added_c += cur.rowcount
    ph = ",".join("?" * len(cols))
    for f in fills:
        cur = dst.execute(
            f"INSERT OR IGNORE INTO fills ({', '.join(cols)}) VALUES ({ph})",
            tuple(f[c] for c in cols))
        added_f += cur.rowcount

    # The record now starts earlier than the row says it does, and the earliest CURVE
    # POINT is what says when — not the source row's own , which is when the
    # reconstruction was RUN. Taking that one moved  to today and the board went on
    # reporting a record that starts the day the basket was created, which is the single
    # line a reader checks.
    if rows:
        earliest = rows[0]["ts"]
        dst.execute("""UPDATE strategies SET first_seen = ?
                       WHERE sid = ? AND first_seen > ?""",
                    (earliest, rows[0]["sid"], earliest))
    return {"curve": added_c, "fills": added_f,
            "first": rows[0]["ts"] if rows else None,
            "last": rows[-1]["ts"] if rows else None}


def _fill_columns_of(conn: sqlite3.Connection) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(fills)")]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from-dir", required=True)
    ap.add_argument("--into", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    srcs = sorted(Path(args.from_dir).glob("*.db"))
    if not srcs:
        print(f"no per-leg databases under {args.from_dir}")
        return 2

    dst = sqlite3.connect(args.into)
    dst.row_factory = sqlite3.Row
    session_id = None
    if not args.dry_run:
        earliest = "9999"
        for src in srcs:
            s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            row = s.execute("SELECT MIN(ts) FROM curve").fetchone()
            s.close()
            if row and row[0]:
                earliest = min(earliest, row[0])
        cur = dst.execute(
            "INSERT INTO sessions (started_at, ended_at, pid) VALUES (?,?,?)",
            (earliest, None, None))
        session_id = cur.lastrowid

    total_c = total_f = 0
    spans = []
    for src in srcs:
        out = merge_one(src, dst, session_id, args.dry_run)
        total_c += out["curve"]
        total_f += out["fills"]
        if out["first"]:
            spans.append((out["first"], out["last"]))
        print(f"  {src.stem[-52:]:<54} {out['curve']:>5} points {out['fills']:>5} fills")

    if not args.dry_run:
        dst.commit()
    dst.close()

    verb = "would add" if args.dry_run else "added"
    print(f"\n{verb} {total_c} curve points and {total_f} fills across {len(srcs)} legs")
    if spans:
        print(f"window {min(s[0] for s in spans)[:10]} .. {max(s[1] for s in spans)[:10]}")
    if args.dry_run:
        print("dry run — nothing written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
