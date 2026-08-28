"""Portfolios: one pot of money, one switch, and a record of what was in the basket.

Part of the root suite for the same reason `test_deskdb.py` is: `stockhunt.portfolios`
imports nothing but the standard library and `stockhunt.deskdb`, so it needs no bars, no
vendor and no result CSV.

Three properties carry the design and most of these tests exist for them:

* **A leg is an ordinary book registration.** Everything the desk already does to a
  promoted book has to keep working on a leg, so the only difference is `portfolio_id`.
* **The toggle is one act.** It reaches every leg, and it touches nothing the desk owns.
* **The membership is a diff, and diffs are run repeatedly.** Applying the same target
  twice must change nothing and log nothing, or the nightly reconcile writes a fresh
  history of events that did not happen.
"""

from __future__ import annotations

import sqlite3

import pytest

from stockhunt import deskdb, portfolios


@pytest.fixture()
def db(tmp_path):
    deskdb.use(tmp_path / "desk.db")
    deskdb.connect()
    yield deskdb
    deskdb.close()


def _pf(account="00", name="core", kind="manual", **kw):
    return portfolios.create(account, name, kind, **kw)


TOP3 = [("us_stocks", "1d", "ibs"),
        ("us_etfs", "1d", "SMA_200"),
        ("crypto", "1d", "RSI_2")]


# ------------------------------------------------------------------------- creating

def test_a_new_portfolio_is_empty_and_readable(db):
    pf = _pf()
    assert pf["portfolio_id"] == "pf_00_core"
    assert pf["capital"] == 100_000.0 and pf["rebalance"] == "monthly"
    assert pf["want"] == "live" and pf["state"] == "pending"
    assert pf["inception"] and pf["created_at"]
    assert portfolios.get("pf_00_core") == pf
    assert portfolios.legs("pf_00_core") == []


def test_a_manual_portfolio_carries_no_source(db):
    """A source recorded on a basket nobody reconciles against it is a claim the row
    cannot keep — and the daily pass would honour it, rewriting the rules somebody
    picked by hand."""
    pf = _pf(source_cls="us_stocks", source_tf="1d", top_n=5)
    assert pf["source_cls"] is None and pf["source_tf"] is None and pf["top_n"] is None


def test_a_follow_portfolio_records_the_sheet_it_tracks(db):
    pf = _pf(name="follow-stocks", kind="follow", source_cls="us_stocks",
             source_tf="1d", top_n=5)
    assert (pf["source_cls"], pf["source_tf"], pf["top_n"]) == ("us_stocks", "1d", 5)


@pytest.mark.parametrize("kw", [
    {"kind": "follow"},                                   # no sheet named
    {"kind": "follow", "source_cls": "us_stocks"},        # half a sheet
    {"kind": "follow", "source_cls": "us_stocks", "source_tf": "1d", "top_n": 0},
    {"kind": "index_fund"},                               # not a kind
    {"rebalance": "weekly"},                              # nothing rebalances weekly
    {"capital": 0.0},
    {"name": "   "},
])
def test_a_portfolio_that_cannot_be_run_is_refused(db, kw):
    with pytest.raises(ValueError):
        _pf(**{"name": "bad", **kw})


def test_a_name_is_taken_once_per_account(db):
    """Not idempotent, deliberately: the same name arriving with different settings is a
    mistake, and answering it with the row already there hands back settings nobody
    asked for."""
    _pf(name="core", capital=100_000.0)
    with pytest.raises(ValueError):
        _pf(name="core", capital=50_000.0)
    assert portfolios.get("pf_00_core")["capital"] == 100_000.0

    # The same name under another account is a different portfolio.
    assert _pf(account="a7", name="core")["portfolio_id"] == "pf_a7_core"
    assert len(portfolios.listing()) == 2


def test_listing_carries_the_legs(db):
    pid = _pf()["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "first cut")
    [row] = portfolios.listing("00")
    assert sorted(leg["rule"] for leg in row["legs"]) == ["RSI_2", "SMA_200", "ibs"]
    assert portfolios.listing("a7") == []


# ------------------------------------------------------------------------------ legs

def test_a_leg_is_an_ordinary_book_registration(db):
    """The whole design rests on this. Warm-up, fills, P&L, curves and the controller's
    attach/retire all already work for a book, so a leg differs in one column."""
    pid = _pf()["portfolio_id"]
    leg = portfolios.add_leg(pid, "us_stocks", "1d", "ibs")

    assert leg["kind"] == "book" and leg["rule"] == "ibs"
    assert leg["portfolio_id"] == pid
    assert leg["want"] == "live" and leg["state"] == "pending"
    # ...and the ledger's own reads see it, without knowing what a portfolio is.
    assert leg in db.active_registrations()
    assert db.registration(leg["strategy_id"])["portfolio_id"] == pid


def test_a_leg_cannot_adopt_a_hand_promoted_book(db):
    """`register` is idempotent on (account, name), so a leg named like a promotion would
    return that promotion's row — quietly putting a book nobody meant to move under this
    portfolio's resize and its toggle."""
    promoted = db.register("00", "us_stocks-1d-ibs", "us_stocks", [], "1d", 100_000.0,
                           kind="book", rule="ibs")
    pid = _pf()["portfolio_id"]
    leg = portfolios.add_leg(pid, "us_stocks", "1d", "ibs")

    assert leg["strategy_id"] != promoted["strategy_id"]
    assert db.registration(promoted["strategy_id"])["portfolio_id"] is None
    assert db.registration(promoted["strategy_id"])["capital"] == 100_000.0


def test_two_portfolios_may_hold_the_same_rule(db):
    a = _pf(name="core")["portfolio_id"]
    b = _pf(name="satellite")["portfolio_id"]
    first = portfolios.add_leg(a, "us_stocks", "1d", "ibs")
    second = portfolios.add_leg(b, "us_stocks", "1d", "ibs")
    assert first["strategy_id"] != second["strategy_id"]
    assert len(portfolios.legs(a)) == len(portfolios.legs(b)) == 1


def test_a_dropped_legs_record_survives(db):
    """Retired, never deleted. A forward test somebody can erase is not a record."""
    pid = _pf()["portfolio_id"]
    leg = portfolios.add_leg(pid, "us_stocks", "1d", "ibs")
    sid = leg["strategy_id"]

    assert portfolios.remove_leg(pid, sid, "fell off the sheet") is True
    assert portfolios.legs(pid) == []
    kept = db.registration(sid)
    assert kept is not None and kept["want"] == "retired"
    assert kept["portfolio_id"] == pid, "it must stay attributable to the basket"
    assert [r["strategy_id"] for r in portfolios.legs(pid, include_retired=True)] == [sid]


def test_a_leg_belongs_to_exactly_one_portfolio(db):
    """Two baskets on one account must not be able to retire each other's legs."""
    a = _pf(name="core")["portfolio_id"]
    b = _pf(name="satellite")["portfolio_id"]
    mine = portfolios.add_leg(a, "us_stocks", "1d", "ibs")["strategy_id"]

    assert portfolios.remove_leg(b, mine, "not mine to drop") is False
    assert db.registration(mine)["want"] == "live"


# ---------------------------------------------------------------------- the toggle

def test_the_toggle_reaches_every_leg(db):
    """A basket half switched off is a position nobody chose to hold."""
    pid = _pf()["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "first cut")

    assert portfolios.set_want(pid, "paused") is True
    assert portfolios.get(pid)["want"] == "paused"
    assert {leg["want"] for leg in portfolios.legs(pid)} == {"paused"}

    portfolios.set_want(pid, "live")
    assert {leg["want"] for leg in portfolios.legs(pid)} == {"live"}


def test_the_toggle_does_not_touch_state(db):
    """`want` is the owner's and `state` is the desk's. Pausing while the desk is down
    leaves them disagreeing, and that is the truth rather than a bug to paper over."""
    pid = _pf()["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "first cut")
    for leg in portfolios.legs(pid):
        db.mark_registration(leg["strategy_id"], "live")
    portfolios.mark_state(pid, "live")

    portfolios.set_want(pid, "paused")

    assert portfolios.get(pid)["state"] == "live"
    assert {leg["state"] for leg in portfolios.legs(pid)} == {"live"}
    # ...and the desk can see there is something to do.
    assert len(db.pending_registrations()) == 3


def test_resuming_does_not_resurrect_a_dropped_leg(db):
    """It fell off the sheet three months ago. Switching the basket back on must not put
    it back on the desk."""
    pid = _pf()["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "first cut")
    gone = portfolios.legs(pid)[0]["strategy_id"]
    portfolios.remove_leg(pid, gone, "fell off")

    portfolios.set_want(pid, "paused")
    portfolios.set_want(pid, "live")

    assert db.registration(gone)["want"] == "retired"
    assert gone not in [leg["strategy_id"] for leg in portfolios.legs(pid)]


def test_an_unknown_want_is_refused(db):
    pid = _pf()["portfolio_id"]
    with pytest.raises(ValueError):
        portfolios.set_want(pid, "on")


# ------------------------------------------------------------------- the membership

def test_apply_membership_adds_what_is_missing(db):
    pid = _pf(name="follow-stocks", kind="follow", source_cls="us_stocks",
              source_tf="1d", top_n=3)["portfolio_id"]
    out = portfolios.apply_membership(pid, TOP3, "nightly reconcile")

    assert len(out["added"]) == 3 and out["removed"] == []
    assert out["n_legs"] == 3
    assert sorted(leg["rule"] for leg in portfolios.legs(pid)) == \
        sorted(rule for _c, _t, rule in TOP3)


def test_applying_the_same_target_twice_changes_nothing(db):
    """The property the nightly reconcile rests on. Without it every night writes a
    fresh history of events that did not happen."""
    pid = _pf()["portfolio_id"]
    first = portfolios.apply_membership(pid, TOP3, "night one")
    before = [leg["strategy_id"] for leg in portfolios.legs(pid)]

    again = portfolios.apply_membership(pid, TOP3, "night two")

    assert again["added"] == [] and again["removed"] == []
    assert again["unchanged"] == 3
    assert [leg["strategy_id"] for leg in portfolios.legs(pid)] == before
    assert len(portfolios.changes(pid)) == len(first["added"])


def test_a_swap_retires_one_and_adds_the_other(db):
    pid = _pf()["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "night one")
    dropped = [leg for leg in portfolios.legs(pid) if leg["rule"] == "RSI_2"][0]

    target = TOP3[:2] + [("crypto", "1d", "MACD")]
    out = portfolios.apply_membership(pid, target, "night two")

    assert out["removed"] == [dropped["strategy_id"]]
    assert len(out["added"]) == 1 and out["unchanged"] == 2
    assert sorted(leg["rule"] for leg in portfolios.legs(pid)) == \
        ["MACD", "SMA_200", "ibs"]
    assert db.registration(dropped["strategy_id"])["want"] == "retired"


def test_a_rule_that_comes_back_continues_its_record(db):
    """Revived under the same `strategy_id`, so the downtime is a measured gap rather
    than a shorter, newer track record that happens to begin at a good time."""
    pid = _pf()["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "night one")
    was = [leg for leg in portfolios.legs(pid) if leg["rule"] == "ibs"][0]["strategy_id"]

    portfolios.apply_membership(pid, TOP3[1:], "night two")
    back = portfolios.apply_membership(pid, TOP3, "night three")

    assert back["added"] == [was]
    assert db.registration(was)["want"] == "live"


def test_a_target_naming_a_rule_twice_buys_it_once(db):
    pid = _pf()["portfolio_id"]
    out = portfolios.apply_membership(pid, TOP3 + [TOP3[0]], "a sheet that repeats")
    assert out["n_legs"] == 3 and len(out["added"]) == 3
    # ...and it does not flap on the next pass either.
    assert portfolios.apply_membership(pid, TOP3 + [TOP3[0]], "again")["added"] == []


def test_an_empty_target_empties_the_basket(db):
    pid = _pf()["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "night one")
    out = portfolios.apply_membership(pid, [], "the sheet went stale")
    assert len(out["removed"]) == 3 and out["n_legs"] == 0
    assert portfolios.legs(pid) == []
    assert len(portfolios.legs(pid, include_retired=True)) == 3


# -------------------------------------------------------------------------- the log

def test_one_row_per_change_and_it_says_why(db):
    pid = _pf(name="follow-stocks", kind="follow", source_cls="us_stocks",
              source_tf="1d", top_n=3)["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "night one")
    portfolios.apply_membership(pid, TOP3[:2] + [("crypto", "1d", "MACD")], "night two")

    log = portfolios.changes(pid)
    assert len(log) == 5, "three added, one removed, one added"
    assert [r["action"] for r in log[:2]] == ["added", "removed"], "newest first"
    assert log[0]["rule"] == "MACD" and log[0]["reason"] == "night two"
    assert log[0]["source"] == "us_stocks_1d"
    assert all(r["at"] and r["strategy_id"] for r in log)


def test_a_change_row_says_where_the_rule_stood(db):
    """The sheet is re-ranked nightly and cannot be asked afterwards, so "it came in
    4th" is only ever answerable if the rank was written down at the time."""
    pid = _pf(name="follow-stocks", kind="follow", source_cls="us_stocks",
              source_tf="1d", top_n=3)["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "night one")

    ranks = {r["rule"]: r["rank_at"] for r in portfolios.changes(pid)}
    assert ranks == {"ibs": 1, "SMA_200": 2, "RSI_2": 3}

    # A removal has no rank — falling off the sheet is the whole event.
    portfolios.apply_membership(pid, TOP3[:2], "night two")
    assert portfolios.changes(pid)[0]["rank_at"] is None


def test_a_change_row_says_what_the_basket_became(db):
    """Otherwise reconstructing what a dollar was doing on a given day means replaying
    every row from inception and hoping none is missing."""
    pid = _pf()["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "night one")
    for row in portfolios.changes(pid):
        assert row["n_legs"] == 3
        assert row["leg_capital"] == pytest.approx(100_000.0 / 3)


def test_the_log_is_never_rewritten(db):
    """A 'follow' basket's composition is decided by a sheet that moves underneath it, so
    a step in its curve is explained by a row here or by nothing at all."""
    pid = _pf()["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "night one")
    portfolios.apply_membership(pid, [], "everything out")
    portfolios.apply_membership(pid, TOP3, "everything back")
    assert len(portfolios.changes(pid)) == 9


# ----------------------------------------------------------------------- the money

def test_the_pot_is_split_equally(db):
    pid = _pf(capital=100_000.0)["portfolio_id"]
    portfolios.apply_membership(pid, TOP3[:2], "two legs")
    assert [leg["capital"] for leg in portfolios.legs(pid)] == [50_000.0, 50_000.0]

    portfolios.apply_membership(pid, TOP3, "three legs")
    for leg in portfolios.legs(pid):
        assert leg["capital"] == pytest.approx(100_000.0 / 3)


def test_a_swap_re_splits_the_pot(db):
    """The count is what changes the weights, so a like-for-like swap must not."""
    pid = _pf(capital=60_000.0)["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "three legs")
    portfolios.apply_membership(pid, TOP3[:2] + [("crypto", "1d", "MACD")], "swap one")
    assert [leg["capital"] for leg in portfolios.legs(pid)] == [20_000.0] * 3

    portfolios.apply_membership(pid, TOP3[:2], "drop to two")
    assert [leg["capital"] for leg in portfolios.legs(pid)] == [30_000.0] * 2


def test_a_retired_leg_does_not_hold_money(db):
    pid = _pf(capital=100_000.0)["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "three legs")
    gone = portfolios.legs(pid)[0]["strategy_id"]
    portfolios.remove_leg(pid, gone, "dropped")
    assert [leg["capital"] for leg in portfolios.legs(pid)] == [50_000.0, 50_000.0]


def test_resize_touches_nothing_outside_the_portfolio(db):
    """A hand-promoted book and another basket's legs are not this portfolio's to
    resize."""
    promoted = db.register("00", "us_stocks-1d-macd", "us_stocks", [], "1d", 100_000.0,
                           kind="book", rule="MACD")["strategy_id"]
    other = portfolios.add_leg(_pf(name="satellite", capital=10_000.0)["portfolio_id"],
                               "crypto", "1d", "RSI_2")["strategy_id"]

    pid = _pf(name="core", capital=100_000.0)["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "three legs")

    assert db.registration(promoted)["capital"] == 100_000.0
    assert db.registration(other)["capital"] == 10_000.0


def test_resizing_an_empty_portfolio_is_not_an_error(db):
    pid = _pf()["portfolio_id"]
    assert portfolios.resize(pid) == {"n_legs": 0, "leg_capital": 0.0}


# --------------------------------------------------------------------- account scope

def test_one_account_cannot_see_or_touch_anothers_portfolio(db):
    theirs = _pf(account="a7", name="core")["portfolio_id"]
    portfolios.apply_membership(theirs, TOP3, "theirs")

    assert portfolios.get(theirs, account="c2") is None
    assert portfolios.listing("c2") == []
    assert portfolios.set_want(theirs, "retired", account="c2") is False
    assert portfolios.get(theirs)["want"] == "live"
    assert {leg["want"] for leg in portfolios.legs(theirs)} == {"live"}

    for call in (lambda: portfolios.add_leg(theirs, "crypto", "1d", "MACD",
                                            account="c2"),
                 lambda: portfolios.remove_leg(
                     theirs, portfolios.legs(theirs)[0]["strategy_id"], "no",
                     account="c2"),
                 lambda: portfolios.apply_membership(theirs, [], "no", account="c2"),
                 lambda: portfolios.resize(theirs, account="c2")):
        with pytest.raises(LookupError):
            call()

    assert len(portfolios.legs(theirs)) == 3
    assert portfolios.set_want(theirs, "retired", account="a7") is True


def test_the_house_account_is_a_portfolio_owner_like_any_other(db):
    """No per-account cap and no special case: the house desk and a member's are the
    same machinery, which is what keeps them from drifting apart."""
    house = _pf(account="00", name="research-top5", kind="follow",
                source_cls="us_stocks", source_tf="1d", top_n=3)["portfolio_id"]
    member = _pf(account="a7", name="research-top5")["portfolio_id"]
    portfolios.apply_membership(house, TOP3, "nightly")
    portfolios.apply_membership(member, TOP3, "by hand")

    assert len(portfolios.legs(house)) == len(portfolios.legs(member)) == 3
    assert len(portfolios.listing("00")) == len(portfolios.listing("a7")) == 1


# ------------------------------------------------------- a database that predates this

def test_a_database_created_before_portfolios_still_opens(db, tmp_path):
    """The one most likely to break in production. `CREATE TABLE IF NOT EXISTS` does
    nothing to a table that already exists, so `portfolio_id` never reaches a file
    somebody is already running — and one of the two processes holding that file is a
    live trading node, which is not something to hand a fresh database to.
    """
    db.close()
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE registrations (
            strategy_id TEXT PRIMARY KEY,
            account     TEXT NOT NULL,
            name        TEXT NOT NULL,
            kind        TEXT NOT NULL DEFAULT 'member',
            cls         TEXT NOT NULL,
            symbols     TEXT NOT NULL,
            tf          TEXT NOT NULL,
            capital     REAL NOT NULL,
            benchmark   TEXT,
            rule        TEXT,
            allow_short INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            want        TEXT NOT NULL DEFAULT 'live',
            state       TEXT NOT NULL DEFAULT 'pending',
            reason      TEXT,
            applied_at  TEXT,
            UNIQUE(account, name)
        );
        INSERT INTO registrations
            (strategy_id, account, name, kind, cls, symbols, tf, capital, created_at,
             want, state)
        VALUES ('str_00_older', '00', 'older', 'book', 'us_stocks', '[]', '1d',
                100000.0, '2026-01-01T00:00:00+00:00', 'live', 'live');
    """)
    old.commit()
    old.close()

    deskdb.use(path)
    was_there = deskdb.registration("str_00_older")
    assert was_there["portfolio_id"] is None, "the column arrived, empty, as it should"
    assert was_there["capital"] == 100_000.0, "and the row it landed on is untouched"

    # ...and the whole feature works on top of it.
    pid = portfolios.create("00", "core", "manual")["portfolio_id"]
    portfolios.apply_membership(pid, TOP3, "on an old ledger")
    assert len(portfolios.legs(pid)) == 3
    assert len(portfolios.changes(pid)) == 3
    assert deskdb.registration("str_00_older")["capital"] == 100_000.0
