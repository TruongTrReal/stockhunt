"""`/v1/portfolios` — a basket of strategies with one pot of money and one switch.

The desk's unit used to be a single rule: one $100,000 book per (class, timeframe, rule),
each of them its own row on the board with its own curve. That is the right shape for
*measuring* a rule and the wrong shape for *running* money, because nobody allocates to a
rule — they allocate to a basket, and the only question that matters about a basket is
what the members do to each other. Five rules that each look acceptable alone and are the
same bet in disguise is not visible on five separate rows and is obvious on one.

So a portfolio is:

    one pot of capital        $100,000, split equally across the legs
    rebalanced monthly        back to equal weight, so "five equal bets" stays true
    one toggle                paper-trade it, or don't
    one curve                 the legs blended, against a blended matched benchmark

**A leg is an ordinary book registration.** `kind='book'`, the same row
`/v1/house/strategies` writes when a rule is promoted, carrying a `portfolio_id`. That is
deliberate and it is the whole reason this feature is small: warm-up, subscriptions,
fills, P&L, `desk_control`'s attach and retire, `paper_state`'s publishing and the Alpaca
mirror all keep working with no knowledge that portfolios exist. A parallel registration
type would have meant re-proving every one of them.

**This process still owns no trading.** Creating a portfolio writes rows to
`stockhunt.deskdb`; the desk picks them up on its next control tick, exactly as it does
for a promotion or a member's registration. A `201` here means written down, never live.

**The preview is not the portfolio.** `POST /v1/portfolios/preview` blends legs that do
not exist yet, so somebody can see what a basket would have done before committing to it.
It touches no ledger and needs no admin. That is the "see it combined first" half of the
feature and it is intentionally reachable without creating anything.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

import api_auth
import api_live
import api_paths                                                        # noqa: F401
import authdb
from stockhunt import deskdb, portfolios

log = logging.getLogger("stockhunt.api.portfolios")

router = APIRouter(prefix="/v1/portfolios", tags=["portfolios"])

HOUSE = "00"

# What a portfolio is funded with when the caller does not say. The same $100,000 a
# promoted book gets, so a portfolio's curve and a single book's curve are directly
# comparable — which is the only reason a reader can tell whether combining helped.
DEFAULT_CAPITAL = 100_000.0

# How many legs a follow-portfolio tracks by default.
DEFAULT_TOP_N = 5

# A ceiling on legs, and it is about the DESK rather than about taste. Each leg is a book
# holding a whole asset class, so it costs a subscription per (symbol, timeframe) and a
# warm-up of `DEFAULT_WINDOW_BARS` per name. Books sharing a (symbol, timeframe) share the
# subscription, so the cost is sub-linear in leg count — but it is not free, and a caller
# who asks for two hundred legs should learn it here rather than from a feed that stopped
# answering for everybody.
MAX_LEGS = 25


class Leg(BaseModel):
    cls: str = Field(..., examples=["us_stocks"])
    tf: str = Field(..., examples=["1d"])
    rule: str = Field(..., examples=["ibs"])


class CreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    # 'manual' carries `legs`; 'follow' carries `source_cls`/`source_tf` and tracks that
    # one sheet's top `top_n`, re-checked daily by the desk.
    kind: str = Field("manual", examples=["manual", "follow"])
    legs: list[Leg] = []
    source_cls: str | None = None
    source_tf: str | None = None
    top_n: int = DEFAULT_TOP_N
    capital: float = DEFAULT_CAPITAL
    allow_short: bool = False
    # Where the record starts. Left unset it is now, which is the honest default; it is
    # settable because a portfolio may be given a history deliberately.
    inception: str | None = None


class PreviewRequest(BaseModel):
    """Blend legs that need not exist yet."""
    legs: list[Leg]
    capital: float = DEFAULT_CAPITAL
    rebalance: str = "monthly"
    start: str | None = None


def _principal(who: dict = Depends(api_auth.current_principal)) -> dict:
    return who


def _catalog_or_503() -> dict:
    doc = api_live.catalog()
    if doc is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The catalog has not been built yet. Run `python catalog.py` in "
                   "`paper trading engine/`.")
    return doc


def _check_leg(doc: dict, leg: Leg) -> dict:
    """Refuse a leg here rather than let it sit `pending` and be rejected by the desk.

    Same three refusals `/v1/house/strategies` makes, and for the same reason: a caller
    learns at the click instead of from a registration that goes quiet. The authoritative
    check is still the desk's — these exist to give a fast, useful error.
    """
    key = f"{leg.cls}_{leg.tf}"
    sheet = (doc.get("sheets") or {}).get(key)
    if sheet is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"No walk-forward sheet for {key}. Available: "
                   f"{', '.join(sorted(doc.get('sheets') or {}))}")
    cell = {c["rule"]: c for c in sheet.get("cells", [])}.get(leg.rule)
    if cell is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{leg.rule!r} is not on the {key} leaderboard, or it was collapsed "
                   f"into an equivalent rule under another name.")
    if not cell.get("tradable", True):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{leg.rule!r} is on the board but cannot be traded live: "
                   f"{cell.get('not_tradable_because') or 'no dispatcher can build it'}.")
    return cell


def _benchmarks(doc: dict) -> dict:
    """What each class's legs are scored against, as the DESK declared it.

    Read from the published catalog rather than inferred from a book's holdings: a book
    has no single instrument to hold against, and a baseline that differs from the
    strategy in more than the signal is this repo's most-repeated warning. A class with no
    index ETF carries none, and that is a real answer.
    """
    return dict(((doc.get("book") or {}).get("benchmark") or {}))


def _view(portfolio_id: str) -> dict:
    """A portfolio with its legs.

    `portfolios.get` returns the row alone — legs are a second query, and composing them
    here rather than in the model keeps that module free of any opinion about what a
    reader wants. Every route that answers with a portfolio goes through this, so the
    shape on the wire is one shape.
    """
    row = portfolios.get(portfolio_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such portfolio.")
    row = dict(row)
    row["legs"] = portfolios.legs(portfolio_id)
    # The desk's state, derived from the legs rather than read from the column — nothing
    # writes that column after creation, so a basket read `live -> pending` for its whole
    # life while every one of its legs was running. `portfolios.state_of` is the one
    # definition, shared with `listing`.
    row["state"] = portfolios.state_of(row["legs"])
    return row


def _visible(who: dict, row: dict) -> bool:
    """The house's portfolios are readable by everybody; a member's are their own.

    Same rule `/v1/house/strategies` follows — the house book IS the research made
    visible, and being able to read it is most of why a member would trust the desk.
    Writing is a different question and is checked separately.
    """
    return row["account"] == HOUSE or row["account"] == who["account_id"]


def _own_or_404(portfolio_id: str, who: dict) -> dict:
    """A stranger's id answers 404, never 403.

    A 403 confirms the id exists, which is the one bit an enumeration needs. Same
    doctrine as `/v1/research/jobs`.
    """
    row = portfolios.get(portfolio_id)
    if row is None or not _visible(who, row):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such portfolio.")
    return row


def _writable_or_404(portfolio_id: str, who: dict) -> dict:
    row = _own_or_404(portfolio_id, who)
    # Reading the house's portfolios is open; changing one is the owner's decision, for
    # the same reason promotion is.
    if row["account"] == HOUSE and not who["is_admin"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such portfolio.")
    if row["account"] != HOUSE and row["account"] != who["account_id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such portfolio.")
    return row


@router.get("", summary="Your portfolios, and the house's")
def listing(who: dict = Depends(_principal)) -> list[dict]:
    rows = portfolios.listing()
    return [r for r in rows if _visible(who, r)]


@router.get("/{portfolio_id}", summary="One portfolio and its legs")
def one(portfolio_id: str, who: dict = Depends(_principal)) -> dict:
    _own_or_404(portfolio_id, who)
    return _view(portfolio_id)


@router.post("", status_code=status.HTTP_201_CREATED,
             summary="Create a portfolio")
def create(body: CreateRequest, request: Request,
           who: dict = Depends(_principal)) -> dict:
    """Comes back `pending`, like every other registration on this desk.

    Members create on their own account and the house account is the owner's. There is no
    per-account cap on portfolios by policy; the ceilings that remain
    (`desk_control.MAX_MEMBER_STRATEGIES`, `paper_config.MAX_1M_SYMBOLS`) are machine
    protection and belong to the desk, not to this layer.
    """
    if body.kind not in ("manual", "follow"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="kind must be 'manual' or 'follow'.")
    if body.capital <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="capital must be positive.")

    doc = _catalog_or_503()
    account = HOUSE if who["is_admin"] else who["account_id"]

    if body.kind == "manual":
        if not body.legs:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="A manual portfolio needs at least one leg. To track a "
                       "leaderboard instead, send kind='follow' with source_cls and "
                       "source_tf.")
        if len(body.legs) > MAX_LEGS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"At most {MAX_LEGS} legs; each one is a book holding a whole "
                       f"asset class.")
        seen = {(l.cls, l.tf, l.rule) for l in body.legs}
        if len(seen) != len(body.legs):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                detail="The same leg is listed twice.")
        for leg in body.legs:
            _check_leg(doc, leg)
    else:
        if not (body.source_cls and body.source_tf):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="A follow portfolio needs source_cls and source_tf — the one "
                       "leaderboard it tracks.")
        if body.top_n < 1 or body.top_n > MAX_LEGS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                detail=f"top_n must be between 1 and {MAX_LEGS}.")
        key = f"{body.source_cls}_{body.source_tf}"
        if key not in (doc.get("sheets") or {}):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail=f"No walk-forward sheet for {key}. Available: "
                       f"{', '.join(sorted(doc.get('sheets') or {}))}")

    try:
        row = portfolios.create(
            account, body.name, body.kind,
            capital=float(body.capital),
            source_cls=body.source_cls, source_tf=body.source_tf,
            top_n=body.top_n, inception=body.inception)
    except ValueError as exc:
        # `UNIQUE(account, name)` — a name is how a human refers to a portfolio, so two
        # with one name is a worse outcome than a refusal.
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None

    if body.kind == "manual":
        portfolios.apply_membership(
            row["portfolio_id"],
            [(l.cls, l.tf, l.rule) for l in body.legs],
            reason="created by hand", account=account,
            benchmarks=_benchmarks(doc))

    authdb.audit("portfolio.created", who["email"], api_auth.client_ip(request),
                 f"{row['portfolio_id']} kind={body.kind} legs={len(body.legs)}")
    return _view(row["portfolio_id"])


@router.post("/{portfolio_id}/pause", summary="Stop trading it, keep the positions")
def pause(portfolio_id: str, request: Request,
          who: dict = Depends(_principal)) -> dict:
    """THE TOGGLE, off.

    Sets `want` on the portfolio and on every one of its legs in one transaction. The
    desk's `state` is untouched — only the desk writes that, and the two genuinely
    disagree for a while: pressing pause while the desk is down leaves want='paused' and
    state='live' until the next tick, which is the truth rather than a bug to paper over.
    """
    row = _writable_or_404(portfolio_id, who)
    portfolios.set_want(portfolio_id, "paused", account=row["account"])
    authdb.audit("portfolio.paused", who["email"], api_auth.client_ip(request),
                 portfolio_id)
    return _view(portfolio_id)


@router.post("/{portfolio_id}/resume", summary="Trade it again")
def resume(portfolio_id: str, request: Request,
           who: dict = Depends(_principal)) -> dict:
    row = _writable_or_404(portfolio_id, who)
    portfolios.set_want(portfolio_id, "live", account=row["account"])
    authdb.audit("portfolio.resumed", who["email"], api_auth.client_ip(request),
                 portfolio_id)
    return _view(portfolio_id)


@router.delete("/{portfolio_id}", summary="Retire a portfolio")
def retire(portfolio_id: str, request: Request,
           who: dict = Depends(_principal)) -> dict:
    """Retire, never delete.

    Every leg is retired with it and every record is kept, for the reason
    `/v1/strategies` refuses a delete: a forward test somebody can erase is not a record,
    and a manager who can remove a losing run can remove the evidence of it.
    """
    row = _writable_or_404(portfolio_id, who)
    portfolios.set_want(portfolio_id, "retired", account=row["account"])
    authdb.audit("portfolio.retired", who["email"], api_auth.client_ip(request),
                 portfolio_id)
    return _view(portfolio_id)


@router.post("/{portfolio_id}/legs", status_code=status.HTTP_201_CREATED,
             summary="Add a rule to an existing basket")
def add_legs(portfolio_id: str, body: list[Leg], request: Request,
             who: dict = Depends(_principal)) -> dict:
    """Put more rules into a basket that already exists.

    The money is re-split across everything the portfolio then holds, so adding a fifth
    leg to four takes each of the first four from a quarter of the pot to a fifth. That
    is what "one pot" means, and it is the reason this is a route on the portfolio rather
    than a second promotion: promoting adds a book with its own $100,000 beside the
    basket, which is a different thing that looks the same on a list.

    **Refused on a `follow` portfolio.** Its membership is the leaderboard's answer,
    re-applied nightly, so a hand-added leg would be silently retired on the next
    reconcile — a change the caller made, the desk undid, and nothing announced.
    """
    row = _writable_or_404(portfolio_id, who)
    if row["kind"] == "follow":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="This portfolio follows a leaderboard, so its legs are decided by the "
                   "sheet and re-applied every day — a leg added by hand would be retired "
                   "on the next pass. Create a manual portfolio to choose rules yourself.")
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Send at least one leg.")

    doc = _catalog_or_503()
    for leg in body:
        _check_leg(doc, leg)

    held = [(l["cls"], l["tf"], l["rule"]) for l in portfolios.legs(portfolio_id)
            if l.get("rule")]
    target = list(held)
    for leg in body:
        cell = (leg.cls, leg.tf, leg.rule)
        if cell not in target:
            target.append(cell)
    if len(target) > MAX_LEGS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"That would take the basket past {MAX_LEGS} legs; each one is a book "
                   f"holding a whole asset class.")

    # Through the same diff the nightly reconcile uses, rather than a second add path.
    # One implementation of "make the basket hold exactly this" is what keeps the money
    # split and the change log correct no matter who asked.
    portfolios.apply_membership(portfolio_id, target, reason="added by hand",
                                account=row["account"], benchmarks=_benchmarks(doc))
    authdb.audit("portfolio.legs_added", who["email"], api_auth.client_ip(request),
                 f"{portfolio_id} +{len(body)}")
    return _view(portfolio_id)


@router.delete("/{portfolio_id}/legs/{strategy_id}",
               summary="Drop one rule from a basket")
def drop_leg(portfolio_id: str, strategy_id: str, request: Request,
             who: dict = Depends(_principal)) -> dict:
    """Retire one leg and re-split the money across the rest.

    The leg is retired, never deleted — it may have traded, and then it is a record. It
    keeps its `portfolio_id` forever, so what the basket held in March is still
    answerable in June.
    """
    row = _writable_or_404(portfolio_id, who)
    if row["kind"] == "follow":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="This portfolio follows a leaderboard; a leg dropped by hand would be "
                   "bought back on the next pass.")
    if not portfolios.remove_leg(portfolio_id, strategy_id, "dropped by hand",
                                 account=row["account"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail="That leg is not in this portfolio.")
    authdb.audit("portfolio.leg_dropped", who["email"], api_auth.client_ip(request),
                 f"{portfolio_id} -{strategy_id}")
    return _view(portfolio_id)


@router.get("/{portfolio_id}/changes", summary="When the basket changed, and why")
def changes(portfolio_id: str, who: dict = Depends(_principal)) -> list[dict]:
    """The membership log.

    A follow-portfolio's holdings move under it as the leaderboard moves, so its curve is
    the record of several different baskets. Without this the reader cannot tell a rule
    that earned its place from one that arrived last week.
    """
    _own_or_404(portfolio_id, who)
    return portfolios.changes(portfolio_id)


@router.get("/{portfolio_id}/backtest", summary="What the legs did together")
def backtest(portfolio_id: str, start: str | None = None,
             who: dict = Depends(_principal)) -> dict:
    """The combined curve, blended from the legs' own book curves.

    **Days of paper fills and years of walk-forward are different measurements**, and this
    is the second one: it is what the basket WOULD have done over the research history,
    not what it has done since the desk picked it up. The live record is on `/live.json`
    and the two are never added together.
    """
    _own_or_404(portfolio_id, who)
    row = _view(portfolio_id)
    legs = [(l["cls"], l["tf"], l["rule"]) for l in row["legs"] if l.get("rule")]
    if not legs:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="This portfolio holds no legs yet, so there is "
                                   "nothing to combine.")
    return _blend(legs, float(row.get("capital") or DEFAULT_CAPITAL),
                  row.get("rebalance") or "monthly",
                  start or row.get("inception"))


@router.post("/preview", summary="Blend legs without creating anything")
def preview(body: PreviewRequest, who: dict = Depends(_principal)) -> dict:
    """See the basket before committing to it.

    Writes nothing and needs no admin: choosing what to put in a portfolio is exactly the
    moment somebody needs to know whether the picks are one bet, and making them create it
    first to find out would be the wrong order.
    """
    if not body.legs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail="Send at least one leg to blend.")
    if len(body.legs) > MAX_LEGS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail=f"At most {MAX_LEGS} legs.")
    return _blend([(l.cls, l.tf, l.rule) for l in body.legs],
                  float(body.capital), body.rebalance, body.start)


def _blend(legs: list[tuple], capital: float, rebalance: str,
           start: str | None) -> dict:
    """The one call into the blend engine.

    Imported lazily because it pulls pandas and numpy, and this process is expected to
    start and answer `/healthz` on a box where the heavy stack is missing or broken —
    the property `api_paths` exists to protect. A reader who never opens a portfolio
    never pays for it.
    """
    from stockhunt import blend
    try:
        return blend.blend(legs, capital=capital, rebalance=rebalance, start=start)
    except FileNotFoundError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No book curves for one of these legs yet ({exc}). Run "
                   f"`run_book.sh` for that sheet in `walk-forward optimization/`."
        ) from None
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"{exc} is not on its sheet's book run, so it has no curve to "
                   f"combine.") from None
    except ValueError as exc:
        # The legs do not overlap in time at all, or one is shorter than `start`. A real
        # answer with a caveat is impossible here; the honest reply is the reason.
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
