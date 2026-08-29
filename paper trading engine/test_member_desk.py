"""Gate: an order written into the ledger becomes a fill, a position and a record.

This is stage 02's proof, and it deliberately stops short of HTTP. Orders arrive the way
they will in production — as rows in `desk.db` — but they are put there by this script
rather than by an API, so the engine half can be proven before the web half exists.

What it exercises end to end:

    a registration in the ledger
      -> DeskController reconciles it and attaches a MemberStrategy to a RUNNING trader
      -> orders drained in seq order
      -> validated against that strategy's OWN book
      -> submitted to the simulated exchange, filled from a bar
      -> cash and units move, the fill lands in paper.db WITH its symbol
      -> the ledger row goes from accepted to filled

It also proves the refusals, which matter more than the fills: an order beyond the cash,
an order for a symbol the strategy did not register, and a stale order left over from
downtime must all be rejected with a reason a human can act on.

**`--leverage` runs the whole thing again on a levered book**, and the order that separates
them is `over-cash`: sized deliberately between the book's capital and its levered ceiling,
so an unlevered run must refuse it and a levered run must FILL it. That is the property
worth proving end to end rather than in a unit test — the ceiling is enforced in
`desk_orders`, but the money it lets through has to reach a real exchange, move real cash
into a negative balance, and land in the record.

Run from this directory::

    ..\\.venv\\Scripts\\python test_member_desk.py
    ..\\.venv\\Scripts\\python test_member_desk.py --leverage 2

Nonzero exit means the order path is broken. It writes only to temporary files — the real
`paper.db` and `desk.db` are never touched.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paper_config                                                     # noqa: F401

# Redirect BOTH stores before anything opens them for real. The record this gate would
# otherwise write into is the tracked forward-test file.
#
# The directory is passed through the ENVIRONMENT rather than just computed, because
# `ImportableControllerConfig` resolves `test_member_desk:ScriptedController` by importing
# this file — so it runs a second time, as a second module object, while `__main__` is
# still live. A plain `mkdtemp()` would therefore mint a SECOND temp directory and the
# second copy would repoint `store` and `deskdb` at empty databases, silently discarding
# everything the first copy had written. `setdefault` makes both copies agree.
_TMP = Path(os.environ.setdefault(
    "STOCKHUNT_MEMBERDESK_TMP", tempfile.mkdtemp(prefix="stockhunt-memberdesk-")))

import store                                                            # noqa: E402
store.DB_PATH = _TMP / "paper.db"

import paper_state                                                      # noqa: E402
paper_state.STATE_PATH = _TMP / "paper_state.json"
paper_state.MIRROR_PATH = None                    # publish nowhere

from stockhunt import deskdb                                            # noqa: E402
deskdb.use(_TMP / "desk.db")

from backtest_paper import build_bars, load_bars                        # noqa: E402
from desk_control import DeskController, DeskControllerConfig           # noqa: E402
from member_strategy import MemberStrategy                              # noqa: E402
import td_nautilus                                                      # noqa: E402

from nautilus_trader.backtest.engine import BacktestEngine              # noqa: E402
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig  # noqa: E402
from nautilus_trader.live.config import ControllerConfig                # noqa: E402
from nautilus_trader.model.currencies import USD                        # noqa: E402
from nautilus_trader.model.data import Bar, BarType                     # noqa: E402
from nautilus_trader.model.enums import AccountType, OmsType            # noqa: E402
from nautilus_trader.model.identifiers import TraderId, Venue           # noqa: E402
from nautilus_trader.model.objects import Money                         # noqa: E402
from nautilus_trader.trading.config import ImportableControllerConfig   # noqa: E402

SYMBOL = "SPY"
VENUE = "SANDBOX"
CAPITAL = 10_000.0
ACCOUNT = "a7"
NAME = "meanrev"
REG_ID = f"str_{ACCOUNT}_{NAME}"


class ScriptedConfig(DeskControllerConfig, frozen=True):
    bar_type: str = ""
    place_at_bar: int = 0


class ScriptedController(DeskController):
    """The real controller, driven by bars instead of by a wall clock.

    `tick()` is called per bar rather than on a timer: a TestClock and a LiveClock do not
    agree about when a timer fires, and a gate that depends on that is testing Nautilus's
    scheduler rather than this desk. The reconcile and drain logic underneath is the
    production code, untouched.
    """

    def __init__(self, config: ScriptedConfig, trader) -> None:
        super().__init__(config=config, trader=trader)
        self.bars_seen = 0
        self.scripted = False

    def on_start(self) -> None:
        deskdb.connect()
        self.tick()                       # attach whatever is already registered
        self.subscribe_bars(BarType.from_str(self.config.bar_type))

    def on_bar(self, bar: Bar) -> None:
        self.bars_seen += 1
        if self.bars_seen == self.config.place_at_bar and not self.scripted:
            self.scripted = True
            _write_orders(float(bar.close))
        if self.bars_seen >= self.config.place_at_bar:
            self.tick()


def _write_orders(price: float) -> None:
    """Seven orders: three that must work, three that must be refused, and one that
    depends on how far the book is levered.

    `price` is the bar the orders are written on, so `over-cash` can be SIZED against the
    real close rather than against a guess. A hardcoded quantity would separate the two
    runs only until SPY moved, and it would fail as "the ceiling is broken" rather than as
    "the test is stale" — which is the worse of the two ways to be wrong.
    """
    ok = dict(symbol=SYMBOL, side="buy", qty=5, order_type="market")
    deskdb.submit_order(ACCOUNT, REG_ID, "buy-5", **ok)
    deskdb.submit_order(ACCOUNT, REG_ID, "buy-5-again", **ok)         # a second real buy
    deskdb.submit_order(ACCOUNT, REG_ID, "sell-3", symbol=SYMBOL, side="sell", qty=3,
                        order_type="market")

    deskdb.submit_order(ACCOUNT, REG_ID, "too-dear", symbol=SYMBOL, side="buy",
                        qty=100_000, order_type="market")
    deskdb.submit_order(ACCOUNT, REG_ID, "wrong-symbol", symbol="TSLA", side="buy",
                        qty=1, order_type="market")

    # Left over from an outage: submitted two days ago, drained now.
    stale, _ = deskdb.submit_order(ACCOUNT, REG_ID, "stale", symbol=SYMBOL, side="buy",
                                   qty=1, order_type="market")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    deskdb.connect().execute("UPDATE orders SET submitted_at = ? WHERE seq = ?",
                             (old, stale["seq"]))

    # LAST, so it is priced against the book the six above leave behind (7 shares held).
    # 1.2x the capital in notional: past an unlevered ceiling, inside a 2x one.
    deskdb.submit_order(ACCOUNT, REG_ID, "over-cash", symbol=SYMBOL, side="buy",
                        qty=max(int(CAPITAL * 1.2 / price), 1), order_type="market")


def run(bars_limit: int, place_at: int, log_level: str, leverage: float = 1.0) -> dict:
    df = load_bars(SYMBOL, "1d").tail(bars_limit)
    inst = td_nautilus.equity_instrument(SYMBOL, VENUE)
    bar_type = BarType.from_str(f"{inst.id}-{paper_config.BAR_SPEC['1d']}")

    deskdb.register(ACCOUNT, NAME, "us_stocks", [SYMBOL], "1d", CAPITAL,
                    kind="member", benchmark=SYMBOL, leverage=leverage)

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("MEMBER-001"),
        logging=LoggingConfig(bypass_logging=log_level == "OFF", log_level=log_level),
        controller=ImportableControllerConfig(
            controller_path="test_member_desk:ScriptedController",
            config_path="test_member_desk:ScriptedConfig",
            config={"bar_type": str(bar_type), "place_at_bar": place_at,
                    "tick_seconds": 30, "export_state": True}),
    ))
    engine.add_venue(venue=Venue(VENUE), oms_type=OmsType.NETTING,
                     account_type=AccountType.CASH, base_currency=USD,
                     starting_balances=[Money(CAPITAL * 10, USD)])
    engine.add_instrument(inst)
    # (2, 0) to match the whole-share Equity above. The exchange rejects any bar whose
    # precision differs from the instrument's, rather than rounding it.
    engine.add_data(build_bars(df, bar_type, price_precision=2, size_precision=0))
    engine.run()

    controller = engine.kernel._controller
    strat = controller._running.get(REG_ID) if controller else None
    out = {
        "bars": len(df),
        "attached": isinstance(strat, MemberStrategy),
        "applied": controller.applied if controller else 0,
        "rejected": controller.rejected if controller else 0,
        "units": dict(strat._units) if strat else {},
        "cash": strat._cash if strat else None,
        "fills": strat._n_fills if strat else 0,
        "orders": {o["client_order_id"]: o for o in deskdb.orders(ACCOUNT)},
        "registration": deskdb.registration(REG_ID),
        "record_fills": store.recent_fills(store.sid_for(ACCOUNT, NAME)),
        "watermark": deskdb.watermark_seq(),
    }
    engine.dispose()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bars", type=int, default=400)
    ap.add_argument("--place-at", type=int, default=300)
    ap.add_argument("--log-level", default="OFF")
    ap.add_argument("--leverage", type=float, default=1.0,
                    help="how far the book may lever. 1 is the desk's original behaviour")
    args = ap.parse_args()

    levered = args.leverage > 1.0
    r = run(args.bars, args.place_at, args.log_level, args.leverage)
    orders = r["orders"]

    print(f"\n  {SYMBOL} 1d, {r['bars']} bars, orders written at bar {args.place_at}, "
          f"leverage {args.leverage:g}x")
    print(f"  registration: {r['registration']['state']}  "
          f"strategy attached: {r['attached']}")
    print(f"  applied {r['applied']}, rejected {r['rejected']}, "
          f"fills {r['fills']}, watermark {r['watermark']}")
    print(f"  book: cash {r['cash']:,.2f}  units {r['units']}")
    print("\n  ledger:")
    for coid, o in orders.items():
        reason = f"  — {o['reason']}" if o["reason"] else ""
        print(f"    {coid:<14} {o['state']:<10} filled {o['filled_qty']:g}{reason}")

    problems = []
    if not r["attached"]:
        problems.append("the controller never attached the member strategy")
    if r["registration"]["state"] != "live":
        problems.append(f"registration is {r['registration']['state']}, expected live")

    for coid in ("buy-5", "buy-5-again", "sell-3"):
        if orders.get(coid, {}).get("state") != "filled":
            problems.append(f"{coid} should have filled, is "
                            f"{orders.get(coid, {}).get('state')}")

    # An order past the ceiling is refused either way; only the WORDING moves. An unlevered
    # book gets the desk's original sentence — that is the promise `leverage = 1` makes and
    # a member who never asked for leverage must not start reading about gross exposure.
    over = "not enough cash" if not levered else "leverage ceiling"
    for coid, expect in (("too-dear", over),
                         ("wrong-symbol", "not one of this strategy's symbols"),
                         ("stale", "stale")):
        o = orders.get(coid, {})
        if o.get("state") != "rejected":
            problems.append(f"{coid} should have been rejected, is {o.get('state')}")
        elif expect not in (o.get("reason") or ""):
            problems.append(f"{coid} rejected for the wrong reason: {o.get('reason')}")

    # THE ONE ORDER THE TWO RUNS DISAGREE ABOUT. Sized at 1.2x the capital in notional:
    # past an unlevered ceiling, comfortably inside a 2x one.
    oc = orders.get("over-cash", {})
    if levered:
        if oc.get("state") != "filled":
            problems.append(f"a {args.leverage:g}x book should have filled over-cash, "
                            f"it is {oc.get('state')} — {oc.get('reason')}")
        # ...and the money is really borrowed: cash goes NEGATIVE, bounded by the ceiling.
        if r["cash"] is not None and r["cash"] >= 0:
            problems.append("a levered fill left the cash positive; nothing was borrowed")
    else:
        if oc.get("state") != "rejected" or over not in (oc.get("reason") or ""):
            problems.append(f"an unlevered book must refuse over-cash, it is "
                            f"{oc.get('state')}")

    # 5 + 5 - 3 = 7 shares before over-cash, and the cash must have moved by those trades.
    held = r["units"].get(SYMBOL)
    if not levered and held != 7:
        problems.append(f"expected 7 {SYMBOL}, holding {held}")
    if levered and (held or 0) <= 7:
        problems.append(f"the levered fill never reached the book: holding {held}")
    if r["cash"] is not None and r["cash"] >= CAPITAL:
        problems.append("cash did not fall when shares were bought")

    if len(r["record_fills"]) != r["fills"]:
        problems.append(f"{r['fills']} fills happened but {len(r['record_fills'])} "
                        f"reached paper.db")
    if any(f["symbol"] != SYMBOL for f in r["record_fills"]):
        problems.append("a fill reached the record without its symbol, which weakens "
                        "the deduplication key")

    if problems:
        print("\n  MEMBER ORDER PATH IS BROKEN:")
        for p in problems:
            print(f"    - {p}")
        raise SystemExit(1)

    print(f"\n  order path works: ledger -> running trader -> fill -> book -> record."
          f"\n  {r['applied']} orders applied, {r['rejected']} refused with a reason.")


if __name__ == "__main__":
    main()
