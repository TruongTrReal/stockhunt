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
import api_symbols
import api_webhook
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
#
# `cme_futures` is the odd one and `api_symbols` is why: it is the only class whose
# symbols are not spelled in capitals (`ES.v.0`, not `ES`), so naming it here without
# fixing the fold would have produced registrations the desk cannot match.
CLASSES = ("us_stocks", "us_etfs", "crypto", "commodities", "cme_futures")

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
    capital: float | None = Field(
        None, examples=[10000],
        description="What the book is funded with. Defaults to the desk's standard "
                    "book; ask for more when one unit of what you trade costs more than "
                    "that. On cme_futures a unit is a fractional notional unit of a "
                    "back-adjusted series, so one NQ.v.0 is ~$29,600.")

    @field_validator("capital")
    @classmethod
    def _capital(cls, v: float | None) -> float | None:
        """Bounded at both ends, and the two bounds exist for different reasons.

        The FLOOR is the rounding argument that made this fixed in the first place: a book
        smaller than the standard one rounds a whole share into a position that is a
        decision rather than a rounding. The CEILING is that this is a shared sandbox
        venue — `run_paper` funds each venue from the sum of what is registered on it, so
        an unbounded book is a way to make every other book on that venue meaningless.
        """
        if v is None:
            return None
        if v < api_config.CAPITAL_PER_STRATEGY:
            raise ValueError(
                f"the smallest book is ${api_config.CAPITAL_PER_STRATEGY:,.0f} — below "
                f"that, rounding a whole share stops being a rounding")
        if v > api_config.MAX_CAPITAL_PER_STRATEGY:
            raise ValueError(
                f"the largest is ${api_config.MAX_CAPITAL_PER_STRATEGY:,.0f}; this is a "
                f"shared sandbox venue and every book on it is funded from the same total")
        return float(v)

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
        # `api_symbols.canonical`, not `.upper()`. A plain fold to capitals is right for
        # every class but `cme_futures`, where it rewrites `ES.v.0` — the desk's own
        # spelling, offered by the console out of the desk's own published universe — as
        # `ES.V.0` and registers a symbol no leg holds.
        out = [api_symbols.canonical(s) for s in v if s and s.strip()]
        if not out:
            raise ValueError("name at least one symbol")
        # Compared after the fold, so `ES.v.0` and `ES.V.0` in one request are caught as
        # the duplicate they are rather than registered as two names for one contract.
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
    max_capital_per_strategy: float
    max_strategies: int
    max_orders_per_minute: int
    # Per class, the symbols the desk already holds — empty when the desk has not
    # published a catalog yet. The console offers these as suggestions.
    universe: dict[str, list[str]]
    # Whether the desk will take a symbol the list above does NOT hold. Read from the
    # desk's own published catalog, never assumed here, and defaulting to False: this
    # process cannot import the trading stack to ask, so the only honest default is the
    # old behaviour. A console that assumed True against a desk that still refuses would
    # accept a symbol here and have it rejected there, minutes later, in a `reason` on a
    # table nobody is still watching — which is the exact failure the picker exists to
    # prevent, inverted.
    universe_open: bool = False
    # How many off-list symbols the desk will carry at once, and how many are in use. It
    # is a FEED budget — each one is a vendor request per bar — so the console can say why
    # a refusal happened rather than only that one did. Zero when the desk has published
    # no catalog, which reads the same as `universe_open` being false.
    open_symbols_max: int = 0
    open_symbols_in_use: int = 0


@limits_router.get("/limits", response_model=LimitsOut,
                   summary="What a registration costs you, and what it may not exceed")
def limits(who: dict = Depends(api_auth.current_principal)) -> LimitsOut:
    """Every number the console shows a manager *before* they register.

    The console states these terms up front — capital, the class list, the order cap —
    and every one of them is settable from the environment. Written into the page instead,
    they would go on reading `$10,000` and offering four classes on the day somebody
    changed `API_CAPITAL_PER_STRATEGY` or added a fifth, and a page that misstates the
    terms is worse than one that omits them.

    This paragraph used to name those four in prose, and `cme_futures` made it wrong — the
    drift it was warning about, in the sentence warning about it. So the count stays and
    the list does not: `CLASSES` is the one place they are written down.

    Authenticated like the rest of `/v1`, though it carries no account data: the whole API
    is behind the allowlist and there is no reason for this to be the one door left open.

    The universe is read from what the desk PUBLISHED, never computed here — same contract
    as `live.json`. An empty one means the desk has not run `catalog.py` yet, and the
    console falls back to accepting a typed symbol rather than refusing to register at all:
    a research artifact that has not been rebuilt must not be able to close the door.

    `universe_open` comes from the same document and says whether the desk will resolve a
    symbol that is NOT on the list. It is read rather than inferred from the universe being
    non-empty, because those are different facts: a full list and a closed door was the
    desk's behaviour until 2026-08-28, and a console that conflated them would offer a
    typed symbol to a desk that still refuses it.
    """
    doc = api_live.catalog() or {}
    universe = (doc.get("universe") or {})
    open_syms = (doc.get("open_symbols") or {})
    return LimitsOut(
        universe={k: list(v) for k, v in universe.items() if k in CLASSES},
        universe_open=bool(open_syms.get("enabled")),
        open_symbols_max=int(open_syms.get("max") or 0),
        open_symbols_in_use=int(open_syms.get("in_use") or 0),
        classes=list(CLASSES),
        timeframes=list(api_config.TIMEFRAMES),
        name_max=NAME_MAX,
        symbols_max=SYMBOLS_MAX,
        capital_per_strategy=api_config.CAPITAL_PER_STRATEGY,
        max_capital_per_strategy=api_config.MAX_CAPITAL_PER_STRATEGY,
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
        body.capital or api_config.CAPITAL_PER_STRATEGY, kind="member",
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


# ------------------------------------------------------------------ the webhook secret
#
# Minted behind the browser login and never by a key or by the webhook itself — the same
# containment `/auth/keys` has, for a sharper reason here. This credential travels in a
# request body and lives in plain text inside a TradingView alert, so it is the one most
# likely to leak; if it could mint its own replacement, revoking it would be a race rather
# than an ending.
#
# It is scoped to ONE strategy and can do exactly one thing: submit an order for it. That
# is the whole argument for its existing at all — the alternative is `sk_live_…`, which
# trades every strategy on the account, reads the book and retires registrations, pasted
# into a field TradingView shows on screen.


class WebhookInfo(BaseModel):
    """The live secret's metadata. Never the secret."""
    strategy_id: str
    url: str
    exists: bool
    prefix: str | None = None
    created_at: str | None = None
    # Whether TradingView has ever actually called with it. The console shows this because
    # "I set up the alert" and "the alert fires" are different claims, and only one of them
    # is checkable from here.
    last_used_at: str | None = None


class NewWebhookOut(WebhookInfo):
    """The one response that carries the secret. It is never retrievable again."""
    secret: str
    alert: dict
    warning: str = ("Store this now — it is not saved anywhere and cannot be shown "
                    "again. Anyone who has it can place orders for this one strategy.")


def _webhook_url(request: Request) -> str:
    """Where the alert posts. Read from the route itself, and substituted per request for
    the reason `agent.md`'s `{{BASE}}` is: this string goes straight into a TradingView
    alert, and behind the tunnel the scheme is knowable only from the forwarded header."""
    return api_auth.public_base_url(request) + api_webhook.PATH


def _alert_body(strategy_id: str, secret: str, symbols: list[str]) -> dict:
    """The JSON to paste into TradingView's Message box, ready to work.

    `{{…}}` placeholders are what TradingView substitutes at fire time. Three of them are
    load-bearing and the console says so:

    * `{{strategy.order.action}}` — buy or sell. Only a STRATEGY alert can fill this in.
    * `{{strategy.order.contracts}}` — the size. The desk does not size for you.
    * `{{time}}` — the BAR's timestamp, which is what makes a duplicate alert on one bar
      idempotent. `{{timenow}}` is the moment the alert fired and differs between two
      copies of the same signal, so it would defeat exactly the protection it looks like.

    `ticker` is included whenever the strategy holds more than one symbol, and omitted
    when it holds one — a field that can only ever be right is noise, and TradingView's
    chart ticker will not match the desk's spelling on every venue anyway.
    """
    body = {
        "strategyId": strategy_id,
        "secret": secret,
        "action": "{{strategy.order.action}}",
        "qty": "{{strategy.order.contracts}}",
        "bar_time": "{{time}}",
    }
    if len(symbols) > 1:
        body["ticker"] = "{{ticker}}"
    return body


@router.post("/{strategy_id}/webhook", response_model=NewWebhookOut,
             status_code=status.HTTP_201_CREATED,
             summary="Mint (or rotate) this strategy's TradingView webhook secret")
def mint_webhook(strategy_id: str, request: Request,
                 session: dict = Depends(api_auth.current_session)) -> NewWebhookOut:
    """Turn the alert on, or replace the secret behind it.

    Minting a second time revokes the first, so there is never more than one live secret
    per strategy — rotating after a leak is one click and takes effect on the next alert,
    rather than leaving a list somebody has to remember to prune.
    """
    account = session["account_id"]
    row = _mine(strategy_id, account)
    if row["kind"] != "member":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="This strategy trades a rule the desk selected; it does not take "
                   "orders, so a webhook would have nothing to send.")

    raw, secret = authdb.create_webhook_secret(account, strategy_id)
    authdb.audit("webhook.minted", session["email"], api_auth.client_ip(request),
                 strategy_id)
    return NewWebhookOut(
        strategy_id=strategy_id, url=_webhook_url(request), exists=True,
        prefix=secret["prefix"], created_at=secret["created_at"], secret=raw,
        alert=_alert_body(strategy_id, raw, list(row["symbols"])))


@router.get("/{strategy_id}/webhook", response_model=WebhookInfo,
            summary="Whether this strategy has a live webhook, and if it has been used")
def get_webhook(strategy_id: str, request: Request,
                who: dict = Depends(api_auth.current_principal)) -> WebhookInfo:
    account = who["account_id"]
    _mine(strategy_id, account)
    secret = authdb.webhook_secret_for(account, strategy_id)
    return WebhookInfo(
        strategy_id=strategy_id, url=_webhook_url(request), exists=secret is not None,
        prefix=(secret or {}).get("prefix"),
        created_at=(secret or {}).get("created_at"),
        last_used_at=(secret or {}).get("last_used_at"))


@router.delete("/{strategy_id}/webhook", status_code=status.HTTP_204_NO_CONTENT,
               summary="Revoke it. The alert stops working on its next fire.")
def revoke_webhook(strategy_id: str, request: Request,
                   session: dict = Depends(api_auth.current_session)) -> Response:
    account = session["account_id"]
    _mine(strategy_id, account)
    if not authdb.revoke_webhook_secret(account, strategy_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail="This strategy has no live webhook secret.")
    authdb.audit("webhook.revoked", session["email"], api_auth.client_ip(request),
                 strategy_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
