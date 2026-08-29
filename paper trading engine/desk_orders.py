"""What the desk will and will not do with an order that arrived over the API.

Deliberately free of `nautilus_trader`: everything here is a pure function over plain
dicts, so the rules that decide whether somebody's money moves can be tested exhaustively
in milliseconds without building a trading node. `desk_control.py` is the part that wires
these decisions to the engine.

**These checks are the binding ones.** The API runs its own, earlier and cheaper, so a
caller gets a useful error in the response instead of minutes later. But the API cannot
see the book — it does not hold the cash balance or the open positions — so anything that
depends on those is decided here, and everything arriving from the API is treated as
untrusted input whatever the API already said about it.

Two things are worth reading before changing anything:

**Staleness is time-in-force applied to the queue.** An order that waited while the desk
was down and then filled at a price the manager never saw is worse than one rejected
outright: a rejection can be retried, a fill cannot be undone. The window is one bar of
the strategy's own timeframe, because the bar is the unit its signal was computed on — a
two-minute restart therefore rejects nothing at all, and only a real outage does.

**Leverage is a registered setting, and 1.0 is exactly the old rule.** Until 2026-08-29 a
buy that would take cash below zero was refused and the sentence ended *"there is no
margin on this desk"*. It is now one inequality with a member-chosen ceiling on it:

    gross exposure after the order   <=   leverage x equity after the order

`gross` is the sum of |units| x price over every name the book holds; `equity` is cash
plus the mark of what it holds, which is the same definition `MemberStrategy.equity()`
publishes. Read `headroom` for why that inequality is the OLD one, arithmetic for
arithmetic, when `leverage` is 1 and the book is long-only.

Three things about it are load-bearing:

* **The ceiling is measured against EQUITY, not against capital.** Against capital it
  would cap a book at the money it started with, so a profitable book could never deploy
  its own gains and "leverage 1" would silently mean "no compounding". Against equity it
  also shrinks as the book loses, which is what makes the zero-equity case below fall out
  of the arithmetic instead of needing a rule of its own.
* **An order that strictly REDUCES gross exposure is always allowed**, even from a book
  already outside its limit. Without that, a short that has run against its owner refuses
  the buy that would close it — the desk would trap somebody in the position it is trying
  to bound. De-risking is never refused.
* **At zero or negative equity the ceiling is zero or negative**, so every order that
  would add exposure is refused and only closing orders get through. That is the whole
  behaviour at a blown-up book, and it is arithmetic rather than a special case.
  `desk_control._watch_equity` is what makes it VISIBLE, on the registration row, rather
  than leaving the owner to infer it from refusals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# One bar of each timeframe, PARSED rather than tabulated.
#
# It was `{"1d": 86_400, "4h": 14_400}` with a comment saying the desk trades those two
# and "anything else... cannot be registered in the first place". That stopped being true
# the moment `MEMBER_TIMEFRAMES` grew, and the failure was silent in the worst direction:
# `stale_window` read `BAR_SECONDS.get(tf, 86_400)`, so **every intraday timeframe the
# desk gained — 2h, 1h, 15m, 5m, 1m — silently got a ONE DAY staleness window.** A 1m
# order that waited fourteen hours was still "fresh", which is the exact opposite of what
# this guard exists to do, and nothing anywhere said so.
#
# A table of timeframes is a third list to keep in step with `paper_config.TIMEFRAMES` and
# `td_live.INTERVALS`, and this module deliberately imports neither — it is the checks
# that BIND, and they are worth being testable in milliseconds without the trading stack.
# So the length is derived from the name, which cannot drift because it is not a copy.
_UNIT_SECONDS = {"m": 60, "h": 3_600, "d": 86_400}


def bar_seconds(tf: str) -> int:
    """`"15m"` -> 900. Falls back to a day only for a name that is not a timeframe."""
    try:
        return int(tf[:-1]) * _UNIT_SECONDS[tf[-1]]
    except (ValueError, KeyError, IndexError):
        return 86_400

# Multiplier on that bar. 1.0 means "an order may not outlive the bar it was computed on".
# Raising it buys tolerance for a slow desk at the cost of filling staler decisions.
STALE_BARS = 1.0

SIDES = ("buy", "sell")
TYPES = ("market", "limit")

# No leverage. The value every registration written before the column existed reads as, and
# the default on every one written since — see `deskdb._add_late_columns`.
NO_LEVERAGE = 1.0


def leverage_of(registration: dict) -> float:
    """How far this book may lever. Anything absent, NULL or malformed is 1.0.

    Read off the ROW rather than off a constant with a fallback elsewhere, because there
    are two ways to be unlevered — a registration written before the column existed, and
    one written since with the default — and they have to mean the identical thing. They
    do: both arrive here as 1.0 and `headroom` then reduces to the cash rule.

    Clamped UP to 1.0 and never down to a ceiling. A ceiling is per class and lives in
    `paper_config.MAX_LEVERAGE`, enforced at attach by `desk_control._launch` where a
    refusal reaches the registration's owner; enforcing it again here would silently trade
    a book on terms other than the ones its row records, which is worse than refusing it.
    """
    try:
        lev = float(registration.get("leverage") or NO_LEVERAGE)
    except (TypeError, ValueError):
        return NO_LEVERAGE
    if lev != lev or lev < NO_LEVERAGE:            # NaN, or below 1: no leverage
        return NO_LEVERAGE
    return lev


def sides(units: dict, prices: dict) -> tuple[float, float]:
    """`(long_notional, short_notional)`, both positive, marked at `prices`.

    A held name with no price contributes nothing. That understates a SHORT, which is the
    permissive direction — but a position exists only because a fill created it, and
    `MemberStrategy.on_order_filled` sets `_last_price` from the fill itself, so a held
    name without a mark is not a state this desk can reach. It is written this way rather
    than raising because a missing price must not turn into an exception inside `tick()`.
    """
    long_n = short_n = 0.0
    for symbol, held in units.items():
        price = prices.get(symbol)
        if not price or not held:
            continue
        notional = abs(float(held)) * float(price)
        if held > 0:
            long_n += notional
        else:
            short_n += notional
    return long_n, short_n


def headroom(book: dict, prices: dict, leverage: float = NO_LEVERAGE) -> float:
    """How much room this book has left under its own leverage ceiling. Negative is over.

    It is `leverage * equity - gross` with the algebra already done:

        L*(cash + long - short) - (long + short)  ==  L*cash + (L-1)*long - (L+1)*short

    **The rearrangement is the whole reason `leverage = 1` is bit-for-bit the old rule.**
    Evaluated in the first form on a long-only book at L = 1, the answer is
    `(cash + long) - long`, which is `cash` in algebra and is NOT `cash` in floating point:
    a $10,000 cash balance beside a $10,000,000 position rounds away entirely, and a buy
    the desk has always accepted would start being refused. In the second form the same
    case is `1.0*cash + 0.0*long - 2.0*0.0`, which is `cash` exactly — the identical
    double the old `cost > cash` comparison was made against.

    So `headroom(after) < 0` at L = 1 on a long-only book is exactly `cost > cash`, for
    every input, and not merely for the ones a test happened to try.
    """
    long_n, short_n = sides(book.get("units") or {}, prices)
    cash = float(book.get("cash", 0.0))
    return (leverage * cash
            + (leverage - 1.0) * long_n
            - (leverage + 1.0) * short_n)


def gross(book: dict, prices: dict) -> float:
    """Total notional held, long plus short. What the leverage ceiling is a ceiling on."""
    long_n, short_n = sides(book.get("units") or {}, prices)
    return long_n + short_n


def equity(book: dict, prices: dict) -> float:
    """Cash plus the mark of what is held.

    The same definition `MemberStrategy.equity()` publishes and the same one
    `paper_pnl_pct` is computed from, deliberately: a book refused an order for want of
    equity and a book reporting its equity on the board must not be two different numbers.
    """
    long_n, short_n = sides(book.get("units") or {}, prices)
    return float(book.get("cash", 0.0)) + long_n - short_n


def stale_window(tf: str, stale_bars: float = STALE_BARS) -> timedelta:
    return timedelta(seconds=bar_seconds(tf) * stale_bars)


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_stale(order: dict, tf: str, now: datetime | None = None,
             stale_bars: float = STALE_BARS) -> bool:
    """Has this order outlived the bar its signal was computed on?"""
    now = now or datetime.now(timezone.utc)
    return (now - _parse(order["submitted_at"])) > stale_window(tf, stale_bars)


def validate(order: dict, registration: dict, book: dict,
             price: float | None, now: datetime | None = None,
             stale_bars: float = STALE_BARS,
             prices: dict | None = None) -> tuple[bool, str]:
    """`(ok, reason)`. `reason` is written into the ledger and shown to the manager, so it
    is phrased for somebody debugging their own bot at 2am, not for a log.

    `book` is this strategy's own: `{"cash": float, "units": {symbol: float}}`. Its own,
    and never the venue account — Nautilus nets every strategy on an instrument into one
    position, so the shared figure answers "what does the whole desk hold in SPY", which
    is a different question and would let one manager's trade size another's.

    `price` is the mark for the symbol being traded and `prices` is every mark the desk
    has. The second is needed because a leverage ceiling is a statement about the WHOLE
    book — one order's affordability cannot be decided from one name's price — and it is
    optional because at leverage 1 on a long-only book the other names cancel out of the
    arithmetic entirely (see `headroom`). Omitting it there costs nothing; omitting it on
    a book carrying shorts understates them, which is why `partition` always passes it.
    """
    if registration["state"] == "rejected":
        return False, "this strategy was rejected and is not running"
    if registration["state"] == "retired" or registration["want"] == "retired":
        return False, "this strategy has been retired"
    if registration["want"] == "paused":
        return False, "this strategy is paused; resume it before sending orders"

    if is_stale(order, registration["tf"], now, stale_bars):
        window = stale_window(registration["tf"], stale_bars)
        return False, (f"rejected as stale: it waited longer than one {registration['tf']} "
                       f"bar ({int(window.total_seconds() // 60)} minutes) before the desk "
                       f"could act on it. Check the price and send a fresh order.")

    if order["action"] == "cancel":
        return True, ""

    symbol = order.get("symbol")
    if symbol not in registration["symbols"]:
        return False, (f"{symbol!r} is not one of this strategy's symbols "
                       f"({', '.join(registration['symbols'])}). Register it first.")

    side = (order.get("side") or "").lower()
    if side not in SIDES:
        return False, f"side must be one of {SIDES}, got {order.get('side')!r}"

    otype = (order.get("order_type") or "").lower()
    if otype not in TYPES:
        return False, f"type must be one of {TYPES}, got {order.get('order_type')!r}"

    qty = order.get("qty")
    if qty is None or float(qty) <= 0:
        return False, "qty must be greater than zero"
    qty = float(qty)

    if otype == "limit":
        limit = order.get("limit_price")
        if limit is None or float(limit) <= 0:
            return False, "a limit order needs a limit_price greater than zero"

    if price is None or price <= 0:
        return False, (f"no price for {symbol} yet — the desk has not received a bar for "
                       f"it since starting. Try again after the next {registration['tf']} "
                       f"close.")

    # The price the check is made against: a limit order can only ever fill at its limit
    # or better, so that is what it can cost. A market order is checked at the last close,
    # which is an ESTIMATE and is the one number here that can turn out wrong — the fill
    # arrives at the next bar's price, so a book validated to the edge of its ceiling can
    # cross it by whatever the market moved in between. That is bounded by a bar rather
    # than by this function, and it is the same exposure the old cash rule carried.
    cost_price = float(order["limit_price"]) if otype == "limit" else price
    held = float(book.get("units", {}).get(symbol, 0.0))

    # Checked before the exposure arithmetic, and it is not an ordering of convenience:
    # long/flat is a statement about DIRECTION and leverage is a statement about SIZE, so a
    # long/flat book asking to go short must be told which of the two it broke. Refusing it
    # for want of exposure would be true and useless.
    if side == "sell" and not registration.get("allow_short") and qty > held + 1e-9:
        return False, (f"cannot sell {qty:g} {symbol}: this strategy holds {held:g} "
                       f"and was not registered with allow_short.")

    # The marks the whole book is valued at. The traded symbol is pinned to the price this
    # order will actually cost — its limit, or the last close for a market order — so the
    # order is checked against what it does rather than against what the tape last said.
    marks = dict(prices or {})
    marks[symbol] = cost_price

    lev = leverage_of(registration)
    signed = qty if side == "buy" else -qty
    after = {"cash": float(book.get("cash", 0.0)) - signed * cost_price,
             "units": {**(book.get("units") or {}), symbol: held + signed}}

    # A DE-RISKING order is never refused, and this is the branch that says so. Without it
    # a short that has run against its owner refuses the very buy that would close it: the
    # book is already outside its ceiling, so every order fails the test below, including
    # the ones that would bring it back inside. The desk would have bounded the position by
    # trapping somebody in it.
    #
    # For a long-only book at leverage 1 this branch is dead — a buy always adds notional —
    # so it changes nothing about the desk as it has always run.
    if gross(after, marks) > gross(book, marks):
        if headroom(after, marks, lev) < 0:
            return False, _no_room(symbol, side, qty, cost_price, book, after, marks, lev)

    return True, ""


def _no_room(symbol: str, side: str, qty: float, cost_price: float,
             book: dict, after: dict, marks: dict, leverage: float) -> str:
    """Why this order does not fit, in the terms the book is actually run under.

    Three sentences rather than one, because three different things are being refused and
    a member who reads the wrong one debugs the wrong thing:

    * **An unlevered, long-only book** gets the desk's original wording, unchanged. That is
      every book that has ever run here, and its refusal must not start reading differently
      because a feature it does not use was added.
    * **A book at or below zero equity** is told that, because no size of order will fit
      and the useful next act is to close rather than to retry smaller.
    * **A levered book** is told the inequality it broke, with all three numbers in it, so
      "how much would fit" is arithmetic rather than a bisection search against the ledger.
    """
    cash = float(book.get("cash", 0.0))
    _, short_before = sides(book.get("units") or {}, marks)
    _, short_after = sides(after.get("units") or {}, marks)

    if leverage == NO_LEVERAGE and not short_before and not short_after:
        cost = qty * cost_price
        return (f"not enough cash: {symbol} {qty:g} at {cost_price:,.2f} costs "
                f"{cost:,.2f} and this strategy holds {cash:,.2f}. "
                f"There is no margin on this desk.")

    eq = equity(after, marks)
    if eq <= 0:
        return (f"this book's equity is {eq:,.2f} — at or below zero — so the desk will "
                f"only take orders that REDUCE what it holds. {side} {qty:g} {symbol} at "
                f"{cost_price:,.2f} would add to it. Close the position; there is nothing "
                f"left to fund a new one.")

    ceiling = leverage * eq
    return (f"over the leverage ceiling: {side} {qty:g} {symbol} at {cost_price:,.2f} "
            f"would take this book to {gross(after, marks):,.2f} of gross exposure "
            f"against equity of {eq:,.2f}, and it is registered at {leverage:g}x, so the "
            f"most it may hold is {ceiling:,.2f}. Gross exposure counts shorts as well as "
            f"longs. Send a smaller size, close something, or register a book with more "
            f"capital.")


def _reserve(book: dict, order: dict, price: float) -> None:
    """Commit an accepted order's effect to a working copy of the book.

    This is buying-power reservation, and it is not an optimisation. A batch is validated
    before any of it reaches the exchange, so without it every order in the batch is
    checked against the same starting cash: ten individually affordable buys all pass and
    collectively spend money the strategy does not have. It also makes a batch *coherent*
    — buy 5 then sell 3 in one tick is a thing a manager will do on their first day, and
    validating the sell against a book that has not seen the buy refuses it for a reason
    that is not true by the time it would have executed.

    Reservation is optimistic: it assumes the order fills. If it does not, the next tick
    reads the real book back from the strategy and the reservation is forgotten. Erring
    this way refuses an order that might have been affordable; the other way lets a
    position exceed the capital, which is the failure that matters.
    """
    otype = (order.get("order_type") or "market").lower()
    fill_price = float(order["limit_price"]) if otype == "limit" else price
    qty = float(order["qty"])
    symbol = order["symbol"]
    signed = qty if order["side"].lower() == "buy" else -qty
    book["units"][symbol] = book["units"].get(symbol, 0.0) + signed
    book["cash"] = book.get("cash", 0.0) - signed * fill_price


def partition(orders: list[dict], registrations: dict, books: dict,
              prices: dict, now: datetime | None = None,
              stale_bars: float = STALE_BARS) -> tuple[list, list]:
    """Split a drained batch into `(to_submit, to_reject)`, in `seq` order.

    `to_reject` carries `(order, reason)` so the ledger records WHY, which is the only
    thing standing between a manager and guessing. An order whose strategy is not
    registered at all is rejected rather than skipped: skipping would leave it pending
    forever, and a queue that silently never empties looks exactly like a working one.
    """
    submit, reject = [], []
    # A working copy per strategy, so reservations accumulate across the batch without
    # touching the strategies' real books — those only move on an actual fill.
    working = {rid: {"cash": float(b.get("cash", 0.0)),
                     "units": dict(b.get("units", {}))}
               for rid, b in books.items()}

    for order in orders:
        reg = registrations.get(order["strategy_id"])
        if reg is None:
            reject.append((order, "no such strategy, or it is not running on this desk"))
            continue
        book = working.setdefault(order["strategy_id"], {"cash": 0.0, "units": {}})
        price = prices.get(order.get("symbol"))
        # The whole mark map, not just this symbol's. A leverage ceiling is a statement
        # about the book, so an order can only be priced against it with every held name
        # valued — checking one symbol at a time would let a book creep past its ceiling
        # one affordable-looking order per name.
        ok, reason = validate(order, reg, book, price, now, stale_bars, prices=prices)
        if not ok:
            reject.append((order, reason))
            continue
        submit.append(order)
        if order["action"] == "new":
            _reserve(book, order, price)
    return submit, reject
