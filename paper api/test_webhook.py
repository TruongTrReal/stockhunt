"""The TradingView door: a credential in the body, and everything that has to hold anyway.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_webhook.py -q

This is the only route in the API where the secret arrives in the request body, so the
questions are sharper than elsewhere and three of them fail silently if they break:

* **A webhook secret must be worth less than an API key.** One strategy, orders only, and
  useless as a bearer credential anywhere else.
* **A duplicate alert must not double a position.** TradingView has no `client_order_id`
  and re-fires happen, so the id is derived — and if that derivation stops being stable
  across a re-fire, nothing errors and the book is simply wrong.
* **A refusal must be a 4xx.** TradingView shows its operator nothing but the status, so
  a soft "ok, but I ignored it" is a green tick over an alert that traded nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api_paths                                                        # noqa: F401
import api_auth
import api_config
import authdb
from stockhunt import deskdb

HOOK = "/v1/webhook/tradingview"


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


def session_headers(email: str = "m@example.com") -> dict:
    """A browser session, which is the only thing that may mint a webhook secret."""
    authdb.allow(email)
    token = "sess-" + email.replace("@", "-")
    authdb.create_session(email, api_auth._hash_token(token),
                          api_config.SESSION_TTL_DAYS)
    return {"Authorization": f"Bearer {token}"}


def strategy(client, headers, symbols=("SPY",), name="meanrev", **kw) -> str:
    body = {"name": name, "cls": "us_stocks", "symbols": list(symbols), "tf": "1d"}
    body.update(kw)
    r = client.post("/v1/strategies", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["strategy_id"]


def webhook(client, headers, sid) -> str:
    r = client.post(f"/v1/strategies/{sid}/webhook", headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["secret"]


def desk(client, symbols=("SPY",), **kw):
    """The usual setup: an account, a strategy, and a live webhook secret on it."""
    h = session_headers()
    sid = strategy(client, h, symbols, **kw)
    return h, sid, webhook(client, h, sid)


def alert(sid, secret, **kw) -> dict:
    body = {"strategyId": sid, "secret": secret, "action": "buy", "qty": 10,
            "bar_time": "2026-08-19T13:30:00Z"}
    body.update(kw)
    return body


# ------------------------------------------------------------------------ the happy path

def test_an_alert_becomes_an_order(client):
    h, sid, secret = desk(client)
    r = client.post(HOOK, json=alert(sid, secret))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["resolved"]["symbol"] == "SPY"
    assert body["resolved"]["dedupe"] == "bar"
    assert body["order"]["state"] == "accepted"
    assert body["order"]["qty"] == 10

    on_ledger = client.get("/v1/orders", headers=h).json()
    assert len(on_ledger) == 1
    assert on_ledger[0]["client_order_id"] == body["resolved"]["client_order_id"]


def test_the_size_comes_from_the_alert(client):
    """The desk does not size. A missing qty is a refusal, not a default."""
    _h, sid, secret = desk(client)
    body = alert(sid, secret)
    del body["qty"]
    assert client.post(HOOK, json=body).status_code == 422
    assert client.post(HOOK, json=alert(sid, secret, qty=0)).status_code == 422
    # `{{strategy.order.contracts}}` renders as a bare number, but people quote it.
    assert client.post(HOOK, json=alert(sid, secret, qty="7")).status_code == 202


def test_tradingview_field_names_are_accepted(client):
    """`password` is what the first integration asked for; `contracts` is TradingView's
    own word for size. Neither is a second endpoint."""
    _h, sid, secret = desk(client)
    r = client.post(HOOK, json={"strategyId": sid, "password": secret,
                                "action": "sell", "contracts": 3,
                                "time": "2026-08-19T13:30:00Z"})
    assert r.status_code == 202, r.text
    assert r.json()["resolved"]["side"] == "sell"
    assert r.json()["resolved"]["qty"] == 3


def test_a_text_plain_body_still_works(client):
    """TradingView labels the body `text/plain` unless it decides the message is JSON.

    FastAPI hands a pydantic model raw bytes in that case and answers "input should be a
    valid dictionary" for a body that is a perfectly good JSON object — a 422 nobody can
    act on, against a field they cannot set.
    """
    _h, sid, secret = desk(client)
    import json
    r = client.post(HOOK, content=json.dumps(alert(sid, secret)),
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 202, r.text


# ---------------------------------------------------------------------------- the secret

def test_a_wrong_secret_is_a_401(client):
    _h, sid, _secret = desk(client)
    r = client.post(HOOK, json=alert(sid, "whk_not_a_real_secret"))
    assert r.status_code == 401
    assert not deskdb.orders(_account())


def test_a_secret_cannot_trade_another_strategy(client):
    """The blast radius IS the scope. A secret names one strategy and only that one."""
    h = session_headers()
    a = strategy(client, h, name="one")
    b = strategy(client, h, name="two")
    secret_a = webhook(client, h, a)

    assert client.post(HOOK, json=alert(b, secret_a)).status_code == 401
    assert client.post(HOOK, json=alert(a, secret_a)).status_code == 202


def test_a_webhook_secret_is_not_a_bearer_credential(client):
    """It opens the one route it was minted for. `current_principal` routes by prefix, so
    a `whk_` in an Authorization header never even reaches the keys table."""
    _h, _sid, secret = desk(client)
    headers = {"Authorization": f"Bearer {secret}"}
    assert client.get("/v1/strategies", headers=headers).status_code == 401
    assert client.get("/v1/orders", headers=headers).status_code == 401
    assert client.post("/v1/orders", headers=headers, json={}).status_code == 401


def test_rotating_kills_the_previous_secret(client):
    h, sid, first = desk(client)
    second = webhook(client, h, sid)
    assert first != second
    assert client.post(HOOK, json=alert(sid, first)).status_code == 401
    assert client.post(HOOK, json=alert(sid, second)).status_code == 202


def test_revoking_stops_the_alert(client):
    h, sid, secret = desk(client)
    assert client.delete(f"/v1/strategies/{sid}/webhook",
                         headers=h).status_code == 204
    assert client.post(HOOK, json=alert(sid, secret)).status_code == 401
    # And a second revoke says so rather than pretending.
    assert client.delete(f"/v1/strategies/{sid}/webhook", headers=h).status_code == 404


def test_revoking_the_account_stops_the_alert(client):
    """The same protection `api_key`'s join against `users` gives: a deactivated account
    stops working on the next call, not whenever somebody remembers this table."""
    _h, sid, secret = desk(client)
    authdb.revoke("m@example.com")
    assert client.post(HOOK, json=alert(sid, secret)).status_code == 401


def test_only_a_session_may_mint_one(client):
    """A key cannot mint a key, and it cannot mint a webhook secret either — otherwise
    revoking a leaked credential is a race against whoever holds it."""
    h = session_headers()
    sid = strategy(client, h)
    raw, _ = authdb.create_api_key("m@example.com")
    r = client.post(f"/v1/strategies/{sid}/webhook",
                    headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 401

    # ...and the webhook secret cannot mint its own replacement.
    secret = webhook(client, h, sid)
    assert client.post(f"/v1/strategies/{sid}/webhook",
                       headers={"Authorization": f"Bearer {secret}"}).status_code == 401


def test_one_account_cannot_mint_on_anothers_strategy(client):
    """404, not 403: telling a caller an id exists but is not theirs answers a question
    they should not be able to ask."""
    mine = session_headers("m@example.com")
    theirs = session_headers("other@example.com")
    sid = strategy(client, mine)
    assert client.post(f"/v1/strategies/{sid}/webhook",
                       headers=theirs).status_code == 404


def test_the_secret_is_shown_once_and_never_again(client):
    h, sid, secret = desk(client)
    info = client.get(f"/v1/strategies/{sid}/webhook", headers=h).json()
    assert info["exists"] is True
    assert secret not in str(info)
    assert info["prefix"] and secret.startswith(info["prefix"])


# ----------------------------------------------------------------------- idempotency

def test_the_same_bar_twice_is_one_order(client):
    """The property the whole route rests on.

    TradingView sends no `client_order_id` and an alert can fire twice on one bar. If the
    derived id stops being stable across a re-fire nothing errors — the book is simply
    doubled, and nobody sees it until a human reconciles it.
    """
    h, sid, secret = desk(client)
    first = client.post(HOOK, json=alert(sid, secret))
    assert first.status_code == 202

    for _ in range(5):
        again = client.post(HOOK, json=alert(sid, secret))
        assert again.status_code == 200          # not 202: it already existed
        assert again.json()["order"]["seq"] == first.json()["order"]["seq"]

    assert len(client.get("/v1/orders", headers=h).json()) == 1


def test_a_different_bar_is_a_different_order(client):
    h, sid, secret = desk(client)
    client.post(HOOK, json=alert(sid, secret, bar_time="2026-08-19T13:30:00Z"))
    r = client.post(HOOK, json=alert(sid, secret, bar_time="2026-08-20T13:30:00Z"))
    assert r.status_code == 202
    assert len(client.get("/v1/orders", headers=h).json()) == 2


def test_the_other_side_of_the_same_bar_is_a_different_order(client):
    h, sid, secret = desk(client)
    client.post(HOOK, json=alert(sid, secret, action="buy"))
    assert client.post(HOOK, json=alert(sid, secret, action="sell")).status_code == 202
    assert len(client.get("/v1/orders", headers=h).json()) == 2


def test_without_a_bar_time_it_says_so(client):
    """A weaker promise, reported rather than implied: duplicates only collapse inside one
    wall-clock minute."""
    _h, sid, secret = desk(client)
    body = alert(sid, secret)
    del body["bar_time"]
    r = client.post(HOOK, json=body)
    assert r.status_code == 202
    assert r.json()["resolved"]["dedupe"] == "minute"
    assert client.post(HOOK, json=body).status_code == 200


def test_your_own_client_order_id_wins(client):
    _h, sid, secret = desk(client)
    r = client.post(HOOK, json=alert(sid, secret, client_order_id="mine-1"))
    assert r.json()["resolved"]["client_order_id"] == "mine-1"
    assert r.json()["resolved"]["dedupe"] == "client"


def test_the_derived_id_fits_the_ledger(client):
    """`client_order_id` is capped at 64 characters on `/v1/orders`, and a derived one has
    to live in the same column."""
    import api_webhook
    coid, _ = api_webhook.derive_coid("str_01_a-very-long-strategy-name-indeed",
                                      "BTC/USD", "sell", "2026-08-19T13:30:00Z")
    assert len(coid) <= 64
    assert " " not in coid


# --------------------------------------------------------------------------- the symbol

@pytest.mark.parametrize("sent,expected", [
    ("BINANCE:BTCUSDT", "BTC/USD"),        # venue prefix, and a stablecoin quote
    ("BTCUSDT.P", "BTC/USD"),              # a perpetual
    ("btcusd", "BTC/USD"),                 # case, and no separator
    ("COINBASE:BTCUSD", "BTC/USD"),
])
def test_a_chart_ticker_finds_the_desks_symbol(client, sent, expected):
    """`{{ticker}}` is the CHART's spelling, and the alert is generated from the chart.

    Refusing every venue prefix would be correct and useless — nobody can make TradingView
    say `BTC/USD`.
    """
    h = session_headers()
    sid = strategy(client, h, symbols=["BTC/USD", "ETH/USD"], cls="crypto")
    secret = webhook(client, h, sid)
    r = client.post(HOOK, json=alert(sid, secret, ticker=sent))
    assert r.status_code == 202, r.text
    assert r.json()["resolved"]["symbol"] == expected


def test_a_symbol_that_was_not_registered_is_refused(client):
    _h, sid, secret = desk(client)
    r = client.post(HOOK, json=alert(sid, secret, ticker="NASDAQ:TSLA"))
    assert r.status_code == 422
    assert "TSLA" in r.json()["detail"]


def test_one_registered_symbol_needs_no_ticker(client):
    _h, sid, secret = desk(client)
    body = alert(sid, secret)
    assert "ticker" not in body
    assert client.post(HOOK, json=body).status_code == 202


def test_several_symbols_need_a_ticker(client):
    h = session_headers()
    sid = strategy(client, h, symbols=["SPY", "QQQ"])
    secret = webhook(client, h, sid)
    r = client.post(HOOK, json=alert(sid, secret))
    assert r.status_code == 422
    assert "ticker" in r.json()["detail"]


def test_the_order_carries_the_desks_spelling(client):
    """The normalised form is a comparison key and is never stored: the desk holds an
    instrument under its own name and an order for `BTCUSDT` would match nothing there."""
    h = session_headers()
    sid = strategy(client, h, symbols=["BTC/USD"], cls="crypto")
    secret = webhook(client, h, sid)
    client.post(HOOK, json=alert(sid, secret, ticker="BINANCE:BTCUSDT"))
    assert client.get("/v1/orders", headers=h).json()[0]["symbol"] == "BTC/USD"


# ------------------------------------------------------------------- honest refusals

def test_an_unsubstituted_placeholder_says_which_mistake_it_is(client):
    """The commonest TradingView error by a distance: a strategy placeholder in an alert
    on an INDICATOR, which substitutes nothing and sends the braces through."""
    _h, sid, secret = desk(client)
    r = client.post(HOOK, json=alert(sid, secret, qty="{{strategy.order.contracts}}"))
    assert r.status_code == 422
    assert "indicator" in r.json()["detail"]


def test_a_body_that_is_not_json_is_a_422_that_explains_itself(client):
    r = client.post(HOOK, content="buy SPY now",
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 422
    assert "JSON" in r.json()["detail"]


def test_an_empty_body_is_refused(client):
    assert client.post(HOOK, content=b"").status_code == 422


def test_a_bad_action_is_refused(client):
    _h, sid, secret = desk(client)
    assert client.post(HOOK, json=alert(sid, secret, action="hodl")).status_code == 422


def test_long_and_short_are_read_as_buy_and_sell(client):
    """Written by hand in a Pine `alert()` call, and they mean the same thing."""
    _h, sid, secret = desk(client)
    r = client.post(HOOK, json=alert(sid, secret, action="LONG"))
    assert r.json()["resolved"]["side"] == "buy"


def test_a_retired_strategy_still_belongs_to_you(client):
    """Retiring does not delete the row, so the webhook keeps authenticating — and the
    desk is what refuses the order. What must not happen is a 500 or a silent accept."""
    h, sid, secret = desk(client)
    client.delete(f"/v1/strategies/{sid}", headers=h)
    assert client.post(HOOK, json=alert(sid, secret)).status_code in (202, 409)


def test_the_rate_limit_applies_here_too(client, monkeypatch):
    """It is counted from the ledger, per account, so a second door does not hand a bot a
    second allowance."""
    monkeypatch.setattr(api_config, "MAX_ORDERS_PER_MINUTE", 2)
    _h, sid, secret = desk(client)
    for i in range(2):
        assert client.post(HOOK, json=alert(sid, secret,
                                            bar_time=f"2026-08-19T13:3{i}:00Z")
                           ).status_code == 202
    r = client.post(HOOK, json=alert(sid, secret, bar_time="2026-08-19T13:39:00Z"))
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "60"


# ------------------------------------------------------------------ what the console gives

def test_the_console_hands_over_a_working_alert(client):
    """The block the page shows is the block that has to work, so it is built here and
    posted back verbatim in this test rather than being retyped in the markup."""
    h = session_headers()
    sid = strategy(client, h, symbols=["SPY", "QQQ"])
    minted = client.post(f"/v1/strategies/{sid}/webhook", headers=h).json()

    assert minted["url"].endswith("/v1/webhook/tradingview")
    body = dict(minted["alert"])
    assert body["ticker"] == "{{ticker}}"          # two symbols, so it must say which

    # Stand in for TradingView: substitute the placeholders it would substitute.
    body.update(action="buy", qty=5, ticker="AMEX:SPY", bar_time="2026-08-19T13:30:00Z")
    r = client.post(HOOK, json=body)
    assert r.status_code == 202, r.text
    assert r.json()["resolved"]["symbol"] == "SPY"


def test_one_symbol_gets_no_ticker_field(client):
    h = session_headers()
    sid = strategy(client, h, symbols=["SPY"])
    minted = client.post(f"/v1/strategies/{sid}/webhook", headers=h).json()
    assert "ticker" not in minted["alert"]


def _account(email: str = "m@example.com") -> str:
    return next(u["account_id"] for u in authdb.users() if u["email"] == email)
