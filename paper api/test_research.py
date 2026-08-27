"""`/v1/research` — reading the board, and queuing a rule onto it.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_research.py -q

The store is seeded with synthetic rows rather than pointed at the real `results.db`.
Nothing here needs a real backtest: what is being tested is that the endpoint ranks what
the store holds, refuses what it cannot score, and cannot show one member another's
submissions. A test that broke when somebody re-ran a sweep is a test nobody would trust.

The one test worth reading twice is `test_a_new_row_appears_with_no_rebuild`. It is the
whole point of this layer — before it, a scored rule reached the page only when somebody
re-ran `build_dashboard.py`, because the board was a constant in `data.js`.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import api_paths                                                        # noqa: F401
import api_live
import authdb
from stockhunt import deskdb, resultsdb

GATES = [{"key": "dsharpe", "letter": "S", "label": "Delta-Sharpe", "target": ">= 0.10"},
         {"key": "t", "letter": "T", "label": "t across folds", "target": ">= 2.0"}]

CLS, TF, SCEN = "crypto", "1d", "retail"


def wf(rule, ir=0.2, **over):
    row = {"class": CLS, "timeframe": TF, "rule": rule, "scenario": SCEN,
           "ir_net": ir, "ir_hit_rate": 0.5, "t_stat": 1.1, "years": 6.0,
           "n_folds": 6, "rankable": True, "is_baseline": False,
           "wf_mode": "published", "long_frac": 0.5, "exposure": 0.5,
           "excess_return_pct": 3.0, "strategy": rule, "family": "reversion",
           "source": "test"}
    row.update(over)
    return row


def edge(rule, passed=1, dsharpe=0.2, side="long"):
    return {"class": CLS, "tf": TF, "rule": rule, "side": side,
            "edge_dsharpe": dsharpe, "edge_t": 1.5, "edge_vs_random": 0.1,
            "edge_vs_constant": 0.1, "wealth": 12000.0, "bench_wealth": 11000.0,
            "edge_wealth": 1000.0, "edge_headroom": 2.0, "sharpe": 0.8,
            "bench_sharpe": 0.6, "max_dd": -0.3, "bench_max_dd": -0.5,
            "profit_factor": 1.2, "trades_per_asset": 50, "noise_ceiling": 0.4,
            "exposure": 0.5, "n_assets": 20, "years": 6.0, "n_trials": 1134,
            "edge_passed": passed, "edge_n": 2, "edge_verdict": "partial",
            "edge_powered": True, "edge_rankable": True,
            "edge_gate_dsharpe": True, "edge_gate_t": False}


def book(rule, excess=1.5, trades=100):
    return {"class": CLS, "tf": TF, "rule": rule, "cashmatch_excess_cagr": excess,
            "n_trades": trades, "n_names": 20, "years": 6.0, "wealth": 12000.0,
            "bench_wealth": 11000.0, "bench_cagr": 2.0, "cagr": 3.5,
            "edge_passed": 1, "edge_n": 2, "edge_verdict": "partial",
            "edge_powered": True, "edge_rankable": True, "n_folds_scored": 6,
            "edge_gate_dsharpe": True, "edge_gate_t": False}


def seed(rules=("alpha", "beta")):
    resultsdb.set_meta("gates", GATES)
    resultsdb.set_meta("headline", {CLS: SCEN})
    resultsdb.set_meta("timeframes", [TF])
    resultsdb.set_meta("top_n", 30)
    resultsdb.set_meta("groups", [{"key": "crypto", "cls": CLS, "label": "20 pairs",
                                  "universe": ["BTC/USD", "ETH/USD"]}])
    # `single`, not `published`: `board_rank.build_sheet` builds from the singles frame
    # and merges pairs and published strategies onto it, which is what `wf_summary_*.csv`
    # was. Seeding only published rows gives a sheet with no base frame and it answers
    # None — correctly, and confusingly if the fixture is what is wrong.
    for i, r in enumerate(rules):
        resultsdb.put_wf([wf(r, ir=0.3 - 0.1 * i)], "single")
        resultsdb.put_edge([edge(r, dsharpe=0.3 - 0.1 * i)])
        resultsdb.put_book([book(r, excess=3.0 - i)])
        resultsdb.put_per_asset([
            {"cls": CLS, "tf": TF, "rule": r, "side": "long", "symbol": s,
             "src": "riskmatch", "ir": 0.2, "years": 6.0, "net_cagr": 4.0,
             "bh_cagr": 2.0, "net_pct": 30.0, "bench_pct": 12.0}
            for s in ("BTC/USD", "ETH/USD")])


@pytest.fixture()
def client(tmp_path, monkeypatch):
    authdb.use(tmp_path / "auth.db")
    deskdb.use(tmp_path / "desk.db")
    resultsdb.use(tmp_path / "results.db")
    authdb.connect()
    deskdb.connect()
    seed()

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


def key_for(email, admin=False):
    authdb.allow(email, is_admin=admin)
    raw, _ = authdb.create_api_key(email)
    return {"Authorization": f"Bearer {raw}"}


# ------------------------------------------------------------------------- reading

def test_the_board_needs_a_session(client):
    assert client.get(f"/v1/research/leaderboard?cls={CLS}&tf={TF}").status_code == 401


def test_the_leaderboard_pages_without_moving_the_sheet_under_the_reader(client):
    """A page is a window on the ROWS. Every population statistic must survive it.

    The failure this guards would look like a data bug rather than a paging one: recompute
    `n_rules` or `noise_ceiling` over the page instead of over the population, and page 2
    tells the reader a different number of rules was searched than page 1 did. That number
    is what every `dsr` on the sheet is deflated against, so two screens of one leaderboard
    would disagree about how much to believe.
    """
    head = key_for("m@example.com")
    q = f"/v1/research/leaderboard?cls={CLS}&tf={TF}"
    first = client.get(f"{q}&offset=0&limit=1", headers=head).json()
    second = client.get(f"{q}&offset=1&limit=1", headers=head).json()
    assert [r["rule"] for r in first["rows"]] == ["alpha"]
    assert [r["rule"] for r in second["rows"]] == ["beta"]
    for k in ("n_rules", "n_ranked", "noise_ceiling", "exposure_corr", "years", "universe"):
        assert first[k] == second[k], f"{k} moved between pages"

    # `limit=0` is the cheap probe: how deep is this sheet, before asking for any of it.
    header_only = client.get(f"{q}&limit=0", headers=head).json()
    assert header_only["rows"] == []
    assert header_only["n_ranked"] == first["n_ranked"] == 2

    # Past the end is empty -- not an error, and not a wrapped-around first page.
    assert client.get(f"{q}&offset=99&limit=5", headers=head).json()["rows"] == []

    # Omitting `limit` keeps the pre-pagination behaviour for every existing caller.
    assert client.get(q, headers=head).json()["offset"] == 0

    # The cap is a response-size bound, so asking past it is refused rather than served.
    assert client.get(f"{q}&limit=100000", headers=head).status_code == 422
    assert client.get(f"{q}&offset=-1", headers=head).status_code == 422


def test_the_leaderboard_ranks_what_the_store_holds(client):
    r = client.get(f"/v1/research/leaderboard?cls={CLS}&tf={TF}",
                   headers=key_for("m@example.com"))
    assert r.status_code == 200
    sheet = r.json()
    assert [row["rule"] for row in sheet["rows"]] == ["alpha", "beta"]
    assert sheet["ranked_on"] == "edge_passed"
    assert sheet["ranked_tiebreak"] == "book_cm_excess_cagr"


def test_an_unscored_sheet_answers_404_rather_than_an_empty_board(client):
    r = client.get(f"/v1/research/leaderboard?cls={CLS}&tf=4h",
                   headers=key_for("m@example.com"))
    assert r.status_code == 404
    assert "crypto/1d" in r.json()["detail"], "say which sheets DO exist"


def test_the_rule_page_carries_every_name(client):
    r = client.get(f"/v1/research/rule/{CLS}/{TF}/alpha", headers=key_for("m@example.com"))
    assert r.status_code == 200
    body = r.json()
    assert {row["symbol"] for row in body["rows"]} == {"BTC/USD", "ETH/USD"}
    assert body["stats"]["n"] == 2


# ---------------------------------------------------- the population statistics move

def test_a_new_row_appears_with_no_rebuild(client):
    """The whole reason this layer exists.

    Before it, a scored rule reached the page only when somebody re-ran
    `build_dashboard.py`, because the leaderboard was a constant baked into `data.js`.
    """
    head = key_for("m@example.com")
    before = client.get(f"/v1/research/leaderboard?cls={CLS}&tf={TF}", headers=head).json()
    assert len(before["rows"]) == 2

    resultsdb.put_wf([wf("gamma", ir=0.9)], "single")
    resultsdb.put_edge([edge("gamma", passed=2, dsharpe=0.9)])
    resultsdb.put_book([book("gamma", excess=9.0)])

    after = client.get(f"/v1/research/leaderboard?cls={CLS}&tf={TF}", headers=head).json()
    assert [row["rule"] for row in after["rows"]] == ["gamma", "alpha", "beta"]


def test_the_noise_ceiling_moves_when_the_population_does(client):
    """A population statistic recomputed per request, not read back from a build.

    The ceiling is where the best of N worthless candidates lands by luck, so it rises
    with N. Baked into a payload it silently describes whatever the search size was on the
    day somebody last ran the builder.
    """
    head = key_for("m@example.com")
    before = client.get(f"/v1/research/leaderboard?cls={CLS}&tf={TF}", headers=head).json()
    for i in range(20):
        resultsdb.put_wf([wf(f"pad{i}", ir=0.01)], "single")
    after = client.get(f"/v1/research/leaderboard?cls={CLS}&tf={TF}", headers=head).json()
    assert after["n_rules"] > before["n_rules"]
    assert after["noise_ceiling"] > before["noise_ceiling"]


# ----------------------------------------------------------------------- submitting

def test_a_variant_can_be_queued(client):
    r = client.post("/v1/research/trials", headers=key_for("m@example.com"),
                    json={"label": "alpha@buy=0.35", "cls": CLS, "tf": TF,
                          "why": "is the published cut arbitrary?"})
    assert r.status_code == 202, "202 is queued, not ranked"
    body = r.json()
    assert body["state"] == "queued" and body["kind"] == "label"
    assert resultsdb.jobs(state="queued")[0]["label"] == "alpha@buy=0.35"


def test_a_malformed_label_is_refused_before_anything_is_queued(client):
    for bad in ("rm -rf /", "../../etc/passwd", "two words", "x" * 200):
        r = client.post("/v1/research/trials", headers=key_for("m@example.com"),
                        json={"label": bad, "cls": CLS, "tf": TF})
        assert r.status_code == 422, bad
    assert resultsdb.jobs() == []


def test_a_sheet_that_was_never_scored_is_refused(client):
    r = client.post("/v1/research/trials", headers=key_for("m@example.com"),
                    json={"label": "alpha", "cls": "us_stocks", "tf": "1d"})
    assert r.status_code == 404
    assert resultsdb.jobs() == [], "queuing it would burn a run to discover there are no bars"


def test_a_code_submission_is_queued_with_its_source(client):
    r = client.post("/v1/research/strategies", headers=key_for("m@example.com"),
                    json={"name": "my_reversion", "code": "def position(): ...\nGRID = ()",
                          "cls": CLS, "tf": TF})
    assert r.status_code == 202
    job = resultsdb.job(r.json()["job_id"])
    assert job["kind"] == "code" and "GRID" in job["code"]
    assert "code" not in r.json(), "a job listing is polled; do not echo the source back"


def test_a_strategy_name_that_could_be_a_path_is_refused(client):
    for bad in ("../evil", "My_Rule", "a", "x/y"):
        r = client.post("/v1/research/strategies", headers=key_for("m@example.com"),
                        json={"name": bad, "code": "x = 1", "cls": CLS, "tf": TF})
        assert r.status_code == 422, bad


def test_an_oversized_module_is_refused(client):
    r = client.post("/v1/research/strategies", headers=key_for("m@example.com"),
                    json={"name": "big_one", "code": "#" * 70_000, "cls": CLS, "tf": TF})
    assert r.status_code == 413


def test_submissions_are_rate_limited_from_the_store(client, monkeypatch):
    """Counted from the store, so restarting the API does not hand a looping bot a fresh
    allowance — which is exactly when it would be hammering."""
    import api_config
    monkeypatch.setattr(api_config, "MAX_TRIALS_PER_MINUTE", 2)
    head = key_for("m@example.com")
    codes = [client.post("/v1/research/trials", headers=head,
                         json={"label": f"alpha@buy=0.{i}", "cls": CLS, "tf": TF}
                         ).status_code for i in range(3)]
    assert codes == [202, 202, 429]


# --------------------------------------------------------------------------- privacy

def test_a_member_cannot_read_another_members_job(client):
    mine = client.post("/v1/research/trials", headers=key_for("a@example.com"),
                       json={"label": "alpha", "cls": CLS, "tf": TF}).json()
    r = client.get(f"/v1/research/jobs/{mine['job_id']}", headers=key_for("b@example.com"))
    assert r.status_code == 404, "404 not 403 — a 403 confirms the id exists"
    assert client.get("/v1/research/jobs", headers=key_for("b@example.com")).json() == []


def test_the_owner_can_read_any_job(client):
    mine = client.post("/v1/research/trials", headers=key_for("a@example.com"),
                       json={"label": "alpha", "cls": CLS, "tf": TF}).json()
    r = client.get(f"/v1/research/jobs/{mine['job_id']}",
                   headers=key_for("o@example.com", admin=True))
    assert r.status_code == 200


# ------------------------------------------------------------------- one row, by label

def test_a_row_carries_its_rank_and_its_sheet_context(client):
    """The detail page's hero strip, in one request instead of ten.

    Without this the only way to reach a rule's own row is to page the sheet looking for
    the label -- on us_stocks 1d, ten requests and ~500 rows to render seven numbers, which
    is exactly the shape of thing the paging work exists to stop.
    """
    head = key_for("m@example.com")
    r = client.get(f"/v1/research/row/{CLS}/{TF}/alpha", headers=head)
    assert r.status_code == 200
    body = r.json()
    assert body["rule"] == "alpha"
    # The row itself...
    assert body["book"] is not None
    assert body["edge"] is not None
    # ...and the SHEET's context, which cannot be derived from the row. A reader who
    # arrived from a link has no other way to know whether this is 1st or 300th.
    assert body["rank"] == 1
    assert body["n_ranked"] == 2
    assert body["noise_ceiling"] is not None


def test_the_row_agrees_with_the_page_it_came_from(client):
    """Two routes, one `leaderboard_entry`. If these ever disagree, the board has two
    answers to `which rule is better` and neither is trustworthy."""
    head = key_for("m@example.com")
    page = client.get(f"/v1/research/leaderboard?cls={CLS}&tf={TF}&offset=1&limit=1",
                      headers=head).json()
    row = client.get(f"/v1/research/row/{CLS}/{TF}/beta", headers=head).json()
    assert row["rank"] == 2
    for k in ("rule", "kind", "ir_net", "t_stat", "edge", "book"):
        assert row[k] == page["rows"][0][k], k


def test_a_row_carries_its_asset_table(client):
    """`assets=True` here, unlike the leaderboard's default: a detail page is the one
    caller that draws it, and one row's worth is ~29 KB rather than a MB."""
    head = key_for("m@example.com")
    body = client.get(f"/v1/research/row/{CLS}/{TF}/alpha", headers=head).json()
    assert [r["symbol"] for r in body["per_asset"]] == ["BTC/USD", "ETH/USD"]


def test_an_unranked_label_is_a_404_that_points_somewhere(client):
    """Not ranked is not the same as unknown, and the message has to say so: an off-board
    rule still answers on /rule and /curve, which is what the detail page falls back to."""
    head = key_for("m@example.com")
    r = client.get(f"/v1/research/row/{CLS}/{TF}/nosuchrule", headers=head)
    assert r.status_code == 404
    assert "/v1/research/rule/" in r.json()["detail"]


def test_a_row_needs_a_session(client):
    assert client.get(f"/v1/research/row/{CLS}/{TF}/alpha").status_code == 401


# ------------------------------------------------------------------ search is server-side

def test_search_filters_the_whole_sheet_and_not_the_page(client):
    """The failure this exists to prevent is a silent one.

    Filtering in the browser reaches only the rows already fetched, so a search from page 1
    of a 493-row sheet finds nothing that sits on page 6 — and "no matches" is
    indistinguishable from "no such rule". Matching before the window means a query reaches
    every ranked candidate.
    """
    head = key_for("m@example.com")
    q = f"/v1/research/leaderboard?cls={CLS}&tf={TF}&limit=1"
    # `beta` is the SECOND row, so a page-1-sized window would never contain it.
    body = client.get(f"{q}&q=beta", headers=head).json()
    assert [r["rule"] for r in body["rows"]] == ["beta"]
    assert body["n_matched"] == 1


def test_a_query_leaves_the_population_statistics_alone(client):
    """Same invariant as paging: the header describes the SHEET, not the result set.

    `n_rules` is the number every `dsr` on the sheet is deflated against. If a search moved
    it, a reader who typed something would be told the search space was smaller than it was.
    """
    head = key_for("m@example.com")
    q = f"/v1/research/leaderboard?cls={CLS}&tf={TF}"
    whole = client.get(q, headers=head).json()
    found = client.get(f"{q}&q=alpha", headers=head).json()
    for k in ("n_rules", "n_ranked", "noise_ceiling", "exposure_corr", "n_singles"):
        assert found[k] == whole[k], k
    # ...and only `n_matched` moves.
    assert whole["n_matched"] == 2 and found["n_matched"] == 1


def test_search_is_case_insensitive_substring(client):
    head = key_for("m@example.com")
    q = f"/v1/research/leaderboard?cls={CLS}&tf={TF}"
    assert client.get(f"{q}&q=ALPH", headers=head).json()["n_matched"] == 1
    assert client.get(f"{q}&q=a", headers=head).json()["n_matched"] == 2


def test_a_query_that_matches_nothing_is_an_empty_sheet_not_a_404(client):
    """A reader has to be able to tell "nothing matched" from "this sheet is not scored"."""
    head = key_for("m@example.com")
    r = client.get(f"/v1/research/leaderboard?cls={CLS}&tf={TF}&q=zzzz", headers=head)
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == [] and body["n_matched"] == 0
    # The echo is what lets the page say WHICH query found nothing.
    assert body["q"] == "zzzz"
    assert body["n_ranked"] == 2


def test_no_query_pages_exactly_as_before(client):
    """`n_matched` equals `n_ranked` with no query, so a client can page on it always."""
    head = key_for("m@example.com")
    body = client.get(f"/v1/research/leaderboard?cls={CLS}&tf={TF}", headers=head).json()
    assert body["n_matched"] == body["n_ranked"] == 2
    assert body["q"] == ""
