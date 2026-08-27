"""The order endpoint: idempotency, ordering, scoping, and honest status codes.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_orders.py -q

The property most of this file exists for is idempotency. Without it every network
timeout on a manager's side becomes a doubled position, and it is invisible until somebody
reconciles a book by hand.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api_paths                                                        # noqa: F401
import api_config
import authdb
from stockhunt import deskdb


@pytest.fixture()
def client(tmp_path):
    authdb.use(tmp_path / "auth.db")
    deskdb.use(tmp_path / "desk.db")
    authdb.connect()
    deskdb.connect()
    import api_app
    with TestClient(api_app.create_app()) as c:
        yield c
    authdb.close()
    deskdb.close()


def account(client, email="m@example.com"):
    authdb.allow(email)
    raw, _ = authdb.create_api_key(email)
    headers = {"Authorization": f"Bearer {raw}"}
    sid = client.post("/v1/strategies", headers=headers, json={
        "name": "meanrev", "cls": "us_stocks", "symbols": ["SPY", "QQQ"], "tf": "1d",
    }).json()["strategy_id"]
    return headers, sid


def account_id(email="m@example.com"):
    """The account id that `email` actually got.

    Never hardcode it. Account ids are handed out in sequence, so whether `m@example.com`
    is `01` or `02` depends on whether anything was seeded ahead of it -- and `API_OWNER_EMAIL`
    seeds exactly that, at startup, into whatever allowlist the app opens, including a
    fixture's empty temp one. A literal `"01"` therefore passes on a dev box with no owner
    configured and fails on a deployed one that has it set, which is a test that reports
    the deployment's configuration as a defect in the order endpoint.
    """
    return next(u["account_id"] for u in authdb.users() if u["email"] == email)


def order(sid, coid="o-1", **kw):
    body = {"strategy_id": sid, "client_order_id": coid, "symbol": "SPY",
            "side": "buy", "qty": 10, "type": "market", "tif": "day"}
    body.update(kw)
    return body


# ------------------------------------------------------------------------ idempotency

def test_the_same_client_order_id_is_one_order(client):
    """The single most important property here."""
    h, sid = account(client)
    first = client.post("/v1/orders", headers=h, json=order(sid))
    assert first.status_code == 202

    for _ in range(9):
        again = client.post("/v1/orders", headers=h, json=order(sid))
        # 200, not 202: the order was not accepted again, it already existed.
        assert again.status_code == 200
        assert again.json()["seq"] == first.json()["seq"]

    assert len(client.get("/v1/orders", headers=h).json()) == 1


def test_a_retry_with_different_fields_returns_the_original(client):
    """Honouring the second body would let a typo silently replace a live order."""
    h, sid = account(client)
    client.post("/v1/orders", headers=h, json=order(sid, qty=10))
    again = client.post("/v1/orders", headers=h, json=order(sid, qty=999, side="sell"))
    assert again.json()["qty"] == 10 and again.json()["side"] == "buy"


def test_client_order_id_is_required(client):
    h, sid = account(client)
    body = order(sid)
    del body["client_order_id"]
    assert client.post("/v1/orders", headers=h, json=body).status_code == 422


def test_two_accounts_may_reuse_a_client_order_id(client):
    """Unique per account, not globally: one manager's numbering must not collide with,
    or be probeable by, another's."""
    h1, s1 = account(client, "a@example.com")
    h2, s2 = account(client, "b@example.com")
    assert client.post("/v1/orders", headers=h1, json=order(s1, "same")).status_code == 202
    assert client.post("/v1/orders", headers=h2, json=order(s2, "same")).status_code == 202


# ------------------------------------------------------------------- honest semantics

def test_202_means_written_down_not_filled(client):
    h, sid = account(client)
    body = client.post("/v1/orders", headers=h, json=order(sid)).json()
    assert body["state"] == "accepted"
    assert body["filled_qty"] == 0 and body["avg_price"] is None
    assert body["applied_at"] is None


def test_the_desks_refusal_comes_back(client):
    """The desk rejects for things this process cannot see — cash, positions, staleness.
    If the reason did not surface, a manager would be left guessing."""
    h, sid = account(client)
    seq = client.post("/v1/orders", headers=h, json=order(sid)).json()["seq"]
    deskdb.mark_order(seq, "rejected", reason="not enough cash: SPY 10 at 574.20 ...")

    got = client.get("/v1/orders/o-1", headers=h).json()
    assert got["state"] == "rejected" and "not enough cash" in got["reason"]


def test_a_fill_comes_back(client):
    h, sid = account(client)
    seq = client.post("/v1/orders", headers=h, json=order(sid)).json()["seq"]
    deskdb.mark_order(seq, "filled", filled_qty=10, avg_price=574.2)
    got = client.get("/v1/orders/o-1", headers=h).json()
    assert got["state"] == "filled" and got["filled_qty"] == 10
    assert got["avg_price"] == 574.2


# ----------------------------------------------------------------------------- ordering

def test_seq_is_monotonic(client):
    """A cancel must never overtake the order it cancels."""
    h, sid = account(client)
    seqs = [client.post("/v1/orders", headers=h, json=order(sid, f"o-{i}")).json()["seq"]
            for i in range(5)]
    assert seqs == sorted(seqs)
    assert [o["seq"] for o in client.get("/v1/orders", headers=h).json()] == seqs


def test_since_seq_pages_forward(client):
    h, sid = account(client)
    for i in range(5):
        client.post("/v1/orders", headers=h, json=order(sid, f"o-{i}"))
    all_ = client.get("/v1/orders", headers=h).json()
    rest = client.get(f"/v1/orders?since_seq={all_[1]['seq']}", headers=h).json()
    assert [o["client_order_id"] for o in rest] == ["o-2", "o-3", "o-4"]


# ------------------------------------------------------------------------------ cancels

def test_a_cancel_is_queued_and_names_its_target(client):
    h, sid = account(client)
    client.post("/v1/orders", headers=h, json=order(sid, "o-1"))
    r = client.delete("/v1/orders/o-1", headers=h)
    assert r.status_code == 202
    body = r.json()
    assert body["action"] == "cancel" and body["target_coid"] == "o-1"


def test_cancelling_twice_is_one_cancel(client):
    h, sid = account(client)
    client.post("/v1/orders", headers=h, json=order(sid, "o-1"))
    first = client.delete("/v1/orders/o-1", headers=h)
    again = client.delete("/v1/orders/o-1", headers=h)
    assert first.status_code == 202 and again.status_code == 200
    assert again.json()["seq"] == first.json()["seq"]


def test_cancelling_a_finished_order_is_a_409(client):
    h, sid = account(client)
    seq = client.post("/v1/orders", headers=h, json=order(sid, "o-1")).json()["seq"]
    deskdb.mark_order(seq, "filled", filled_qty=10, avg_price=1.0)
    r = client.delete("/v1/orders/o-1", headers=h)
    assert r.status_code == 409 and "already filled" in r.json()["detail"]


def test_cancelling_something_that_never_existed_is_a_404(client):
    h, _ = account(client)
    assert client.delete("/v1/orders/ghost", headers=h).status_code == 404


# ------------------------------------------------------------------------------ scoping

def test_one_account_cannot_see_or_touch_anothers_orders(client):
    h1, s1 = account(client, "a@example.com")
    h2, _ = account(client, "b@example.com")
    client.post("/v1/orders", headers=h1, json=order(s1, "secret"))

    assert client.get("/v1/orders", headers=h2).json() == []
    assert client.get("/v1/orders/secret", headers=h2).status_code == 404
    assert client.delete("/v1/orders/secret", headers=h2).status_code == 404
    assert deskdb.order(account_id("a@example.com"), "secret")["state"] == "accepted"


def test_orders_for_a_strategy_you_do_not_own_are_refused(client):
    h1, s1 = account(client, "a@example.com")
    h2, _ = account(client, "b@example.com")
    r = client.post("/v1/orders", headers=h2, json=order(s1, "x"))
    assert r.status_code == 404


def test_orders_need_a_credential(client):
    _, sid = account(client)
    assert client.post("/v1/orders", json=order(sid)).status_code == 401
    assert client.get("/v1/orders").status_code == 401


def test_a_rule_strategy_does_not_take_orders(client):
    """A rule promoted off a backtest trades what the research said, not what anybody
    sends it. Accepting an order for one would silently do nothing."""
    h, _ = account(client)
    house = deskdb.register(account_id(), "spy-1d-sma_200", "us_stocks", ["SPY"], "1d",
                            10_000.0, kind="house_rule", rule="SMA_200")
    r = client.post("/v1/orders", headers=h,
                    json=order(house["strategy_id"], "x"))
    assert r.status_code == 409 and "does not take orders" in r.json()["detail"]


# --------------------------------------------------------------------------- shape/limits

@pytest.mark.parametrize("bad", [
    {"side": "long"}, {"type": "stop"}, {"tif": "fok"},
    {"qty": 0}, {"qty": -1}, {"limit_price": 0},
    {"client_order_id": "has space"}, {"client_order_id": ""},
])
def test_malformed_orders_are_refused_before_the_ledger(client, bad):
    h, sid = account(client)
    assert client.post("/v1/orders", headers=h, json=order(sid, **bad)).status_code == 422
    assert client.get("/v1/orders", headers=h).json() == []


def test_the_rate_limit_answers_429(client, monkeypatch):
    """Unlike the sign-in limits, this one may answer with a status: the caller is already
    authenticated, so the code leaks nothing about who is registered."""
    monkeypatch.setattr(api_config, "MAX_ORDERS_PER_MINUTE", 3)
    h, sid = account(client)
    for i in range(3):
        assert client.post("/v1/orders", headers=h,
                           json=order(sid, f"o-{i}")).status_code == 202
    over = client.post("/v1/orders", headers=h, json=order(sid, "o-4"))
    assert over.status_code == 429 and over.headers.get("Retry-After") == "60"


def test_symbols_are_normalised(client):
    h, sid = account(client)
    body = client.post("/v1/orders", headers=h,
                       json=order(sid, symbol="spy")).json()
    assert body["symbol"] == "SPY"


@pytest.mark.parametrize("sent", ["ES.v.0", "es.v.0", "ES.V.0"])
def test_a_futures_order_reaches_the_ledger_spelled_as_the_desk_holds_it(client, sent):
    """`cme_futures` is the one class whose symbols are not capitals, and this endpoint
    writes the string the desk will match with `in`. A plain `.upper()` here put `ES.V.0`
    in the inbox against a book registered for `ES.v.0`, and every order the strategy ever
    sent would have been refused — each one after a `202`, because the refusal arrives
    later, on a different endpoint, from a process this one cannot see.
    """
    authdb.allow("fut@example.test")
    raw, _ = authdb.create_api_key("fut@example.test")
    h = {"Authorization": f"Bearer {raw}"}
    sid = client.post("/v1/strategies", headers=h, json={
        "name": "roll", "cls": "cme_futures", "symbols": ["ES.v.0"], "tf": "1d",
    }).json()["strategy_id"]

    r = client.post("/v1/orders", headers=h, json=order(sid, symbol=sent))
    assert r.status_code == 202, r.text
    assert r.json()["symbol"] == "ES.v.0"
    # Read back off the ledger, not out of the response: the row is what the desk drains.
    written = deskdb.orders(account_id("fut@example.test"))
    assert [o["symbol"] for o in written] == ["ES.v.0"]
