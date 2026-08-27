"""`/v1/webhook/tradingview` — the one door where the credential rides in the body.

TradingView posts a JSON body to a URL and gives you **no way to add a header**. So a
caller that lives inside a TradingView alert cannot send `Authorization: Bearer` and
cannot use `/v1/orders` at all. This route exists for exactly that constraint and for no
other reason: everything else about it is `/v1/orders`, deliberately, because the two write
to the same ledger and the desk cannot tell them apart afterwards.

Three things are different, and each is a consequence of what an alert can and cannot say.

**The credential is a per-strategy webhook secret, never the account key.** An alert
message is stored in plain text in TradingView's UI, travels in exports, and gets pasted
into chat — it is a place secrets *leak*, so the secret that lives there is the weakest one
the desk can issue. `sk_live_…` trades every strategy on the account, reads the whole book
and retires registrations; `whk_…` submits orders for ONE strategy and does nothing else.
Rotating it costs that one alert instead of every integration the account runs.

**`client_order_id` is derived, because TradingView has no such concept.** It is the
property the whole order API rests on — send the same id twice and you get the first order
back rather than a doubled position — and an alert that fires twice on one bar (a
repainting condition, a re-armed alert) is precisely the case it defends. So the id is
built from strategy, symbol, side and the BAR the alert names. Send `"bar_time": "{{time}}"`
and a duplicate alert on the same bar collapses into one order. Without it there is
nothing stable to key on and the fallback is a one-minute bucket, which is a weaker promise
and is reported back in the response as `dedupe`.

**Errors are 4xx, on purpose.** TradingView never reads our `202`; the only thing its alert
log shows is whether the request failed. A bad secret, an unregistered symbol or a missing
size must therefore come back as a failure status or the operator sees a green tick above
an alert that traded nothing.

The size comes from the alert (`{{strategy.order.contracts}}`) and is required. This
process does not size positions — it does not know the book, and inventing a quantity here
would put the API's opinion inside somebody's track record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import (AliasChoices, BaseModel, ConfigDict, Field, ValidationError,
                      field_validator, model_validator)

import api_auth
import api_orders
import api_paths                                                        # noqa: F401
import authdb
from stockhunt import deskdb

log = logging.getLogger("stockhunt.api.webhook")

router = APIRouter(prefix="/v1/webhook", tags=["webhook"])

PATH = "/v1/webhook/tradingview"

# `{{strategy.order.action}}` renders `buy` or `sell`. The rest are accepted because
# people write them by hand in a Pine `alert()` call and mean the same thing.
ACTIONS = {"buy": "buy", "long": "buy", "sell": "sell", "short": "sell"}

# One refusal, one wording, for every way a credential can fail: unknown secret, revoked
# secret, revoked account, or a secret used against a strategy it was not minted for. They
# are the same answer to the caller because any difference between them tells whoever is
# probing which half they got right.
_UNAUTH = "Bad webhook secret, or it is not this strategy's."


def _no_placeholders(value: str, field: str) -> None:
    """A `{{…}}` that arrived literally is the most common TradingView mistake there is.

    It means a strategy placeholder was used in an alert on an *indicator* — TradingView
    substitutes nothing and sends the braces through. The generic "not a valid number"
    that a raw `{{strategy.order.contracts}}` produces sends people looking at their JSON,
    which is fine; this sends them to the alert.
    """
    if "{{" in value:
        raise ValueError(
            f"{field} still contains the TradingView placeholder {value!r}. It was not "
            f"substituted, which usually means the alert is on an indicator rather than "
            f"on a strategy — only strategy alerts can fill in "
            f"{{{{strategy.order.contracts}}}} and {{{{strategy.order.action}}}}.")


class TradingViewAlert(BaseModel):
    """The alert body.

    Field names are generous on input because the body is typed by hand into a
    TradingView alert box and there is no client library to get it right: `strategyId` and
    `strategy_id` are one field, and so are `password`, `secret` and `key`. `extra` is
    ignored rather than rejected, because an alert that also carries `{{close}}` for a
    human reading the log must not be refused for it.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    strategy_id: str = Field(
        ..., validation_alias=AliasChoices("strategy_id", "strategyId", "strategy"),
        examples=["str_01_meanrev"])
    secret: str = Field(
        ..., validation_alias=AliasChoices("secret", "password", "key", "webhook_secret"),
        examples=["whk_…"],
        description="The strategy's webhook secret, from the desk console. NOT your "
                    "sk_live_ API key — that one is never sent in a body.")
    action: str = Field(..., validation_alias=AliasChoices("action", "side"),
                        examples=["buy"])
    qty: float = Field(
        ..., gt=0,
        validation_alias=AliasChoices("qty", "contracts", "quantity", "size", "amount"),
        description="From {{strategy.order.contracts}}. Required: this process does not "
                    "size positions, because it cannot see the book.")
    symbol: str | None = Field(
        None, validation_alias=AliasChoices("symbol", "ticker"), examples=["{{ticker}}"],
        description="Optional only when the strategy registered exactly one symbol.")
    bar_time: str | None = Field(
        None, validation_alias=AliasChoices("bar_time", "time", "bar", "timenow"),
        examples=["{{time}}"],
        description="The bar's own timestamp. Send {{time}}, not {{timenow}}: it is what "
                    "makes a duplicate alert on one bar idempotent.")
    client_order_id: str | None = Field(
        None, max_length=64,
        validation_alias=AliasChoices("client_order_id", "clientOrderId", "coid"),
        description="Yours, if you would rather build it. Overrides the derived one.")
    type: str = Field("market", validation_alias=AliasChoices("type", "order_type"))
    limit_price: float | None = Field(
        None, gt=0, validation_alias=AliasChoices("limit_price", "price"))
    tif: str = "day"

    @model_validator(mode="before")
    @classmethod
    def _unsubstituted(cls, data):
        """Catch `{{…}}` before any per-field parse turns it into a shapeless error."""
        if isinstance(data, dict):
            for name, value in data.items():
                if isinstance(value, str):
                    _no_placeholders(value, str(name))
        return data

    @field_validator("strategy_id", "secret", mode="before")
    @classmethod
    def _trim(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("action")
    @classmethod
    def _action(cls, v: str) -> str:
        key = v.strip().lower()
        if key not in ACTIONS:
            raise ValueError(f"must be one of {', '.join(sorted(set(ACTIONS)))}")
        return ACTIONS[key]

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in api_orders.TYPES:
            raise ValueError(f"must be one of {', '.join(api_orders.TYPES)}")
        return v

    @field_validator("tif")
    @classmethod
    def _tif(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in api_orders.TIFS:
            raise ValueError(f"must be one of {', '.join(api_orders.TIFS)}")
        return v


class Resolved(BaseModel):
    """What the alert was understood to mean. Nothing here is a fill."""
    symbol: str
    side: str
    qty: float
    client_order_id: str
    # `bar` when the alert named its bar and the id is stable across a re-fire; `minute`
    # when it did not, and duplicates only collapse within the same wall-clock minute.
    dedupe: str


class WebhookOut(BaseModel):
    ok: bool = True
    resolved: Resolved
    order: api_orders.OrderOut


# ----------------------------------------------------------------- resolving the symbol

def normalize_symbol(symbol: str) -> str:
    """A TradingView ticker and a desk symbol, reduced to the same string.

    `{{ticker}}` is the chart's ticker, not the desk's: it arrives as `BINANCE:BTCUSDT`,
    `NASDAQ:AAPL`, `BTCUSDT.P` or `ES1!` for a book that holds `BTC/USD`, `AAPL` and
    `ES.v.0`. Refusing every one of those and telling a manager to type the desk's spelling
    into their alert would be correct and useless — the alert is generated from the chart.

    So both sides are reduced: venue prefix dropped, perpetual and continuous-contract
    suffixes dropped, punctuation removed (`BRK.B` and `BTC/USD` survive that intact), and
    a stablecoin quote folded to USD, because `BTCUSDT` on Binance and the desk's `BTC/USD`
    are the same trade to a paper book that prices in dollars.

    It is deliberately a comparison key and NEVER a symbol that gets stored: the order
    carries the symbol as the DESK spells it, which is the registration's own string.
    """
    s = symbol.strip().upper()
    if ":" in s:
        s = s.rsplit(":", 1)[1]                     # BINANCE:BTCUSDT -> BTCUSDT
    if s.endswith(".P"):
        s = s[:-2]                                  # BTCUSDT.P (perpetual) -> BTCUSDT
    s = re.sub(r"\d*!$", "", s)                     # ES1! (continuous) -> ES
    # ...and the DESK's spelling of the same continuous contract. Both sides are reduced
    # here, so dropping one form and not the other is not a smaller version of the same
    # rule — it is the two sides landing on `ES` and `ESV0` and never matching. Every
    # alert on a `cme_futures` book would have answered 422 naming the symbol it was
    # registered for.
    s = re.sub(r"\.[A-Z]\.\d{1,2}$", "", s)         # ES.v.0 (Databento continuous) -> ES
    s = re.sub(r"[^A-Z0-9]", "", s)                 # BTC/USD -> BTCUSD, BRK.B -> BRKB
    for quote in ("USDT", "USDC"):
        if s.endswith(quote) and len(s) > len(quote):
            s = s[: -len(quote)] + "USD"
    return s


def resolve_symbol(registered: list[str], asked: str | None) -> str:
    """Which registered symbol the alert means, as the desk spells it."""
    if asked is None:
        if len(registered) == 1:
            return registered[0]
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"This strategy is registered for {', '.join(registered)}, so the "
                    f"alert has to say which. Add \"ticker\": \"{{{{ticker}}}}\" to the "
                    f"message."))

    want = normalize_symbol(asked)
    hits = [s for s in registered if normalize_symbol(s) == want]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"{asked} is not one of this strategy's symbols "
                    f"({', '.join(registered)}). The desk only holds instruments it was "
                    f"asked for, so an order for anything else is refused there too."))
    # Two registered symbols that reduce to one key. Unreachable with today's universes,
    # and if it ever happens the honest answer is to say so rather than to pick the first
    # one — a silent choice here is a position in the wrong book.
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=(f"{asked} matches more than one of this strategy's symbols "
                f"({', '.join(hits)}). Send the desk's own spelling."))


# --------------------------------------------------------- deriving the idempotency key

def derive_coid(strategy_id: str, symbol: str, side: str,
                bar_time: str | None) -> tuple[str, str]:
    """`(client_order_id, dedupe)` for an alert that carries no id of its own.

    Stable across a re-fire when the alert names its bar, which is the whole point: the
    same bar, symbol and side produce the same id, so the second copy answers `200` with
    the first order instead of doubling the position.

    Without a bar it falls back to a one-minute bucket. That is a real trade-off in the
    other direction — two *deliberate* same-side orders on one symbol inside one minute
    would collapse into one — and it is taken knowingly: this desk trades bar closes at
    `1d` and `4h`, where a doubled position is the far more likely accident. The response
    says which of the two applied so nobody has to guess.
    """
    if bar_time and bar_time.strip():
        stamp, dedupe = bar_time.strip(), "bar"
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
        dedupe = "minute"
    digest = hashlib.sha256(
        f"{strategy_id}|{symbol}|{side}|{stamp}".encode("utf-8")).hexdigest()[:12]
    head = re.sub(r"[^A-Za-z0-9._-]", "", f"tv-{symbol}-{side}")[:40]
    return f"{head}-{digest}", dedupe


# ------------------------------------------------------------------------- the endpoint

def _parse(raw: bytes) -> TradingViewAlert:
    """The body, whatever TradingView called it.

    TradingView sets `Content-Type: text/plain` unless it decides the message is JSON, and
    FastAPI hands a pydantic model nothing but bytes in that case — a `422` reading "input
    should be a valid dictionary" for a body that is a perfectly good JSON object. So the
    parse happens here and the content type is not consulted at all.
    """
    if not raw.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail="Empty body. The alert message must be the JSON.")
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The alert message is not JSON. Paste the block from the desk console "
                   "into the alert's Message box exactly as it is given.")
    if not isinstance(data, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail="The alert message must be a JSON object.")
    try:
        return TradingViewAlert.model_validate(data)
    except ValidationError as exc:
        # Flattened to one line per bad field. The caller reading this is a person looking
        # at TradingView's alert log, which shows a response body and no formatting.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'body'}: {e['msg']}"
            for e in exc.errors())
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problems)


def _submit(alert: TradingViewAlert, ip: str | None) -> tuple[WebhookOut, bool]:
    """Everything after the parse. Synchronous, and run off the event loop."""
    row = authdb.webhook_secret(authdb.hash_key(alert.secret))
    if row is None or row["strategy_id"] != alert.strategy_id:
        # Audited without an email because there is no identity to name: whoever sent this
        # did not authenticate. The strategy id is recorded because it is the only handle
        # on which alert is misconfigured.
        authdb.audit("webhook.denied", None, ip, f"strategy={alert.strategy_id!r}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_UNAUTH)

    account = row["account_id"]
    reg = api_orders.strategy_of(account, alert.strategy_id)
    api_orders.rate_limit(account)

    symbol = resolve_symbol(list(reg["symbols"]), alert.symbol)
    if alert.client_order_id:
        coid, dedupe = alert.client_order_id.strip(), "client"
    else:
        coid, dedupe = derive_coid(alert.strategy_id, symbol, alert.action,
                                   alert.bar_time)

    order, created = deskdb.submit_order(
        account, alert.strategy_id, coid, action="new", symbol=symbol,
        side=alert.action, qty=alert.qty, order_type=alert.type,
        limit_price=alert.limit_price, tif=alert.tif)

    return WebhookOut(
        resolved=Resolved(symbol=symbol, side=alert.action, qty=alert.qty,
                          client_order_id=coid, dedupe=dedupe),
        order=api_orders.order_out(order),
    ), created


@router.post("/tradingview", response_model=WebhookOut,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Submit an order from a TradingView alert. 202 = written down.")
async def tradingview(request: Request, response: Response) -> WebhookOut:
    """The alert lands here.

    Unauthenticated as a *dependency* — there is no header to read — and authenticated in
    the body, by a secret that names one strategy. It reaches the same ledger, under the
    same limits, with the same `202` meaning: written down and sequenced, never filled.
    """
    alert = _parse(await request.body())
    out, created = await run_in_threadpool(_submit, alert, api_auth.client_ip(request))
    if not created:
        # The alert fired twice on one bar, and the second one changed nothing. `200` and
        # not an error: it genuinely did want this order, once.
        response.status_code = status.HTTP_200_OK
    return out
