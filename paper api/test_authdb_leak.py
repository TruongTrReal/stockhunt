"""The connection registry must not grow with threads that have died.

This guards a bug that took the public board down twice: `authdb` hands out one SQLite
connection per thread and used to keep every one of them in a list forever. FastAPI runs
sync endpoints on anyio worker threads that expire after ~10s idle, so a quiet board — one
tab polling `index.html` once a minute — got a fresh thread, and therefore a fresh
connection, on almost every request. Nothing ever closed them. The process reached the
1024 file-descriptor limit in about seven hours and then failed every request that had to
open a file, including serving the board's own `index.html`.

**The assertion is on the registry, not on file descriptors, and that is deliberate.**
Windows has no comparable per-process fd ceiling, so the failure itself cannot be
reproduced on the machine this repo is developed on — but the registry growth that causes
it is plain Python and reproduces anywhere. Counting `_open` is what makes a
Linux-only production bug testable on a Windows dev box.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("STOCKHUNT_API_STATE", tempfile.mkdtemp(prefix="authdb-leak-"))

import authdb                                                    # noqa: E402


def _connect_on_a_short_lived_thread() -> None:
    """Exactly what an expired anyio worker did: connect, then exit."""
    t = threading.Thread(target=authdb.connect)
    t.start()
    t.join()


def test_dead_threads_do_not_accumulate_connections():
    authdb.connect()
    for _ in range(25):
        _connect_on_a_short_lived_thread()
    # One entry for this thread; the 25 dead ones are reaped on the next cache miss.
    assert len(authdb._open) <= 2, (
        f"{len(authdb._open)} connections retained after 25 short-lived threads — "
        f"the registry is leaking again")


def test_the_live_thread_keeps_its_own_connection():
    """Reaping must never close a connection whose thread is still running."""
    conn = authdb.connect()
    for _ in range(5):
        _connect_on_a_short_lived_thread()
    assert authdb.connect() is conn
    conn.execute("SELECT 1").fetchone()          # raises if it was closed underneath us


def test_close_still_reaches_every_thread():
    """The registry exists so `close()` can reach other threads' handles. Still must."""
    authdb.connect()
    done = threading.Event()
    holder: list = []

    def hold():
        holder.append(authdb.connect())
        done.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    while not holder:
        pass
    assert len(authdb._open) >= 2                # this thread's and the live holder's
    authdb.close()
    assert authdb._open == {}
    done.set()
    t.join()
