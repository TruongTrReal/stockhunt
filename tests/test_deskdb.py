"""The order ledger between the API and the desk.

Part of the root suite: `stockhunt.deskdb` imports nothing but the standard library, so it
needs no bars, no vendor and no result CSV — the same contract as the rest of `tests/`.

Two properties carry the design and most of these tests exist for them:

* **Idempotency.** The same `client_order_id` twice is one order. This is what makes a
  network timeout safe, and without it every retry is a doubled position.
* **Ordering, and not losing anything.** `seq` is monotonic, the desk drains in `seq`
  order, and the watermark only advances once a batch has actually been applied.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stockhunt import deskdb


@pytest.fixture()
def db(tmp_path):
    deskdb.use(tmp_path / "desk.db")
    deskdb.connect()
    yield deskdb
    deskdb.close()


def _reg(db, account="a7", name="meanrev", **kw):
    return db.register(account, name, kw.pop("cls", "us_stocks"),
                       kw.pop("symbols", ["SPY"]), kw.pop("tf", "1d"),
                       kw.pop("capital", 10_000.0), **kw)


# ------------------------------------------------------------------- registrations

def test_registering_twice_is_one_strategy(db):
    """A deploy script that runs twice must not create two books under one name,
    silently splitting the manager's capital between them."""
    first = _reg(db)
    again = _reg(db)
    assert again["strategy_id"] == first["strategy_id"]
    assert len(db.registrations("a7")) == 1


def test_strategy_id_is_readable_and_account_scoped(db):
    assert _reg(db, "a7", "meanrev")["strategy_id"] == "str_a7_meanrev"
    # The same NAME under a different account is a different strategy.
    assert _reg(db, "c2", "meanrev")["strategy_id"] == "str_c2_meanrev"
    assert len(db.registrations()) == 2
    assert len(db.registrations("a7")) == 1


def test_symbols_round_trip_as_a_list(db):
    r = _reg(db, symbols=["SPY", "QQQ"])
    assert r["symbols"] == ["SPY", "QQQ"]
    assert db.registration(r["strategy_id"])["symbols"] == ["SPY", "QQQ"]


def test_one_account_cannot_touch_anothers_registration(db):
    mine = _reg(db, "a7", "meanrev")["strategy_id"]
    assert db.registration(mine, account="c2") is None
    assert db.set_want("c2", mine, "retired") is False
    assert db.registration(mine)["want"] == "live"
    assert db.set_want("a7", mine, "retired") is True


def test_want_and_state_are_allowed_to_disagree(db):
    """Pausing while the desk is down is not an error — it is the honest state, and the
    page can say 'you asked to pause; it stops at the next tick'."""
    sid = _reg(db)["strategy_id"]
    db.mark_registration(sid, "live")
    assert db.pending_registrations() == []

    db.set_want("a7", sid, "paused")
    pending = db.pending_registrations()
    assert [p["strategy_id"] for p in pending] == [sid]
    assert pending[0]["want"] == "paused" and pending[0]["state"] == "live"


def test_a_retired_registration_stops_being_pending(db):
    sid = _reg(db)["strategy_id"]
    db.mark_registration(sid, "retired")
    db.set_want("a7", sid, "live")          # a late request cannot resurrect it here
    assert db.pending_registrations() == []


def test_a_house_rule_promoted_from_a_backtest_is_just_a_registration(db):
    """Picking a rule off a walk-forward sheet and paper trading it uses the same
    machinery a manager does — one code path, not two."""
    r = db.register("00", "spy-1d-sma_200", "us_stocks", ["SPY"], "1d", 10_000.0,
                    kind="house_rule", rule="SMA_200", benchmark="SPY")
    assert r["kind"] == "house_rule" and r["rule"] == "SMA_200"
    assert r in db.pending_registrations()


# ------------------------------------------------------------------------- orders

def test_the_same_client_order_id_is_one_order(db):
    """The single most important property in the ledger."""
    sid = _reg(db)["strategy_id"]
    first, created = db.submit_order("a7", sid, "coid-1", symbol="SPY", side="buy", qty=10)
    assert created is True

    for _ in range(9):
        again, created = db.submit_order("a7", sid, "coid-1", symbol="SPY",
                                         side="buy", qty=10)
        assert created is False, "a retry created a second order"
        assert again["seq"] == first["seq"]

    assert len(db.orders("a7")) == 1


def test_a_retry_returns_the_original_not_the_retry(db):
    """A client that retried with different fields still gets the order that exists.
    Honouring the second body would let a typo silently replace a live order."""
    sid = _reg(db)["strategy_id"]
    first, _ = db.submit_order("a7", sid, "coid-1", symbol="SPY", side="buy", qty=10)
    again, created = db.submit_order("a7", sid, "coid-1", symbol="QQQ", side="sell",
                                     qty=999)
    assert created is False
    assert again["symbol"] == "SPY" and again["qty"] == 10 and again["side"] == "buy"


def test_two_accounts_may_use_the_same_client_order_id(db):
    """It is unique per account, not globally — one manager's numbering must not be able
    to collide with, or probe for, another's."""
    a = _reg(db, "a7")["strategy_id"]
    c = _reg(db, "c2")["strategy_id"]
    _, first = db.submit_order("a7", a, "order-1", symbol="SPY", side="buy", qty=1)
    _, second = db.submit_order("c2", c, "order-1", symbol="SPY", side="buy", qty=1)
    assert first and second
    assert len(db.orders("a7")) == 1 and len(db.orders("c2")) == 1


def test_orders_are_scoped_to_their_account(db):
    a = _reg(db, "a7")["strategy_id"]
    db.submit_order("a7", a, "coid-1", symbol="SPY", side="buy", qty=1)
    assert db.orders("c2") == []
    assert db.order("c2", "coid-1") is None
    assert db.order("a7", "coid-1") is not None


def test_seq_is_monotonic_and_drain_is_in_order(db):
    """A cancel must never overtake the order it cancels."""
    sid = _reg(db)["strategy_id"]
    for i in range(5):
        db.submit_order("a7", sid, f"coid-{i}", symbol="SPY", side="buy", qty=1)
    db.submit_order("a7", sid, "cancel-2", action="cancel")

    seqs = [o["seq"] for o in db.drain()]
    assert seqs == sorted(seqs)
    assert [o["client_order_id"] for o in db.drain()][-1] == "cancel-2"


# ---------------------------------------------------------------------- the watermark

def test_reading_does_not_consume(db):
    """Draining twice without committing must return the same batch. A watermark
    advanced on read loses every order in flight if the desk dies mid-batch."""
    sid = _reg(db)["strategy_id"]
    db.submit_order("a7", sid, "coid-1", symbol="SPY", side="buy", qty=1)
    assert len(db.drain()) == 1
    assert len(db.drain()) == 1

    db.commit_drain(db.drain()[-1]["seq"])
    assert db.drain() == []


def test_the_watermark_never_goes_backwards(db):
    sid = _reg(db)["strategy_id"]
    for i in range(3):
        db.submit_order("a7", sid, f"coid-{i}", symbol="SPY", side="buy", qty=1)
    db.commit_drain(3)
    db.commit_drain(1)                       # a late or out-of-order commit
    assert db.watermark_seq() == 3
    assert db.drain() == []


def test_new_orders_after_a_commit_are_still_seen(db):
    sid = _reg(db)["strategy_id"]
    db.submit_order("a7", sid, "coid-1", symbol="SPY", side="buy", qty=1)
    db.commit_drain(db.drain()[-1]["seq"])
    db.submit_order("a7", sid, "coid-2", symbol="SPY", side="buy", qty=1)
    assert [o["client_order_id"] for o in db.drain()] == ["coid-2"]


# ------------------------------------------------------------------------ outcomes

def test_the_desk_records_a_fill(db):
    sid = _reg(db)["strategy_id"]
    o, _ = db.submit_order("a7", sid, "coid-1", symbol="SPY", side="buy", qty=10)
    db.mark_order(o["seq"], "filled", filled_qty=10, avg_price=574.2)
    got = db.order("a7", "coid-1")
    assert got["state"] == "filled" and got["filled_qty"] == 10
    assert got["avg_price"] == 574.2 and got["applied_at"]


def test_a_terminal_order_cannot_be_reopened(db):
    """Nautilus can deliver a late event for an order rejected minutes ago. Letting it
    reopen a closed order makes the ledger disagree with the book."""
    sid = _reg(db)["strategy_id"]
    o, _ = db.submit_order("a7", sid, "coid-1", symbol="SPY", side="buy", qty=10)
    db.mark_order(o["seq"], "rejected", reason="not enough cash")
    db.mark_order(o["seq"], "filled", filled_qty=10, avg_price=1.0)
    got = db.order("a7", "coid-1")
    assert got["state"] == "rejected" and got["filled_qty"] == 0


def test_a_partial_fill_can_still_progress(db):
    sid = _reg(db)["strategy_id"]
    o, _ = db.submit_order("a7", sid, "coid-1", symbol="SPY", side="buy", qty=10)
    db.mark_order(o["seq"], "partially_filled", filled_qty=4, avg_price=100.0)
    db.mark_order(o["seq"], "filled", filled_qty=10, avg_price=101.0)
    assert db.order("a7", "coid-1")["state"] == "filled"


# -------------------------------------------------------------------------- staleness

def test_stale_orders_are_findable_before_they_are_executed(db):
    """An order held while the desk was down and filled hours later at a price the
    manager never saw is worse than a rejection: a rejection can be retried."""
    sid = _reg(db)["strategy_id"]
    o, _ = db.submit_order("a7", sid, "old", symbol="SPY", side="buy", qty=1)
    db.connect().execute("UPDATE orders SET submitted_at = ? WHERE seq = ?",
                         ("2020-01-01T00:00:00+00:00", o["seq"]))
    db.submit_order("a7", sid, "fresh", symbol="SPY", side="buy", qty=1)

    stale = db.stale_orders(timedelta(hours=4))
    assert [s["client_order_id"] for s in stale] == ["old"]


def test_a_drained_order_is_never_stale(db):
    """Staleness is about the queue, not about history — an order already applied has
    had its outcome and must not be reconsidered."""
    sid = _reg(db)["strategy_id"]
    o, _ = db.submit_order("a7", sid, "old", symbol="SPY", side="buy", qty=1)
    db.connect().execute("UPDATE orders SET submitted_at = ? WHERE seq = ?",
                         ("2020-01-01T00:00:00+00:00", o["seq"]))
    db.commit_drain(o["seq"])
    assert db.stale_orders(timedelta(hours=4)) == []


# ------------------------------------------------------------------ restart semantics

def test_a_live_registration_is_still_active_after_a_restart(db):
    """The bug this exists for: `pending_registrations` answers "what CHANGED", which is
    the wrong question for a desk that has just come up.

    A book the previous process started reads want=live, state=live — nothing has changed,
    the ledger is satisfied, and the new process holds no strategies at all. The desk came
    up empty and the record said everything was running.
    """
    sid = _reg(db)["strategy_id"]
    db.mark_registration(sid, "live")

    assert db.pending_registrations() == [], "nothing has changed, correctly"
    assert [r["strategy_id"] for r in db.active_registrations()] == [sid], \
        "but it still needs to exist on the desk"


def test_active_excludes_what_should_not_run(db):
    live = _reg(db, name="live-one")["strategy_id"]
    retired = _reg(db, name="retired-one")["strategy_id"]
    rejected = _reg(db, name="rejected-one")["strategy_id"]
    asked_off = _reg(db, name="asked-off")["strategy_id"]

    db.mark_registration(live, "live")
    db.mark_registration(retired, "retired")
    db.mark_registration(rejected, "rejected", reason="no such class")
    db.mark_registration(asked_off, "live")
    db.set_want("a7", asked_off, "retired")

    got = {r["strategy_id"] for r in db.active_registrations()}
    assert got == {live}, got


def test_a_paused_registration_is_still_active(db):
    """Paused means "hold the positions, stop taking orders" — the strategy must still be
    attached, or a resume would have nothing to resume."""
    sid = _reg(db)["strategy_id"]
    db.mark_registration(sid, "live")
    db.set_want("a7", sid, "paused")
    assert [r["strategy_id"] for r in db.active_registrations()] == [sid]


def test_a_retire_the_desk_never_saw_is_still_visible(db):
    """The lossless half of retiring.

    `active_registrations` filters on `want <> 'retired'`, so a retire that lands while the
    desk is DOWN is invisible to the reconcile loop forever after — the loop iterates what
    the process holds, and this process never held it. The row keeps `state='live'` and the
    record goes on claiming a dead book is trading.
    """
    sid = _reg(db, name="asked-off-while-down")["strategy_id"]
    db.mark_registration(sid, "live")
    db.set_want("a7", sid, "retired")

    assert db.active_registrations() == []                 # nothing will start it again
    assert [r["strategy_id"] for r in db.unapplied_retirements()] == [sid]

    db.mark_registration(sid, "retired")
    assert db.unapplied_retirements() == []                # and it converges, once


def test_unapplied_retirements_ignores_what_is_already_done(db):
    for name, state in (("done", "retired"), ("refused", "rejected")):
        sid = _reg(db, name=name)["strategy_id"]
        db.mark_registration(sid, state)
        db.set_want("a7", sid, "retired")
    assert db.unapplied_retirements() == []


# ------------------------------------------------------------------- the heartbeat

def test_a_ledger_no_desk_has_read_has_no_pulse(db):
    """A fresh file and a dead desk are different situations, and the caller has to be able
    to tell them apart — `age_seconds` is None only for the first."""
    p = db.pulse()
    assert p["at"] is None and p["age_seconds"] is None and p["ticks"] == 0


def test_the_pulse_reports_the_age_of_the_last_pass(db):
    now = datetime.now(timezone.utc)
    db.beat(7, node="TRADER-001", at=(now - timedelta(seconds=45)).isoformat(
        timespec="seconds"))

    p = db.pulse(now=now)
    assert p["ticks"] == 7
    assert p["node"] == "TRADER-001"
    assert 44 <= p["age_seconds"] <= 46


def test_beating_overwrites_rather_than_accumulates(db):
    """One row. At a one-second tick an appended pulse would be 86,400 rows a day of
    something nobody reads twice."""
    for i in range(5):
        db.beat(i)
    assert db.connect().execute("SELECT COUNT(*) FROM heartbeat").fetchone()[0] == 1
    assert db.pulse()["ticks"] == 4


def test_a_pulse_written_by_a_test_clock_does_not_read_as_now(db):
    """`beat` takes its timestamp because the desk's clock is a Nautilus clock — under a
    TestClock it is years from the wall clock, and a heartbeat that quietly used
    `datetime.now()` would report a backtest's desk as live today."""
    db.beat(1, at="2019-03-01T00:00:00+00:00")
    assert db.pulse()["age_seconds"] > 86_400


def test_the_pulse_carries_the_last_failure(db):
    """A beating pulse is the loop TURNING, not the loop working.

    The desk guards each lane and carries on past a failure — one malformed row must not
    stop a trading system — so a pass that fails identically every time still completes and
    still beats. Without the reason on it, a desk that is up and getting nowhere reads
    exactly like a healthy one.
    """
    db.beat(1, error="reconcile failed: no such class 'fx'")
    assert db.pulse()["error"] == "reconcile failed: no such class 'fx'"

    db.beat(2)                       # the next pass came good
    assert db.pulse()["error"] is None


def test_a_database_predating_the_error_column_still_opens(db, tmp_path):
    """`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so a
    column added later never reaches a file already in use — and one of the two processes
    opening this one is a live trading node."""
    import sqlite3
    db.close()
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE heartbeat (id INTEGER PRIMARY KEY CHECK (id = 1), at TEXT,
                                ticks INTEGER NOT NULL DEFAULT 0, pid INTEGER, node TEXT);
        INSERT INTO heartbeat (id, ticks) VALUES (1, 41);
    """)
    old.commit()
    old.close()

    deskdb.use(path)
    deskdb.beat(42, error="boom")
    assert deskdb.pulse()["error"] == "boom" and deskdb.pulse()["ticks"] == 42


# ------------------------------------------------------- one connection per THREAD

def test_reads_are_not_corrupted_by_writes_on_other_threads(db):
    """The bug a shared connection causes, and the reason it is expensive to find.

    `check_same_thread=False` makes a single `sqlite3.Connection` look shareable. It is
    not: a read here is an `execute` then a fetch and takes no lock, while every write
    helper holds one — so a write on another thread resets the reader's statement between
    the two. The read comes back with EMPTY COLUMNS instead of raising, and an empty column
    is not obviously wrong to whoever receives it. `symbols` became `''`, `json.loads` threw
    from deep inside `_shape`, and the API answered 500 on a request that was valid.

    FastAPI runs every sync endpoint on a threadpool worker, so a page that polls makes
    this continuous rather than theoretical.
    """
    import threading

    for i in range(20):
        _reg(db, name=f"s{i}")
    sids = [r["strategy_id"] for r in db.registrations("a7")]

    errors: list = []
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            db.mark_registration(sids[i % len(sids)], "live")
            db.beat(i)
            i += 1

    def reader():
        try:
            for _ in range(150):
                for row in db.registrations("a7"):
                    # The shape every caller assumes. Under a shared connection these come
                    # back empty and `_shape` raises before the assert is ever reached.
                    assert isinstance(row["symbols"], list) and row["symbols"] == ["SPY"]
                    assert row["strategy_id"] and row["account"] == "a7"
                for row in db.drain():
                    assert row["seq"] > 0
                assert db.pulse()["ticks"] >= 0
        except Exception as exc:                       # noqa: BLE001 — reported, not raised
            errors.append(exc)

    writers = [threading.Thread(target=writer, daemon=True) for _ in range(2)]
    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in writers + readers:
        t.start()
    for t in readers:
        t.join(timeout=60)
    stop.set()
    for t in writers:
        t.join(timeout=10)

    assert not errors, f"{len(errors)} corrupted read(s), first: {errors[0]!r}"


def test_each_thread_gets_its_own_connection(db):
    import threading
    seen = {}

    def grab(name):
        # The connection object, not its id(). Dead threads' connections are now reaped,
        # and CPython recycles the address of anything it frees — so holding only the id
        # of a closed connection can collide with a later one and read as "these two
        # threads shared", which is the opposite of what happened.
        seen[name] = db.connect()

    threads = [threading.Thread(target=grab, args=(f"t{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = {id(c) for c in seen.values()}
    assert len(ids) == 3, seen
    assert id(db.connect()) not in ids                # and this thread has its own


def test_repointing_reaches_threads_that_already_opened_one(db, tmp_path):
    """A thread caches its connection, so `use()` has to invalidate every OTHER thread's
    too — without the generation bump a worker keeps serving the previous database."""
    import threading

    _reg(db, name="before-the-move")
    opened = threading.Event()
    result = {}

    def worker(step):
        if step == "open":
            db.connect()
            opened.set()
        else:
            result["names"] = [r["name"] for r in db.registrations("a7")]

    t = threading.Thread(target=worker, args=("open",))
    t.start()
    t.join()
    assert opened.is_set()

    deskdb.use(tmp_path / "moved.db")                  # same thread pool, new file
    t2 = threading.Thread(target=worker, args=("read",))
    t2.start()
    t2.join()
    assert result["names"] == []                       # the new database, not the old one


# ------------------------------------------------- removing what never was a record

def test_a_registration_that_never_traded_can_be_removed(db):
    sid = _reg(db, name="typo")["strategy_id"]
    db.mark_registration(sid, "retired")

    deleted, why = db.delete_registration("a7", sid)
    assert deleted and why == ""
    assert db.registration(sid) is None


def test_one_FILLED_order_makes_it_permanent(db):
    """A fill is the record. One of them locks the row forever."""
    sid = _reg(db, name="traded")["strategy_id"]
    o, _ = db.submit_order("a7", sid, "coid-1", symbol="SPY", side="buy", qty=1)
    db.mark_order(o["seq"], "filled", filled_qty=1.0, avg_price=100.0)
    db.mark_registration(sid, "retired")

    deleted, why = db.delete_registration("a7", sid)
    assert not deleted and "1 filled order" in why
    assert db.registration(sid) is not None


def test_orders_that_never_FILLED_do_not_make_it_permanent(db):
    """The bug this replaced, and it stranded exactly the rows the exception is for.

    The test used to be "no orders, ever", which is a PROXY for "recorded nothing" — and
    the proxy comes apart in the one case that matters. A registration the desk REFUSED
    never attached, so it has no instrument and no book, and its orders cannot fill: they
    sit in the ledger as a record of a client talking to something that never existed.
    That is litter, which is what this exception exists for, and it was unremovable
    forever — retiring did not help, because the orders stayed.
    """
    sid = _reg(db, name="refused")["strategy_id"]
    db.submit_order("a7", sid, "coid-1", symbol="NQ.v.0", side="buy", qty=1)
    db.submit_order("a7", sid, "coid-2", symbol="ES.v.0", side="buy", qty=1)
    db.mark_registration(sid, "rejected", "1m is not a bar this desk can feed there")

    deleted, why = db.delete_registration("a7", sid)
    assert deleted, why
    assert "2 orders" in why and "never filled" in why, "say what went with it"
    assert db.registration(sid) is None


def test_a_partial_fill_is_still_a_record(db):
    """`filled_qty > 0`, not `state == 'filled'`. A partial moved real money."""
    sid = _reg(db, name="partial")["strategy_id"]
    o, _ = db.submit_order("a7", sid, "coid-1", symbol="SPY", side="buy", qty=10)
    db.mark_order(o["seq"], "canceled", filled_qty=3.0, avg_price=100.0)
    db.mark_registration(sid, "retired")

    deleted, why = db.delete_registration("a7", sid)
    assert not deleted and "filled" in why


def test_removing_takes_the_unfilled_orders_with_it(db):
    """Otherwise the ledger keeps orders pointing at a `strategy_id` that no longer
    resolves — the same litter under a different name, and still listed by the API."""
    sid = _reg(db, name="refused")["strategy_id"]
    db.submit_order("a7", sid, "coid-1", symbol="NQ.v.0", side="buy", qty=1)
    db.mark_registration(sid, "rejected")

    assert db.delete_registration("a7", sid)[0]
    left = db.connect().execute(
        "SELECT COUNT(*) FROM orders WHERE strategy_id = ?", (sid,)).fetchone()[0]
    assert left == 0


def test_a_live_registration_is_not_removable(db):
    sid = _reg(db, name="running")["strategy_id"]
    db.mark_registration(sid, "live")
    deleted, why = db.delete_registration("a7", sid)
    assert not deleted and "Retire it first" in why


def test_a_house_rule_is_never_removable_on_an_empty_ledger(db):
    """The load-bearing guard. "No orders" proves "never traded" only for a strategy that
    trades ON INSTRUCTION. A house rule trades itself — it fills constantly and submits
    nothing through this ledger, so the same test would read as "never traded" for
    something with months of record behind it."""
    sid = db.register("00", "us_stocks-1d-ibs", "us_stocks", ["SPY"], "1d", 10_000.0,
                      kind="house_rule", rule="ibs")["strategy_id"]
    db.mark_registration(sid, "retired")

    deleted, why = db.delete_registration("00", sid)
    assert not deleted and "house_rule" in why
    assert db.registration(sid) is not None


def test_one_account_cannot_remove_anothers(db):
    sid = _reg(db, account="b3", name="theirs")["strategy_id"]
    db.mark_registration(sid, "retired")
    deleted, why = db.delete_registration("a7", sid)
    assert not deleted and why == "no such strategy"
    assert db.registration(sid) is not None


def test_a_dead_threads_connection_is_reclaimed(db):
    """The registry must not grow with threads that have exited.

    `desk_control` runs on a Nautilus `clock.set_timer` callback, and those fire on a NEW
    thread every tick. Each one misses the thread-local cache and opens a connection, so a
    registry that only ever appends holds one strong reference per tick forever — the
    connection is never collected and its descriptor never closed. On Linux the desk hits
    the 1024-fd soft limit in about eight minutes and then fails every reconcile, drain and
    heartbeat with `unable to open database file`, while systemd still calls the unit
    healthy. Windows has no comparable ceiling, which is why the dev box cannot show it.

    Asserted on the registry rather than on `/proc/self/fd`, so it means the same thing on
    both platforms.
    """
    import threading

    for _ in range(25):
        t = threading.Thread(target=db.connect)
        t.start()
        t.join()

    # The property is BOUNDEDNESS, not emptiness. The sweep runs on a cache miss, so each
    # new thread clears the ones before it; the entry for the most recent thread may still
    # be there, and this thread's own connection certainly is. What must never happen is
    # the registry tracking the number of threads that have ever run.
    assert len(db._open) <= 3, f"{len(db._open)} connections held after 25 threads"
