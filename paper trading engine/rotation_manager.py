"""The monthly ETF rotation, traded on the live desk through the public API.

This is a **manager**, not a desk component. It holds no positions, imports no Nautilus,
and touches no database: it computes a pick and posts orders to `/v1/orders`, exactly as
an outside manager's code would. `desk_control` drains the ledger and does the trading.
That separation is the whole design of the member path and it is why this file can be
edited and restarted without the desk noticing.

WHAT IT TRADES

`QQQ IWM XLK TLT`, the basket named `desk4` in `walk-forward optimization/rotation.py`.
Once a month it ranks the four on their trailing 63-session total return and puts
everything into the winner. Research, controls and caveats are in that module and in the
report it links; the honest summary is that this beats a coin toss that keeps the same
asset mix (p = 0.003 on the sibling basket) and does NOT clear this project's own
significance bar, so it is deployed as a forward test rather than as a conclusion.

The basket is what the desk can already trade. `paper_config.UNIVERSE["us_etfs"]` is five
names and `desk_control` refuses anything outside it, so the better-evidenced `broad`
basket would have cost a desk restart and a re-warm of every open book. That trade was
made deliberately and it is recorded in `rotation.py`'s `BASKETS["desk4"]`.

WHEN IT DECIDES, AND WHY NOT AT THE CLOSE

A rotation scored on a session's own close and filled at that same close assumes a print
nobody could have known until it happened. The fix this repo uses is not to delay the fill
but to **decide earlier and still trade the close**: at `DECIDE_LEAD_MIN` before the bell
on the last trading day of the month, this builds each session from the part of it that
had already happened, ranks on that, and sends market orders into the closing auction.
`stockhunt.sessions` is the arithmetic and `book_strategy.py` already does the same thing
for the house's IBS books.

IT WRITES TO THE LEDGER, NOT TO THE HTTP API

`stockhunt.deskdb` is the order ledger, and BOTH the API and the desk open it. The API
exists so an outside manager -- somebody with no account on this box -- can reach that
ledger over the network; it is a door, not the room. This runs ON the box, as the desk's
own operator, so it writes to the ledger directly: no key to mint, no browser login, no
console step, no HTTP to fail. `register()` and `submit_order()` are the same two calls
the API makes after it has finished authenticating somebody.

That also makes provisioning idempotent and self-healing. `deskdb.register` keys on
`(account, name)` and revives a retired row rather than creating a second, so this can
call it on every single firing and the desk ends up with exactly one registration.

`client_order_id` is derived from the strategy, the symbol and the SESSION DATE -- never
from a clock, a counter or a random value. Run this twice in the same session, or retry
after a timeout, and the desk returns the first order rather than opening a second
position. The timer that runs it fires several times inside the decision window on
purpose, because a single fire that hits a network error would silently skip a month;
with a stable key, extra fires are free.

Run::

    python rotation_manager.py --dry-run        # decide and print, write nothing
    python rotation_manager.py --status         # the registration and what it holds
    python rotation_manager.py                  # the real thing, gated on the window
    python rotation_manager.py --force          # decide now, whatever day it is

No credentials. Run it from this directory as the user that owns the desk's state
directory, which on the box is `stockhunt`.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import paper_config                                   # noqa: F401  (path bootstrap)
import td_live
from stockhunt import deskdb
from stockhunt import rotation as sh_rotation
from stockhunt.sessions import fold_sessions, session_of

BASKET = ["QQQ", "IWM", "XLK", "TLT"]
ASSET_CLASS = "us_etfs"
SIGNAL_TF = "5m"            # a `td_live.INTERVALS` key, not the vendor string ("5min")
DECIDE_LEAD_MIN = 15        # act this many minutes before the bell
LOOKBACK = sh_rotation.LOOKBACK

# Enough 5-minute bars to rebuild 64 partial sessions with room for holidays and gaps.
# 78 bars a session x ~90 sessions. Vendor caps a single request well above this.
SIGNAL_BARS = 5000

# Account 00 is the house. `kind="member"` because the desk must trade this ON INSTRUCTION
# -- a rotation picks one name out of a basket by comparing them, which no per-symbol rule
# can express, so there is nothing for a `book` or `house_rule` to run. The desk supplies
# the book, the fills and the record; this supplies the decision.
ACCOUNT = "00"
NAME = "rotation-etf-1d"
STRATEGY_ID = f"str_{ACCOUNT}_{NAME}"
CAPITAL = 100_000.0
BENCHMARK = "SPY"

DESK_DB = Path(paper_config.HERE if hasattr(paper_config, "HERE")
               else Path(__file__).resolve().parent) / "state" / "desk.db"
LIVE_JSON = Path(paper_config.PUBLISH_DIR) / "live.json" if getattr(
    paper_config, "PUBLISH_DIR", "") else None


# ------------------------------------------------------------------- the ledger


def open_ledger() -> None:
    """Point `deskdb` at the desk's own database. Must precede every other call here."""
    deskdb.use(DESK_DB)


def ensure_registered() -> dict:
    """Make sure the desk has a live registration for this strategy, and return it.

    Called on every firing rather than once at install time. `deskdb.register` keys on
    `(account, name)`: an existing live row comes back untouched, and a row somebody
    retired is REVIVED with the same `strategy_id`, so the record continues as one track
    rather than restarting. There is therefore no provisioning step to forget, and no way
    to end up with two books quietly splitting the capital.
    """
    reg = deskdb.register(
        ACCOUNT, NAME, ASSET_CLASS, BASKET, "1d", CAPITAL,
        kind="member", benchmark=BENCHMARK, allow_short=False)
    return reg


def desk_view() -> dict:
    """`{cash, equity, holdings{symbol: units}}` as the DESK believes them.

    Read from the `live.json` the desk publishes, not remembered locally. A manager that
    keeps its own idea of the book will eventually disagree with the desk about it --
    after a rejected order, a restart or a partial fill -- and every order after that
    compounds the error.

    An empty view is a real answer, not a failure: before the desk has picked the
    registration up there is nothing to hold, and `rebalance` sizes off `CAPITAL` in that
    case rather than refusing to open the first position.
    """
    out = {"cash": None, "equity": None, "holdings": {}, "seen": False}
    if LIVE_JSON is None or not LIVE_JSON.exists():
        return out
    try:
        doc = json.loads(LIVE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    for s_ in doc.get("strategies") or []:
        if s_.get("id", "").endswith(f":{NAME}") or s_.get("id") == STRATEGY_ID:
            out["seen"] = True
            out["cash"] = s_.get("cash")
            out["equity"] = s_.get("equity")
            for h in s_.get("holdings") or []:
                u = float(h.get("units") or 0.0)
                if abs(u) > 1e-9:
                    out["holdings"][h["symbol"]] = u
            break
    return out


# --------------------------------------------------------------------- the signal


def bell() -> tuple[str, int, int]:
    tz, (hh, mm) = paper_config.SESSION_CLOSE[ASSET_CLASS]
    return tz, hh, mm


def sessions_for(symbol: str) -> pd.DataFrame | None:
    """Trailing partial sessions for one name, each cut before the bell.

    Every row is a partial session, today's included. A rule whose history is full-day
    bars and whose newest row is partial is comparing two different statistics -- so the
    history is built the same way the live row is, and that is what makes the live signal
    comparable to the backtest rather than merely similar to it.
    """
    bars = td_live.fetch_bars(symbol, SIGNAL_TF, n=SIGNAL_BARS)
    if bars is None or len(bars) == 0:
        return None
    idx = pd.DatetimeIndex(bars.index)
    if idx.tz is None:
        bars = bars.tz_localize("UTC")
    tz, hh, mm = bell()
    return fold_sessions(bars, tz, hh, mm, DECIDE_LEAD_MIN)


def decide() -> tuple[str | None, pd.DataFrame]:
    """`(winning symbol, the score table)`. `None` when nothing has enough history."""
    frames, rows = {}, []
    for s in BASKET:
        df = sessions_for(s)
        if df is None or len(df) < LOOKBACK + 1:
            rows.append({"symbol": s, "sessions": 0 if df is None else len(df),
                         "score": float("nan"), "last": float("nan")})
            continue
        frames[s] = df["Close"]
        rows.append({"symbol": s, "sessions": len(df), "score": float("nan"),
                     "last": float(df["Close"].iloc[-1])})
    if not frames:
        return None, pd.DataFrame(rows)

    closes = pd.DataFrame(frames).sort_index()
    sc = sh_rotation.scores(closes, LOOKBACK)
    latest = sc[-1]
    table = pd.DataFrame(rows).set_index("symbol")
    for i, s in enumerate(closes.columns):
        table.loc[s, "score"] = latest[i]
    winner = sh_rotation.pick(latest, list(closes.columns))
    return winner, table.reset_index()


# ------------------------------------------------------------------- the schedule


def is_decision_time(now: datetime | None = None) -> tuple[bool, str]:
    """Is now inside the decision window of the last trading day of the month?

    Two conditions, and the second is the one that needs care. "Last trading day" cannot
    be read off a calendar -- the source's own code tested `date.day == monthrange(...)`
    and therefore skipped 30% of months, every one whose 31st fell on a weekend. It is
    asked of the SESSION INDEX instead: today is the last trading day if the next session
    the vendor knows about belongs to a different month.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tz, hh, mm = bell()
    local = pd.Timestamp(now).tz_convert(tz)
    _, decide_at = session_of(pd.Timestamp(now), tz, hh, mm, DECIDE_LEAD_MIN)
    if local < decide_at:
        return False, f"before the window ({local:%H:%M} < {decide_at:%H:%M} {tz})"
    close_at = decide_at + pd.Timedelta(minutes=DECIDE_LEAD_MIN)
    if local > close_at:
        return False, f"after the bell ({local:%H:%M} > {close_at:%H:%M} {tz})"

    # A calendar month-end that is not a session is exactly the source's bug, so the test
    # is "is there another session in this month", not "is today the 31st".
    nxt = _next_session(local)
    if nxt is None:
        return False, "no exchange calendar; refusing to guess the month end"
    if (nxt.year, nxt.month) == (local.year, local.month):
        return False, f"not the last trading day -- {nxt} is still this month"
    return True, f"last trading day, {local:%H:%M} {tz}, bell at {close_at:%H:%M}"


def _next_session(local: pd.Timestamp):
    """The next US equity session date after `local`, or None if it cannot be determined.

    Returns a plain `date`, and that is deliberate. `valid_days` hands back midnight UTC
    for each session, and converting that to New York lands on the PREVIOUS evening --
    2026-09-01 becomes 2026-08-31 20:00, so the last trading day of August reads as "there
    is another August session" and the month is never traded. The gate caught exactly
    that. A session is a calendar date on its own exchange; do not give it a time.

    None is a refusal to guess. The caller treats it as "not the last trading day" and
    skips, because trading a wrongly-inferred month end is worse than missing one.
    """
    try:
        import pandas_market_calendars as mcal
        cal = mcal.get_calendar("NYSE")
        days = cal.valid_days(start_date=(local + pd.Timedelta(days=1)).date(),
                              end_date=(local + pd.Timedelta(days=12)).date())
        if not len(days):
            return None
        return pd.Timestamp(days[0]).tz_localize(None).normalize().date()
    except Exception:
        return None


# --------------------------------------------------------------------- the orders


# A rejection is retried at most this many times within one session. Bounded because an
# unbounded retry against a desk that keeps refusing is a loop that fills the ledger with
# identical dead orders, and the reason it refuses (no price yet, insufficient cash) is
# usually not something another attempt fixes inside fifteen minutes.
MAX_ATTEMPTS = 4


def order_id(symbol: str, session: str, attempt: int = 0) -> str:
    """`rot-<symbol>-<session>[-r<n>]`. Stable across every retry within a session.

    Derived from the SESSION DATE and nothing else that moves, so the timer firing five
    times inside the decision window places one order rather than five. This is the only
    defect in this file that could cost money silently, which is why the gate pins it.

    `attempt` exists for exactly one case: an order the desk REJECTED. See `place`.
    """
    return f"rot-{symbol}-{session}" + (f"-r{attempt}" if attempt else "")


def place(symbol: str, side: str, qty: float, session: str, dry: bool) -> bool:
    """Queue one order, and treat a rejection as retryable while a fill is not.

    The naive version of idempotency loses a month. `submit_order` returns the prior row
    for a known `client_order_id` whatever state it is in -- so an order the desk refused
    at 15:45 ("no price for QQQ yet") would be "already sent" at 15:50, 15:55 and 16:00,
    and this strategy trades twelve times a year. One refusal, one month gone.

    So a terminal REJECTION earns a fresh id under the same session; anything live or
    filled blocks, which is the property that actually matters. `MAX_ATTEMPTS` bounds it,
    because a desk that has refused four times is not going to be talked round.
    """
    for attempt in range(MAX_ATTEMPTS):
        coid = order_id(symbol, session, attempt)
        if dry:
            print(f"    DRY   {side:4s} {qty:>12.4f} {symbol}   client_order_id={coid}")
            return True
        prior, created = deskdb.submit_order(
            ACCOUNT, STRATEGY_ID, coid, action="new", symbol=symbol,
            side=side, qty=float(qty), order_type="market", tif="day")
        if created:
            print(f"    queued        {side:4s} {qty:>12.4f} {symbol}   {coid}")
            return True
        if (prior or {}).get("state") != "rejected":
            print(f"    already sent  {side:4s} {qty:>12.4f} {symbol}   {coid}  "
                  f"state={(prior or {}).get('state')}")
            return False
        print(f"    rejected      {coid}: {(prior or {}).get('reason')} -- retrying")
    print(f"    GIVING UP on {symbol} after {MAX_ATTEMPTS} rejections this session")
    return False


def rebalance(winner: str, prices: dict, session: str, dry: bool) -> None:
    """Sell everything that is not the winner, then buy the winner with the proceeds.

    Sells go in first and in one pass, because `desk_control` drains strictly in `seq`
    order: a buy queued ahead of the sell that funds it is refused for want of cash, and
    the month is then spent in the wrong name. Ordering here is cheaper than reconciling
    there.
    """
    view = desk_view()
    held = view["holdings"]
    if held:
        print(f"    desk holds: " + ", ".join(f"{k} {v:.4f}" for k, v in sorted(held.items())))
    elif not view["seen"]:
        print("    the desk has not published this strategy yet -- first run, sizing off "
              f"${CAPITAL:,.0f}")

    for symbol, qty in sorted(held.items()):
        if symbol != winner and qty > 0:
            place(symbol, "sell", qty, session, dry)

    if held.get(winner, 0.0) > 0:
        print(f"    hold  {winner} -- already the pick, nothing to buy")
        return

    px = prices.get(winner)
    if not px or px <= 0:
        print(f"    SKIP  no price for {winner}; not sizing an order blind")
        return
    # Equity, not cash: the sells above are queued but not filled, so the cash they will
    # release is still sitting in the names being sold. Sizing off cash alone would buy a
    # sliver on the first rotation of every month and leave the rest idle.
    equity = view["equity"]
    if equity is None:
        equity = CAPITAL if not held else (
            float(view["cash"] or 0.0)
            + sum(prices.get(s_, 0.0) * q for s_, q in held.items()))
    budget = float(equity) * 0.98        # the same 2% buffer the house books keep
    if budget <= 0:
        print("    SKIP  no equity reported yet; the desk may still be warming up")
        return
    place(winner, "buy", budget / px, session, dry)


# ------------------------------------------------------------------------ cli


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and print; register but queue no orders")
    ap.add_argument("--force", action="store_true",
                    help="skip the last-trading-day and window checks")
    ap.add_argument("--status", action="store_true",
                    help="show the registration and what the desk holds, then exit")
    args = ap.parse_args()

    open_ledger()

    if args.status:
        reg = deskdb.registration(STRATEGY_ID, ACCOUNT)
        print(json.dumps(reg, indent=2, default=str) if reg else
              f"{STRATEGY_ID}: not registered yet (any run registers it)")
        view = desk_view()
        print(f"desk view: cash={view['cash']} equity={view['equity']} "
              f"holdings={view['holdings'] or '{}'}"
              + ("" if view["seen"] else "   (not published yet)"))
        recent = deskdb.orders(ACCOUNT, strategy_id=STRATEGY_ID)[-8:]
        for o in recent:
            print(f"  seq {o['seq']:<5} {o['side'] or '':4s} {str(o['qty'] or ''):>14s} "
                  f"{o['symbol'] or '':6s} {o['state']:9s} {o.get('reason') or ''}")
        return 0

    # The window is checked BEFORE anything is written. The timer fires ~36 times a day
    # and all but a handful are outside it; a firing at 10am should cost one journal line
    # and touch nothing.
    ok, why = is_decision_time()
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ}] {why}")
    if not ok and not args.force:
        return 0
    if not ok:
        print("  --force: deciding anyway")

    reg = ensure_registered()
    print(f"  registration {reg['strategy_id']}: state={reg['state']} want={reg['want']}"
          + (f"  desk says: {reg['reason']}" if reg.get("reason") else ""))

    winner, table = decide()
    print(table.to_string(index=False, float_format=lambda x: f"{x:10.4f}"))
    if winner is None:
        print("  no name has enough history yet; holding whatever is held")
        return 0

    tz, hh, mm = bell()
    session = pd.Timestamp.now(tz=tz).strftime("%Y%m%d")
    print(f"  pick: {winner}   session {session}")

    prices = td_live.fetch_prices(BASKET) or {}
    rebalance(winner, prices, session, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
