"""`stockhunt.parallel` — the rule loop across cores.

Two properties carry the safety argument and both are tested here: **a pool failure is
loud and complete** (never a short result frame, which is indistinguishable from "those
rules legitimately produced nothing"), and **ordering is preserved** (several result CSVs
are written in loop order, and a parallel run that shuffled them would produce a diff that
looks like a change but is not).

The process pool itself is exercised only through its failure path. Spawning real workers
under pytest on Windows re-imports the test module in each one, which is slow and proves
nothing about this module — what matters is that the fallback catches it.
"""

from __future__ import annotations

import os

import pytest

from stockhunt import parallel

_CTX = None


def _init(value):
    global _CTX
    _CTX = value


def _score(rule):
    return [(rule, _CTX)]


# ------------------------------------------------------------------- worker_count

def test_worker_count_leaves_two_cores_free():
    n = parallel.worker_count(1000)
    assert n == max(1, (os.cpu_count() or 2) - parallel.RESERVED_CORES)
    assert n <= (os.cpu_count() or 2) - 1


def test_worker_count_never_exceeds_the_task_count():
    assert parallel.worker_count(3) <= 3
    assert parallel.worker_count(1) == 1
    assert parallel.worker_count(0) == 1


def test_worker_count_honours_an_explicit_request():
    assert parallel.worker_count(100, requested=4) == 4
    assert parallel.worker_count(2, requested=8) == 2       # still capped by tasks
    assert parallel.worker_count(100, requested=0) == 1     # never zero workers


def test_the_env_switch_overrides_everything(monkeypatch):
    """`STOCKHUNT_WORKERS=1` forces the serial path — how a parallel run is proved to
    produce the same answer as a serial one."""
    monkeypatch.setenv("STOCKHUNT_WORKERS", "1")
    assert parallel.worker_count(1000) == 1
    assert parallel.worker_count(1000, requested=8) == 1


def test_the_env_switch_is_still_capped_by_the_task_count(monkeypatch):
    monkeypatch.setenv("STOCKHUNT_WORKERS", "16")
    assert parallel.worker_count(3) == 3


@pytest.mark.parametrize("bad", ["", "  ", "many", "3.5"])
def test_an_unparseable_env_value_falls_back_rather_than_crashing(monkeypatch, bad):
    monkeypatch.setenv("STOCKHUNT_WORKERS", bad)
    assert parallel.worker_count(100) == parallel.worker_count(100)
    assert parallel.worker_count(100) >= 1


# ---------------------------------------------------------------------- map_rules

def test_small_jobs_run_serially_and_do_not_pay_for_spawn():
    """Windows uses spawn, so each worker reloads a sheet's bars (~1.5s). Below
    `MIN_TASKS` that dominates, so it must not happen."""
    rules = [f"R{i}" for i in range(parallel.MIN_TASKS - 1)]
    rows = parallel.map_rules(rules, _score, _init, ("ctx",), progress=False)
    assert rows == [(r, "ctx") for r in rules]


def test_results_come_back_in_submission_order():
    rules = [f"R{i}" for i in range(10)]
    rows = parallel.map_rules(rules, _score, _init, ("ctx",), progress=False,
                              serial_fn=lambda r: [(r, "ctx")])
    assert [r for r, _ in rows] == rules


def test_rows_are_concatenated_not_nested():
    """`fn` returns a list of rows per rule; the lists flatten into one frame."""
    rows = parallel.map_rules(["a", "b"], None, progress=False,
                              serial_fn=lambda r: [r + "1", r + "2"])
    assert rows == ["a1", "a2", "b1", "b2"]


def test_a_rule_yielding_nothing_contributes_nothing():
    rows = parallel.map_rules(["a", "b", "c"], None, progress=False,
                              serial_fn=lambda r: [] if r == "b" else [r])
    assert rows == ["a", "c"]


def test_serial_fn_is_used_in_the_parent_and_never_touches_the_pool_global():
    """The regression this parameter exists for: `fn` reads a module global only the pool
    initializer sets, so calling it in the parent found that global still None and the
    serial path crashed with `'NoneType' object is not subscriptable` the first time it
    was exercised."""
    global _CTX
    _CTX = None
    rows = parallel.map_rules(["a"], _score, progress=False,
                              serial_fn=lambda r: [(r, "parent-ctx")])
    assert rows == [("a", "parent-ctx")]
    assert _CTX is None                       # the initializer was never run in-process


def test_without_serial_fn_the_initializer_runs_in_process_exactly_once():
    calls = []

    def init(tag):
        calls.append(tag)
        _init(tag)

    rows = parallel.map_rules(["a", "b", "c"], _score, init, ("ctx",), progress=False)
    assert rows == [("a", "ctx"), ("b", "ctx"), ("c", "ctx")]
    assert calls == ["ctx"]                   # once, not once per rule


def test_a_pool_failure_falls_back_to_a_COMPLETE_serial_run(capsys, monkeypatch):
    """The core safety property. A dead executor's natural failure mode is a short result
    frame — some rules missing, no error — which is the exact shape of every bug this
    pipeline has hidden behind. It must re-run everything instead."""
    monkeypatch.delenv("STOCKHUNT_WORKERS", raising=False)
    rules = [f"R{i}" for i in range(parallel.MIN_TASKS + 5)]

    # A lambda cannot be pickled, so `ex.map` raises exactly as a dying pool would.
    rows = parallel.map_rules(rules, lambda r: [(r, "pool")], progress=False, workers=2,
                              serial_fn=lambda r: [(r, "serial")])

    assert len(rows) == len(rules)                          # complete, not partial
    assert [r for r, _ in rows] == rules                    # and still in order
    assert {src for _, src in rows} == {"serial"}
    assert "falling back to serial" in capsys.readouterr().err


def test_an_empty_rule_list_is_not_an_error():
    assert parallel.map_rules([], None, progress=False, serial_fn=lambda r: [r]) == []
