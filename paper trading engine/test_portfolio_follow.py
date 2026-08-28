"""The follow-the-leaderboard selector, offline and on synthetic sheets.

Every fixture here is built in this file. Nothing reads `catalog.json`, a result CSV or
the store, because a test that fails when somebody re-runs the research is a test nobody
will trust — and this one has to keep being trusted precisely on the days the research
moves, which is when the selector's behaviour matters.

The cells are shaped like the real ones: `catalog.cells()` emits a flat row carrying
`tradable`, `edge_passed`, `book_cm_excess_cagr`, `long_frac` and `exposure`, so that is
what these carry. The closet-tracker fixtures add a `book` blob, which is the shape a row
straight off `board_rank.build_sheet` has — both are accepted, and both are exercised.

Run from this folder (this suite is not collected from the repo root):

    ..\\.venv\\Scripts\\python -m pytest test_portfolio_follow.py -q
"""

from __future__ import annotations

import pytest

import portfolio_follow as pf


# --------------------------------------------------------------------------- fixtures

def cell(rule, *, tradable=True, passed=3, cagr=0.01, long_frac=0.4, **extra):
    """One promotable row, in `catalog.cells()`' shape."""
    row = {"rule": rule, "tradable": tradable, "not_tradable_because": "",
           "kind": "single", "family": "talib",
           "edge_passed": passed, "book_cm_excess_cagr": cagr,
           "long_frac": long_frac}
    row.update(extra)
    return row


def doc(sheets, *, names=None, stale=(), generated="2026-08-28T00:00:00+00:00"):
    """A catalog document holding exactly the sheets a test needs."""
    classes = {key.rpartition("_")[0] for key in sheets}
    return {
        "generated_at": generated,
        "timeframes": ["1d", "4h"],
        "book": {"capital": 100_000.0, "timeframes": ["1d", "4h", "5m"],
                 "names": names if names is not None else {c: 10 for c in classes}},
        "health": {"warning": "ranking is not passing", "stale_sheets": list(stale)},
        "sheets": {key: {"mtime": generated,
                         "ranked_on": "edge_passed, then book_cm_excess_cagr",
                         "cells": cells}
                   for key, cells in sheets.items()},
    }


FIVE = [cell("ibs"), cell("volmanaged"), cell("RSI_14"), cell("MACD"), cell("ATR_20"),
        cell("CCI_14"), cell("ADX_14")]


# ------------------------------------------------------------------------------ sheets

def test_sheets_are_derived_from_the_catalog_not_a_list():
    d = doc({"us_stocks_1d": FIVE, "crypto_4h": FIVE, "cme_futures_15m": FIVE})
    assert pf.sheets(d, ["1d", "4h", "15m"]) == [
        ("us_stocks", "1d"), ("crypto", "4h"), ("cme_futures", "15m")]


def test_a_sheet_at_an_untradable_timeframe_yields_no_portfolio():
    """`BOOK_TIMEFRAMES` is the gate, and it is a parameter: widening it is the only
    change needed when the desk is measured at 1h and 15m."""
    d = doc({"us_stocks_1d": FIVE, "us_stocks_1h": FIVE})
    assert pf.sheets(d, ["1d", "4h"]) == [("us_stocks", "1d")]
    assert pf.sheets(d, ["1d", "1h"]) == [("us_stocks", "1d"), ("us_stocks", "1h")]


def test_sheets_defaults_to_the_desks_own_book_timeframes():
    import paper_config
    d = doc({f"us_stocks_{tf}": FIVE for tf in ("1d", "4h", "1h", "15m", "5m")})
    got = [tf for _cls, tf in pf.sheets(d)]
    assert got == [tf for tf in ("1d", "4h", "1h", "15m", "5m")
                   if tf in paper_config.BOOK_TIMEFRAMES]


def test_a_class_with_no_live_names_yields_no_portfolio():
    """A book of zero names registers fine and then holds air."""
    d = doc({"us_stocks_1d": FIVE, "crypto_1d": FIVE},
            names={"us_stocks": 100, "crypto": 0})
    assert pf.sheets(d, ["1d"]) == [("us_stocks", "1d")]


# --------------------------------------------------------------------------------- top

def test_ordering_matches_the_board():
    d = doc({"us_stocks_1d": FIVE})
    got = pf.top("us_stocks", "1d", 5, d)
    assert [r["rule"] for r in got] == ["ibs", "volmanaged", "RSI_14", "MACD", "ATR_20"]
    assert [r["rank"] for r in got] == [1, 2, 3, 4, 5]
    assert [r["board_pos"] for r in got] == [1, 2, 3, 4, 5]


def test_the_ordering_keys_travel_with_the_row():
    d = doc({"us_stocks_1d": [cell("ibs", passed=2, cagr=0.031, long_frac=0.86)]})
    row = pf.top("us_stocks", "1d", 5, d)[0]
    assert row["edge_passed"] == 2
    assert row["book_cm_excess_cagr"] == 0.031
    assert row["long_frac"] == 0.86
    assert row["ranked_on"] == "edge_passed, then book_cm_excess_cagr"


def test_an_untradable_row_is_skipped_not_counted():
    """`--top 5` must mean five holdings, never "five rows, two of which we cannot build"."""
    d = doc({"crypto_1d": [cell("A~B|or", tradable=False,
                                not_tradable_because="one leg is unknown"),
                           cell("ibs"), cell("RSI_14")]})
    got = pf.top("crypto", "1d", 2, d)
    assert [r["rule"] for r in got] == ["ibs", "RSI_14"]
    # ...and it is still ranked ahead: the skip is about what can run, not what scored.
    assert [r["board_pos"] for r in got] == [2, 3]


def test_an_idle_book_is_excluded():
    """In cash 99%+ of the time — a T-bill account with a signal attached."""
    d = doc({"us_stocks_1d": [cell("CDL3STARSINSOUTH", exposure=0.0),
                              cell("BBANDS", n_trades=0),
                              cell("ibs", exposure=0.42)]})
    assert [r["rule"] for r in pf.top("us_stocks", "1d", 5, d)] == ["ibs"]
    assert "cash" in pf.excluded_because(cell("x", exposure=0.0))


def test_a_closet_tracker_is_excluded():
    """The book IS buy-and-hold; the market's return would arrive under a rule's name."""
    d = doc({"us_etfs_1d": [
        cell("mc_williams_r_reactive", book={"exposure": 0.988, "n_trades": 53,
                                             "r2_vs_bh": 0.992, "beta_bh": 1.0}),
        cell("ibs", book={"exposure": 0.42, "n_trades": 900,
                          "r2_vs_bh": 0.31, "beta_bh": 0.4})]})
    assert [r["rule"] for r in pf.top("us_etfs", "1d", 5, d)] == ["ibs"]


def test_a_cell_the_book_stage_never_reached_is_not_cut():
    """Unknown is not idle. Cutting an unmeasured rule as though it were empty is the
    same mistake as ranking one, in the other direction."""
    d = doc({"crypto_1d": [cell("ibs")]})          # no exposure, no book blob
    assert pf.excluded_because(cell("ibs")) == ""
    assert [r["rule"] for r in pf.top("crypto", "1d", 5, d)] == ["ibs"]


def test_same_idea_duplicates_collapse():
    """Two names for one idea would double an exposure while reading as diversification."""
    d = doc({"us_stocks_1d": [cell("MA_50"), cell("SMA_50"), cell("SAR"),
                              cell("SAREXT"), cell("ibs")]})
    got = [r["rule"] for r in pf.top("us_stocks", "1d", 5, d)]
    assert got == ["MA_50", "SAR", "ibs"]


def test_a_near_miss_is_not_collapsed():
    """MAXINDEX/MININDEX agree on 74% of bars, which is what any two long-biased rules
    do. Collapsing them would be an opinion about the indicators."""
    d = doc({"us_stocks_1d": [cell("MAXINDEX_50"), cell("MININDEX_50")]})
    assert len(pf.top("us_stocks", "1d", 5, d)) == 2


def test_a_thin_sheet_yields_what_it_has():
    d = doc({"commodities_4h": [cell("ibs"), cell("RSI_14")]})
    got = pf.top("commodities", "4h", 5, d)
    assert [r["rule"] for r in got] == ["ibs", "RSI_14"]


def test_a_sheet_that_does_not_exist_yields_nothing():
    d = doc({"us_stocks_1d": FIVE})
    assert pf.top("us_stocks", "5m", 5, d) == []
    assert pf.top("cme_futures", "1d", 5, d) == []


def test_a_stale_sheet_is_flagged_on_every_row():
    d = doc({"us_stocks_1d": FIVE}, stale=["us_stocks_1d"])
    assert all(r["stale_sheet"] for r in pf.top("us_stocks", "1d", 3, d))


# -------------------------------------------------------------------------------- diff

def test_diff_is_empty_when_current_equals_target():
    d = doc({"us_stocks_1d": FIVE})
    target = pf.top("us_stocks", "1d", 5, d)
    got = pf.diff([r["rule"] for r in target], target)
    assert got["add"] == [] and got["retire"] == []
    assert got["changed"] is False
    assert got["hold"] == [r["rule"] for r in target]


def test_diff_add_and_retire_on_a_partial_overlap():
    d = doc({"us_stocks_1d": FIVE})
    target = pf.top("us_stocks", "1d", 5, d)          # ibs volmanaged RSI_14 MACD ATR_20
    current = ["ibs", "RSI_14", "OBV", "MFI_14", "ATR_20"]
    got = pf.diff(current, target)
    assert [a["rule"] for a in got["add"]] == ["volmanaged", "MACD"]
    assert [r["rule"] for r in got["retire"]] == ["OBV", "MFI_14"]
    assert got["hold"] == ["ibs", "RSI_14", "ATR_20"]
    assert got["changed"] is True


def test_every_change_carries_a_reason_a_person_can_read():
    d = doc({"us_stocks_1d": FIVE})
    target = pf.top("us_stocks", "1d", 3, d)
    got = pf.diff(["OBV"], target)
    for change in got["add"] + got["retire"]:
        assert change["reason"] and isinstance(change["reason"], str)
    assert "us_stocks_1d" in got["add"][0]["reason"]
    assert "rank 1" in got["add"][0]["reason"]
    assert "dropped out of the top 3" in got["retire"][0]["reason"]


def test_no_reason_claims_a_rule_passed_anything():
    """Ranking is not passing, and a string shown to a person may not imply otherwise."""
    d = doc({"us_stocks_1d": FIVE})
    target = pf.top("us_stocks", "1d", 3, d)
    for change in pf.diff(["OBV"], target)["add"]:
        assert pf.NOT_PASSING in change["reason"]


def test_an_empty_target_retires_with_the_honest_reason():
    """Nothing holdable on the sheet is a different fact from a rule falling out of it."""
    got = pf.diff(["ibs"], [], sheet="crypto_5m")
    assert [r["rule"] for r in got["retire"]] == ["ibs"]
    assert "no holdable rule" in got["retire"][0]["reason"]


def test_diff_tolerates_a_duplicated_holding():
    d = doc({"us_stocks_1d": FIVE})
    target = pf.top("us_stocks", "1d", 2, d)
    got = pf.diff(["ibs", "ibs", "OBV"], target)
    assert [r["rule"] for r in got["retire"]] == ["OBV"]
    assert got["hold"] == ["ibs"]


# -------------------------------------------------------------------------------- plan

def test_plan_covers_every_sheet_and_creates_the_missing_portfolios():
    d = doc({"us_stocks_1d": FIVE, "crypto_1d": FIVE})
    rows = pf.plan({"us_stocks_1d": ["ibs", "OBV"]}, 3, d, ["1d"])
    by = {r["sheet"]: r for r in rows}
    assert set(by) == {"us_stocks_1d", "crypto_1d"}
    assert by["us_stocks_1d"]["action"] == "reconcile"
    assert by["crypto_1d"]["action"] == "create"
    assert by["crypto_1d"]["create"] is True
    assert [a["rule"] for a in by["crypto_1d"]["add"]] == ["ibs", "volmanaged", "RSI_14"]
    assert [r["rule"] for r in by["us_stocks_1d"]["retire"]] == ["OBV"]


def test_a_sheet_that_has_not_landed_yet_produces_no_portfolio():
    """The 5m sheets are still being scored. They must be absent, not empty."""
    d = doc({"us_stocks_1d": FIVE})
    rows = pf.plan({}, 5, d, ["1d", "5m"])
    assert [r["sheet"] for r in rows] == ["us_stocks_1d"]


def test_a_portfolio_whose_sheet_left_the_menu_is_reported_never_emptied():
    """A withdrawn sheet and a catalog that failed to rebuild look the same from here."""
    d = doc({"us_stocks_1d": FIVE})
    rows = pf.plan({"crypto_4h": ["ibs", "RSI_14"]}, 5, d, ["1d", "4h"])
    orphan = [r for r in rows if r["sheet"] == "crypto_4h"][0]
    assert orphan["action"] == "orphan"
    assert orphan["retire"] == [] and orphan["changed"] is False
    assert orphan["hold"] == ["ibs", "RSI_14"]


def test_a_sheet_that_ranks_nothing_leaves_its_basket_alone():
    """`catalog.py` writes a key with an empty `cells` list for a sheet the board cannot
    rank yet. Applying that diff would liquidate a live basket mid-rebuild."""
    d = doc({"us_stocks_1d": []})
    row = pf.plan({"us_stocks_1d": ["ibs", "RSI_14"]}, 5, d, ["1d"])[0]
    assert row["action"] == "empty"
    assert row["retire"] == [] and row["changed"] is False
    with pytest.raises(ValueError):
        pf.target_cells(row)


def test_diff_still_answers_the_empty_sheet_honestly():
    """The guard is in `plan`, not in `diff` — the arithmetic stays what was asked for."""
    got = pf.diff(["ibs"], [], sheet="us_stocks_1d")
    assert [r["rule"] for r in got["retire"]] == ["ibs"]


def test_plan_filters_by_class_and_timeframe():
    d = doc({"us_stocks_1d": FIVE, "us_stocks_4h": FIVE, "crypto_1d": FIVE})
    rows = pf.plan({}, 5, d, ["1d", "4h"], only_cls="us_stocks")
    assert {r["sheet"] for r in rows} == {"us_stocks_1d", "us_stocks_4h"}
    rows = pf.plan({}, 5, d, ["1d", "4h"], only_tf="1d")
    assert {r["sheet"] for r in rows} == {"us_stocks_1d", "crypto_1d"}


def test_plan_accepts_the_membership_shapes_a_caller_has():
    d = doc({"us_stocks_1d": FIVE})
    want = pf.plan({"us_stocks_1d": ["ibs"]}, 3, d, ["1d"])
    for other in ({("us_stocks", "1d"): ["ibs"]},
                  [{"cls": "us_stocks", "tf": "1d", "members": ["ibs"]}]):
        got = pf.plan(other, 3, d, ["1d"])
        assert [(r["sheet"], r["hold"], [a["rule"] for a in r["add"]]) for r in got] \
            == [(r["sheet"], r["hold"], [a["rule"] for a in r["add"]]) for r in want]


def test_plan_carries_the_catalogs_own_age_and_staleness():
    d = doc({"us_stocks_1d": FIVE}, stale=["us_stocks_1d"],
            generated="2026-08-27T23:20:26+00:00")
    row = pf.plan({}, 5, d, ["1d"])[0]
    assert row["catalog_generated_at"] == "2026-08-27T23:20:26+00:00"
    assert row["stale_sheet"] is True
    assert row["note"] == pf.NOT_PASSING


def test_the_target_hands_over_in_rank_order():
    """`apply_membership` writes the rank down as it attaches, and the sheet is re-ranked
    nightly, so a target in any other order records a rank nobody can check later."""
    d = doc({"crypto_1d": FIVE})
    row = pf.plan({}, 3, d, ["1d"])[0]
    assert pf.target_cells(row) == [("crypto", "1d", "ibs"),
                                    ("crypto", "1d", "volmanaged"),
                                    ("crypto", "1d", "RSI_14")]


def test_an_orphan_refuses_to_hand_over_an_empty_target():
    """An empty target does not mean "leave this alone" — it means "retire every leg"."""
    d = doc({"us_stocks_1d": FIVE})
    orphan = [r for r in pf.plan({"crypto_4h": ["ibs"]}, 5, d, ["1d", "4h"])
              if r["action"] == "orphan"][0]
    with pytest.raises(ValueError):
        pf.target_cells(orphan)


def test_plan_only_computes():
    """Twice over the same document is the same answer, and neither call touched anything
    to make it so. Deciding and acting are split; the actor is `stockhunt.portfolios`."""
    d = doc({"us_stocks_1d": FIVE, "crypto_1d": FIVE})
    first = pf.plan({"us_stocks_1d": ["ibs"]}, 4, d, ["1d"])
    second = pf.plan({"us_stocks_1d": ["ibs"]}, 4, d, ["1d"])
    assert first == second
    assert not hasattr(pf, "apply_membership")


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
