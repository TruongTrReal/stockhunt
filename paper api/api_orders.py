"""`/v1/orders` — the data plane. Where a manager's strategy actually trades.

**This endpoint is an inbox, not a gateway.** It answers `202 Accepted`, which means
"written down, in order" and never "filled". The desk is the only thing that talks to the
trading node; it drains this ledger on its next tick, re-validates everything against the
authoritative book, and submits. Everything after the `202` arrives asynchronously, on
`GET /v1/orders` or the fill log.

That is also how every real broker API behaves, so it costs a manager nothing to
integrate against — and it is what keeps this process unable to trade even if it is
compromised.

**`client_order_id` is required, and it is the idempotency key.** Sending the same one
twice returns the first order with `200` instead of creating a second, so a network
timeout, a retry loop, or a bot restarting mid-flight cannot double a position. This is
the single most important property in the API and the reason the field is not optional.

**Ordering is by `seq`.** The desk drains strictly in submission order, so a cancel can
never overtake the order it cancels.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator

import api_auth
import api_config
import api_paths                                                        # noqa: F401
from stockhunt import deskdb

log = logging.getLogger("stockhunt.api.orders")

router = APIRouter(prefix="/v1", tags=["orders"])

SIDES = ("buy", "sell")
TYPES = ("market", "limit")
TIFS = ("day", "gtc")


class OrderRequest(BaseModel):
    strategy_id: str
    client_order_id: str = Field(
        ..., min_length=1, max_length=64, examples=["mr-2026-08-14-0031"],
        description="Yours, and unique within your account. It is the idempotency key: "
                    "send it twice and you get the first order back, not a second one.")
    symbol: str = Field(..., examples=["SPY"])
    side: str = Field(..., examples=["buy"])
    qty: float = Field(..., gt=0)
    type: str = Field("market", examples=["limit"])
    limit_price: float | None = Field(None, gt=0)
    tif: str = Field("day", examples=["day"])

    @field_validator("client_order_id")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = v.strip()
        if not v or any(c.isspace() for c in v):
            raise ValueError("no spaces, and not empty")
        return v

    @field_validator("side")
    @classmethod
    def _side(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in SIDES:
            raise ValueError(f"must be one of {', '.join(SIDES)}")
        return v

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in TYPES:
            raise ValueError(f"must be one of {', '.join(TYPES)}")
        return v

    @field_validator("tif")
    @classmethod
    def _tif(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in TIFS:
            raise ValueError(f"must be one of {', '.join(TIFS)}")
        return v

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        return v.strip().upper()


class OrderOut(BaseModel):
    seq: int
    client_order_id: str
    strategy_id: str
    action: str
    symbol: str | None = None
    side: str | None = None
    qty: float | None = None
    type: str | None = None
    limit_price: float | None = None
    tif: str | None = None
    target_coid: str | None = None
    # `accepted` means the desk has not looked at it yet. It is not a fill and it is not a
    # promise of one; `reason` carries the desk's refusal when there is one.
    state: str
    filled_qty: float = 0.0
    avg_price: float | None = None
    reason: str | None = None
    submitted_at: str
    applied_at: str | None = None


def _out(row: dict) -> OrderOut:
    return OrderOut(
        seq=row["seq"], client_order_id=row["client_order_id"],
        strategy_id=row["strategy_id"], action=row["action"],
        symbol=row["symbol"], side=row["side"], qty=row["qty"],
        type=row["order_type"], limit_price=row["limit_price"], tif=row["tif"],
        target_coid=row["target_coid"], state=row["state"],
        filled_qty=row["filled_qty"], avg_price=row["avg_price"],
        reason=row["reason"], submitted_at=row["submitted_at"],
        applied_at=row["applied_at"])


def _strategy_of(account: str, strategy_id: str) -> dict:
    reg = deskdb.registration(strategy_id, account=account)
    if reg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such strategy.")
    if reg["kind"] != "member":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="This strategy trades a rule the desk selected; it does not take "
                   "orders.")
    return reg


def _rate_limit(account: str) -> None:
    """Orders per minute, per account.

    Counted from the ledger rather than from an in-process window, so restarting the API
    does not hand a bot a fresh allowance — which is precisely when a bot stuck in a retry
    loop would be hammering it. This one DOES answer with a 429, unlike the sign-in
    limits: the caller is already authenticated, so the status leaks nothing.
    """
    since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
        timespec="seconds")
    if deskdb.orders_since(account, since) >= api_config.MAX_ORDERS_PER_MINUTE:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"More than {api_config.MAX_ORDERS_PER_MINUTE} orders in a minute. "
                   f"The desk trades on bar closes at 1d and 4h — if your strategy needs "
                   f"more than this, something is looping.",
            headers={"Retry-After": "60"})


@router.post("/orders", response_model=OrderOut,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Submit an order. 202 means written down, not filled.")
def submit(body: OrderRequest, response: Response, request: Request,
           who: dict = Depends(api_auth.current_principal)) -> OrderOut:
    """Queue an order for the desk.

    A `202` says the order is recorded and sequenced. It has not reached the exchange and
    may still be refused there — for cash, for a position, or for having gone stale while
    the desk was down. Read `state` and `reason` back to find out.

    Re-sending a `client_order_id` you have already used answers `200` with the original
    order. That is not an error: a client that retried after a timeout genuinely did want
    this order, once.
    """
    account = who["account_id"]
    _strategy_of(account, body.strategy_id)
    _rate_limit(account)

    row, created = deskdb.submit_order(
        account, body.strategy_id, body.client_order_id,
        action="new", symbol=body.symbol, side=body.side, qty=body.qty,
        order_type=body.type, limit_price=body.limit_price, tif=body.tif)

    if not created:
        response.status_code = status.HTTP_200_OK
    return _out(row)


@router.delete("/orders/{client_order_id}", response_model=OrderOut,
               status_code=status.HTTP_202_ACCEPTED,
               summary="Ask to cancel an order. It may already have filled.")
def cancel(client_order_id: str, response: Response,
           who: dict = Depends(api_auth.current_principal)) -> OrderOut:
    """Queue a cancel.

    Also a `202`, and for the same reason: by the time the desk reads it the order may
    already be filled. A cancel that arrives too late is not an error — check the original
    order's state.

    The cancel is itself an order in the ledger, with a derived `client_order_id`, so
    asking twice is one cancel rather than two.
    """
    account = who["account_id"]
    target = deskdb.order(account, client_order_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such order.")
    if target["state"] in deskdb.ORDER_DONE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"That order is already {target['state']}; there is nothing to cancel.")

    _rate_limit(account)
    row, created = deskdb.submit_order(
        account, target["strategy_id"], f"cancel:{client_order_id}",
        action="cancel", target_coid=client_order_id)
    if not created:
        response.status_code = status.HTTP_200_OK
    return _out(row)


@router.get("/orders", response_model=list[OrderOut], summary="Your orders")
def listing(who: dict = Depends(api_auth.current_principal),
            strategy_id: str | None = None,
            state: str | None = None,
            since_seq: int = Query(0, ge=0,
                                   description="Poll from the last seq you saw"),
            limit: int = Query(200, ge=1, le=1000)) -> list[OrderOut]:
    return [_out(r) for r in deskdb.orders(
        who["account_id"], strategy_id=strategy_id, state=state,
        since_seq=since_seq, limit=limit)]


@router.get("/orders/{client_order_id}", response_model=OrderOut, summary="One order")
def one(client_order_id: str,
        who: dict = Depends(api_auth.current_principal)) -> OrderOut:
    row = deskdb.order(who["account_id"], client_order_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such order.")
    return _out(row)
