# Stockhunt paper desk — integration brief

You are connecting a trading strategy to the Stockhunt paper-trading desk. This document
is the whole contract. Everything in it is enforced; nothing in it is advisory.

    BASE   {{BASE}}
    AUTH   Authorization: Bearer sk_live_...        on every call
    DOCS   {{BASE}}/desk/docs        human reference
           {{BASE}}/openapi.json     machine-readable schema

There is exactly one exception to that AUTH line, and it exists because TradingView cannot
send a header at all: `POST /v1/webhook/tradingview` carries a per-strategy secret in the
body instead. See **TradingView** below. Everything else on this page needs the key.

Your key and your `strategy_id` come from the desk console at `{{BASE}}/desk`. A key is
shown once, at the moment it is minted, and is stored as a hash — it cannot be retrieved
later. If you do not have one, stop and ask the person who runs your strategy.

**This is paper trading.** No real money is at risk anywhere in this system. The desk fills
against live bars and keeps a book, and the record of what your strategy did is permanent.

---

## The five rules

**1. `client_order_id` is the idempotency key, and it is required.**
Send the same one twice and you get the first order back with `200` instead of a second
order with `202`. Derive it from something that is stable across a retry — strategy,
symbol, and the timestamp of the *bar* you are trading. Never from a clock, a counter in
memory, or a random value: a network timeout followed by a retry then becomes two orders
and a doubled position, and nobody sees it until a human reconciles the book by hand.

    good   meanrev-SPY-20260815          derived from the bar
    bad    meanrev-SPY-1755264331        derived from now(); a retry makes a second order

**2. `202` means written down, not filled.**
This endpoint is an inbox. The desk drains it on its next pass, re-checks every order
against the authoritative book, and only then submits. A `202` is not a fill, not a
promise of one, and carries no price. Read the state back with `GET /v1/orders`; `reason`
is where a refusal explains itself.

**3. You may only trade the symbols, class and timeframe you registered.**
Anything else is refused with a reason. Registration is a deliberate act performed by a
human in the console — do not attempt to register, widen, or re-register a strategy to
make an order fit.

**4. Rate limit: orders per minute, per account, answered with `429` and `Retry-After`.**
Honour the header. Do not retry in a tight loop. The desk trades on bar closes at `1d` and
`4h`; if you need more than the cap, something in your code is looping rather than
trading.

**5. On `401`, stop.**
The key is wrong or has been revoked. Retrying will not fix it and re-minting is not
something you can do — keys are minted only behind a browser login, never by another key.
Surface the error to your operator and exit.

---

## Sending an order

    POST /v1/orders
    Authorization: Bearer sk_live_...
    Content-Type: application/json

    {
      "strategy_id":     "str_01_meanrev",   required, yours
      "client_order_id": "meanrev-SPY-20260815",   required, unique within your account
      "symbol":          "SPY",          must be one you registered
      "side":            "buy",          buy | sell
      "qty":             10,             > 0
      "type":            "market",       market | limit          (default market)
      "limit_price":     null,           required when type=limit
      "tif":             "day"           day | gtc               (default day)
    }

    → 202 {"seq": 1, "client_order_id": "...", "state": "accepted",
           "filled_qty": 0.0, "avg_price": null, "reason": null,
           "submitted_at": "2026-08-15T12:04:31+00:00", "applied_at": null}

`seq` is monotonic per account and the desk drains strictly in that order, so a cancel can
never overtake the order it cancels.

If shorting was not enabled on the strategy, a `sell` may only close a long position.

## Reading it back

    GET /v1/orders?strategy_id=str_01_meanrev&since_seq=<last seq you saw>&limit=200
    GET /v1/orders/{client_order_id}

Poll with `since_seq`, not with a timestamp: it is exact, it cannot skip a row, and it
cannot return the same row twice. Keep the highest `seq` you have processed.

    state       meaning
    accepted    in the ledger; the desk has not looked at it yet
    working     submitted to the venue
    partial     partially filled; `filled_qty` says how much
    filled      done; `avg_price` is the fill
    rejected    refused. `reason` says by whom and why
    cancelled   your cancel won the race

`accepted`, `working` and `partial` are live. `filled`, `rejected` and `cancelled` are
terminal — an order in one of those states will never change again.

## Cancelling

    DELETE /v1/orders/{client_order_id}      → 202

Also a `202`, and for the same reason: by the time the desk reads it, the order may already
have filled. That is not an error — check the original order's state. Cancelling twice is
one cancel, not two.

## Your strategies

    GET /v1/strategies                       everything you own
    GET /v1/strategies/{strategy_id}         one

`want` is what was asked for and `state` is what the desk has actually done. They disagree
for a while by design: asking to pause while the desk is between passes leaves
`want=paused, state=live` until it catches up. Trade on `state`.

A strategy that is `paused` accepts no orders. One that is `retired` accepts none and never
will again.

    DELETE /v1/strategies/{strategy_id}              retire. The record is kept
    DELETE /v1/strategies/{strategy_id}?purge=true   remove it entirely → 204

Retiring keeps everything: fills, equity curve, gaps. That is deliberate — a forward test
somebody can erase is not a record, and a manager who could remove a losing run could
remove the evidence of it.

`?purge=true` is the single exception and it is narrow on purpose. It removes a
registration that **never placed an order**, which recorded nothing and is therefore
evidence of nothing — a typo or a trial run, not history. It refuses with `409` and a
reason for anything else: still running, not yours, a house rule (those trade on their own
rule, so an empty order ledger proves nothing about them), or one order ever, including a
rejected one.

Without the flag `DELETE` behaves exactly as it always has, so a client that has never
heard of purging cannot lose a strategy by being upgraded.

## TradingView, where the credential is in the body

TradingView posts JSON to a URL and offers **no way to add a header**, so an alert cannot
authenticate the way everything above does. One route exists for that constraint:

    POST {{BASE}}/v1/webhook/tradingview     no Authorization header, no cookie

Everything else about it is `/v1/orders`: the same ledger, the same per-minute cap, the
same `202` meaning written down and never filled.

**The credential is a per-strategy webhook secret, not your API key.** Mint it in the
console — the **TradingView** tab while registering, or the **TradingView** button on the
strategy's row afterwards. It is `whk_…`, and it is shown once. It can do exactly one
thing: place orders for that one strategy. Your `sk_live_…` key trades everything you own,
reads your whole book and retires registrations, and an alert message is stored in plain
text in TradingView's UI and travels in exports, so the two are deliberately not the same
credential. Minting again rotates it and kills the previous one.

Paste this into the alert's **Message** box — the console generates it with your secret
already in it:

    {
      "strategyId": "str_01_meanrev",
      "secret":     "whk_...",
      "action":     "{{strategy.order.action}}",
      "qty":        "{{strategy.order.contracts}}",
      "bar_time":   "{{time}}",
      "ticker":     "{{ticker}}"
    }

    → 202 {"ok": true,
           "resolved": {"symbol": "SPY", "side": "buy", "qty": 10,
                        "client_order_id": "tv-SPY-buy-9f2c1a0b3d4e", "dedupe": "bar"},
           "order": {"seq": 41, "state": "accepted", ...}}

Four things about that body, each of which is the difference between working and looking
like it works:

**1. The alert must be on a strategy, not on an indicator.** Only a strategy can fill in
`{{strategy.order.action}}` and `{{strategy.order.contracts}}`; on an indicator TradingView
substitutes nothing and sends the braces through, and the reply says exactly that.

**2. `bar_time` is `{{time}}`, never `{{timenow}}`.** There is no `client_order_id` in a
TradingView alert, so one is derived from strategy, symbol, side and the bar — which makes
an alert that fires twice on one bar **one** order instead of a doubled position. `{{time}}`
is the bar's own timestamp and is identical across those two firings; `{{timenow}}` is the
moment the alert fired and differs between them, which would defeat the protection it
looks like it provides. Leave the field out and the fallback is a one-minute bucket; the
reply says which applied, as `dedupe`: `bar`, `minute` or `client`.

**3. The size is yours.** `qty` is required, from `{{strategy.order.contracts}}`. This desk
does not size positions for you — it cannot see your book, and a quantity invented here
would be the API's opinion inside your track record.

**4. `ticker` is optional when the strategy holds one symbol.** When you send it, the chart's
spelling is matched against what you registered: `BINANCE:BTCUSDT`, `BTCUSDT.P` and
`COINBASE:BTCUSD` all resolve to a registered `BTC/USD`. Anything that resolves to nothing
you registered is refused with `422` and the list of what you did.

Field names are generous, because the body is typed by hand: `strategyId`/`strategy_id`,
`secret`/`password`/`key`, `qty`/`contracts`/`size`, `symbol`/`ticker`, `bar_time`/`time`.
Extra fields are ignored, so adding `{{close}}` for your own log costs nothing.

**Read the status, not the body.** TradingView's alert log shows you whether the request
failed and nothing else, so every refusal here is a `4xx` on purpose — `401` for a wrong or
revoked secret, `422` for a symbol or a size it could not use, `429` over the cap. A green
tick means the order reached the ledger. It still does not mean a fill; that is `GET
/v1/orders`, and it needs an API key.

**The URL must be port 80 or 443, on a publicly trusted certificate** — that is
TradingView's rule, not this desk's. `{{BASE}}` above already satisfies it.

## Is the desk actually running?

    GET /v1/desk    → {"live": true, "seconds_ago": 0.4, "ticks": 91204,
                       "pending": 0, "error": null}

The desk passes over the ledger about once a second, and stamps a heartbeat when each pass
finishes. `live` is that heartbeat being recent; `pending` is how many of **your**
strategies are still waiting for a pass.

`error` is what the last pass failed with, and `live: true` with an `error` set is a desk
that is up and getting nowhere — it guards each stage and carries on past a failure, so a
pass that fails every time still completes and still beats. Treat that as down for the
purpose of waiting on anything.

**This is the one thing `want <> state` cannot tell you.** A registration that the desk
read a moment ago and one that nothing has read since Tuesday look identical in
`/v1/strategies` — both just show the two fields disagreeing. If your client waits on a
`state`, wait on this too, or a stopped desk is indistinguishable from a slow one and your
loop blocks forever on a pass that is not coming.

Nothing in this API depends on it. Your orders are written to the ledger and applied
whenever the desk next runs, heartbeat or no heartbeat — this only tells you when that
will be.

---

## Errors

    200   you already sent this client_order_id — the first order is in the body. Not an error.
    202   accepted into the ledger. Not a fill.
    401   key wrong or revoked. Stop; do not re-mint, you cannot.
    404   not your strategy, or no such order. Check strategy_id.
    409   a limit, or an order that is already terminal. `detail` says which.
    422   the body is the wrong shape. `detail` names the field and what it wanted.
    429   over the per-minute order cap. Honour `Retry-After`.
    5xx   the desk, not you. Retry the SAME client_order_id — idempotency makes that safe.

Every error body is `{"detail": ...}` — a string, or for a `422` a list of
`{"loc": [...], "msg": "..."}`. Both are written for whoever is debugging a client, so
show them verbatim rather than replacing them with your own wording.

## A minimal client

```python
import os, requests

BASE = os.environ["STOCKHUNT_BASE"]
SID  = os.environ["STOCKHUNT_STRATEGY_ID"]
H    = {"Authorization": "Bearer " + os.environ["STOCKHUNT_API_KEY"]}


def order(symbol, side, qty, bar_date):
    """Idempotent on (symbol, bar). Safe to call again after a timeout."""
    r = requests.post(f"{BASE}/v1/orders", headers=H, timeout=15, json={
        "strategy_id": SID,
        "client_order_id": f"{symbol}-{bar_date:%Y%m%d}-{side}",
        "symbol": symbol, "side": side, "qty": qty,
        "type": "market", "tif": "day"})
    if r.status_code == 401:
        raise SystemExit("key revoked or wrong — stopping")
    if r.status_code == 429:
        raise RuntimeError(f"rate limited; retry after {r.headers.get('Retry-After')}s")
    r.raise_for_status()
    return r.json()            # state is 'accepted', NOT a fill


def drain(since_seq=0):
    """Everything that has happened since you last looked."""
    r = requests.get(f"{BASE}/v1/orders", headers=H, timeout=15,
                     params={"strategy_id": SID, "since_seq": since_seq})
    r.raise_for_status()
    return r.json()
```

## What this desk will not do

It will not tell you what to trade. There is no signal, no recommendation and no model
behind this API — the research that lives elsewhere in Stockhunt is not exposed here, and
the house's own strategies do not take orders. Deciding is entirely your side of the line;
executing and keeping the record is entirely this side.
