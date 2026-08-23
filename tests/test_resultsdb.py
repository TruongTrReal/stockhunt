"""`stockhunt.resultsdb` — the store the leaderboard is queried out of.

Synthetic rows only. This suite never opens a result CSV or the real `results.db`, so
`use()` points every test at a tmp file; the store is the one module here whose *state*
can leak between tests if that is forgotten.

`tools/test_board_equivalence.py` already proves the rendered board is unchanged. It
cannot pin down the store's contract on its own — it would pass just as happily if
`_shape` and `board_rank` were wrong in matching directions — so the properties the
ranker leans on are stated here directly:

* a stage's row comes back with the **dtypes `read_csv` gave it**, bools included, which
  is what `df[df.rankable]` needs to keep meaning what it meant;
* an upsert **replaces** a rule's row rather than adding a second one, which is the whole
  reason the store exists;
* `NaN` survives the round trip as `None` and never as the literal `NaN` that no JSON
  parser will read back;
* `drop_rule` leaves no half-scored rule behind for the ranker to rank.
"""

from __future__ import annotations

import math

import pytest

from stockhunt import resultsdb


@pytest.fixture(autouse=True)
def store(tmp_path):
    """A fresh database per test, and the module put back afterwards.

    `use()` is module-global, so without the restore a later test in the same process —
    or a later file — inherits whatever tmp path this one left behind, and the failure
    surfaces somewhere else entirely.
    """
    original = resultsdb.DB_PATH
    resultsdb.use(tmp_path / "results.db")
    yield
    resultsdb.close()
    resultsdb.use(original)


def wf_row(rule="ibs", scenario="retail", **over):
    row = {"class": "us_stocks", "timeframe": "1d", "rule": rule, "scenario": scenario,
           "ir_net": 0.25, "long_frac": 0.46, "exposure": 0.46, "years": 23.6,
           "n_folds": 21, "rankable": True, "is_baseline": False,
           "wf_mode": "published", "net_return_pct": 1234.5}
    row.update(over)
    return row


# --------------------------------------------------------------- the round trip

def test_row_comes_back_as_the_stage_wrote_it():
    resultsdb.put_wf([wf_row()], "published")
    got = resultsdb.wf_rows("us_stocks", "1d")
    assert len(got) == 1
    r = got[0]
    # Every column of the original row, not just the promoted ones.
    assert r["rule"] == "ibs"
    assert r["wf_mode"] == "published"
    assert r["net_return_pct"] == 1234.5
    assert r["ir_net"] == 0.25


def test_bools_stay_bools():
    """`board_rank` filters with `df[df.rankable] & ~df.is_baseline`.

    SQLite has no boolean type, so the promoted copies of these columns come back as 0/1
    and boolean masking on an integer column raises rather than filtering. `_shape` prefers
    the JSON doc for exactly this reason; if that preference is ever inverted, this is the
    test that says so.
    """
    resultsdb.put_wf([wf_row()], "single")
    r = resultsdb.wf_rows("us_stocks", "1d")[0]
    assert r["rankable"] is True
    assert r["is_baseline"] is False


def test_nan_becomes_none_not_the_json_literal():
    resultsdb.put_wf([wf_row(ir_net=float("nan"))], "single")
    r = resultsdb.wf_rows("us_stocks", "1d")[0]
    assert r["ir_net"] is None


def test_infinity_becomes_none():
    # `json.dumps` emits bare `Infinity`, which is not JSON and does not read back.
    resultsdb.put_wf([wf_row(ir_net=float("inf"))], "single")
    assert resultsdb.wf_rows("us_stocks", "1d")[0]["ir_net"] is None


def test_jsonable_unwraps_numpy_scalars_without_importing_numpy():
    np = pytest.importorskip("numpy")
    assert resultsdb.jsonable(np.float64(1.5)) == 1.5
    assert resultsdb.jsonable(np.bool_(True)) is True
    assert resultsdb.jsonable(np.float64("nan")) is None


# --------------------------------------------------------------- upsert, not append

def test_rescoring_a_rule_replaces_its_row():
    """The entire point of the store: one rule, one row, updated in place.

    A CSV cannot do this — every stage rewrites its sheet whole, which is why a scoped
    `--rules` run has to land as `*.partial.csv`.
    """
    resultsdb.put_wf([wf_row(ir_net=0.25)], "published")
    resultsdb.put_wf([wf_row(ir_net=0.31)], "published")
    got = resultsdb.wf_rows("us_stocks", "1d")
    assert len(got) == 1
    assert got[0]["ir_net"] == 0.31


def test_scenarios_are_separate_rows():
    resultsdb.put_wf([wf_row(scenario="retail"), wf_row(scenario="gross")], "single")
    assert len(resultsdb.wf_rows("us_stocks", "1d")) == 2
    assert len(resultsdb.wf_rows("us_stocks", "1d", scenario="gross")) == 1


def test_both_edge_sides_are_kept():
    """`board_rank` picks the stronger side on delta-Sharpe. It cannot if ingest collapsed
    them, and "short is optional" would stop being a decision anything could revisit."""
    base = {"class": "us_stocks", "tf": "1d", "rule": "ibs", "edge_passed": 3,
            "edge_verdict": "partial", "sharpe": 0.9, "years": 23.6}
    resultsdb.put_edge([dict(base, side="long", edge_dsharpe=0.20),
                        dict(base, side="short", edge_dsharpe=0.05)])
    got = {r["side"]: r for r in resultsdb.edge_rows("us_stocks", "1d")}
    assert set(got) == {"long", "short"}
    assert got["long"]["edge_dsharpe"] == 0.20


def test_reingest_does_not_blank_a_submitters_provenance():
    """Re-running ingest over the sheets must not erase who submitted a rule.

    The sheets carry no author column, so a naive upsert writes NULL over it and a
    submitted strategy silently becomes anonymous on the next ingest.
    """
    resultsdb.put_rules([{"cls": "us_stocks", "tf": "1d", "rule": "mine",
                          "kind": "submitted", "submitted_by": "acct-7",
                          "code_sha": "abc123"}])
    resultsdb.put_rules([{"cls": "us_stocks", "tf": "1d", "rule": "mine",
                          "kind": "published", "family": "reversion"}])
    r = resultsdb.rule_rows("us_stocks", "1d")[0]
    assert r["submitted_by"] == "acct-7"
    assert r["code_sha"] == "abc123"
    assert r["kind"] == "published"          # what the sheet says now, which is fresher
    assert r["family"] == "reversion"


# --------------------------------------------------------------- per-asset and removal

def test_per_asset_is_keyed_on_side_as_well_as_symbol():
    rows = [{"cls": "us_stocks", "tf": "1d", "rule": "ibs", "symbol": "AAPL",
             "side": s, "src": "riskmatch", "ir": v, "years": 20.0,
             "net_cagr": v, "bh_cagr": 1.0, "net_pct": v, "bench_pct": 1.0}
            for s, v in (("long", 0.3), ("short", -0.1))]
    resultsdb.put_per_asset(rows)
    got = resultsdb.per_asset_rows("us_stocks", "1d", "ibs")
    assert len(got) == 2, "one side overwrote the other"


def test_negative_zero_survives_the_store():
    """A rule that lost four thousandths of a percent a year prints "−0.0%".

    Rounding inside the store turned that into a plain `0.0` on three of twenty sheets,
    and it was the only drift the equivalence test ever found. The store keeps the raw
    value; the ranker rounds.
    """
    resultsdb.put_per_asset([{"cls": "c", "tf": "1d", "rule": "r", "symbol": "S",
                              "side": "long", "src": "riskmatch", "net_cagr": -0.004}])
    got = resultsdb.per_asset_rows("c", "1d", "r")[0]
    assert math.copysign(1.0, round(got["net_cagr"], 1)) == -1.0


def test_drop_rule_removes_every_trace():
    """The worker calls this before re-scoring. A rule that keeps a stale `book` row while
    its `wf` row is fresh would be ranked on a number nothing recomputed."""
    resultsdb.put_wf([wf_row()], "published")
    resultsdb.put_edge([{"class": "us_stocks", "tf": "1d", "rule": "ibs", "side": "long",
                         "edge_passed": 3}])
    resultsdb.put_book([{"class": "us_stocks", "tf": "1d", "rule": "ibs",
                         "cashmatch_excess_cagr": 4.2, "n_trades": 642}])
    resultsdb.put_per_asset([{"cls": "us_stocks", "tf": "1d", "rule": "ibs",
                              "symbol": "AAPL", "side": "long", "src": "riskmatch"}])
    resultsdb.drop_rule("us_stocks", "1d", "ibs")
    assert resultsdb.wf_rows("us_stocks", "1d") == []
    assert resultsdb.edge_rows("us_stocks", "1d") == []
    assert resultsdb.book_rows("us_stocks", "1d") == []
    assert resultsdb.per_asset_rows("us_stocks", "1d", "ibs") == []


# --------------------------------------------------------------- meta

def test_meta_round_trips_and_overwrites():
    resultsdb.set_meta("gates", [{"key": "dsharpe", "letter": "S"}])
    assert resultsdb.get_meta("gates")[0]["letter"] == "S"
    resultsdb.set_meta("gates", [{"key": "t", "letter": "T"}])
    assert len(resultsdb.get_meta("gates")) == 1
    assert resultsdb.get_meta("nothing", "fallback") == "fallback"


# --------------------------------------------------------------- jobs

def test_claim_takes_the_oldest_and_only_once():
    """`seq` order, so a rule submitted first is scored first, and a second worker on the
    same database cannot take a job that is already running."""
    a = resultsdb.submit_job("acct", "label", "ibs@buy=0.3", "crypto", "1d")
    b = resultsdb.submit_job("acct", "label", "ibs@buy=0.4", "crypto", "1d")
    first = resultsdb.claim_job()
    assert first["job_id"] == a["job_id"]
    assert first["state"] == "running"
    second = resultsdb.claim_job()
    assert second["job_id"] == b["job_id"]
    assert resultsdb.claim_job() is None


def test_a_rejected_job_carries_its_reason():
    job = resultsdb.submit_job("acct", "code", "peeker", "crypto", "1d", code="...")
    resultsdb.claim_job()
    resultsdb.mark_job(job["job_id"], "rejected", stage="causality",
                       reason="positions differ under truncation")
    got = resultsdb.job(job["job_id"])
    assert got["state"] == "rejected"
    assert got["stage"] == "causality"
    assert "truncation" in got["reason"]
    assert got["finished_at"]


def test_submit_job_refuses_an_unknown_kind():
    with pytest.raises(ValueError):
        resultsdb.submit_job("acct", "sql", "drop table", "crypto", "1d")


def test_jobs_since_counts_only_this_account():
    resultsdb.submit_job("a", "label", "x", "crypto", "1d")
    resultsdb.submit_job("b", "label", "y", "crypto", "1d")
    assert resultsdb.jobs_since("a", "1970-01-01T00:00:00+00:00") == 1
    assert resultsdb.jobs_since("a", "2999-01-01T00:00:00+00:00") == 0
