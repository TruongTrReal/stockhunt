"""`/v1/portfolios` — the control plane for a basket of strategies.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_portfolios.py -q

Two properties here matter more than the rest and both fail quietly if broken: the toggle
has to reach every leg (a portfolio that pauses four of its five legs is worse than one
that pauses none, because the reader believes it stopped), and a member must never read or
write another member's basket.

Nothing in this file blends anything. `stockhunt/blend.py` owns that arithmetic and
`tests/test_blend.py` proves it; here the blend is monkeypatched, because an endpoint test
that also re-derives a portfolio's statistics is two tests wearing one name and it would
start failing when somebody re-runs a book stage.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import api_paths                                                        # noqa: F401
import api_live
import authdb
from stockhunt import deskdb, portfolios

CATALOG = {
    "generated_at": "2026-08-28T00:00:00+00:00",
    "universe": {"us_stocks": ["SPY", "AAPL"], "crypto": ["BTC/USD"]},
    "timeframes": ["1d", "4h"],
    "book": {
        "capital": 100000.0,
        "timeframes": ["1d", "4h"],
        "names": {"us_stocks": 100, "crypto": 10},
        # Declared per class. `crypto` has no index ETF and carries none, which is a real
        # answer rather than a gap to fill in.
        "benchmark": {"us_stocks": "SPY", "crypto": None},
    },
    "health": {"warning": "Ranking is not passing.", "stale_sheets": []},
    "sheets": {
        "us_stocks_1d": {"mtime": "2026-08-27T00:00:00+00:00", "cells": [
            {"rule": "ibs", "tradable": True},
            {"rule": "SMA_200", "tradable": True},
            {"rule": "MAXINDEX", "tradable": True},
            {"rule": "A~B|vote", "tradable": False,
             "not_tradable_because": "a combo has no single definition to hold"},
        ]},
        "crypto_1d": {"mtime": "2026-08-27T00:00:00+00:00", "cells": [
            {"rule": "ibs", "tradable": True},
        ]},
    },
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    authdb.use(tmp_path / "auth.db")
    deskdb.use(tmp_path / "desk.db")
    authdb.connect()
    deskdb.connect()

    web = tmp_path / "web"
    web.mkdir()
    (web / "catalog.json").write_text(json.dumps(CATALOG), encoding="utf-8")
    monkeypatch.setattr(api_live, "CATALOG_PATH", web / "catalog.json")

    import api_app
    with TestClient(api_app.create_app()) as c:
        yield c
    authdb.close()
    deskdb.close()


def key_for(email, admin=False):
    authdb.allow(email, is_admin=admin)
    raw, _ = authdb.create_api_key(email)
    return {"Authorization": f"Bearer {raw}"}


def make(client, headers, **over):
    body = {"name": "mine", "kind": "manual",
            "legs": [{"cls": "us_stocks", "tf": "1d", "rule": "ibs"},
                     {"cls": "us_stocks", "tf": "1d", "rule": "SMA_200"}]}
    body.update(over)
    return client.post("/v1/portfolios", headers=headers, json=body)


# ------------------------------------------------------------------------- creating

def test_a_member_creates_a_portfolio_on_their_own_account(client):
    r = make(client, key_for("m@example.com"), name="mine")
    assert r.status_code == 201
    body = r.json()
    assert body["kind"] == "manual"
    assert body["state"] == "pending", "this process owns no trading; the desk applies it"
    assert len(body["legs"]) == 2
    assert body["account"] != "00"


def test_the_owner_creates_on_the_house_account(client):
    body = make(client, key_for("o@example.com", admin=True), name="house one").json()
    assert body["account"] == "00"


def test_the_pot_is_split_equally_across_the_legs(client):
    body = make(client, key_for("m@example.com")).json()
    assert body["capital"] == 100_000.0
    assert [leg["capital"] for leg in body["legs"]] == [50_000.0, 50_000.0]


def test_a_leg_is_an_ordinary_book_registration(client):
    """The whole feature rests on this: nothing downstream had to learn about portfolios."""
    leg = make(client, key_for("m@example.com")).json()["legs"][0]
    assert leg["kind"] == "book"
    assert leg["symbols"] == [], "the roster is read live, never frozen at registration"
    assert leg["portfolio_id"]


def test_a_leg_carries_the_benchmark_the_desk_declared(client):
    """Never inferred from the holdings — that is how a baseline stops matching."""
    legs = make(client, key_for("m@example.com")).json()["legs"]
    assert {leg["benchmark"] for leg in legs} == {"SPY"}


def test_a_class_with_no_index_carries_no_benchmark(client):
    body = make(client, key_for("m@example.com"), name="c",
                legs=[{"cls": "crypto", "tf": "1d", "rule": "ibs"}]).json()
    assert body["legs"][0]["benchmark"] is None


def test_an_untradable_rule_is_refused_here_not_by_the_desk_later(client):
    r = make(client, key_for("m@example.com"), name="bad",
             legs=[{"cls": "us_stocks", "tf": "1d", "rule": "A~B|vote"}])
    assert r.status_code == 400
    assert "cannot be traded live" in r.json()["detail"]


def test_a_rule_that_is_not_on_the_board_is_refused(client):
    r = make(client, key_for("m@example.com"), name="bad",
             legs=[{"cls": "us_stocks", "tf": "1d", "rule": "NOPE"}])
    assert r.status_code == 400


def test_a_sheet_that_does_not_exist_is_refused_by_name(client):
    r = make(client, key_for("m@example.com"), name="bad",
             legs=[{"cls": "us_stocks", "tf": "15m", "rule": "ibs"}])
    assert r.status_code == 404
    assert "us_stocks_1d" in r.json()["detail"], "say what IS available"


def test_a_manual_portfolio_with_no_legs_is_refused(client):
    r = make(client, key_for("m@example.com"), name="empty", legs=[])
    assert r.status_code == 400
    assert "follow" in r.json()["detail"], "point at the other kind"


def test_the_same_leg_twice_is_refused(client):
    r = make(client, key_for("m@example.com"), name="dup",
             legs=[{"cls": "us_stocks", "tf": "1d", "rule": "ibs"},
                   {"cls": "us_stocks", "tf": "1d", "rule": "ibs"}])
    assert r.status_code == 400


def test_a_follow_portfolio_records_the_sheet_it_tracks(client):
    r = client.post("/v1/portfolios", headers=key_for("m@example.com"),
                    json={"name": "top5", "kind": "follow",
                          "source_cls": "us_stocks", "source_tf": "1d", "top_n": 5})
    assert r.status_code == 201
    body = r.json()
    assert (body["source_cls"], body["source_tf"], body["top_n"]) == ("us_stocks", "1d", 5)
    assert body["legs"] == [], "the desk fills it on its next reconcile, not this process"


def test_a_follow_portfolio_needs_a_sheet_to_follow(client):
    r = client.post("/v1/portfolios", headers=key_for("m@example.com"),
                    json={"name": "top5", "kind": "follow"})
    assert r.status_code == 400


def test_one_name_per_account(client):
    headers = key_for("m@example.com")
    assert make(client, headers, name="same").status_code == 201
    assert make(client, headers, name="same").status_code == 409


def test_two_accounts_may_use_one_name(client):
    assert make(client, key_for("a@example.com"), name="same").status_code == 201
    assert make(client, key_for("b@example.com"), name="same").status_code == 201


# --------------------------------------------------------------------- the toggle

def test_pausing_reaches_every_leg(client):
    """A portfolio that pauses four legs of five is worse than one that pauses none."""
    headers = key_for("m@example.com")
    pid = make(client, headers).json()["portfolio_id"]
    body = client.post(f"/v1/portfolios/{pid}/pause", headers=headers).json()
    assert body["want"] == "paused"
    assert {leg["want"] for leg in body["legs"]} == {"paused"}


def test_pausing_does_not_touch_what_the_desk_did(client):
    """`want` is intent and `state` is the desk's record. Only the desk writes state."""
    headers = key_for("m@example.com")
    pid = make(client, headers).json()["portfolio_id"]
    before = {leg["strategy_id"]: leg["state"]
              for leg in client.get(f"/v1/portfolios/{pid}", headers=headers).json()["legs"]}
    body = client.post(f"/v1/portfolios/{pid}/pause", headers=headers).json()
    assert {leg["strategy_id"]: leg["state"] for leg in body["legs"]} == before


def test_resuming_puts_it_back(client):
    headers = key_for("m@example.com")
    pid = make(client, headers).json()["portfolio_id"]
    client.post(f"/v1/portfolios/{pid}/pause", headers=headers)
    body = client.post(f"/v1/portfolios/{pid}/resume", headers=headers).json()
    assert body["want"] == "live"
    assert {leg["want"] for leg in body["legs"]} == {"live"}


def test_retiring_keeps_the_record(client):
    """A forward test somebody can erase is not a record."""
    headers = key_for("m@example.com")
    pid = make(client, headers).json()["portfolio_id"]
    body = client.delete(f"/v1/portfolios/{pid}", headers=headers).json()
    assert body["want"] == "retired"
    assert portfolios.get(pid) is not None
    assert len(portfolios.legs(pid, include_retired=True)) == 2


# ---------------------------------------------------------------------- who sees what

def test_a_member_does_not_see_another_members_portfolio(client):
    make(client, key_for("a@example.com"), name="theirs")
    rows = client.get("/v1/portfolios", headers=key_for("b@example.com")).json()
    assert rows == []


def test_a_strangers_id_answers_404_and_not_403(client):
    """A 403 confirms the id exists, which is the one bit an enumeration needs."""
    pid = make(client, key_for("a@example.com"), name="theirs").json()["portfolio_id"]
    r = client.get(f"/v1/portfolios/{pid}", headers=key_for("b@example.com"))
    assert r.status_code == 404


def test_a_member_cannot_pause_another_members_portfolio(client):
    pid = make(client, key_for("a@example.com"), name="theirs").json()["portfolio_id"]
    r = client.post(f"/v1/portfolios/{pid}/pause", headers=key_for("b@example.com"))
    assert r.status_code == 404
    assert portfolios.get(pid)["want"] == "live"


def test_every_member_can_read_the_house_portfolios(client):
    """The house book is the research made visible; reading it is why a member trusts it."""
    make(client, key_for("o@example.com", admin=True), name="house one")
    rows = client.get("/v1/portfolios", headers=key_for("m@example.com")).json()
    assert [r["account"] for r in rows] == ["00"]


def test_a_member_cannot_change_a_house_portfolio(client):
    pid = make(client, key_for("o@example.com", admin=True),
               name="house one").json()["portfolio_id"]
    assert client.post(f"/v1/portfolios/{pid}/pause",
                       headers=key_for("m@example.com")).status_code == 404
    assert portfolios.get(pid)["want"] == "live"


def test_signing_in_is_required(client):
    assert client.get("/v1/portfolios").status_code == 401


# ------------------------------------------------------------------- the combination

@pytest.fixture()
def fake_blend(monkeypatch):
    """The endpoint's job is to call the engine with the right legs, not to be it."""
    from stockhunt import blend as blend_mod
    seen = {}

    def fake(legs, capital=100_000.0, rebalance="monthly", start=None, **kw):
        seen.update(legs=list(legs), capital=capital, rebalance=rebalance, start=start)
        return {"axis": {"dates": ["2026-01-01"], "years": 1.0}, "curve": [100.0]}

    monkeypatch.setattr(blend_mod, "blend", fake)
    return seen


def test_the_backtest_blends_exactly_the_legs_the_portfolio_holds(client, fake_blend):
    headers = key_for("m@example.com")
    pid = make(client, headers).json()["portfolio_id"]
    r = client.get(f"/v1/portfolios/{pid}/backtest", headers=headers)
    assert r.status_code == 200
    assert fake_blend["legs"] == [("us_stocks", "1d", "ibs"),
                                  ("us_stocks", "1d", "SMA_200")]
    assert fake_blend["capital"] == 100_000.0


def test_a_portfolio_with_no_legs_says_so_rather_than_charting_nothing(client, fake_blend):
    headers = key_for("m@example.com")
    pid = client.post("/v1/portfolios", headers=headers,
                      json={"name": "top5", "kind": "follow", "source_cls": "us_stocks",
                            "source_tf": "1d"}).json()["portfolio_id"]
    r = client.get(f"/v1/portfolios/{pid}/backtest", headers=headers)
    assert r.status_code == 409


def test_the_preview_writes_nothing(client, fake_blend):
    """Choosing rules is exactly when somebody needs to know whether they are one bet."""
    headers = key_for("m@example.com")
    r = client.post("/v1/portfolios/preview", headers=headers,
                    json={"legs": [{"cls": "us_stocks", "tf": "1d", "rule": "ibs"}]})
    assert r.status_code == 200
    assert client.get("/v1/portfolios", headers=headers).json() == []


def test_the_preview_needs_at_least_one_leg(client, fake_blend):
    r = client.post("/v1/portfolios/preview", headers=key_for("m@example.com"),
                    json={"legs": []})
    assert r.status_code == 400


def test_legs_that_do_not_overlap_in_time_answer_with_the_reason(client, monkeypatch):
    from stockhunt import blend as blend_mod

    def boom(*a, **kw):
        raise blend_mod.BlendError("these legs share no dates")

    monkeypatch.setattr(blend_mod, "blend", boom)
    r = client.post("/v1/portfolios/preview", headers=key_for("m@example.com"),
                    json={"legs": [{"cls": "us_stocks", "tf": "1d", "rule": "ibs"}]})
    assert r.status_code == 409
    assert "share no dates" in r.json()["detail"]


# ------------------------------------------------------------------------ the log

def test_the_membership_log_records_what_was_bought(client):
    headers = key_for("m@example.com")
    pid = make(client, headers).json()["portfolio_id"]
    rows = client.get(f"/v1/portfolios/{pid}/changes", headers=headers).json()
    assert {r["rule"] for r in rows} == {"ibs", "SMA_200"}
    assert {r["action"] for r in rows} == {"added"}
    assert all(r["reason"] for r in rows), "a change with no reason is unreadable later"


# ------------------------------------------------------------------- changing a basket

def test_adding_a_leg_re_splits_the_pot(client):
    """One pot. A fifth leg takes the other four from a quarter each to a fifth."""
    headers = key_for("m@example.com")
    pid = make(client, headers).json()["portfolio_id"]
    body = client.post(f"/v1/portfolios/{pid}/legs", headers=headers,
                       json=[{"cls": "us_stocks", "tf": "1d", "rule": "MAXINDEX"}]).json()
    assert len(body["legs"]) == 3
    assert [leg["capital"] for leg in body["legs"]] == pytest.approx([100_000 / 3] * 3)


def test_adding_a_leg_already_held_changes_nothing(client):
    headers = key_for("m@example.com")
    pid = make(client, headers).json()["portfolio_id"]
    body = client.post(f"/v1/portfolios/{pid}/legs", headers=headers,
                       json=[{"cls": "us_stocks", "tf": "1d", "rule": "ibs"}]).json()
    assert len(body["legs"]) == 2
    rows = client.get(f"/v1/portfolios/{pid}/changes", headers=headers).json()
    assert len(rows) == 2, "an add that added nothing must not write a change"


def test_dropping_a_leg_keeps_its_record_and_re_splits(client):
    headers = key_for("m@example.com")
    legs = make(client, headers).json()["legs"]
    pid = legs[0]["portfolio_id"]
    body = client.delete(f"/v1/portfolios/{pid}/legs/{legs[0]['strategy_id']}",
                         headers=headers).json()
    assert [leg["capital"] for leg in body["legs"]] == [100_000.0]
    assert len(portfolios.legs(pid, include_retired=True)) == 2


def test_a_leg_that_is_not_in_this_basket_answers_404(client):
    headers = key_for("m@example.com")
    pid = make(client, headers).json()["portfolio_id"]
    r = client.delete(f"/v1/portfolios/{pid}/legs/str_nope", headers=headers)
    assert r.status_code == 404


def test_a_follow_portfolio_refuses_a_hand_added_leg(client):
    """The desk would retire it on the next pass, and nothing would announce that."""
    headers = key_for("m@example.com")
    pid = client.post("/v1/portfolios", headers=headers,
                      json={"name": "top5", "kind": "follow", "source_cls": "us_stocks",
                            "source_tf": "1d"}).json()["portfolio_id"]
    r = client.post(f"/v1/portfolios/{pid}/legs", headers=headers,
                    json=[{"cls": "us_stocks", "tf": "1d", "rule": "ibs"}])
    assert r.status_code == 409
    assert "retired on the next pass" in r.json()["detail"]


def test_a_member_cannot_add_a_leg_to_another_members_basket(client):
    pid = make(client, key_for("a@example.com"), name="theirs").json()["portfolio_id"]
    r = client.post(f"/v1/portfolios/{pid}/legs", headers=key_for("b@example.com"),
                    json=[{"cls": "us_stocks", "tf": "1d", "rule": "MAXINDEX"}])
    assert r.status_code == 404
    assert len(portfolios.legs(pid)) == 2
