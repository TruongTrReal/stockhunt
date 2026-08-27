"""API keys, and the strategy control plane.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_strategies.py -q

Two things are being protected, and both fail silently rather than loudly:

* **A key is a credential with a blast radius.** It can trade its owner's paper book. It
  must not be able to mint another key, read another account's strategies, or outlive the
  account it belongs to.
* **A strategy belongs to exactly one account.** Every read and every write is scoped in
  the SQL, not checked beforehand, so there is no window and no path where guessing an id
  gets you somebody else's configuration.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api_paths                                                        # noqa: F401
import authdb
from stockhunt import deskdb


@pytest.fixture()
def client(tmp_path, monkeypatch):
    authdb.use(tmp_path / "auth.db")
    deskdb.use(tmp_path / "desk.db")
    authdb.connect()
    deskdb.connect()

    import api_app
    app = api_app.create_app()
    with TestClient(app) as c:
        yield c
    authdb.close()
    deskdb.close()


def key_for(email: str, admin: bool = False) -> str:
    authdb.allow(email, is_admin=admin)
    raw, _ = authdb.create_api_key(email, label="test")
    return raw


def auth(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def register(client, raw, **kw):
    body = {"name": "meanrev", "cls": "us_stocks", "symbols": ["SPY"], "tf": "1d"}
    body.update(kw)
    return client.post("/v1/strategies", json=body, headers=auth(raw))


# ------------------------------------------------------------------------------ limits

def test_the_limits_are_what_the_console_will_enforce(client):
    """The console states the terms before a manager registers, and every one of them is
    settable from the environment. If this endpoint and `api_config` ever disagree, the
    page tells somebody they get capital or an order cap they do not get."""
    import api_config

    r = client.get("/v1/limits", headers=auth(key_for("m@example.com")))
    assert r.status_code == 200
    body = r.json()
    assert body["capital_per_strategy"] == api_config.CAPITAL_PER_STRATEGY
    assert body["max_strategies"] == api_config.MAX_STRATEGIES_PER_ACCOUNT
    assert body["max_orders_per_minute"] == api_config.MAX_ORDERS_PER_MINUTE
    assert body["timeframes"] == list(api_config.TIMEFRAMES)
    # A class the picker offers but the validator refuses is a form that fails against its
    # own dropdown.
    import api_strategies
    assert body["classes"] == list(api_strategies.CLASSES)
    assert body["symbols_max"] == api_strategies.SYMBOLS_MAX
    # The console offers only these symbols because the desk refuses everything else. An
    # unpublished catalog gives an empty map, and the console falls back to free text
    # rather than refusing to register at all.
    assert isinstance(body["universe"], dict)
    assert set(body["universe"]) <= set(api_strategies.CLASSES)


def test_every_offered_timeframe_is_one_the_desk_can_feed(client):
    """The API is a subset of `paper_config.MEMBER_TIMEFRAMES` — it has to be, because the
    desk is what subscribes to the bar. Offering one it cannot feed is a 201 here and a
    rejection there, minutes later, which is the failure the console exists to remove.

    Read off disk rather than imported: this process must not import the trading stack.
    """
    import re
    import api_config
    import api_paths

    src = (api_paths.REPO / "paper trading engine" / "paper_config.py").read_text(
        encoding="utf-8")
    listed = re.search(r"MEMBER_TIMEFRAMES = \[([^\]]*)\]", src).group(1)
    desk = set(re.findall(r'"([^"]+)"', listed))
    assert desk, "MEMBER_TIMEFRAMES is not a literal list any more; update this test"
    assert set(api_config.TIMEFRAMES) <= desk, (
        f"the API offers {sorted(set(api_config.TIMEFRAMES) - desk)}, which the desk "
        f"cannot subscribe to")


def test_limits_needs_a_credential(client):
    assert client.get("/v1/limits").status_code == 401


def test_limits_did_not_shadow_a_strategy_id(client):
    """`/v1/limits` is a sibling of `/v1/strategies`, not a child, so no ordering between
    the two routers can make one eat the other."""
    raw = key_for("m@example.com")
    sid = register(client, raw).json()["strategy_id"]
    assert client.get(f"/v1/strategies/{sid}", headers=auth(raw)).status_code == 200


# --------------------------------------------------------------------------- api keys

def test_a_key_authenticates(client):
    raw = key_for("m@example.com")
    assert register(client, raw).status_code == 201


def test_no_credential_is_a_401(client):
    assert client.get("/v1/strategies").status_code == 401


def test_a_made_up_key_is_a_401(client):
    key_for("m@example.com")
    r = client.get("/v1/strategies", headers=auth("sk_live_" + "0" * 64))
    assert r.status_code == 401


def test_a_revoked_key_stops_working_immediately(client):
    """Not at the end of some TTL. Revocation that takes effect later is not revocation."""
    authdb.allow("m@example.com")
    raw, row = authdb.create_api_key("m@example.com")
    assert client.get("/v1/strategies", headers=auth(raw)).status_code == 200
    authdb.revoke_api_key("m@example.com", row["id"])
    assert client.get("/v1/strategies", headers=auth(raw)).status_code == 401


def test_revoking_the_account_kills_its_keys(client):
    """A bot never closes its tab and notices. If the key outlived the account, a revoked
    manager would keep trading."""
    raw = key_for("m@example.com")
    assert client.get("/v1/strategies", headers=auth(raw)).status_code == 200
    authdb.revoke("m@example.com")
    assert client.get("/v1/strategies", headers=auth(raw)).status_code == 401


def test_a_key_cannot_mint_another_key(client):
    """The containment that makes revocation final. A stolen key can trade the paper
    book; it must not be able to issue a replacement that survives revoking the original."""
    raw = key_for("m@example.com")
    r = client.post("/auth/keys", json={"label": "second"}, headers=auth(raw))
    assert r.status_code == 401


def test_a_key_is_only_ever_shown_once(client):
    authdb.allow("m@example.com")
    raw, row = authdb.create_api_key("m@example.com", "bot")
    listed = authdb.api_keys_for("m@example.com")
    assert len(listed) == 1
    assert "key" not in listed[0] and "key_hash" not in listed[0]
    # Only a recognisable head is kept, so a leaked key can be found and revoked.
    assert listed[0]["prefix"] and listed[0]["prefix"] in raw
    assert len(listed[0]["prefix"]) < len(raw)


def test_a_key_cannot_be_minted_for_a_stranger():
    with pytest.raises(ValueError):
        authdb.create_api_key("nobody@example.com")


def test_one_account_cannot_revoke_anothers_key(client):
    authdb.allow("a@example.com")
    authdb.allow("b@example.com")
    _, theirs = authdb.create_api_key("b@example.com")
    assert authdb.revoke_api_key("a@example.com", theirs["id"]) is False
    assert authdb.api_keys_for("b@example.com")[0]["revoked_at"] is None


# ------------------------------------------------------------------------ registering

def test_a_registration_starts_pending_not_live(client):
    """This process owns no trading. It writes down what was asked for; the desk decides
    on its next tick, and may still refuse for something only it can see."""
    r = register(client, key_for("m@example.com"))
    body = r.json()
    assert body["state"] == "pending" and body["want"] == "live"
    assert body["kind"] == "member"
    assert body["strategy_id"].startswith("str_")


def test_capital_is_fixed_by_the_desk_not_chosen_by_the_caller(client):
    r = register(client, key_for("m@example.com"),
                 **{"capital": 999_999_999})       # ignored: not part of the schema
    assert r.status_code == 201
    assert r.json()["capital"] == 10_000.0


def test_registering_the_same_name_twice_is_one_strategy(client):
    raw = key_for("m@example.com")
    first = register(client, raw).json()
    again = register(client, raw).json()
    assert again["strategy_id"] == first["strategy_id"]
    assert len(client.get("/v1/strategies", headers=auth(raw)).json()) == 1


def test_the_per_account_ceiling_holds(client):
    raw = key_for("m@example.com")
    for i in range(6):
        assert register(client, raw, name=f"s{i}").status_code == 201
    over = register(client, raw, name="s6")
    assert over.status_code == 409 and "limit" in over.json()["detail"]


def test_retiring_frees_a_slot(client):
    raw = key_for("m@example.com")
    for i in range(6):
        register(client, raw, name=f"s{i}")
    sid = client.get("/v1/strategies", headers=auth(raw)).json()[0]["strategy_id"]
    client.delete(f"/v1/strategies/{sid}", headers=auth(raw))
    deskdb.mark_registration(sid, "retired")      # the desk applying it
    assert register(client, raw, name="s6").status_code == 201


@pytest.mark.parametrize("bad", [
    {"cls": "forex"},
    # `1m`, not `5m`: the desk gained the intraday timeframes down to 5m, and 1m is the one
    # deliberately left off. One poll task per subscription aligned to the bar close makes
    # a minute book a different vendor-credit regime, not a faster version of the same one.
    {"tf": "1m"},
    {"tf": "1w"},
    {"symbols": []},
    {"symbols": ["SPY", "SPY"]},
    {"name": "has spaces"},
    {"name": "x" * 41},
])
def test_malformed_registrations_are_refused(client, bad):
    assert register(client, key_for("m@example.com"), **bad).status_code == 422


def test_symbols_are_normalised(client):
    r = register(client, key_for("m@example.com"), symbols=["spy", " qqq "])
    assert r.json()["symbols"] == ["SPY", "QQQ"]


# ----------------------------------------------------------------------- cme_futures
#
# The fifth class, and the only one whose symbols are not spelled in capitals. Everything
# below is one failure wearing four faces: a fold to upper case rewrites the desk's own
# `ES.v.0` as `ES.V.0`, the API answers 201, and the desk refuses it on its next pass for
# a symbol the console offered out of the desk's own published universe.


def test_the_fifth_class_can_be_registered(client):
    r = register(client, key_for("fut@example.test"), cls="cme_futures",
                 symbols=["ES.v.0"])
    assert r.status_code == 201, r.text
    assert r.json()["cls"] == "cme_futures"


@pytest.mark.parametrize("typed", ["ES.v.0", "es.v.0", "ES.V.0", " es.V.0 "])
def test_a_continuous_contract_keeps_the_desks_spelling(client, typed):
    """`desk_control` checks each registered name against `paper_config.CLASS_OF` with
    `in`, and `desk_orders` checks an order's symbol against the registration's own list
    the same way. Both are plain string comparisons, so the case of one letter decides
    whether the strategy trades at all."""
    r = register(client, key_for("fut@example.test"), cls="cme_futures", symbols=[typed])
    assert r.status_code == 201, r.text
    assert r.json()["symbols"] == ["ES.v.0"]


def test_two_spellings_of_one_contract_are_one_symbol(client):
    """Caught as the duplicate it is. Registered as two names, it would be two subscription
    requests for one instrument and a book that looks twice its size."""
    r = register(client, key_for("fut@example.test"), cls="cme_futures",
                 symbols=["ES.v.0", "ES.V.0"])
    assert r.status_code == 422


def test_the_fold_leaves_the_other_four_classes_alone(client):
    """`BRK.B` is the near miss — a dot in the middle of an equity ticker — and the reason
    the pattern is anchored on a trailing rank rather than on 'contains a dot'."""
    r = register(client, key_for("eq@example.test"), symbols=["brk.b", "BTC/USD"])
    assert r.json()["symbols"] == ["BRK.B", "BTC/USD"]


def test_limits_advertises_the_fifth_class(client):
    """The wizard builds its class buttons from `LIMITS.classes` and nothing else, so a
    class the validator accepts but this endpoint omits is one nobody can pick."""
    body = client.get("/v1/limits", headers=auth(key_for("m@example.com"))).json()
    assert "cme_futures" in body["classes"]


def test_limits_passes_the_futures_universe_through_unfolded(client, monkeypatch):
    """`/v1/limits` filters the desk's published universe by `CLASSES`, so this class
    reaches the picker only once BOTH halves have landed. The symbols it carries are the
    desk's own strings and must arrive verbatim — the console offers exactly what is in
    this list and refuses everything else."""
    import api_live
    monkeypatch.setattr(api_live, "catalog", lambda: {
        "universe": {"us_stocks": ["SPY"], "cme_futures": ["ES.v.0", "GC.v.0"]}})

    body = client.get("/v1/limits", headers=auth(key_for("m@example.com"))).json()
    assert body["universe"]["cme_futures"] == ["ES.v.0", "GC.v.0"]


def test_a_desk_with_no_catalog_still_offers_the_class(client, monkeypatch):
    """The other half of this change is landing separately, and until it does the desk
    publishes no futures universe. That must leave the class registrable on a typed symbol
    rather than un-offerable: a research artifact that has not been rebuilt cannot be
    allowed to close the door, and the desk checks anyway."""
    import api_live
    monkeypatch.setattr(api_live, "catalog", lambda: None)

    raw = key_for("m@example.com")
    body = client.get("/v1/limits", headers=auth(raw)).json()
    assert body["universe"] == {}
    assert "cme_futures" in body["classes"]
    assert register(client, raw, cls="cme_futures",
                    symbols=["ES.v.0"]).status_code == 201


# ---------------------------------------------------------------------------- scoping

def test_one_account_cannot_see_anothers_strategies(client):
    mine = key_for("a@example.com")
    theirs = key_for("b@example.com")
    register(client, mine, name="secret")

    assert client.get("/v1/strategies", headers=auth(theirs)).json() == []
    sid = client.get("/v1/strategies", headers=auth(mine)).json()[0]["strategy_id"]
    # 404, not 403: confirming an id exists but is not yours answers a question the
    # caller should not be able to ask.
    assert client.get(f"/v1/strategies/{sid}", headers=auth(theirs)).status_code == 404


@pytest.mark.parametrize("method,path", [
    ("post", "/pause"), ("post", "/resume"), ("delete", ""),
])
def test_one_account_cannot_control_anothers_strategy(client, method, path):
    mine = key_for("a@example.com")
    theirs = key_for("b@example.com")
    sid = register(client, mine).json()["strategy_id"]

    r = getattr(client, method)(f"/v1/strategies/{sid}{path}", headers=auth(theirs))
    assert r.status_code == 404
    assert deskdb.registration(sid)["want"] == "live", "their call changed my strategy"


def test_two_accounts_may_use_the_same_strategy_name(client):
    a = register(client, key_for("a@example.com"), name="meanrev").json()
    b = register(client, key_for("b@example.com"), name="meanrev").json()
    assert a["strategy_id"] != b["strategy_id"]


# -------------------------------------------------------------------------- lifecycle

def test_pause_and_resume_move_want_not_state(client):
    """The desk owns `state`. Until its next tick the two disagree, and that is the
    honest answer to 'has it stopped yet'."""
    raw = key_for("m@example.com")
    sid = register(client, raw).json()["strategy_id"]
    deskdb.mark_registration(sid, "live")

    paused = client.post(f"/v1/strategies/{sid}/pause", headers=auth(raw)).json()
    assert paused["want"] == "paused" and paused["state"] == "live"

    back = client.post(f"/v1/strategies/{sid}/resume", headers=auth(raw)).json()
    assert back["want"] == "live"


def test_a_retired_strategy_cannot_be_resumed(client):
    raw = key_for("m@example.com")
    sid = register(client, raw).json()["strategy_id"]
    client.delete(f"/v1/strategies/{sid}", headers=auth(raw))
    deskdb.mark_registration(sid, "retired")
    r = client.post(f"/v1/strategies/{sid}/resume", headers=auth(raw))
    assert r.status_code == 409 and "record is kept" in r.json()["detail"]


def test_the_desks_refusal_reaches_the_caller(client):
    """The desk rejects for things this process cannot see — a symbol outside the
    universe, say. The reason has to come back or a manager is left guessing."""
    raw = key_for("m@example.com")
    sid = register(client, raw, symbols=["NOTREAL"]).json()["strategy_id"]
    deskdb.mark_registration(sid, "rejected", reason="NOTREAL is not in the desk's universe")

    body = client.get(f"/v1/strategies/{sid}", headers=auth(raw)).json()
    assert body["state"] == "rejected"
    assert "not in the desk's universe" in body["reason"]


# ------------------------------------------------------------------------ the desk pulse

def test_a_desk_that_has_never_run_is_reported_as_down(client):
    """`want <> state` says the same thing whether the desk is mid-pass or has been down
    since Tuesday. Without the pulse the console could only ever guess, and it guessed
    optimistically — one canned sentence promising a pass that might never come."""
    raw = key_for("nobody@home.test")
    d = client.get("/v1/desk", headers=auth(raw)).json()
    assert d["live"] is False
    assert d["last_pass_at"] is None and d["seconds_ago"] is None


def test_a_beating_desk_is_reported_as_live(client):
    raw = key_for("watcher@example.test")
    deskdb.beat(3, node="TRADER-001")
    d = client.get("/v1/desk", headers=auth(raw)).json()
    assert d["live"] is True
    assert d["ticks"] == 3 and d["seconds_ago"] < 5


def test_a_stale_pulse_is_down(client):
    from datetime import datetime, timedelta, timezone
    import api_config
    raw = key_for("stale@example.test")
    old = datetime.now(timezone.utc) - timedelta(
        seconds=api_config.DESK_STALE_SECONDS + 30)
    deskdb.beat(9, at=old.isoformat(timespec="seconds"))

    d = client.get("/v1/desk", headers=auth(raw)).json()
    assert d["live"] is False
    assert d["seconds_ago"] > api_config.DESK_STALE_SECONDS


def test_pending_counts_only_the_callers_own_work(client):
    """An outage is shared; a backlog is not. A desk-wide pending count would tell one
    member how many strategies everybody else is waiting on."""
    mine, theirs = key_for("mine@example.test"), key_for("theirs@example.test")
    register(client, mine, name="a")
    register(client, theirs, name="b")
    register(client, theirs, name="c")

    assert client.get("/v1/desk", headers=auth(mine)).json()["pending"] == 1
    assert client.get("/v1/desk", headers=auth(theirs)).json()["pending"] == 2


def test_an_applied_strategy_stops_being_pending(client):
    raw = key_for("applied@example.test")
    sid = register(client, raw).json()["strategy_id"]
    assert client.get("/v1/desk", headers=auth(raw)).json()["pending"] == 1

    deskdb.mark_registration(sid, "live")               # the desk caught up
    assert client.get("/v1/desk", headers=auth(raw)).json()["pending"] == 0


def test_the_pulse_needs_a_credential(client):
    assert client.get("/v1/desk").status_code == 401


def test_a_desk_that_is_up_and_failing_is_not_reported_as_healthy(client):
    """The desk guards each lane and carries on, so a pass that fails every time still
    completes and still beats. `live` alone would report that as fine while a change nobody
    can apply sits there looking merely slow."""
    raw = key_for("failing@example.test")
    deskdb.beat(5, error="reconcile failed: unknown asset class 'fx'")

    d = client.get("/v1/desk", headers=auth(raw)).json()
    assert d["live"] is True
    assert d["error"] == "reconcile failed: unknown asset class 'fx'"

    deskdb.beat(6)
    assert client.get("/v1/desk", headers=auth(raw)).json()["error"] is None


def test_a_terminal_strategy_cannot_be_paused(client):
    """`want` and `state` may disagree while the desk catches up — but a terminal row has
    no catching up left, so pausing one writes a disagreement no pass can ever reconcile.
    `want=paused, state=retired` then sits on the console forever."""
    raw = key_for("terminal@example.test")
    sid = register(client, raw).json()["strategy_id"]
    deskdb.mark_registration(sid, "retired")

    was = deskdb.registration(sid)["want"]
    r = client.post(f"/v1/strategies/{sid}/pause", headers=auth(raw))
    assert r.status_code == 409
    assert "cannot be paused" in r.json()["detail"]
    assert deskdb.registration(sid)["want"] == was      # refused, and wrote nothing


def test_retiring_a_row_the_desk_already_retired_realigns_it(client):
    """The repair path for a row already in that state. Retire is the one write that puts
    `want` back where `state` is, so it stays available while the two disagree."""
    raw = key_for("realign@example.test")
    sid = register(client, raw).json()["strategy_id"]
    deskdb.set_want(deskdb.registration(sid)["account"], sid, "paused")
    deskdb.mark_registration(sid, "retired")
    assert deskdb.registration(sid)["want"] == "paused"      # the stuck shape

    assert client.delete(f"/v1/strategies/{sid}", headers=auth(raw)).status_code == 200
    row = deskdb.registration(sid)
    assert row["want"] == "retired" and row["state"] == "retired"


def test_polling_hard_never_spuriously_signs_you_out(client):
    """The console's symptom, end to end: it polls, and it got logged out at random.

    `authdb` and `deskdb` each shared ONE sqlite connection across FastAPI's threadpool.
    Reads took no lock while writes did, so a concurrent write reset a reader's statement
    and the row came back with empty columns rather than raising. On the credential store
    that is a session read as None (401) or an `account_id` read as '' (403, from a branch
    documented as unreachable); on the ledger it is `symbols` as '' (500).

    The 401 is the one that hurts: the page reads it as "signed out" and sends the reader
    to /login, which — their session being perfectly valid — bounces them to the dashboard.
    Nothing in the logs says anything went wrong except a 401 that should not exist.

    Two calls every two seconds per open tab is all it took.
    """
    import threading

    raw = key_for("poller@example.test")
    for i in range(4):
        assert register(client, raw, name=f"book{i}").status_code == 201

    codes: list[int] = []
    lock = threading.Lock()

    def hammer():
        got = []
        for _ in range(25):
            got.append(client.get("/v1/desk", headers=auth(raw)).status_code)
            got.append(client.get("/v1/strategies", headers=auth(raw)).status_code)
        with lock:
            codes.extend(got)

    def churn():
        for i in range(60):
            deskdb.beat(i)
            deskdb.mark_registration(f"str_{deskdb.registrations()[0]['account']}_book0",
                                     "live")

    threads = [threading.Thread(target=hammer) for _ in range(4)] + \
              [threading.Thread(target=churn) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert codes, "no requests were made"
    assert set(codes) == {200}, f"unexpected: {sorted(set(codes))}"


# ------------------------------------------------------------------------------ purge

def test_purge_removes_a_strategy_that_never_traded(client):
    raw = key_for("tidy@example.test")
    sid = register(client, raw, name="typo").json()["strategy_id"]
    deskdb.mark_registration(sid, "retired")

    r = client.delete(f"/v1/strategies/{sid}?purge=true", headers=auth(raw))
    assert r.status_code == 204
    assert deskdb.registration(sid) is None
    assert client.get(f"/v1/strategies/{sid}", headers=auth(raw)).status_code == 404


def test_purge_refuses_anything_that_ever_placed_an_order(client):
    """The rule the whole exception rests on. If this can be talked round, the desk's
    forward record becomes a track record its author filtered."""
    raw = key_for("traded@example.test")
    sid = register(client, raw, name="real").json()["strategy_id"]
    client.post("/v1/orders", headers=auth(raw), json={
        "strategy_id": sid, "client_order_id": "c1", "symbol": "SPY",
        "side": "buy", "qty": 1})
    deskdb.mark_registration(sid, "retired")

    r = client.delete(f"/v1/strategies/{sid}?purge=true", headers=auth(raw))
    assert r.status_code == 409
    assert "order" in r.json()["detail"]
    assert deskdb.registration(sid) is not None


def test_purge_refuses_a_strategy_the_desk_is_still_running(client):
    raw = key_for("busy@example.test")
    sid = register(client, raw).json()["strategy_id"]
    deskdb.mark_registration(sid, "live")

    r = client.delete(f"/v1/strategies/{sid}?purge=true", headers=auth(raw))
    assert r.status_code == 409
    assert "Retire it first" in r.json()["detail"]


def test_delete_without_the_flag_still_only_retires(client):
    """The contract `agent.md` has always described. Purge is opt-in, and a client that
    does not know about it cannot lose a strategy by upgrading."""
    raw = key_for("plain@example.test")
    sid = register(client, raw).json()["strategy_id"]

    r = client.delete(f"/v1/strategies/{sid}", headers=auth(raw))
    assert r.status_code == 200
    assert r.json()["want"] == "retired"
    assert deskdb.registration(sid) is not None


def test_one_account_cannot_purge_anothers(client):
    mine, theirs = key_for("p1@example.test"), key_for("p2@example.test")
    sid = register(client, theirs, name="theirs").json()["strategy_id"]
    deskdb.mark_registration(sid, "retired")

    r = client.delete(f"/v1/strategies/{sid}?purge=true", headers=auth(mine))
    assert r.status_code == 404                       # not 403: an id you cannot see
    assert deskdb.registration(sid) is not None
