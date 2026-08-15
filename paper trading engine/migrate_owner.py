"""Inspect and apply the account migration on `results/paper.db`.

The migration itself lives in `store._migrate` and runs automatically from
`store.connect()`, because several processes open this database — the desk, the dashboard
builder, the tests — and a half-migrated set of readers is a worse failure than a
migration nobody explicitly asked for.

This script exists for the other half of that bargain: a human being able to see what is
about to happen, or what already did, to a file that is **tracked in git** and holds the
forward-test record.

    python migrate_owner.py --check       # what state is the database in? changes nothing
    python migrate_owner.py               # apply (same thing connect() does), then verify
    python migrate_owner.py --verify      # assert the invariants hold, exit nonzero if not

If a migration goes wrong the recovery is `git checkout -- results/paper.db`, which is the
whole reason that file is tracked. Take a look at `--check` before and after.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

import paper_config                        # noqa: F401  (wires sys.path)
import store


def _open_raw() -> sqlite3.Connection:
    """Open WITHOUT going through `store.connect`, which would migrate on the spot.

    `--check` has to be able to report on an unmigrated database, so it cannot use the
    function whose whole job is to leave no unmigrated database behind.
    """
    if not store.DB_PATH.exists():
        raise SystemExit(f"no database at {store.DB_PATH} — nothing to migrate. "
                         f"It is created on the desk's first run.")
    return sqlite3.connect(store.DB_PATH, timeout=10.0)


def describe() -> dict:
    conn = _open_raw()
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        scols = {r[1] for r in conn.execute("PRAGMA table_info(strategies)")}
        fcols = {r[1] for r in conn.execute("PRAGMA table_info(fills)")}
        counts = {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                  for t in ("strategies", "fills", "curve", "gaps", "sessions")}
        unprefixed = int(conn.execute(
            "SELECT COUNT(*) FROM strategies WHERE sid NOT LIKE '%:%'").fetchone()[0])
        accounts = [(r[0], r[1]) for r in conn.execute(
            "SELECT account, COUNT(*) FROM strategies GROUP BY account ORDER BY account"
        ).fetchall()] if "account" in scols else []
        return {"version": version, "target": store.SCHEMA_VERSION,
                "has_account": "account" in scols, "has_kind": "kind" in scols,
                "has_benchmark": "benchmark" in scols,
                "fills_have_symbol": "symbol" in fcols,
                "counts": counts, "unprefixed_sids": unprefixed, "accounts": accounts}
    finally:
        conn.close()


def verify() -> list[str]:
    """The invariants the migration is supposed to establish. Empty list means healthy."""
    conn = _open_raw()
    problems: list[str] = []
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != store.SCHEMA_VERSION:
            problems.append(f"schema is v{version}, expected v{store.SCHEMA_VERSION}")

        bad = int(conn.execute(
            "SELECT COUNT(*) FROM strategies WHERE sid NOT LIKE '%:%'").fetchone()[0])
        if bad:
            problems.append(f"{bad} strategies still carry an unprefixed sid")

        # The account column and the sid prefix are two spellings of one fact. If they can
        # disagree then a member's row is visible to one query and invisible to another,
        # which is the exact failure the prefix exists to prevent.
        mismatch = int(conn.execute(
            "SELECT COUNT(*) FROM strategies "
            "WHERE account <> substr(sid, 1, instr(sid, ':') - 1)").fetchone()[0])
        if mismatch:
            problems.append(f"{mismatch} strategies whose account column disagrees with "
                            f"their own sid prefix")

        # An orphan is a fill, curve point or gap whose strategy was renamed without it.
        for table in ("fills", "curve", "gaps"):
            orphans = int(conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE sid NOT IN "
                f"(SELECT sid FROM strategies)").fetchone()[0])
            if orphans:
                problems.append(f"{orphans} rows in {table} point at no strategy — the "
                                f"prefix was applied to some tables and not others")

        blank = int(conn.execute(
            "SELECT COUNT(*) FROM fills WHERE symbol IS NULL OR symbol = ''").fetchone()[0])
        if blank:
            problems.append(f"{blank} fills carry no symbol; the backfill from "
                            f"`strategies` did not reach them, and deduplication on those "
                            f"rows is now weaker than it looks")
        return problems
    finally:
        conn.close()


def _print(d: dict) -> None:
    print(f"\n  {store.DB_PATH}")
    print(f"  schema v{d['version']} (target v{d['target']})")
    print(f"  columns   account={d['has_account']} kind={d['has_kind']} "
          f"benchmark={d['has_benchmark']} fills.symbol={d['fills_have_symbol']}")
    print("  rows      " + ", ".join(f"{k} {v}" for k, v in d["counts"].items()))
    print(f"  unprefixed sids: {d['unprefixed_sids']}")
    if d["accounts"]:
        print("  accounts  " + ", ".join(f"{a or '(none)'}: {n}" for a, n in d["accounts"]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report the database's state and change nothing")
    ap.add_argument("--verify", action="store_true",
                    help="assert the post-migration invariants; nonzero exit if broken")
    args = ap.parse_args()

    if args.check:
        _print(describe())
        d = describe()
        if d["version"] < d["target"]:
            print("\n  NOT migrated. It will be, automatically, the next time anything "
                  "opens it.\n  Run this without --check to do it now and see the result.")
        return

    if not args.verify:
        before = describe()
        _print(before)
        if before["version"] >= before["target"]:
            print("\n  already at the target schema — nothing to do")
        else:
            print("\n  migrating ...")
            store.connect()          # this is what performs it
            _print(describe())

    problems = verify()
    if problems:
        print("\n  MIGRATION IS NOT SOUND:")
        for p in problems:
            print(f"    - {p}")
        print("\n  Recover with: git checkout -- \"paper trading engine/results/paper.db\"")
        sys.exit(1)
    print("\n  verified: every sid is account-scoped, no orphans, every fill has a symbol")


if __name__ == "__main__":
    main()
