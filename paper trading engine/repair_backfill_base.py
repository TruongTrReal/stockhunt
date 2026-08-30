r"""Re-zero a session whose curve was trimmed at the front, and report what moved.

    python repair_backfill_base.py --session 53                 # what it would change
    python repair_backfill_base.py --session 53 --apply         # change it

**The defect this repairs.** `equity_pct` is percent from the start of its own session and
`store.lifetime_curve` chains on that promise. `backfill_books.trim_before` deletes the
rows a reconstruction produced BEFORE the window it was asked for — it has to, because the
rule needs hundreds of bars of warm-up and it trades through them — and until
`store.rebase_session` existed it deleted the bars without removing their RETURN. Every
surviving row still carried the warm-up's P&L, so the record opened on a jump nobody
earned and every figure downstream inherited it.

It is invisible from inside the series. From the first surviving bar on, the curve is
internally consistent, its shape is right, its drawdowns are right, and only its ZERO is
wrong. The only thing that gives it away is the rule this script checks: **a session's
first curve point is 0.00 by construction**, because that point IS the session's start.

**Scoped to one session on purpose.** Small non-zero bases exist on ordinary live sessions
— a restart replays warm-up bars and the first point the desk publishes can land after the
book has already moved — and those are the record, not an artifact. Rebasing them would
rewrite live history to fix a reconstruction's bug. Name the session.

`backfill_books` now rebases as part of `trim_before`, so this is for records written
before it did.
"""

from __future__ import annotations

import argparse
import sys

import paper_config                  # noqa: F401  (wires sys.path)
import store


def offenders(session_id: int) -> list[tuple[str, float, float, int]]:
    """Every sid in this session whose first point is not zero."""
    conn = store.connect()
    sids = [r[0] for r in conn.execute(
        "SELECT DISTINCT sid FROM curve WHERE session_id = ? ORDER BY sid", (session_id,))]
    out = []
    for sid in sids:
        row = conn.execute("""SELECT equity_pct, bench_pct FROM curve
                              WHERE sid = ? AND session_id = ? ORDER BY ts LIMIT 1""",
                           (sid, session_id)).fetchone()
        if row is None:
            continue
        e0, b0 = float(row[0] or 0.0), float(row[1] or 0.0)
        if abs(e0) < 1e-9 and abs(b0) < 1e-9:
            continue
        n = conn.execute("SELECT COUNT(*) FROM curve WHERE sid = ? AND session_id = ?",
                         (sid, session_id)).fetchone()[0]
        out.append((sid, e0, b0, n))
    return out


def last_of(sid: str, session_id: int) -> float:
    conn = store.connect()
    row = conn.execute("""SELECT equity_pct FROM curve
                          WHERE sid = ? AND session_id = ? ORDER BY ts DESC LIMIT 1""",
                       (sid, session_id)).fetchone()
    return float(row[0]) if row else 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", type=int, required=True,
                    help="the session to re-zero. Name it; this is not swept over the file")
    ap.add_argument("--apply", action="store_true",
                    help="without this it reports and changes nothing")
    args = ap.parse_args(argv)

    rows = offenders(args.session)
    if not rows:
        print(f"session {args.session}: every curve already starts at zero. Nothing to do.")
        return 0

    print(f"session {args.session}: {len(rows)} curves do not start at zero\n")
    print(f"{'leg':<62} {'base':>9} {'total was':>10} {'becomes':>10}")
    for sid, e0, _b0, _n in sorted(rows, key=lambda r: -abs(r[1])):
        was = last_of(sid, args.session)
        becomes = ((1 + was / 100) / (1 + e0 / 100) - 1) * 100
        print(f"{sid[-60:]:<62} {e0:>+8.2f}% {was:>+9.2f}% {becomes:>+9.2f}%")

    if not args.apply:
        print("\nreporting only — pass --apply to write. Back the file up first: this "
              "rewrites a record and there is no undo.")
        return 0

    moved = 0
    for sid, _e0, _b0, _n in rows:
        moved += store.rebase_session(sid, args.session)
    print(f"\nrebased {len(rows)} curves, {moved} points rewritten.")
    left = offenders(args.session)
    print(f"re-checked: {len(left)} still non-zero"
          + ("" if not left else " — " + ", ".join(s for s, *_ in left)))
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
