"""Promoting a backtested rule, and the per-account cut of the live board.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_house_and_board.py -q

The board tests here are the ones that matter most in the whole suite. Everything else
fails loudly; a broken cut fails by showing one member another member's book, on a page
that looks completely normal.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import api_paths                                                        # noqa: F401
import api_live
import authdb
from stockhunt import deskdb

CATALOG = {
    "generated_at": "2026-08-14T00:00:00+00:00",
    "universe": {"us_stocks": ["SPY", "AAPL"], "crypto": ["BTC/USD"]},
    "timeframes": ["1d", "4h"],
    # What a promotion creates: one book per (class, timeframe, rule), holding whoever is
    # live in the class. No symbol is named anywhere — that is the point.
    "book": {
        "capital": 100000.0,
        "timeframes": ["1d"],
        "names": {"us_stocks": 100, "crypto": 10},
        "benchmark": {"us_stocks": "SPY", "crypto": None},
    },
    "health": {"warning": "Ranking is not passing.", "stale_sheets": ["crypto_1d"]},
    "sheets": {
        "us_stocks_1d": {"mtime": "2026-08-13T00:00:00+00:00",
                         "cells": [{"rule": "SMA_200", "ir_net": 0.1, "long_frac": 0.5},
                                   {"rule": "MAXINDEX", "ir_net": 0.05,
                                    "long_frac": 0.86}]},
        "crypto_1d": {"mtime": "2026-01-01T00:00:00+00:00",
                      "cells": [{"rule": "CORREL", "ir_net": 0.01}]},
    },
}

LIVE = {
    "generated_at": "2026-08-14 09:00 UTC",
    "feed": {"status": "ok"},
    "venue": {"name": "Nautilus sandbox", "balance": 30000.0, "equity": 31234.0},
    "strategies": [
        {"id": "00:spy-1d-sma_200", "account": "00", "symbol": "SPY",
         "capital": 10000.0, "equity": 10500.0},
        {"id": "01:meanrev", "account": "01", "symbol": "SPY",
         "capital": 10000.0, "equity": 10234.0},
        {"id": "02:theirs", "account": "02", "symbol": "QQQ",
         "capital": 10000.0, "equity": 10500.0},
    ],
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
    (web / "live.json").write_text(json.dumps(LIVE), encoding="utf-8")
    monkeypatch.setattr(api_live, "CATALOG_PATH", web / "catalog.json")
    monkeypatch.setattr(api_live, "LIVE_PATH", web / "live.json")

    import api_app
    with TestClient(api_app.create_app()) as c:
        yield c
    authdb.close()
    deskdb.close()


def key_for(email, admin=False):
    authdb.allow(email, is_admin=admin)
    raw, _ = authdb.create_api_key(email)
    return {"Authorization": f"Bearer {raw}"}


# ------------------------------------------------------------------------ the catalog

def test_a_member_can_read_the_catalog(client):
    """It IS the research, and seeing it is most of why a manager trusts the desk."""
    r = client.get("/v1/house/catalog", headers=key_for("m@example.com"))
    assert r.status_code == 200
    assert "us_stocks_1d" in r.json()["sheets"]


def test_the_catalog_carries_its_own_warning(client):
    """A menu showing one flattering number would be read as a recommendation."""
    doc = client.get("/v1/house/catalog", headers=key_for("m@example.com")).json()
    assert "Ranking is not passing" in doc["health"]["warning"]
    cells = doc["sheets"]["us_stocks_1d"]["cells"]
    # `long_frac` beside `ir_net`, so a rule that is simply always invested is visible
    # as such rather than as a winner.
    assert all("long_frac" in c for c in cells)


# ----------------------------------------------------------------------- promoting

def test_the_owner_can_promote_a_rule(client):
    r = client.post("/v1/house/strategies", headers=key_for("o@example.com", admin=True),
                    json={"cls": "us_stocks", "tf": "1d", "rule": "SMA_200"})
    assert r.status_code == 201
    body = r.json()
    assert body["kind"] == "book" and body["rule"] == "SMA_200"
    assert body["state"] == "pending", "the API owns no trading; the desk applies it"
    # Named for what it is. There is no symbol in it, because a book holds the class.
    assert body["name"] == "us_stocks-1d-sma_200"
    assert body["symbols"] == [], "the roster is read live, never frozen at registration"
    assert body["capital"] == 100_000.0
    assert body["benchmark"] == "SPY", "declared per class, never inferred from holdings"
    assert body["strategy_id"].startswith("str_00_")


def test_a_member_cannot_promote(client):
    r = client.post("/v1/house/strategies", headers=key_for("m@example.com"),
                    json={"cls": "us_stocks", "tf": "1d", "rule": "SMA_200"})
    assert r.status_code == 403
    assert deskdb.registrations("00") == []


def test_a_rule_that_is_not_on_the_sheet_is_refused(client):
    """The menu is the contract. A rule the research never ranked cannot be traded live
    just because somebody typed its name."""
    r = client.post("/v1/house/strategies", headers=key_for("o@example.com", admin=True),
                    json={"cls": "us_stocks", "tf": "1d", "rule": "MADE_UP"})
    assert r.status_code == 400 and "not on the" in r.json()["detail"]


def test_a_symbol_is_not_something_a_promotion_can_name(client):
    """There is no per-symbol promotion any more. A stray `symbol` in the body is ignored
    rather than honoured, so an old client cannot quietly get a one-name book."""
    r = client.post("/v1/house/strategies", headers=key_for("o@example.com", admin=True),
                    json={"cls": "us_stocks", "tf": "1d", "rule": "SMA_200",
                          "symbol": "TSLA"})
    assert r.status_code == 201
    assert r.json()["symbols"] == []


def test_a_timeframe_without_books_is_refused(client):
    """Daily first, deliberately — one accounting model live at a time is easier to trust
    than two that look alike on the same board."""
    r = client.post("/v1/house/strategies", headers=key_for("o@example.com", admin=True),
                    json={"cls": "us_stocks", "tf": "4h", "rule": "SMA_200"})
    assert r.status_code in (400, 404)
    assert deskdb.registrations("00") == []


def test_a_class_with_nobody_live_is_refused(client):
    """A book with no names to hold is not a book."""
    doc = json.loads(api_live.CATALOG_PATH.read_text(encoding="utf-8"))
    doc["book"]["names"]["us_stocks"] = 0
    api_live.CATALOG_PATH.write_text(json.dumps(doc), encoding="utf-8")
    r = client.post("/v1/house/strategies", headers=key_for("o@example.com", admin=True),
                    json={"cls": "us_stocks", "tf": "1d", "rule": "SMA_200"})
    assert r.status_code == 400 and "nothing for a book to hold" in r.json()["detail"]


def test_promoting_from_a_stale_sheet_says_so(client):
    """A sheet older than the data corrections it was computed from ranked rules over a
    different sample. Saying so at promotion time beats finding out from a number weeks
    later."""
    r = client.post("/v1/house/strategies", headers=key_for("o@example.com", admin=True),
                    json={"cls": "crypto", "tf": "1d", "rule": "CORREL"})
    assert r.status_code == 201 and r.json()["sheet_is_stale"] is True


def test_promoting_twice_is_one_strategy(client):
    h = key_for("o@example.com", admin=True)
    body = {"cls": "us_stocks", "tf": "1d", "rule": "SMA_200"}
    first = client.post("/v1/house/strategies", headers=h, json=body).json()
    again = client.post("/v1/house/strategies", headers=h, json=body).json()
    assert first["strategy_id"] == again["strategy_id"]
    assert len(deskdb.registrations("00")) == 1


def test_the_house_desk_is_visible_to_members(client):
    client.post("/v1/house/strategies", headers=key_for("o@example.com", admin=True),
                json={"cls": "us_stocks", "tf": "1d", "rule": "SMA_200"})
    seen = client.get("/v1/house/strategies", headers=key_for("m@example.com")).json()
    assert [s["rule"] for s in seen] == ["SMA_200"]


# ----------------------------------------------------------------- the per-account cut

def test_a_member_sees_their_own_book_and_the_house(client):
    doc = api_live.visible_to(LIVE, "01")
    ids = [s["id"] for s in doc["strategies"]]
    assert ids == ["00:spy-1d-sma_200", "01:meanrev"]


def test_a_member_never_sees_another_members_book(client):
    for account in ("01", "02"):
        doc = api_live.visible_to(LIVE, account)
        others = [s for s in doc["strategies"]
                  if s["account"] not in ("00", account)]
        assert others == [], f"{account} saw {others}"


def test_the_venue_total_is_recomputed_not_passed_through(client):
    """The leak with no strategy names attached, and the kind that survives review: the
    published total is the WHOLE desk's, so handing it to one member reports the size of
    everybody else's book."""
    doc = api_live.visible_to(LIVE, "01")
    assert doc["venue"]["equity"] == 20734.0        # 10500 + 10234, not 31234
    assert doc["venue"]["balance"] == 20000.0
    assert doc["venue"]["equity"] < LIVE["venue"]["equity"]


def test_the_owner_sees_every_account(client):
    """A deliberate widening, and the one asymmetry in the cut.

    Somebody has to be able to answer "what is running on my desk". The alternative was a
    second endpoint with a second implementation of the same filter, which is the version
    that drifts. What keeps it honest is that every row still carries its `account` and
    the page separates mine from members' with a filter — so the owner reads the same
    SHAPE a member does, one group at a time, rather than a differently-built page.
    """
    doc = api_live.visible_to(LIVE, "01", is_admin=True)
    assert [s["id"] for s in doc["strategies"]] == [
        "00:spy-1d-sma_200", "01:meanrev", "02:theirs"]
    assert doc["is_admin"] is True
    # The venue total is still re-derived over what is shown, not passed through.
    assert doc["venue"]["equity"] == 31234.0


def test_a_member_is_not_widened_by_asking(client):
    """The widening is on `is_admin`, which comes from the session — never from anything
    the caller supplies."""
    doc = api_live.visible_to(LIVE, "01", is_admin=False)
    assert [s["id"] for s in doc["strategies"]] == ["00:spy-1d-sma_200", "01:meanrev"]
    assert doc["is_admin"] is False


def test_the_reader_is_named_in_every_frame(client):
    """The board's "Whose" filter needs to know who is looking and which account is the
    house; without them it cannot tell mine from anybody else's."""
    doc = api_live.visible_to(LIVE, "01")
    assert doc["account"] == "01" and doc["house"] == "00"


def test_an_untagged_strategy_counts_as_the_house(client):
    """Rows written before accounts existed carry no tag. Treating them as nobody's would
    hide the desk's own history from everyone."""
    doc = api_live.visible_to({"strategies": [{"id": "old", "capital": 1.0}]}, "01")
    assert len(doc["strategies"]) == 1


def test_a_missing_live_file_is_a_stopped_desk_not_a_crash(client):
    doc = api_live.visible_to(None, "01")
    assert doc["strategies"] == [] and doc["feed"]["status"] == "stopped"


def test_live_json_is_not_served_off_disk(client):
    """The trap: `api_board` generates a GET route for every allowlisted file, and
    `/live.json` is on that list. A static route would shadow the filtered handler and
    hand every member the whole desk."""
    import api_board
    assert "/live.json" in api_board.PER_ACCOUNT
    routes = [r.path for r in api_board.router.routes]
    assert routes.count("/live.json") == 1, "registered twice; one shadows the other"


def test_live_json_needs_a_session(client):
    assert client.get("/live.json").status_code == 401


# ------------------------------------------------------------------ the manager console

def test_the_console_is_behind_the_login(client):
    """A page that lists your strategies and mints keys must not be public."""
    r = client.get("/desk", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"


def test_the_console_opens_for_a_signed_in_reader(client, monkeypatch):
    import api_auth
    import api_board
    monkeypatch.setattr(api_auth, "optional_session",
                        lambda request: {"email": "m@example.com", "account_id": "01",
                                         "is_admin": 0})
    r = client.get("/desk")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.text
    # It is this folder's own page, not the generated board — which is build output and
    # must never be hand-edited.
    assert api_board.DESK_PAGE.parent == api_board.api_paths.WEB
    assert "<title>Manager desk" in body
    # The board's nav is carried here too. Losing an entry on this page strands a reader:
    # `/desk` is a real page served by this process, not a route inside the board app, so
    # the back-and-forth the board's own nav provides does not come with it — which is
    # exactly how `Portfolios` came to vanish the moment somebody opened the console.
    #
    # The hrefs are the CANONICAL ones, not the old `/#/paper` hash routes: those were the
    # vanilla board's addresses and the export now answers on real paths. This list and
    # `dashboard-next/components/Nav.tsx` are two copies of one menu, and this assertion is
    # the only thing that notices when they drift.
    for href in ('href="/paper"', 'href="/portfolio"', 'href="/"', 'href="/desk"'):
        assert href in body, href


def test_the_console_pulls_nothing_from_the_generated_board(client):
    """Self-contained, like the login page. An asset added to it later would otherwise
    become a dependency on a directory this process only ever reads."""
    import api_board
    body = api_board.DESK_PAGE.read_text(encoding="utf-8")
    assert "<script src=" not in body and "<link rel=\"stylesheet\"" not in body


def test_a_row_the_desk_cannot_trade_is_refused(client):
    """The board merges families that are not equally runnable live — a combo is rebuilt
    from its legs and has no single definition a strategy can hold. Refusing at the click
    beats a registration that sits pending and is rejected minutes later."""
    import json as _json
    import api_live
    doc = _json.loads(api_live.CATALOG_PATH.read_text(encoding="utf-8"))
    doc["sheets"]["us_stocks_1d"]["cells"].append({
        "rule": "SMA_50~RSI_14|and", "tradable": False,
        "not_tradable_because": "a combination of two rules"})
    api_live.CATALOG_PATH.write_text(_json.dumps(doc), encoding="utf-8")

    r = client.post("/v1/house/strategies", headers=key_for("o@example.com", admin=True),
                    json={"cls": "us_stocks", "tf": "1d", "rule": "SMA_50~RSI_14|and"})
    assert r.status_code == 400
    assert "cannot be traded live" in r.json()["detail"]
    assert deskdb.registrations("00") == []


def test_a_published_strategy_can_be_promoted(client):
    """`ibs` and `volmanaged` lead the real board and are NOT in wf_summary at all —
    they come from `strategies/published/`. The desk must be able to run them."""
    import json as _json
    import api_live
    doc = _json.loads(api_live.CATALOG_PATH.read_text(encoding="utf-8"))
    doc["sheets"]["us_stocks_1d"]["cells"].insert(
        0, {"rule": "ibs", "kind": "published", "family": "published", "tradable": True})
    api_live.CATALOG_PATH.write_text(_json.dumps(doc), encoding="utf-8")

    r = client.post("/v1/house/strategies", headers=key_for("o@example.com", admin=True),
                    json={"cls": "us_stocks", "tf": "1d", "rule": "ibs"})
    assert r.status_code == 201 and r.json()["rule"] == "ibs"
