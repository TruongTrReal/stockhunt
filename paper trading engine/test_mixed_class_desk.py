"""Gate: ONE registration holding TWO asset classes fills on TWO venues.

`test_member_desk.py` proves the order path for a book on one class and one venue.
`test_open_symbol_desk.py` proves it for a symbol the desk was not configured with. This
proves the third thing, added 2026-08-29: that a registration is a portfolio and is no
longer confined to a single class.

    a registration naming SPY (an ETF) and BTC/USD (a coin), filed under `us_stocks`
      -> DeskController resolves each symbol's OWN class
      -> a MemberStrategy attaches with a per-symbol class map
      -> SPY is built as a whole-share Equity on SANDBOX
         BTC/USD is built as a fractional CurrencyPair on BINANCE
      -> orders on both drain, fill from their own venue's bars, and move ONE pot of cash
      -> both fills land in paper.db with their symbols

**Two venues is the whole point and it is what nothing else here tests.** A `cls` on the
registration used to decide the venue, the instrument shape and — through the venue —
which VENDOR Nautilus routes the subscription to. So the failure a single-class gate
cannot see is not an exception: it is `BTC/USD` built as a whole-share `Equity` on
`SANDBOX`, whose `make_qty` rounds 0.15 BTC to zero, on a venue whose bars come from the
wrong feed. Everything logs healthy.

**The cash is one pot across both venues**, which is the other property worth proving end
to end: `MemberStrategy._cash` is the book, the venue accounts are only the sandbox's
bookkeeping behind the fills, and a buy on BINANCE has to reduce the same balance a buy on
SANDBOX did.

Run from this directory::

    ..\\.venv\\Scripts\\python test_mixed_class_desk.py

Nonzero exit means the mixed-class path is broken. Bars are synthetic and both stores are
redirected, so the real `paper.db` and `desk.db` are never touched.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

import paper_config                                                     # noqa: F401

# Redirected through the ENVIRONMENT before anything opens them, for the reason
# `test_member_desk.py` spells out: `ImportableControllerConfig` imports this file a second
# time to resolve the controller, so a plain `mkdtemp()` would mint a second directory and
# the second copy would repoint `store` at an empty database.
_TMP = Path(os.environ.setdefault(
    "STOCKHUNT_MIXEDCLS_TMP", tempfile.mkdtemp(prefix="stockhunt-mixedcls-")))

import store                                                            # noqa: E402
store.DB_PATH = _TMP / "paper.db"

import paper_state                                                      # noqa: E402
paper_state.STATE_PATH = _TMP / "paper_state.json"
paper_state.MIRROR_PATH = None                    # publish nowhere

from stockhunt import deskdb                                            # noqa: E402
deskdb.use(_TMP / "desk.db")

import desk_control                                                     # noqa: E402
import td_nautilus                                                      # noqa: E402
from backtest_paper import build_bars                                   # noqa: E402
from desk_control import DeskController, DeskControllerConfig           # noqa: E402
from member_strategy import MemberStrategy                              # noqa: E402

from nautilus_trader.backtest.engine import BacktestEngine              # noqa: E402
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig  # noqa: E402
from nautilus_trader.model.currencies import USD                        # noqa: E402
from nautilus_trader.model.data import Bar, BarType                     # noqa: E402
from nautilus_trader.model.enums import AccountType, OmsType            # noqa: E402
from nautilus_trader.model.identifiers import TraderId, Venue           # noqa: E402
from nautilus_trader.model.objects import Money                         # noqa: E402
from nautilus_trader.trading.config import ImportableControllerConfig   # noqa: E402

# Both are on PINNED legs, and deliberately: this gate is about the CLASS being per symbol,
# not about resolving an unknown one — `test_open_symbol_desk.py` owns that half, and
# mixing the two would make a failure here ambiguous between them.
#
# SPY is on the `us_etfs` leg (it moved there in the 2026-08-28 widening, because
# `wf_summary_us_etfs_*` is the sheet that scores it) while the registration is filed under
# `us_stocks`. That is not incidental — it means even the "equity" half of this book has a
# class the registration did not declare, so a `cls`-driven lookup cannot pass by accident.
EQUITY = "SPY"
COIN = "BTC/USD"
HOME_CLS = "us_stocks"          # the book's home leg: what the board files it under
CAPITAL = 100_000.0             # enough to buy a whole coin at the synthetic price
ACCOUNT = "c9"
NAME = "mixedbook"
REG_ID = f"str_{ACCOUNT}_{NAME}"


class ScriptedConfig(DeskControllerConfig, frozen=True):
    bar_type: str = ""
    place_at_bar: int = 0


class ScriptedController(DeskController):
    """The real controller, driven by bars instead of by a wall clock.

    Same construction as the other two gates: a TestClock and a LiveClock disagree about
    when a timer fires, and a gate that depends on that is testing Nautilus's scheduler.
    Everything under `tick()` is production code.

    It subscribes to the EQUITY bar and counts on that alone, because the two synthetic
    series share an index — one counter for both venues, and the orders are written once.
    """

    def __init__(self, config: ScriptedConfig, trader) -> None:
        super().__init__(config=config, trader=trader)
        self.bars_seen = 0
        self.scripted = False

    def on_start(self) -> None:
        deskdb.connect()
        self.tick()
        self.subscribe_bars(BarType.from_str(self.config.bar_type))

    def on_bar(self, bar: Bar) -> None:
        self.bars_seen += 1
        if self.bars_seen == self.config.place_at_bar and not self.scripted:
            self.scripted = True
            # One order per venue, and a fractional size on the coin. The fraction is the
            # part a whole-share instrument silently destroys: `Equity.make_qty(0.25)`
            # rounds to zero and `MemberStrategy.place` refuses it with "rounds to zero at
            # this instrument's size increment" — a refusal that reads like a sizing
            # mistake rather than like the instrument being the wrong shape.
            deskdb.submit_order(ACCOUNT, REG_ID, "buy-equity", symbol=EQUITY,
                                side="buy", qty=10, order_type="market")
            deskdb.submit_order(ACCOUNT, REG_ID, "buy-coin", symbol=COIN,
                                side="buy", qty=0.25, order_type="market")
            deskdb.submit_order(ACCOUNT, REG_ID, "sell-coin", symbol=COIN,
                                side="sell", qty=0.1, order_type="market")
            # ...and a refusal that has to name the right book. A symbol on neither leg is
            # not one of this strategy's symbols whatever class it belongs to.
            deskdb.submit_order(ACCOUNT, REG_ID, "wrong-symbol", symbol="TSLA",
                                side="buy", qty=1, order_type="market")
        if self.bars_seen >= self.config.place_at_bar:
            self.tick()


def synthetic_bars(n: int, start: float, step: float, seed: int) -> pd.DataFrame:
    """A deterministic random walk. Two of these, on one index, at two price scales."""
    rng = np.random.default_rng(seed)
    close = start + np.cumsum(rng.normal(0.0, step, n))
    return pd.DataFrame(
        {"Open": close, "High": close + step, "Low": close - step, "Close": close,
         "Volume": np.full(n, 5.0e6)},
        index=pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC"))


def run(bars_limit: int, place_at: int, log_level: str) -> dict:
    # THE INSTRUMENTS, each built from its OWN class. This is the production dispatcher —
    # `td_nautilus.instrument_for` — asked twice, which is exactly what `MemberStrategy`
    # now does per symbol and what it could not do while the class came off the row.
    equity_cls = paper_config.class_of(EQUITY)          # us_etfs, not us_stocks
    coin_cls = paper_config.class_of(COIN)              # crypto
    equity_venue = paper_config.VENUES[equity_cls]      # SANDBOX
    coin_venue = paper_config.VENUES[coin_cls]          # BINANCE
    equity_inst = td_nautilus.instrument_for(EQUITY, equity_cls, equity_venue)
    coin_inst = td_nautilus.instrument_for(COIN, coin_cls, coin_venue)

    equity_bt = BarType.from_str(f"{equity_inst.id}-{paper_config.BAR_SPEC['1d']}")
    coin_bt = BarType.from_str(f"{coin_inst.id}-{paper_config.BAR_SPEC['1d']}")

    equity_df = synthetic_bars(bars_limit, 500.0, 3.0, 20260829).round(2)
    # Rounded to the coin instrument's own precision, because the exchange REJECTS a bar
    # whose precision differs from its instrument's rather than rounding it.
    coin_df = synthetic_bars(bars_limit, 60_000.0, 400.0, 20260830).round(
        coin_inst.price_precision)

    deskdb.register(ACCOUNT, NAME, HOME_CLS, [EQUITY, COIN], "1d", CAPITAL,
                    kind="member")

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("MIXEDCLS-001"),
        logging=LoggingConfig(bypass_logging=log_level == "OFF", log_level=log_level),
        controller=ImportableControllerConfig(
            controller_path="test_mixed_class_desk:ScriptedController",
            config_path="test_mixed_class_desk:ScriptedConfig",
            config={"bar_type": str(equity_bt), "place_at_bar": place_at,
                    "tick_seconds": 30, "export_state": True}),
    ))
    # TWO venues, and they differ in shape as well as in name. The pair venue is left
    # MULTI-CURRENCY (no `base_currency`) for the reason `run_paper.build_node` records: a
    # CurrencyPair trade converts USD into the base asset, and an account that cannot hold
    # BTC fills one size increment and stops — `BUY 0.014691 BTCUSD` came back as
    # `last_qty=0.000001` and every crypto position was reported at a millionth of its size.
    engine.add_venue(venue=Venue(equity_venue), oms_type=OmsType.NETTING,
                     account_type=AccountType.CASH, base_currency=USD,
                     starting_balances=[Money(CAPITAL * 10, USD)])
    engine.add_venue(venue=Venue(coin_venue), oms_type=OmsType.NETTING,
                     account_type=AccountType.CASH,
                     starting_balances=[Money(CAPITAL * 10, USD)])

    engine.add_instrument(equity_inst)
    engine.add_instrument(coin_inst)
    engine.add_data(build_bars(equity_df, equity_bt, price_precision=2,
                               size_precision=0))
    engine.add_data(build_bars(coin_df, coin_bt,
                               price_precision=coin_inst.price_precision,
                               size_precision=coin_inst.size_precision))
    engine.run()

    controller = engine.kernel._controller
    strat = controller._running.get(REG_ID) if controller else None
    out = {
        "bars": len(equity_df),
        "attached": isinstance(strat, MemberStrategy),
        "classes": strat.classes() if strat else [],
        "venues": strat.venues() if strat else [],
        "instrument_ids": {s: str(strat._instrument_id(s)) for s in (EQUITY, COIN)}
                          if strat else {},
        "units": dict(strat._units) if strat else {},
        "cash": strat._cash if strat else None,
        "fills": strat._n_fills if strat else 0,
        "orders": {o["client_order_id"]: o for o in deskdb.orders(ACCOUNT)},
        "registration": deskdb.registration(REG_ID),
        "record_fills": store.recent_fills(store.sid_for(ACCOUNT, NAME)),
        "published": paper_state._strategies.get(store.sid_for(ACCOUNT, NAME), {}),
    }
    engine.dispose()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bars", type=int, default=60)
    ap.add_argument("--place-at", type=int, default=40)
    ap.add_argument("--log-level", default="OFF")
    args = ap.parse_args()

    problems = []
    # Asserted BEFORE the run: the gate is meaningless if the two symbols turn out to share
    # a class, because then it is `test_member_desk.py` again with a longer symbol list.
    if paper_config.CLASS_OF.get(EQUITY) == paper_config.CLASS_OF.get(COIN):
        problems.append(f"{EQUITY} and {COIN} are on the same leg, so this gate is no "
                        f"longer testing a mixed-class book")
    if paper_config.CLASS_OF.get(EQUITY) == HOME_CLS:
        problems.append(f"{EQUITY} is on the {HOME_CLS} leg, so a lookup that ignored the "
                        f"symbol and read the registration's class would pass anyway")
    if problems:
        print("\n  THE GATE'S OWN PREMISE IS BROKEN:")
        for p in problems:
            print(f"    - {p}")
        raise SystemExit(1)

    r = run(args.bars, args.place_at, args.log_level)
    orders = r["orders"]

    print(f"\n  one book: {EQUITY} + {COIN}, registered under {HOME_CLS}, "
          f"{r['bars']} synthetic bars, orders at bar {args.place_at}")
    print(f"  registration: {r['registration']['state']}  attached: {r['attached']}")
    print(f"  classes held: {r['classes']}")
    print(f"  venues:       {r['venues']}")
    print(f"  instruments:  {r['instrument_ids']}")
    print(f"  book: cash {r['cash']:,.2f}  units {r['units']}  fills {r['fills']}")
    print(f"  desk says:    {r['registration']['reason'] or '(nothing)'}")
    print("\n  ledger:")
    for coid, o in orders.items():
        reason = f"  — {o['reason']}" if o["reason"] else ""
        print(f"    {coid:<14} {o['state']:<10} filled {o['filled_qty']:g}{reason}")

    if not r["attached"]:
        problems.append("the controller never attached the member strategy")
    if r["registration"]["state"] != "live":
        problems.append(f"registration is {r['registration']['state']}, expected live")

    # THE PROPERTY. Two classes, two venues, two instrument shapes, from one row.
    if r["classes"] != ["crypto", "us_etfs"]:
        problems.append(f"the book reports classes {r['classes']}, expected "
                        f"['crypto', 'us_etfs'] — the class is supposed to come off each "
                        f"SYMBOL, not off the registration's `cls`")
    if r["venues"] != ["BINANCE", "SANDBOX"]:
        problems.append(f"the book settles on {r['venues']}, expected "
                        f"['BINANCE', 'SANDBOX']")
    if r["instrument_ids"].get(COIN) != "BTCUSD.BINANCE":
        problems.append(f"{COIN} was built as {r['instrument_ids'].get(COIN)} — a coin on "
                        f"the equity venue is a whole-share instrument fed by the wrong "
                        f"client, and it fails by rounding to zero rather than by raising")
    if r["instrument_ids"].get(EQUITY) != "SPY.SANDBOX":
        problems.append(f"{EQUITY} was built as {r['instrument_ids'].get(EQUITY)}")

    for coid in ("buy-equity", "buy-coin", "sell-coin"):
        if orders.get(coid, {}).get("state") != "filled":
            problems.append(f"{coid} should have filled, is "
                            f"{orders.get(coid, {}).get('state')} — "
                            f"{orders.get(coid, {}).get('reason')}")
    if orders.get("wrong-symbol", {}).get("state") != "rejected":
        problems.append("a symbol on neither leg must still be refused")

    # A FRACTIONAL size survived, which a whole-share instrument would have destroyed.
    held_coin = r["units"].get(COIN)
    if held_coin is None or abs(held_coin - 0.15) > 1e-6:
        problems.append(f"expected 0.15 {COIN} after 0.25 - 0.1, holding {held_coin}. A "
                        f"whole-share instrument rounds a fractional size to zero and the "
                        f"refusal reads as a sizing mistake")
    if r["units"].get(EQUITY) != 10:
        problems.append(f"expected 10 {EQUITY}, holding {r['units'].get(EQUITY)}")

    # ONE POT. Both venues spent the same cash balance, so the book is down by the cost of
    # both legs rather than by one of them.
    if r["cash"] is None or r["cash"] >= CAPITAL:
        problems.append("cash did not fall; the two venues are not sharing one book")

    if len(r["record_fills"]) != r["fills"]:
        problems.append(f"{r['fills']} fills happened but {len(r['record_fills'])} "
                        f"reached paper.db")
    got = {f["symbol"] for f in r["record_fills"]}
    if got != {EQUITY, COIN}:
        problems.append(f"the record holds fills for {sorted(got)}; both symbols must be "
                        f"there, and with their own names — the fills table's natural key "
                        f"includes the symbol")

    # The board files a mixed book under its HOME leg and says what else it holds. `mixed`
    # was the obvious alternative and is worse: the dashboard's class filter is built from
    # the five research classes, so a sixth value puts the book in no pill at all.
    published = r["published"]
    if published.get("cls") != HOME_CLS:
        problems.append(f"published cls is {published.get('cls')!r}; a mixed book is filed "
                        f"under its home leg so the board's grouping stays stable")
    if sorted(published.get("classes") or []) != ["crypto", "us_etfs"]:
        problems.append(f"the published record does not name what the book actually holds "
                        f"({published.get('classes')!r}), so filing it under one leg would "
                        f"be a grouping with nothing disclosing it")
    if "crypto" not in (published.get("note") or ""):
        problems.append("the note does not say the book is mixed")

    if problems:
        print("\n  THE MIXED-CLASS PATH IS BROKEN:")
        for p in problems:
            print(f"    - {p}")
        raise SystemExit(1)

    print(f"\n  mixed classes work: one registration -> two classes -> two venues -> "
          f"two instrument shapes -> one book -> the record."
          f"\n  {r['fills']} fills across {len(r['venues'])} venues out of one pot of cash.")


if __name__ == "__main__":
    main()
