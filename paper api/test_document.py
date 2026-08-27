"""`/v1/board` serves the baked document, and never the heavy half of it.

The point of these is the exclusion. `data.js` is 3.7 MB and 87% of it is the `backtest`
section that the paged leaderboard exists to stop shipping; an endpoint that quietly
included it would undo that with nothing on screen to say so.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import api_document
import api_live
import api_paths                                                        # noqa: F401
import authdb
from stockhunt import deskdb, resultsdb


DOC = {
    "generated_at": "2026-08-27 13:15 UTC",
    "feed": {"source": "Twelve Data", "status": "ok"},
    "venue": {"name": "Nautilus sandbox", "equity": 1.0},
    "timeframes": ["1d", "4h"],
    "paper_timeframes": ["1d", "4h", "1h"],
    "paper_groups": [{"key": "equities", "note": "a universe"}],
    "edge_criteria": [{"k": "S", "name": "dsharpe"}],
    "summary": {"anything": 1},
    "research": {"anything": 2},
    "curves": {"us_stocks_1d": {"rules": ["ibs"]}},
    "robust": {"file": "robust.json"},
    # The three that must never come back from /meta.
    "backtest": {"equities": {"sheets": [{"rows": ["...3.2 MB of this..."]}]}},
    "logic": {"ibs": {"logic": "buy the low of the day", "family": "reversion"}},
    "strategies": [{"id": "00:us_stocks-1d-ibs", "trades": []}],
}


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A `data.js` shaped exactly like the builder writes one."""
    f = tmp_path / "data.js"
    f.write_text("/* GENERATED */\nwindow.DEMO = false;\nwindow.DASH = "
                 + json.dumps(DOC) + ";\n", encoding="utf-8")
    monkeypatch.setattr(api_document, "DATA_JS", f)
    api_document._parse.cache_clear()
    yield f
    api_document._parse.cache_clear()


@pytest.fixture()
def anon(tmp_path, monkeypatch, board):
    """The app, with every store repointed. Same construction as `test_research.py`."""
    authdb.use(tmp_path / "auth.db")
    deskdb.use(tmp_path / "desk.db")
    resultsdb.use(tmp_path / "results.db")
    authdb.connect()
    deskdb.connect()

    web = tmp_path / "web"
    web.mkdir()
    (web / "catalog.json").write_text("{}", encoding="utf-8")
    (web / "live.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(api_live, "CATALOG_PATH", web / "catalog.json")
    monkeypatch.setattr(api_live, "LIVE_PATH", web / "live.json")

    import api_app
    with TestClient(api_app.create_app()) as c:
        yield c
    authdb.close()
    deskdb.close()
    resultsdb.close()


class _Signed:
    """A client carrying a member's API key on every request."""

    def __init__(self, client, headers):
        self._client, self._headers = client, headers

    def get(self, url, **kw):
        return self._client.get(url, headers=self._headers, **kw)


@pytest.fixture()
def client(anon):
    authdb.allow("m@example.com")
    raw, _ = authdb.create_api_key("m@example.com")
    return _Signed(anon, {"Authorization": f"Bearer {raw}"})


def test_meta_carries_the_small_sections(client):
    r = client.get("/v1/board/meta")
    assert r.status_code == 200
    body = r.json()
    for k in ("generated_at", "feed", "venue", "timeframes", "paper_timeframes",
              "paper_groups", "edge_criteria", "summary", "research", "curves", "robust"):
        assert k in body, k


def test_meta_never_carries_the_heavy_ones(client):
    """The whole reason this endpoint exists rather than serving `data.js`."""
    body = client.get("/v1/board/meta").json()
    assert "backtest" not in body
    assert "logic" not in body
    assert "strategies" not in body


def test_meta_is_small(client):
    """A guard on the exclusion holding, not a taste in byte counts.

    The real document's `meta` half is ~48 KB against 3.7 MB whole; a section leaking in
    would blow past this by orders of magnitude rather than by a little.
    """
    assert len(client.get("/v1/board/meta").content) < 64 * 1024


def test_a_document_from_an_older_builder_still_answers(client, board):
    """A missing key is omitted, never sent as null."""
    trimmed = {k: v for k, v in DOC.items() if k != "research"}
    board.write_text("window.DASH = " + json.dumps(trimmed) + ";", encoding="utf-8")
    api_document._parse.cache_clear()
    body = client.get("/v1/board/meta").json()
    assert "research" not in body
    assert "timeframes" in body


def test_logic_is_one_rule_at_a_time(client):
    r = client.get("/v1/board/logic/ibs")
    assert r.status_code == 200
    assert r.json()["family"] == "reversion"


def test_an_unrecorded_rule_is_a_404(client):
    assert client.get("/v1/board/logic/nosuchrule").status_code == 404


def test_a_pair_label_survives_the_path(client, board):
    """`A~B|and` and `ha:chart:ibs@buy=0.3` are dictionary keys here, not paths."""
    doc = dict(DOC, logic={"MININDEX~SAREXT|and": {"logic": "both legs agree"}})
    board.write_text("window.DASH = " + json.dumps(doc) + ";", encoding="utf-8")
    api_document._parse.cache_clear()
    r = client.get("/v1/board/logic/MININDEX~SAREXT%7Cand")
    assert r.status_code == 200
    assert r.json()["logic"] == "both legs agree"


def test_systems_is_the_baked_snapshot(client):
    body = client.get("/v1/board/systems").json()
    assert body["strategies"][0]["id"] == "00:us_stocks-1d-ibs"
    assert body["feed"]["source"] == "Twelve Data"


def test_an_unbuilt_board_is_a_503_naming_the_command(client, board):
    board.unlink()
    api_document._parse.cache_clear()
    r = client.get("/v1/board/meta")
    assert r.status_code == 503
    assert "build_dashboard.py" in r.json()["detail"]


def test_a_corrupt_document_is_a_503_not_a_500(client, board):
    board.write_text("window.DASH = {not json at all", encoding="utf-8")
    api_document._parse.cache_clear()
    assert client.get("/v1/board/meta").status_code == 503


def test_a_rebuild_is_picked_up_without_a_restart(client, board):
    """The mtime key, which is what makes the memo legal behind an endpoint."""
    assert client.get("/v1/board/meta").json()["timeframes"] == ["1d", "4h"]
    board.write_text("window.DASH = " + json.dumps(dict(DOC, timeframes=["1d"])) + ";",
                     encoding="utf-8")
    # Same test, one stat() later: the parse is keyed on mtime, so nothing is remembered
    # across a write. `cache_clear` is deliberately NOT called here.
    import os
    st = board.stat()
    os.utime(board, (st.st_atime, st.st_mtime + 10))
    assert client.get("/v1/board/meta").json()["timeframes"] == ["1d"]


def test_it_is_behind_the_login(anon):
    assert anon.get("/v1/board/meta").status_code == 401


def test_an_overlay_falls_back_to_the_base_rule(client, board):
    """`ha:chart:ibs@buy=0.3` is `ibs` with three things done to it."""
    doc = dict(DOC, logic={"ibs": {"logic": "buy the low of the day"}})
    board.write_text("window.DASH = " + json.dumps(doc) + ";", encoding="utf-8")
    api_document._parse.cache_clear()
    body = client.get("/v1/board/logic/ha:chart:ibs@buy%3D0.3").json()
    assert body["logic"] == "buy the low of the day"
    # ...and it says WHICH key answered, so a page can be honest that this is the base
    # rule's description rather than the variant's.
    assert body["matched"] == "ibs"


def test_a_pair_comes_back_as_its_legs_in_one_request(client, board):
    """Resolved server-side: a client doing it makes three round trips and needs the grammar."""
    doc = dict(DOC, logic={"MININDEX": {"logic": "the low is oldest"},
                           "SAREXT": {"logic": "parabolic stop"}})
    board.write_text("window.DASH = " + json.dumps(doc) + ";", encoding="utf-8")
    api_document._parse.cache_clear()
    body = client.get("/v1/board/logic/MININDEX~SAREXT%7Cand").json()
    assert body["op"] == "and"
    assert [leg["leg"] for leg in body["legs"]] == ["MININDEX", "SAREXT"]
    assert body["legs"][1]["logic"] == "parabolic stop"


def test_a_pair_whose_legs_are_unrecorded_is_still_a_404(client, board):
    board.write_text("window.DASH = " + json.dumps(dict(DOC, logic={})) + ";",
                     encoding="utf-8")
    api_document._parse.cache_clear()
    assert client.get("/v1/board/logic/AAA~BBB%7Cand").status_code == 404


def test_meta_carries_the_group_labels_but_not_the_section_they_live_in(client):
    """The tab strip IS this list, so a board that cannot see it cannot draw its filters.

    Five short rows lifted out of a 3.2 MB section is not the same as carrying the section:
    `backtest` itself must still be absent, which the test above asserts.
    """
    body = client.get("/v1/board/meta").json()
    assert body["groups"] == [{"key": "equities"}]
    assert "backtest" not in body


def test_group_labels_survive_a_document_that_has_them(client, board):
    doc = dict(DOC, backtest={"stocks": {"label": "Top 100 US stocks, point-in-time",
                                         "n": 216, "sheets": ["...heavy..."]}})
    board.write_text("window.DASH = " + json.dumps(doc) + ";", encoding="utf-8")
    api_document._parse.cache_clear()
    body = client.get("/v1/board/meta").json()
    assert body["groups"] == [{"key": "stocks", "label": "Top 100 US stocks, point-in-time",
                               "n": 216}]
    # ...and the `sheets` payload they were lifted out of does not come with them.
    assert "sheets" not in json.dumps(body)
