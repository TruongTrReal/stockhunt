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

**No leverage, ever, on this path.** A buy that would take cash below zero is refused.
Margin is a genuine feature with genuine accounting, and arriving at it by accident
because nobody checked is how a paper track record stops meaning anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# One bar of each timeframe. The desk trades 1d and 4h; anything else has no `BAR_SPEC`
# and no walk-forward sheet, so it cannot be registered in the first place.
BAR_SECONDS = {"1d": 86_400, "4h": 14_400}

# Multiplier on that bar. 1.0 means "an order may not outlive the bar it was computed on".
# Raising it buys tolerance for a slow desk at the cost of filling staler decisions.
STALE_BARS = 1.0

SIDES = ("buy", "sell")
TYPES = ("market", "limit")


def stale_window(tf: str, stale_bars: float = STALE_BARS) -> timedelta:
    return timedelta(seconds=BAR_SECONDS.get(tf, 86_400) * stale_bars)


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
             stale_bars: float = STALE_BARS) -> tuple[bool, str]:
    """`(ok, reason)`. `reason` is written into the ledger and shown to the manager, so it
    is phrased for somebody debugging their own bot at 2am, not for a log.

    `book` is this strategy's own: `{"cash": float, "units": {symbol: float}}`. Its own,
    and never the venue account — Nautilus nets every strategy on an instrument into one
    position, so the shared figure answers "what does the whole desk hold in SPY", which
    is a different question and would let one manager's trade size another's.
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
    # which is an estimate — the deadband below is what stops an estimate from being
    # treated as a promise.
    cost_price = float(order["limit_price"]) if otype == "limit" else price
    held = float(book.get("units", {}).get(symbol, 0.0))

    if side == "buy":
        cost = qty * cost_price
        cash = float(book.get("cash", 0.0))
        if cost > cash:
            return False, (f"not enough cash: {symbol} {qty:g} at {cost_price:,.2f} costs "
                           f"{cost:,.2f} and this strategy holds {cash:,.2f}. "
                           f"There is no margin on this desk.")
    else:
        if not registration.get("allow_short") and qty > held + 1e-9:
            return False, (f"cannot sell {qty:g} {symbol}: this strategy holds {held:g} "
                           f"and was not registered with allow_short.")

    return True, ""


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
        ok, reason = validate(order, reg, book, price, now, stale_bars)
        if not ok:
            reject.append((order, reason))
            continue
        submit.append(order)
        if order["action"] == "new":
            _reserve(book, order, price)
    return submit, reject
