"""`board_rank`'s memo, now that a long-lived server sits on top of it.

The ranking itself is gated by `tools/test_board_equivalence.py`, which proves the
rendered document is byte-identical across a change. That gate cannot see any of the
properties below, because none of them change a number — they decide *how often* the join
runs and *how much* of it is kept afterwards, and both were written for a builder that ran
once and exited:

* two readers arriving on a cold cache used to run the whole join twice, in parallel, and
  throw one of the answers away. On the deployed two-core box one build is 20.7s;
* `_RM_CACHE`, `_EDGE_CACHE` and `_BOOK_CACHE` are keyed on the store's revision and were
  never swept, so every write to the store added a full second copy and kept the first
  one for the life of the process. `_RM_CACHE` alone holds the per-asset rows for every
  sheet on the board;
* the warmer is background-thread code, which is the kind that fails silently.

Synthetic rows only, in a tmp store, exactly as `test_resultsdb.py` does it.
"""

from __future__ import annotations

import threading
import time

import pytest

import board_rank
from stockhunt import resultsdb


@pytest.fixture(autouse=True)
def store(tmp_path):
    """A fresh database per test, with every module-global put back afterwards.

    Both `resultsdb.DB_PATH` and `board_rank`'s four caches are process-wide, so a test
    that leaves either behind fails a *later* test instead of itself.
    """
    original = resultsdb.DB_PATH
    resultsdb.use(tmp_path / "results.db")
    _clear()
    yield
    board_rank.stop_warmer()
    _clear()
    resultsdb.close()
    resultsdb.use(original)


def _clear() -> None:
    for cache in (board_rank._BOARD_CACHE, board_rank._RM_CACHE,
                  board_rank._EDGE_CACHE, board_rank._BOOK_CACHE):
        cache.clear()


CLS, TF, SCEN = "crypto", "1d", "retail"
SYMBOLS = ["BTC/USD", "ETH/USD"]


def seed(rule="ibs") -> None:
    """The smallest store `build_board` produces a ranked sheet from.

    All four tables, because the leaderboard is a join across them and a sheet missing any
    one of them answers `None` — correctly, and confusingly when it is the fixture that is
    wrong. `single` and not `published` for the same reason: `build_sheet` builds from the
    singles frame and merges the rest onto it.
    """
    resultsdb.set_meta("gates", [{"key": "dsharpe", "letter": "S", "label": "Delta-Sharpe",
                                 "target": ">= 0.10"}])
    resultsdb.set_meta("headline", {CLS: SCEN})
    resultsdb.set_meta("timeframes", [TF])
    resultsdb.set_meta("top_n", 30)
    resultsdb.set_meta("groups", [{"key": "crypto", "cls": CLS, "label": "Crypto",
                                   "universe": SYMBOLS}])
    resultsdb.put_wf([{"class": CLS, "timeframe": TF, "rule": rule, "scenario": SCEN,
                       "ir_net": 0.2, "ir_hit_rate": 0.5, "t_stat": 1.1, "years": 6.0,
                       "n_folds": 6, "rankable": True, "is_baseline": False,
                       "wf_mode": "published", "long_frac": 0.5, "exposure": 0.5,
                       "excess_return_pct": 3.0, "strategy": rule,
                       "family": "reversion", "source": "test"}], "single")
    resultsdb.put_edge([{"class": CLS, "tf": TF, "rule": rule, "side": "long",
                         "edge_dsharpe": 0.2, "edge_t": 1.5, "edge_vs_random": 0.1,
                         "edge_vs_constant": 0.1, "wealth": 12000.0,
                         "bench_wealth": 11000.0, "edge_wealth": 1000.0,
                         "edge_headroom": 2.0, "sharpe": 0.8, "bench_sharpe": 0.6,
                         "max_dd": -0.3, "bench_max_dd": -0.5, "profit_factor": 1.2,
                         "trades_per_asset": 50, "noise_ceiling": 0.4, "exposure": 0.5,
                         "n_assets": 2, "years": 6.0, "n_trials": 10,
                         "edge_passed": 1, "edge_n": 1, "edge_verdict": "partial",
                         "edge_powered": True, "edge_rankable": True,
                         "edge_gate_dsharpe": True}])
    resultsdb.put_book([{"class": CLS, "tf": TF, "rule": rule, "n_trades": 100,
                         "cashmatch_excess_cagr": 1.5, "n_names": 2, "years": 6.0,
                         "wealth": 12000.0, "bench_wealth": 11000.0, "bench_cagr": 2.0,
                         "cagr": 3.5, "edge_passed": 1, "edge_n": 1,
                         "edge_verdict": "partial", "edge_powered": True,
                         "edge_rankable": True, "n_folds_scored": 6,
                         "edge_gate_dsharpe": True}])
    resultsdb.put_per_asset([
        {"cls": CLS, "tf": TF, "rule": rule, "side": "long", "symbol": s,
         "src": "riskmatch", "ir": 0.2, "years": 6.0, "net_cagr": 4.0,
         "bh_cagr": 2.0, "net_pct": 30.0, "bench_pct": 12.0}
        for s in SYMBOLS])


# ------------------------------------------------------------------- the shared build

def test_concurrent_readers_build_the_board_once():
    """N readers on a cold cache pay for ONE join, not N.

    Counted rather than timed: a wall-clock assertion on a build this small would be
    measuring the scheduler. The counter is what the lock is actually for.
    """
    seed()
    builds = []
    real = board_rank.build_sheet

    def slow(*a, **kw):
        builds.append(1)
        time.sleep(0.05)                     # long enough for the others to pile up
        return real(*a, **kw)

    board_rank.build_sheet = slow
    try:
        threads = [threading.Thread(target=board_rank.build_board) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    finally:
        board_rank.build_sheet = real

    assert len(builds) == 1, f"{len(builds)} concurrent builds of one revision"
    assert len(board_rank._BOARD_CACHE) == 1


def test_a_warm_board_is_not_rebuilt():
    seed()
    board_rank.build_board()
    calls = []
    real = board_rank.build_sheet
    board_rank.build_sheet = lambda *a, **kw: calls.append(1) or real(*a, **kw)
    try:
        board_rank.build_board()
    finally:
        board_rank.build_sheet = real
    assert calls == []


def test_a_write_to_the_store_rebuilds_it():
    """The whole point of keying on the revision: freshness must stay exact."""
    seed()
    first = board_rank.build_board()
    seed(rule="macd")
    second = board_rank.build_board()
    assert second is not first
    labels = {r["rule"] for r in second["crypto"]["sheets"][0]["rows"]}
    assert "macd" in labels


# --------------------------------------------------------------- the sheet-level memos

def test_superseded_revisions_are_not_kept():
    """Three writes must not leave three generations of per-asset rows in memory."""
    seed()
    board_rank.build_board()
    after_one = {len(c) for c in (board_rank._RM_CACHE, board_rank._EDGE_CACHE,
                                  board_rank._BOOK_CACHE)}
    for rule in ("macd", "rsi", "sma"):
        seed(rule=rule)
        board_rank.build_board()
    after_four = {len(c) for c in (board_rank._RM_CACHE, board_rank._EDGE_CACHE,
                                   board_rank._BOOK_CACHE)}
    assert after_four == after_one

    rev = resultsdb.revision()
    for cache in (board_rank._RM_CACHE, board_rank._EDGE_CACHE, board_rank._BOOK_CACHE):
        assert all(key[-1] == rev for key in cache), "a stale revision survived"


# --------------------------------------------------------------------- the warmer

def test_the_warmer_builds_the_board_without_a_reader():
    seed()
    assert board_rank._BOARD_CACHE == {}
    board_rank.start_warmer(0.05)
    deadline = time.time() + 10
    while not board_rank._BOARD_CACHE and time.time() < deadline:
        time.sleep(0.02)
    assert board_rank._BOARD_CACHE, "the warmer never built anything"


def test_the_warmer_is_off_at_zero_and_starts_only_once():
    seed()
    assert board_rank.start_warmer(0) is None
    assert board_rank.start_warmer(-1) is None
    first = board_rank.start_warmer(0.05)
    assert first is not None and first.daemon
    assert board_rank.start_warmer(0.05) is first


def test_stopping_the_warmer_ends_the_thread():
    seed()
    thread = board_rank.start_warmer(0.05)
    board_rank.stop_warmer(timeout=5)
    assert not thread.is_alive()
    assert board_rank._WARM_THREAD is None


def test_a_broken_store_does_not_kill_the_warmer():
    """A missing or half-ingested store must degrade, not take the process down.

    The endpoint already degrades on its own — `app.js` treats any non-200 as "no live
    board" and keeps the baked payload — so a background thread that died on the first
    bad tick would remove the warm-up silently and leave every reader paying full price
    again, with nothing on screen to say so.
    """
    seed()
    boom = []
    real = board_rank.build_board

    def explode():
        boom.append(1)
        if len(boom) == 1:
            raise RuntimeError("store is mid-ingest")
        return real()

    board_rank.build_board = explode
    try:
        board_rank.start_warmer(0.05)
        deadline = time.time() + 10
        while len(boom) < 3 and time.time() < deadline:
            time.sleep(0.02)
    finally:
        board_rank.stop_warmer()
        board_rank.build_board = real

    assert len(boom) >= 3, "the warmer stopped ticking after one failure"
