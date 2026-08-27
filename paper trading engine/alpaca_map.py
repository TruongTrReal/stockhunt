"""What the desk holds, what Alpaca holds, and the orders between the two.

Pure functions over plain dicts, no network and no `requests` import — the same shape as
`desk_orders.py`, and for the same reason: the arithmetic that decides how many shares to
send is the part that must be exhaustively testable in milliseconds.

**Reconcile the position, never forward the order.** The obvious mirror re-sends every
`OrderFilled` to Alpaca. It drifts within a day: Alpaca paper hands out a random partial
fill 10% of the time, rejects on asset eligibility, and rounds fractional quantities its
own way — so after a week the two books hold different share counts and nothing in either
record says which fill diverged. Computing a *target* and sending the *difference* makes a
missed cycle cost latency and never correctness, and makes a rejected order retry itself
on the next pass because the difference is still there. It is `desk_control.tick()`'s
sweep-not-notification design, one process out.

**The desk is bigger than the account, so the target is scaled.** Each class runs several
$100,000 books — 600,000 per class at the time of writing — against one Alpaca paper
account holding 100,000. A raw target would be a stream of rejected buys. Every target is
therefore multiplied by `alpaca_equity / desk_equity`, which makes Alpaca a faithful
*proportional* copy at whatever size the account happens to be and self-corrects as either
side compounds.

**Long only, and the clamp is loud.** A negative target becomes zero and is reported.
Alpaca will happily lend, and a paper track record that arrived at margin because nobody
checked stops meaning anything — the same rule `desk_orders` enforces one process over.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A holding smaller than this is treated as flat. Position quantities arrive as strings
# and cross a scale factor, so an exactly-closed book lands at 1e-17 rather than 0.
DUST_UNITS = 1e-9

# Don't send a rebalance worth less than this. A mark wobble on a 216-name universe can
# otherwise generate a hundred three-dollar orders against a 200-calls-per-minute budget.
MIN_NOTIONAL = 5.0

# ...nor one that moves the position by less than this fraction of its target. The desk's
# own `rebalance_deadband` exists for the same reason one level up.
MIN_DRIFT = 0.02

# A ceiling on one cycle, so a bad snapshot cannot empty the rate limit in one pass.
MAX_ORDERS_PER_CYCLE = 60


@dataclass
class Plan:
    """One class's reconciliation, and everything needed to explain it afterwards."""

    cls: str
    ratio: float = 1.0
    desk_equity: float = 0.0
    alpaca_equity: float = 0.0
    targets: dict[str, float] = field(default_factory=dict)   # scaled, by repo symbol
    held: dict[str, float] = field(default_factory=dict)      # by repo symbol
    marks: dict[str, float] = field(default_factory=dict)     # last mark, by repo symbol
    orders: list[dict] = field(default_factory=list)
    untradable: list[str] = field(default_factory=list)
    clamped: list[str] = field(default_factory=list)          # negative targets
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (symbol, reason)


# ------------------------------------------------------------------ symbols

def norm_key(symbol: str) -> str:
    """The join key between a repo symbol and an Alpaca one.

    Alpaca's crypto endpoints answer in both spellings depending on age of the route —
    `BTC/USD` from `/v2/assets` and, historically, `BTCUSD` from `/v2/positions`. Joining
    on the slash-stripped upper-case form makes the mirror indifferent to which one comes
    back, while `alpaca_symbol` still sends the modern spelling.
    """
    return str(symbol).replace("/", "").replace("-", "").upper().strip()


def alpaca_symbol(repo_symbol: str) -> str:
    """What to put in the order. Equity tickers pass through; the repo already spells
    crypto pairs `BTC/USD`, which is Alpaca's current format."""
    return str(repo_symbol).strip().upper()


def asset_index(assets: list[dict]) -> dict[str, dict]:
    """`/v2/assets` keyed by `norm_key`, keeping only what can actually be traded.

    The repo has been handed the wrong instrument by a vendor three times — `CTRA` came
    back as Ciputra Development on the Indonesia Stock Exchange, `CL` as Colgate-Palmolive
    instead of crude oil, and four `us_stocks` names have an intraday history belonging to
    whoever held the ticker at the time. The lesson each time was the same: never assume a
    symbol resolved, ask the reference endpoint. This is that endpoint.
    """
    out: dict[str, dict] = {}
    for a in assets:
        if a.get("status") != "active" or not a.get("tradable"):
            continue
        out[norm_key(a.get("symbol", ""))] = a
    return out


# ------------------------------------------------------------------ the desk side

def _live(strategy: dict) -> bool:
    """A book the desk is actually running. A paused or retired one holds nothing that
    Alpaca should be mirroring, and a warming one has not decided anything yet."""
    return strategy.get("status") == "running"


def _tickers(field: str | None) -> list[str]:
    """The instruments named in one `symbol` field, which may hold several.

    The same read-side repair `paper_state.feed_symbols` makes, for the same reason: a
    MEMBER's `symbol` is whatever they typed at registration, and the live desk carries
    registrations like `"QQQ, IWM, XLK, TLT"` in that field. Splitting on the comma is
    safe because no ticker contains one -- crypto pairs use `/` (`BTC/USD`).
    """
    if not field:
        return []
    return [part.strip() for part in str(field).split(",") if part.strip()]


def unmirrorable(snapshot: dict, cls: str) -> list[tuple[str, str]]:
    """Running books this process cannot mirror, and why. `(symbol, reason)`.

    One shape, and it is not hypothetical: a member registration whose `symbol` names
    several instruments while `units` is their SUM. `paper_state` repairs the same field
    for marking by splitting it, but splitting cannot recover a per-name breakdown that
    was never published, so there is no honest target to compute here.

    Reported rather than silently dropped. The exposure is real -- a multi-name member on
    the live ETF desk is a whole $100,000 book -- so its absence from Alpaca has to be
    visible, or the second record looks complete while missing a leg.
    """
    out: list[tuple[str, str]] = []
    for s in snapshot.get("strategies", []):
        if s.get("cls") != cls or not _live(s) or s.get("kind") == "book":
            continue
        names = _tickers(s.get("symbol"))
        if len(names) > 1 and (s.get("units") or 0.0):
            out.append((str(s.get("symbol")),
                        f"names {len(names)} instruments and publishes no per-name "
                        f"units, so its {float(s['units']):g} unit(s) cannot be split"))
    return out


def desk_targets(snapshot: dict, cls: str) -> dict[str, float]:
    """Net units per symbol across every running book of one class.

    A book publishes its per-name quantities in `holdings`; a single-instrument system
    publishes `symbol` and `units` at the top level. Both shapes appear in
    `paper_state.snapshot()` and both are summed here, because what Alpaca should hold is
    the desk's *net* exposure and nothing downstream cares which kind of system produced
    it.

    A book's top-level `units` is `held_count()` — how many NAMES it holds, not a share
    quantity — so it must never be added for a `kind == "book"` row. That confusion is
    what `paper_state._mark_book` exists to document.
    """
    out: dict[str, float] = {}
    for s in snapshot.get("strategies", []):
        if s.get("cls") != cls or not _live(s):
            continue
        if s.get("kind") == "book":
            for h in s.get("holdings") or []:
                sym = h.get("symbol")
                units = h.get("units") or 0.0
                if sym and units:
                    out[sym] = out.get(sym, 0.0) + float(units)
        else:
            sym = s.get("symbol")
            units = s.get("units") or 0.0
            # A member's `symbol` may name several instruments while `units` is their sum
            # (see `unmirrorable`). Summing it under the joined string invents a target
            # for a ticker that does not exist, which then reports itself as an ALPACA
            # coverage problem -- blaming the broker for a shape the desk chose.
            if sym and units and len(_tickers(sym)) == 1:
                out[sym] = out.get(sym, 0.0) + float(units)
    return {k: v for k, v in out.items() if abs(v) > DUST_UNITS}


def desk_equity(snapshot: dict, cls: str) -> float:
    """The capital the class's running books are sized against.

    Equity where the desk has marked it, capital where it has not — the same fallback
    `paper_state.snapshot` uses for the venue total, and for the same reason: skipping an
    unmarked book instead would make the denominator smaller than the exposure it is
    dividing, which inflates every target the moment a book restarts.
    """
    total = 0.0
    for s in snapshot.get("strategies", []):
        if s.get("cls") != cls or not _live(s):
            continue
        eq = s.get("equity")
        total += float(eq if eq is not None else (s.get("capital") or 0.0))
    return total


def desk_marks(snapshot: dict, cls: str) -> dict[str, float]:
    """Last mark per symbol, for pricing a delta into a notional. Only used to decide
    whether an order is too small to be worth sending — never to size one."""
    out: dict[str, float] = {}
    for s in snapshot.get("strategies", []):
        if s.get("cls") != cls:
            continue
        if s.get("kind") == "book":
            for h in s.get("holdings") or []:
                mark = h.get("mark")
                if h.get("symbol") and mark:
                    out[h["symbol"]] = float(mark)
        else:
            mark = s.get("mark_price")
            # Same multi-instrument field as in `desk_targets`. A mark filed under the
            # joined string prices nothing, and there is no target for it either.
            if s.get("symbol") and mark and len(_tickers(s["symbol"])) == 1:
                out[s["symbol"]] = float(mark)
    return out


# ------------------------------------------------------------------ the Alpaca side

def held_units(positions: list[dict]) -> dict[str, float]:
    """`/v2/positions` as `norm_key -> signed units`."""
    out: dict[str, float] = {}
    for p in positions:
        try:
            qty = float(p.get("qty", 0.0))
        except (TypeError, ValueError):
            continue
        if abs(qty) > DUST_UNITS:
            out[norm_key(p.get("symbol", ""))] = qty
    return out


def account_equity(account: dict) -> float:
    for key in ("equity", "portfolio_value", "last_equity"):
        try:
            value = float(account.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


# ------------------------------------------------------------------ the arithmetic

def scale_ratio(desk_eq: float, alpaca_eq: float) -> float:
    """How much of the desk one Alpaca account can carry. Zero when either side is
    unknown, which makes every target zero and every order a close — the safe direction:
    a mirror that cannot size itself should be flat, not guessing."""
    if desk_eq <= 0 or alpaca_eq <= 0:
        return 0.0
    return alpaca_eq / desk_eq


def _round_qty(qty: float, asset: dict | None) -> float:
    """Alpaca's own quantity rules for this asset.

    * a non-fractionable equity trades in whole shares;
    * a crypto pair trades in multiples of `min_trade_increment`.

    Rounding is always *toward zero*. A buy rounded up can exceed buying power and a sell
    rounded up becomes a short — both are refusals that the next cycle would simply repeat,
    so the residual is left for a later pass instead.
    """
    if qty <= 0:
        return 0.0
    if asset and not asset.get("fractionable", True):
        return float(int(qty))
    increment = None
    if asset:
        for key in ("min_trade_increment", "increment"):
            try:
                increment = float(asset[key])
            except (KeyError, TypeError, ValueError):
                continue
            break
    if increment and increment > 0:
        return float(int(qty / increment)) * increment
    return qty


def plan_orders(cls: str, *, targets: dict[str, float], held: dict[str, float],
                marks: dict[str, float], ratio: float,
                assets: dict[str, dict] | None = None,
                desk_eq: float = 0.0, alpaca_eq: float = 0.0,
                min_notional: float = MIN_NOTIONAL, min_drift: float = MIN_DRIFT,
                max_orders: int = MAX_ORDERS_PER_CYCLE) -> Plan:
    """The whole reconciliation for one class, as data.

    `targets` and `marks` are keyed by repo symbol; `held` by `norm_key`. Every symbol the
    desk wants *or* Alpaca holds is considered, so a name the desk has dropped entirely
    still gets its closing sell.
    """
    plan = Plan(cls=cls, ratio=ratio, desk_equity=desk_eq, alpaca_equity=alpaca_eq)

    scaled: dict[str, float] = {}
    for sym, units in targets.items():
        want = units * ratio
        if want < 0:
            plan.clamped.append(sym)
            want = 0.0
        scaled[sym] = want
    plan.targets = scaled

    by_key = {norm_key(s): s for s in scaled}
    # A position Alpaca holds under a symbol the desk never named still has to be closed,
    # or a retired book leaves its shares behind forever.
    for key in held:
        by_key.setdefault(key, key)

    candidates: list[tuple[float, dict]] = []
    for key, sym in sorted(by_key.items()):
        want = scaled.get(sym, 0.0)
        have = held.get(key, 0.0)
        plan.held[sym] = have

        asset = (assets or {}).get(key)
        if assets is not None and asset is None:
            if want > 0:
                plan.untradable.append(sym)
                continue
            # Not tradable and not wanted: nothing to do. If Alpaca somehow holds it the
            # sell below is still attempted, because a stuck position is worse than a
            # refused order.
            if abs(have) <= DUST_UNITS:
                continue

        if have < -DUST_UNITS:
            # A short should be impossible on this path; buying it back is the only
            # correct response and it is reported so it cannot pass unnoticed.
            plan.skipped.append((sym, f"unexpected short position of {have:g}"))

        delta = want - have
        if abs(delta) <= DUST_UNITS:
            continue

        side = "buy" if delta > 0 else "sell"
        qty = abs(delta)
        if side == "sell":
            qty = min(qty, max(have, 0.0))     # never sell into a short
            if qty <= DUST_UNITS:
                continue

        # A full exit sells the whole position rather than a rounded share of it, so a
        # non-fractionable name cannot leave a permanent stub behind.
        exact_close = side == "sell" and want <= DUST_UNITS
        qty = qty if exact_close else _round_qty(qty, asset)
        if qty <= DUST_UNITS:
            plan.skipped.append((sym, "delta rounds to zero at this asset's increment"))
            continue

        mark = marks.get(sym) or 0.0
        notional = qty * mark
        if mark and notional < min_notional and not exact_close:
            plan.skipped.append((sym, f"${notional:.2f} below the ${min_notional:.2f} floor"))
            continue

        denom = want if want > DUST_UNITS else have
        drift = abs(delta) / denom if denom > DUST_UNITS else 1.0
        if drift < min_drift and not exact_close:
            plan.skipped.append((sym, f"{drift:.1%} drift below the {min_drift:.0%} band"))
            continue

        candidates.append((notional, {
            "cls": cls, "symbol": sym, "alpaca_symbol": alpaca_symbol(sym),
            "side": side, "qty": qty, "mark": mark or None, "notional": notional,
            "target": want, "held": have,
        }))

    # Sells first — they release buying power the buys in the same cycle need — then the
    # largest trades, so a truncated cycle has done the part that matters most.
    candidates.sort(key=lambda c: (c[1]["side"] != "sell", -c[0]))
    plan.orders = [c[1] for c in candidates[:max_orders]]
    for _, order in candidates[max_orders:]:
        plan.skipped.append((order["symbol"], f"over the {max_orders}-order cycle cap"))
    return plan


def client_order_id(cls: str, symbol: str, cycle: object) -> str:
    """Deterministic, and unique per (class, symbol, cycle).

    Alpaca refuses a duplicate `client_order_id`, so a retry after a timeout, or two
    mirrors started by accident, cannot double a position — the same guarantee
    `deskdb.submit_order` leans on one process over. Capped at 128 characters by the
    venue; nothing this builds comes close.

    **`cycle` must identify the cycle, not the second it happened in.** It was
    `int(time.time())`, which is the same thing only as long as no two cycles ever share a
    second — and two do, routinely: a `--once` run kicked off beside the loop, or a retry
    after a transient failure. The venue then refuses the second order as a duplicate and
    the mirror reports a rejection for an order that was perfectly legitimate, which reads
    in the log exactly like an eligibility problem. The caller passes the store's own
    monotonic cycle id with a timestamp in front of it, so it is unique across restarts
    (where the autoincrement begins again) as well as within one.
    """
    token = str(cycle).replace(" ", "").replace(":", "")
    return f"sh-{cls}-{norm_key(symbol)}-{token}"[:128]
