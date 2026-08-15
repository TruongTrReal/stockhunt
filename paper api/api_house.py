"""`/v1/house` — promoting a rule from the research onto the live desk.

This is the owner's side of the platform: the walk-forward sheets say which rules were
ranked, and this turns a row on one of them into a system trading paper money. Members
read the result; they never promote anything.

**It is the same machinery a manager's registration uses.** A promotion writes a row to
`stockhunt.deskdb` with `kind='house_rule'` and the account `00`, and `desk_control`
starts it exactly as it starts a member's. One code path, one lifecycle, one record shape
— so the house desk cannot quietly drift away from the thing everybody else is using.

**The menu is generated, and it is validated against.** `catalog.json` is written by
`paper trading engine/catalog.py` from the same sheets and the same filters
`paper_config.top_rules` applies, so a rule that cannot be traded live cannot be offered
here. Validating against the published file also means this process needs no pandas and no
trading imports to say "no".
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

import api_auth
import api_config
import api_live
import api_paths                                                        # noqa: F401
import authdb
from stockhunt import deskdb

log = logging.getLogger("stockhunt.api.house")

router = APIRouter(prefix="/v1/house", tags=["house"])

HOUSE = "00"


def _admin(who: dict = Depends(api_auth.current_principal)) -> dict:
    """Promotion is the owner's decision, not a member's.

    A member cannot put anything on the house desk: the house book is the research made
    visible, and a member adding to it would make it something else.
    """
    if not who["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            detail="Only the desk owner can promote a rule.")
    return who


class PromoteRequest(BaseModel):
    cls: str = Field(..., examples=["us_stocks"])
    tf: str = Field(..., examples=["1d"])
    rule: str = Field(..., examples=["SMA_200"])
    allow_short: bool = False
    # No `symbol`. A promotion is a BOOK: one $100,000 account holding the whole class,
    # which is the shape the research scored — `ir_net` is a mean across the class, so a
    # single-symbol forward test measures something the backtest never reported.
    #
    # The roster is deliberately NOT stored on the registration either. `us_stocks` is the
    # point-in-time top 100 and moves under the book; freezing a list here would turn a
    # live index into a snapshot taken on the day somebody clicked.


class PromotedOut(BaseModel):
    strategy_id: str
    name: str
    kind: str
    cls: str
    tf: str
    rule: str | None
    symbols: list[str]
    capital: float
    benchmark: str | None
    want: str
    state: str
    reason: str | None = None
    created_at: str
    # Carried back so a caller who promoted from a stale sheet learns it here rather than
    # from a number on a dashboard weeks later.
    sheet_is_stale: bool = False


@router.get("/catalog", summary="Rules that can be promoted, from the research")
def catalog(who: dict = Depends(api_auth.current_principal)) -> dict:
    """The menu, exactly as the desk published it.

    Readable by any member, deliberately: it IS the research, and being able to see what
    the desk concluded is most of why a manager would trust it. Promoting is the part that
    is restricted.
    """
    doc = api_live.catalog()
    if doc is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The catalog has not been built yet. Run `python catalog.py` in "
                   "`paper trading engine/`.")
    return doc


@router.get("/strategies", summary="What the house desk is running")
def house_strategies(who: dict = Depends(api_auth.current_principal)) -> list[dict]:
    """Visible to everyone. The house book is the research made visible."""
    return [dict(r) for r in deskdb.registrations(HOUSE)]


@router.post("/strategies", response_model=PromotedOut,
             status_code=status.HTTP_201_CREATED,
             summary="Promote a backtested rule to the live paper desk")
def promote(body: PromoteRequest, request: Request,
            who: dict = Depends(_admin)) -> PromotedOut:
    """Put one (symbol, timeframe, rule) cell on the desk.

    Comes back `pending`, like every other registration: this process owns no trading, and
    the desk applies it on its next tick.
    """
    doc = api_live.catalog()
    if doc is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The catalog has not been built yet. Run `python catalog.py` in "
                   "`paper trading engine/`.")

    key = f"{body.cls}_{body.tf}"
    sheet = (doc.get("sheets") or {}).get(key)
    if sheet is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"No walk-forward sheet for {key}. Available: "
                   f"{', '.join(sorted(doc.get('sheets') or {}))}")

    cells = {c["rule"]: c for c in sheet.get("cells", [])}
    cell = cells.get(body.rule)
    if cell is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{body.rule!r} is not on the {key} leaderboard, or it was collapsed "
                   f"into an equivalent rule under another name. The catalog lists what "
                   f"can be promoted, in the order the board ranks it.")
    # The board merges families that are not equally runnable live — a combo has no single
    # definition a strategy can hold. The catalog marks each row; refusing here means a
    # manager learns at the click rather than from a registration that sits `pending` and
    # is then rejected by the desk minutes later.
    if not cell.get("tradable", True):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{body.rule!r} is on the board but cannot be traded live: "
                   f"{cell.get('not_tradable_because') or 'no dispatcher can build it'}.")

    book = doc.get("book") or {}
    if body.tf not in (book.get("timeframes") or ["1d"]):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Books run at {', '.join(book.get('timeframes') or ['1d'])} only for "
                   f"now. Daily first — one accounting model live at a time.")

    n = (book.get("names") or {}).get(body.cls)
    if not n:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"No names are live in {body.cls} right now, so there is nothing for a "
                   f"book to hold.")

    # Named for what it is, not for a symbol it does not have.
    name = f"{body.cls}-{body.tf}-{body.rule.lower()}"
    row = deskdb.register(
        HOUSE, name, body.cls, [], body.tf,
        float(book.get("capital") or 100_000.0),
        kind="book", rule=body.rule,
        # A book has no single instrument to hold against, so the baseline is declared per
        # class rather than inferred: the class's own index ETF where there is one, and
        # nothing where there is not. Never guessed from the holdings.
        benchmark=(book.get("benchmark") or {}).get(body.cls),
        allow_short=body.allow_short)

    stale = key in (doc.get("health") or {}).get("stale_sheets", [])
    authdb.audit("house.promoted", who["email"], api_auth.client_ip(request),
                 f"{row['strategy_id']} rule={body.rule} stale_sheet={stale}")
    return PromotedOut(sheet_is_stale=stale, **{
        k: row[k] for k in ("strategy_id", "name", "kind", "cls", "tf", "rule",
                            "symbols", "capital", "benchmark", "want", "state",
                            "reason", "created_at")})


@router.delete("/strategies/{strategy_id}", summary="Retire a promoted rule")
def retire(strategy_id: str, request: Request,
           who: dict = Depends(_admin)) -> dict:
    if deskdb.registration(strategy_id, account=HOUSE) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such house strategy.")
    deskdb.set_want(HOUSE, strategy_id, "retired")
    authdb.audit("house.retired", who["email"], api_auth.client_ip(request), strategy_id)
    return dict(deskdb.registration(strategy_id, account=HOUSE))     # type: ignore[arg-type]
