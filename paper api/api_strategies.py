"""`/v1/strategies` — registering a strategy for the desk to run.

The control plane: rare, deliberate calls that say what should exist. `api_orders.py` is
the data plane, and the two are separated because they have nothing in common but a
credential — one is a manager deciding to deploy, the other is their code trading.

**Nothing here places an order or touches the trading engine.** A registration is a row in
`stockhunt.deskdb`; the desk reads it on its next tick and decides. That is why every
response comes back `pending` rather than `live`, and why `reason` exists — the desk may
refuse, minutes later, for something this process cannot see.

**Two layers of validation, and they check different things.** Here: shape, limits,
ownership — everything answerable without a book, answered immediately so a manager
debugging their client gets a real error in the response. There: the universe, the venue,
the capital, the position. The desk's checks bind; these exist to be fast and kind.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator

import api_auth
import api_config
import api_live
import api_paths                                                        # noqa: F401
import authdb
from stockhunt import deskdb

log = logging.getLogger("stockhunt.api.strategies")

router = APIRouter(prefix="/v1/strategies", tags=["strategies"])

# `/v1/limits` is a sibling, not a child: a path under `/v1/strategies/` would sit in the
# same namespace as `/{strategy_id}` and be reachable only by declaring it first, which is
# a route table that breaks when somebody reorders it.
limits_router = APIRouter(prefix="/v1", tags=["strategies"])

# Asset classes a registration may name. Held here rather than imported from the engine
# because this process imports no trading code — and it is a list that changes about once
# a year, against a rule that says the desk re-checks it anyway.
CLASSES = ("us_stocks", "us_etfs", "crypto", "commodities")

NAME_MAX = 40
SYMBOLS_MAX = 20


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=NAME_MAX, examples=["meanrev"])
    cls: str = Field(..., examples=["us_stocks"],
                     description="Which asset class, and therefore which venue")
    symbols: list[str] = Field(..., min_length=1, max_length=SYMBOLS_MAX,
                               examples=[["SPY"]])
    tf: str = Field("1d", examples=["1d"])
    benchmark: str | None = Field(
        None, description="Optional. Declared, never guessed — a multi-symbol strategy "
                          "has no obvious baseline, and choosing one for you would put "
                          "this desk's opinion inside your track record.")
    allow_short: bool = False

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        """The name becomes part of a `strategy_id`, a Nautilus `order_id_tag` and a row
        key in the desk's record, so it is restricted to what all three can carry."""
        v = v.strip().lower()
        if not v or not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError("letters, digits, dashes and underscores only")
        return v

    @field_validator("cls")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in CLASSES:
            raise ValueError(f"must be one of {', '.join(CLASSES)}")
        return v

    @field_validator("tf")
    @classmethod
    def _tf(cls, v: str) -> str:
        if v not in api_config.TIMEFRAMES:
            raise ValueError(f"must be one of {', '.join(api_config.TIMEFRAMES)}")
        return v

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        out = [s.strip().upper() for s in v if s and s.strip()]
        if not out:
            raise ValueError("name at least one symbol")
        if len(set(out)) != len(out):
            raise ValueError("the same symbol twice")
        return out


class StrategyOut(BaseModel):
    strategy_id: str
    name: str
    kind: str
    cls: str
    symbols: list[str]
    tf: str
    capital: float
    benchmark: str | None = None
    allow_short: bool = False
    # What you asked for, and what the desk has actually done. They disagree for a while
    # by design: asking to pause while the desk is down leaves want='paused' and
    # state='live' until its next tick, which is the truth rather than a bug.
    want: str
    state: str
    reason: str | None = None
    created_at: str
    applied_at: str | None = None


def _out(row: dict) -> StrategyOut:
    return StrategyOut(
        strategy_id=row["strategy_id"], name=row["name"], kind=row["kind"],
        cls=row["cls"], symbols=row["symbols"], tf=row["tf"],
        capital=row["capital"], benchmark=row["benchmark"],
        allow_short=bool(row["allow_short"]), want=row["want"], state=row["state"],
        reason=row["reason"], created_at=row["created_at"],
        applied_at=row["applied_at"])


def _mine(strategy_id: str, account: str) -> dict:
    """One of my strategies, or 404.

    404 and not 403 for somebody else's id: telling a caller that an id exists but is not
    theirs answers a question they should not be able to ask.
    """
    row = deskdb.registration(strategy_id, account=account)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such strategy.")
    return row


class LimitsOut(BaseModel):
    """The terms a registration runs under, as the desk will actually enforce them."""
    classes: list[str]
    timeframes: list[str]
    name_max: int
    symbols_max: int
    capital_per_strategy: float
    max_strategies: int
    max_orders_per_minute: int
    # Per class, the symbols the desk already subscribes to — empty when the desk has not
    # published a catalog yet. The console offers these and refuses anything else, because
    # the desk does: a symbol it does not hold an instrument for costs a subscription and a
    # full warm-up, so it is refused there rather than quietly added.
    universe: dict[str, list[str]]


@limits_router.get("/limits", response_model=LimitsOut,
                   summary="What a registration costs you, and what it may not exceed")
def limits(who: dict = Depends(api_auth.current_principal)) -> LimitsOut:
    """Every number the console shows a manager *before* they register.

    The console states these terms up front — capital, the class list, the order cap —
    and every one of them is settable from the environment. Written into the page instead,
    they would go on reading `$10,000` and `us_stocks, us_etfs, crypto, commodities` on the
    day somebody changes `API_CAPITAL_PER_STRATEGY` or adds a class, and a page that
    misstates the terms is worse than one that omits them.

    Authenticated like the rest of `/v1`, though it carries no account data: the whole API
    is behind the allowlist and there is no reason for this to be the one door left open.

    The universe is read from what the desk PUBLISHED, never computed here — same contract
    as `live.json`. An empty one means the desk has not run `catalog.py` yet, and the
    console falls back to accepting a typed symbol rather than refusing to register at all:
    a research artifact that has not been rebuilt must not be able to close the door.
    """
    universe = ((api_live.catalog() or {}).get("universe") or {})
    return LimitsOut(
        universe={k: list(v) for k, v in universe.items() if k in CLASSES},
        classes=list(CLASSES),
        timeframes=list(api_config.TIMEFRAMES),
        name_max=NAME_MAX,
        symbols_max=SYMBOLS_MAX,
        capital_per_strategy=api_config.CAPITAL_PER_STRATEGY,
        max_strategies=api_config.MAX_STRATEGIES_PER_ACCOUNT,
        max_orders_per_minute=api_config.MAX_ORDERS_PER_MINUTE,
    )


class DeskOut(BaseModel):
    """Is anybody reading the ledger, and how far behind are they?"""
    live: bool
    last_pass_at: str | None = None
    seconds_ago: float | None = None
    ticks: int = 0
    # What the desk's last pass failed with, if it failed. A beating pulse with this set is
    # a desk that is up and getting nowhere — which without it reads exactly like a healthy
    # one, while a change nobody can apply sits there looking merely slow.
    error: str | None = None
    # How many of THIS caller's strategies are still waiting for a pass — `want` and
    # `state` disagreeing, or never applied at all.
    pending: int = 0
    stale_after: int = api_config.DESK_STALE_SECONDS


@limits_router.get("/desk", response_model=DeskOut,
                   summary="Whether the desk is running, and how far behind it is")
def desk(who: dict = Depends(api_auth.current_principal)) -> DeskOut:
    """The pulse, so the console can stop guessing.

    `want <> state` is the only thing the registrations table says while a request is
    outstanding, and it says exactly the same thing whether the desk read the row a moment
    ago or has been down since Tuesday. The console showed one sentence for both — *the
    desk applies it on its next pass* — which is a promise this process has no standing to
    make: it does not run the desk and cannot start it.

    With the heartbeat those separate. A pending row against a beating pulse is in flight;
    the same row against a silent one is going nowhere, and saying so is the difference
    between a member waiting a second and a member waiting all afternoon.

    Reports the caller's own pending count and nothing about anyone else's: an outage is
    shared, a backlog is not, and the desk-wide figure would leak how many strategies other
    members are running.
    """
    p = deskdb.pulse()
    age = p["age_seconds"]
    mine = deskdb.registrations(who["account_id"])
    return DeskOut(
        live=age is not None and age <= api_config.DESK_STALE_SECONDS,
        last_pass_at=p["at"],
        # Clamped at zero: the desk stamps its own clock and this process reads its own, so
        # a second of skew between them must not surface as a pulse from the future.
        seconds_ago=None if age is None else max(0.0, round(age, 1)),
        ticks=p["ticks"],
        error=p["error"],
        # `want` is only ever live/paused/retired and `state` starts at 'pending', so a
        # disagreement is the whole definition — a fresh registration is already one.
        pending=sum(1 for r in mine
                    if r["state"] not in deskdb.REGISTRATION_DONE
                    and r["want"] != r["state"]),
    )


@router.post("", response_model=StrategyOut, status_code=status.HTTP_201_CREATED,
             summary="Register a strategy for the desk to run")
def register(body: RegisterRequest, request: Request,
             who: dict = Depends(api_auth.current_principal)) -> StrategyOut:
    """Ask the desk to run a strategy. Comes back `pending`.

    Idempotent on the name: registering one you already have returns it unchanged rather
    than creating a second. A deploy script that runs twice must not end up with two books
    under one name, quietly splitting the capital between them.
    """
    account = who["account_id"]

    existing = [r for r in deskdb.registrations(account)
                if r["state"] not in deskdb.REGISTRATION_DONE]
    if (len(existing) >= api_config.MAX_STRATEGIES_PER_ACCOUNT
            and not any(r["name"] == body.name for r in existing)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(f"You already have {len(existing)} strategies, which is the limit. "
                    f"Retire one first."))

    row = deskdb.register(
        account, body.name, body.cls, body.symbols, body.tf,
        api_config.CAPITAL_PER_STRATEGY, kind="member",
        benchmark=body.benchmark, allow_short=body.allow_short)
    authdb.audit("strategy.registered", who["email"], api_auth.client_ip(request),
                 f"{row['strategy_id']} {body.cls} {','.join(body.symbols)} {body.tf}")
    return _out(row)


@router.get("", response_model=list[StrategyOut], summary="Your strategies")
def mine(who: dict = Depends(api_auth.current_principal)) -> list[StrategyOut]:
    return [_out(r) for r in deskdb.registrations(who["account_id"])]


@router.get("/{strategy_id}", response_model=StrategyOut, summary="One strategy")
def one(strategy_id: str,
        who: dict = Depends(api_auth.current_principal)) -> StrategyOut:
    return _out(_mine(strategy_id, who["account_id"]))


@router.post("/{strategy_id}/pause", response_model=StrategyOut,
             summary="Stop accepting orders, keep the positions")
def pause(strategy_id: str, request: Request,
          who: dict = Depends(api_auth.current_principal)) -> StrategyOut:
    row = _mine(strategy_id, who["account_id"])
    # The same guard `resume` carries, and for a sharper reason. `want` and `state` are
    # allowed to disagree while the desk catches up — that is the design — but a terminal
    # row has no catching up left to do, so pausing one writes a disagreement that NO pass
    # can ever reconcile. `want=paused, state=retired` then sits on the console forever,
    # which is exactly the shape of thing this seam is supposed to make impossible.
    if row["state"] in deskdb.REGISTRATION_DONE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"This strategy is {row['state']} and cannot be paused. The desk is "
                   f"finished with it — there is nothing left to stop.")
    deskdb.set_want(who["account_id"], strategy_id, "paused")
    authdb.audit("strategy.paused", who["email"], api_auth.client_ip(request),
                 strategy_id)
    return _out(_mine(strategy_id, who["account_id"]))


@router.post("/{strategy_id}/resume", response_model=StrategyOut,
             summary="Start accepting orders again")
def resume(strategy_id: str, request: Request,
           who: dict = Depends(api_auth.current_principal)) -> StrategyOut:
    row = _mine(strategy_id, who["account_id"])
    if row["state"] in deskdb.REGISTRATION_DONE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"This strategy is {row['state']} and cannot be resumed. Register a "
                   f"new one — its record is kept either way.")
    deskdb.set_want(who["account_id"], strategy_id, "live")
    authdb.audit("strategy.resumed", who["email"], api_auth.client_ip(request),
                 strategy_id)
    return _out(_mine(strategy_id, who["account_id"]))


@router.delete("/{strategy_id}", response_model=None,
               summary="Retire a strategy. Its record is kept.")
def retire(strategy_id: str, request: Request, purge: bool = False,
           who: dict = Depends(api_auth.current_principal)):
    """Flatten and stop.

    The equity curve, the fills and the gaps stay in the desk's record forever. Deleting
    them is not on offer: a forward test somebody can erase is not a record, and a manager
    who could remove a losing run could remove the evidence of it.

    **`?purge=true` is the one exception, and it is narrow by construction.** It removes a
    registration that never traded at all — terminal, a member's own, and with not one
    order against it in the ledger. That row is evidence of nothing, so nothing is being
    hidden; it is a typo or a trial run left lying on somebody's desk. `deskdb` re-checks
    every condition and refuses with the reason, so this flag cannot widen into "delete
    what embarrasses me". Anything that ever placed an order stays, permanently.

    Purging answers `204` and retiring answers the strategy, which is why the response
    model is declared open: they are genuinely different answers to the same verb, and the
    default — no flag — is exactly the contract `agent.md` has always described.
    """
    row = _mine(strategy_id, who["account_id"])

    if purge:
        deleted, why_not = deskdb.delete_registration(who["account_id"], strategy_id)
        if not deleted:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail=f"This strategy cannot be removed: {why_not}.")
        # Audited like every other control-plane act, and more carefully: this is the only
        # call in the API that makes a row stop existing, so the log is the only place it
        # will ever have happened.
        authdb.audit("strategy.purged", who["email"], api_auth.client_ip(request),
                     f"{strategy_id} {row['cls']} {','.join(row['symbols'])} {row['tf']}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    deskdb.set_want(who["account_id"], strategy_id, "retired")
    authdb.audit("strategy.retired", who["email"], api_auth.client_ip(request),
                 strategy_id)
    return _out(_mine(strategy_id, who["account_id"]))
