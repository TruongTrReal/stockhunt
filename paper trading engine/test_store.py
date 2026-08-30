"""Does the desk actually survive a restart? Run: python test_store.py

Not a unit-test suite — one end-to-end check of the property the whole design exists for,
against a throwaway database. It simulates three process lifetimes and asserts that the
record accumulates instead of resetting, that a warm-up replay does not double-count, and
that a gap contributes 0 to the strategy and the real move to the benchmark.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Point the store at a scratch file BEFORE anything imports it for real.
_tmp = Path(tempfile.mkdtemp(prefix="stockhunt-store-test-"))
os.environ["STOCKHUNT_PUBLISH_DIR"] = ""          # publish nothing during the test

import store                                                            # noqa: E402
store.DB_PATH = _tmp / "paper.db"

import paper_state                                                      # noqa: E402
paper_state.STATE_PATH = _tmp / "paper_state.json"
paper_state.MIRROR_PATH = None

SID = "soxl-1d-sma_200"
FIELDS = dict(symbol="SOXL", cls="us_etfs", tf="1d", rule="SMA_200",
              venue="SANDBOX", capital=10_000.0, bt_ir=-0.07, bt_years=4.0, note="test")

ok = True


def check(label: str, got, want) -> None:
    global ok
    good = got == want
    ok = ok and good
    print(f"  {'PASS' if good else 'FAIL'}  {label}: got {got!r}, want {want!r}")


def session(fills, points, patch_gap=None, tag=0):
    """One process lifetime: reset, register, emit events."""
    if patch_gap is not None:
        paper_state._record_gap_if_any = patch_gap
    paper_state.reset()
    paper_state.register(SID, **FIELDS)
    for ts, side, qty, price in fills:
        paper_state.push_trade(SID, ts, side, qty, price, 0.0)
    for i, (eq, bn) in enumerate(points):
        # Distinct bar timestamps, as a real feed would supply.
        bar_ts = datetime(2026, 8, 8, tzinfo=timezone.utc) + timedelta(days=tag + i)
        paper_state.push_point(SID, eq, bn, ts=bar_ts.isoformat(timespec="seconds"))
    store.end_session()
    return paper_state._strategies[SID]


print("session 1 — a fresh strategy")
s1 = session(fills=[("2026-08-08 14:00", "BUY", 10, 100.0),
                    ("2026-08-08 15:00", "SELL", 10, 101.0)],
             points=[(0.0, 0.0), (1.0, 0.5), (2.0, 1.0)],
             patch_gap=lambda *a, **k: None, tag=0)
check("fills recorded", store.fill_count(SID), 2)
check("lifetime_trades", s1["lifetime_trades"], 2)
check("curve length", len(s1["paper_curve"]), 3)
check("last equity pct", s1["paper_curve"][-1], 2.0)
check("no breaks yet", s1["curve_breaks"], [])
since_1 = s1["since"]

print("\nsession 2 — restart, replaying the same fills (warm-up)")
# The gap: benchmark moved +3% while the desk was down.
#
# `to_ts` is STATED, not taken from the clock, and that is load-bearing now that a gap
# shorter than one bar is no longer treated as an outage — see `store._missed_a_bar`. The
# bar timestamps here are fixed dates in August 2026, so a `datetime.now()` restart time
# sits a different distance from them on every day the suite is run, and the property
# under test would quietly start depending on today's date.
def gap_after(days: float, bench_pct):
    def record(sid, symbol, tf):
        last = store.last_point(sid)
        if last:
            back = datetime.fromisoformat(last[0]) + timedelta(days=days)
            store.record_gap(sid, last[0], back.isoformat(timespec="seconds"), bench_pct)
    return record


gap_3pct = gap_after(2, 3.0)          # two days down: a real outage on a 1d system

s2 = session(fills=[("2026-08-08 14:00", "BUY", 10, 100.0),      # replayed
                    ("2026-08-08 15:00", "SELL", 10, 101.0),     # replayed
                    ("2026-08-09 14:00", "BUY", 5, 102.0)],      # new
             points=[(0.0, 0.0), (1.0, 1.0)],
             patch_gap=gap_3pct, tag=10)
check("replay did not double-count", store.fill_count(SID), 3)
check("history survived the restart", s2["lifetime_trades"], 3)
check("inception preserved", s2["since"], since_1)
check("one gap recorded", s2["gaps"], 1)
check("curve continues, not restarts", len(s2["paper_curve"]), 5)

# session 1 ended at strat +2.0% / bench +1.0%.
# gap: strat x1.0 (held nothing), bench x1.03.
# session 2 final point is +1.0% strat / +1.0% bench on top of those carries.
strat_expect = round((1.02 * 1.01 - 1) * 100, 4)
bench_expect = round((1.01 * 1.03 * 1.01 - 1) * 100, 4)
check("strategy chained through gap at 0", s2["paper_curve"][-1], strat_expect)
check("benchmark chained through real gap move", s2["bench_curve"][-1], bench_expect)
check("break marked at the join", s2["curve_breaks"], [3])

print("\nsession 3 — a gap whose benchmark could not be measured")
s3 = session(fills=[], points=[(0.0, 0.0), (0.5, 0.5)], tag=20,
             patch_gap=gap_after(2, None))
check("unknown gap is flagged", s3["unknown_gaps"], 1)
check("two gaps total", s3["gaps"], 2)

print("\nsession 4 — a restart between two consecutive bars is NOT an outage")
# The desk restarts far more often than a bar closes: ten sessions over four days, each
# contributing a single daily point. Marking a break at every session boundary made the
# chained curve a row of isolated single-point segments, and the dashboard drew a field of
# dots with no line anywhere — an unbroken record rendered as nothing but breaks. A
# restart is an outage only if the record lost a bar to it.
s4 = session(fills=[], points=[(0.0, 0.0)], tag=22,      # the bar right after session 3's
             patch_gap=gap_after(0.8, 0.0))              # back up well within the day
check("short restart added no break", s4["curve_breaks"], s3["curve_breaks"])
check("...and no new outage", s4["gaps"], 2)
check("its point joined the line", len(s4["paper_curve"]), len(s3["paper_curve"]) + 1)

print("\nsession 5 - a session written ABOUT THE PAST lands in the past")
# `session_id` is an AUTOINCREMENT: the order sessions were CREATED, which is the order
# they RAN only while nothing writes a session about a window that already closed. A
# reconstruction does exactly that, takes the newest id, and the chained record then drew
# weeks of history AFTER the days that followed it. Sessions are ordered by their own first
# timestamp now, and each stays whole.
back_id = store.start_session()
_conn = store.connect()
before = len(store.lifetime_curve(SID)["equity"])
for i, ts in enumerate(["2026-07-01T00:00:00+00:00", "2026-07-02T00:00:00+00:00"]):
    _conn.execute("INSERT OR IGNORE INTO curve (sid, session_id, ts, equity_pct, bench_pct)"
                  " VALUES (?,?,?,?,?)", (SID, back_id, ts, i * 5.0, 0.0))
_conn.commit()
chained = store.lifetime_curve(SID)
check("the older session's points come first", chained["equity"][:2], [0.0, 5.0])
check("...and nothing was lost", len(chained["equity"]), before + 2)

print("\nsession 6 - a curve trimmed at the front is re-zeroed, BY RATIO")
# `backfill_books.trim_before` deletes the warm-up's BARS; without a rebase it leaves the
# warm-up's RETURN in every surviving row, and the record opens on a jump nobody earned.
trim_id = store.start_session()
for ts, pct in [("2026-06-01T00:00:00+00:00", 10.0), ("2026-06-02T00:00:00+00:00", 21.0)]:
    _conn.execute("INSERT OR IGNORE INTO curve (sid, session_id, ts, equity_pct, bench_pct)"
                  " VALUES (?,?,?,?,?)", (SID, trim_id, ts, pct, 0.0))
_conn.commit()
moved = store.rebase_session(SID, trim_id)
after = _conn.execute("SELECT equity_pct FROM curve WHERE sid=? AND session_id=? ORDER BY ts",
                      (SID, trim_id)).fetchall()
check("both points rewritten", moved, 2)
check("it now starts at zero", round(after[0][0], 9), 0.0)
# 1.21 / 1.10 = 1.10 -- a RATIO, not 21 - 10 = 11.
check("the rest is rebased by ratio, not subtraction", round(after[1][0], 6), 10.0)
check("running it twice changes nothing", store.rebase_session(SID, trim_id), 0)

print("\nstore summary:", store.summary())
print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
