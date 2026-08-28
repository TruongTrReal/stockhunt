"""Gate: a symbol in NO universe is resolved, admitted, filled and recorded.

`test_member_desk.py` proves the order path for a symbol the desk was configured with.
This proves the other half — the one added on 2026-08-28 — for a symbol it was not:

    a registration naming ARKK, which is in no leg of `paper_config.UNIVERSE`
      -> DeskController resolves it against the vendor (country=United States pinned)
      -> `paper_config.admit` puts it on a leg, the cache and the running venue
      -> a MemberStrategy attaches to a RUNNING trader on an instrument built at runtime
      -> an order drains, fills from a bar, and moves cash and units
      -> the fill lands in paper.db with its symbol

and, in the same run, the refusal that matters more than the fill:

    a registration naming CTRA
      -> Twelve Data has no US listing for it
      -> rejected, with a reason naming that, BEFORE any instrument exists

CTRA is the case the whole guard is named after. Unpinned, the vendor answers it with
Ciputra Development Tbk PT on the Indonesia Stock Exchange — 6,405 bars of rupiah that
pass every structural check in this repo and once ranked as the 3rd largest US stock of
2026. A desk that accepts any string a member types reopens that hole one registration at
a time, so the refusal is a gate condition and not a nicety.

**It talks to Twelve Data by default, on purpose.** The identity guard is a claim about
what a vendor does, and a stubbed vendor can only prove that this file agrees with itself.
`--offline` substitutes recorded answers for the two probes when there is no key.

**The bars are synthetic, and that is a property rather than a shortcut.** An
out-of-universe symbol has no cache by definition, which is also why
`desk_control._affordability_caveat` must survive finding nothing — it does, and this run
exercises it.

Run from this directory::

    ..\\.venv\\Scripts\\python test_open_symbol_desk.py
    ..\\.venv\\Scripts\\python test_open_symbol_desk.py --offline

Nonzero exit means the open-symbol path is broken. It writes only to temporary files —
the real `paper.db` and `desk.db` are never touched.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

import paper_config                                                     # noqa: F401

# Both stores redirected before anything opens them, and through the ENVIRONMENT for the
# reason `test_member_desk.py` spells out at length: `ImportableControllerConfig` imports
# this file a second time to resolve the controller, so a plain `mkdtemp()` would mint a
# second directory and the second copy would repoint `store` at an empty database.
_TMP = Path(os.environ.setdefault(
    "STOCKHUNT_OPENSYM_TMP", tempfile.mkdtemp(prefix="stockhunt-opensym-")))

import store                                                            # noqa: E402
store.DB_PATH = _TMP / "paper.db"

import paper_state                                                      # noqa: E402
paper_state.STATE_PATH = _TMP / "paper_state.json"
paper_state.MIRROR_PATH = None                    # publish nowhere

import symbol_resolve                                                   # noqa: E402
# The verdict cache goes to the temp directory too. Reading the desk's real one would let
# a previous run's answer stand in for this run's probe, which is the one thing a gate
# about asking the vendor must not do.
symbol_resolve.CACHE_PATH = _TMP / "symbol_probe.json"
symbol_resolve._cache = None

from stockhunt import deskdb                                            # noqa: E402
deskdb.use(_TMP / "desk.db")

import td_nautilus                                                      # noqa: E402
import venue_instruments                                                # noqa: E402
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

# In no leg of `UNIVERSE` and in no research universe: `ETF_TOP10` is what the desk holds
# and `ARKK` is not in it. Chosen over PLTR and LTC/USD, which the 2026-08-28 widening
# brought INSIDE the pinned legs and which therefore no longer test this path at all.
OPEN_SYMBOL = "ARKK"
# The impostor. Twelve Data has no US listing for it and answers with an Indonesian
# developer when the country pin is dropped.
IMPOSTOR = "CTRA"
CLS = "us_etfs"
VENUE = "SANDBOX"
CAPITAL = 10_000.0
ACCOUNT = "b3"
NAME = "openbook"
REG_ID = f"str_{ACCOUNT}_{NAME}"
BAD_NAME = "impostorbook"
BAD_REG_ID = f"str_{ACCOUNT}_{BAD_NAME}"

# What the vendor answered on 2026-08-28, for `--offline`. Recorded rather than invented,
# so the offline run asserts against the shape the live one really returns.
RECORDED = {
    OPEN_SYMBOL: {"symbol": "ARKK", "name": "ARK Innovation ETF", "exchange": "CBOE",
                  "mic_code": "BATS", "currency": "USD", "close": "87.38000"},
    IMPOSTOR: {"status": "error", "code": 404,
               "message": "**symbol** or **figi** parameter is missing or invalid."},
}


class ScriptedConfig(DeskControllerConfig, frozen=True):
    bar_type: str = ""
    place_at_bar: int = 0


class ScriptedController(DeskController):
    """The real controller, driven by bars instead of by a wall clock.

    Identical in intent to `test_member_desk.ScriptedController`: a TestClock and a
    LiveClock disagree about when a timer fires, and a gate that depends on that is
    testing Nautilus's scheduler. Everything under `tick()` is production code.
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
            deskdb.submit_order(ACCOUNT, REG_ID, "buy-4", symbol=OPEN_SYMBOL,
                                side="buy", qty=4, order_type="market")
            deskdb.submit_order(ACCOUNT, REG_ID, "sell-1", symbol=OPEN_SYMBOL,
                                side="sell", qty=1, order_type="market")
        if self.bars_seen >= self.config.place_at_bar:
            self.tick()


def synthetic_bars(n: int = 60) -> pd.DataFrame:
    """A deterministic random walk. An open symbol has no cache, which is the point."""
    rng = np.random.default_rng(20260828)
    close = 80.0 + np.cumsum(rng.normal(0.0, 0.8, n))
    return pd.DataFrame(
        {"Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close,
         "Volume": np.full(n, 5.0e6)},
        index=pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")).round(2)


def run(bars_limit: int, place_at: int, log_level: str, offline: bool) -> dict:
    if offline:
        symbol_resolve._vendor_quote = lambda sym, country=None: RECORDED.get(sym, {})

    df = synthetic_bars(bars_limit)
    inst = td_nautilus.instrument_for(OPEN_SYMBOL, CLS, VENUE)
    bar_type = BarType.from_str(f"{inst.id}-{paper_config.BAR_SPEC['1d']}")

    deskdb.register(ACCOUNT, NAME, CLS, [OPEN_SYMBOL], "1d", CAPITAL, kind="member")
    deskdb.register(ACCOUNT, BAD_NAME, CLS, [IMPOSTOR], "1d", CAPITAL, kind="member")

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("OPENSYM-001"),
        logging=LoggingConfig(bypass_logging=log_level == "OFF", log_level=log_level),
        controller=ImportableControllerConfig(
            controller_path="test_open_symbol_desk:ScriptedController",
            config_path="test_open_symbol_desk:ScriptedConfig",
            config={"bar_type": str(bar_type), "place_at_bar": place_at,
                    "tick_seconds": 30, "export_state": True}),
    ))
    engine.add_venue(venue=Venue(VENUE), oms_type=OmsType.NETTING,
                     account_type=AccountType.CASH, base_currency=USD,
                     starting_balances=[Money(CAPITAL * 10, USD)])

    # **What the desk does at runtime, recorded rather than assumed.**
    # `_admit_open` calls `venue_instruments.publish`, which in the live desk is
    # `SandboxExecutionClient.exchange.add_instrument`. A `BacktestEngine` exposes no
    # route to its `SimulatedExchange` — `add_instrument` is the public door to the same
    # call — so that is what is registered here, wrapped so the gate can assert the desk
    # went through it.
    published: list[str] = []

    def publish(instrument) -> None:
        published.append(str(instrument.id))
        engine.add_instrument(instrument)

    venue_instruments.clear()
    venue_instruments.register(VENUE, publish)

    # The harness's own instrument, and it is a harness limitation rather than the desk's
    # path: `BacktestEngine.add_data` refuses data for an instrument the cache has never
    # seen, and every bar of a backtest has to be handed over before the run begins. A
    # live desk has no such moment — the bars arrive after the registration does, which is
    # the whole reason this path exists. The desk's own `cache.add_instrument` and its
    # publish above are therefore idempotent here and load-bearing in production.
    engine.add_instrument(inst)
    engine.add_data(build_bars(df, bar_type, price_precision=2, size_precision=0))
    engine.run()

    controller = engine.kernel._controller
    strat = controller._running.get(REG_ID) if controller else None
    out = {
        "bars": len(df),
        "attached": isinstance(strat, MemberStrategy),
        "units": dict(strat._units) if strat else {},
        "cash": strat._cash if strat else None,
        "fills": strat._n_fills if strat else 0,
        "published": list(published),
        "orders": {o["client_order_id"]: o for o in deskdb.orders(ACCOUNT)},
        "good": deskdb.registration(REG_ID),
        "bad": deskdb.registration(BAD_REG_ID),
        "record_fills": store.recent_fills(store.sid_for(ACCOUNT, NAME)),
        "admitted": paper_config.open_symbols(),
    }
    engine.dispose()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bars", type=int, default=60)
    ap.add_argument("--place-at", type=int, default=40)
    ap.add_argument("--log-level", default="OFF")
    ap.add_argument("--offline", action="store_true",
                    help="use the recorded vendor answers instead of asking Twelve Data")
    args = ap.parse_args()

    problems = []
    # Asserted BEFORE the run, because the whole gate is meaningless if the symbol turns
    # out to be pinned: it would then be testing `test_member_desk.py` again.
    if OPEN_SYMBOL in paper_config.CLASS_OF:
        problems.append(f"{OPEN_SYMBOL} is in the desk's pinned universe, so this gate is "
                        f"no longer testing the open path — pick a symbol that is not")
    if IMPOSTOR in paper_config.CLASS_OF:
        problems.append(f"{IMPOSTOR} is in the desk's pinned universe; the identity guard "
                        f"cannot be exercised through it")
    if problems:
        print("\n  THE GATE'S OWN PREMISE IS BROKEN:")
        for p in problems:
            print(f"    - {p}")
        raise SystemExit(1)

    r = run(args.bars, args.place_at, args.log_level, args.offline)
    orders = r["orders"]

    print(f"\n  {OPEN_SYMBOL} {CLS} 1d, {r['bars']} synthetic bars, orders at bar "
          f"{args.place_at}{'  [offline]' if args.offline else '  [live vendor]'}")
    print(f"  admitted: {r['admitted']}")
    print(f"  published to the venue: {r['published']}")
    print(f"  registration: {r['good']['state']}  strategy attached: {r['attached']}")
    print(f"  book: cash {r['cash']:,.2f}  units {r['units']}  fills {r['fills']}")
    print(f"  impostor {IMPOSTOR}: {r['bad']['state']}")
    print(f"    reason: {r['bad']['reason']}")
    print("\n  ledger:")
    for coid, o in orders.items():
        reason = f"  — {o['reason']}" if o["reason"] else ""
        print(f"    {coid:<10} {o['state']:<10} filled {o['filled_qty']:g}{reason}")

    if r["admitted"].get(OPEN_SYMBOL) != CLS:
        problems.append(f"{OPEN_SYMBOL} was never admitted to the {CLS} leg")
    if f"{OPEN_SYMBOL}.{VENUE}" not in r["published"]:
        problems.append(f"the desk never published {OPEN_SYMBOL} to the running venue, so "
                        f"a live SimulatedExchange would raise `No matching engine found` "
                        f"inside a handler that swallows it")
    if not r["attached"]:
        problems.append("the controller never attached the member strategy")
    if r["good"]["state"] != "live":
        problems.append(f"registration is {r['good']['state']}, expected live")

    for coid in ("buy-4", "sell-1"):
        if orders.get(coid, {}).get("state") != "filled":
            problems.append(f"{coid} should have filled, is "
                            f"{orders.get(coid, {}).get('state')}")
    if r["units"].get(OPEN_SYMBOL) != 3:
        problems.append(f"expected 3 {OPEN_SYMBOL} after 4 - 1, holding "
                        f"{r['units'].get(OPEN_SYMBOL)}")
    if r["cash"] is not None and r["cash"] >= CAPITAL:
        problems.append("cash did not fall when shares were bought")
    if len(r["record_fills"]) != r["fills"]:
        problems.append(f"{r['fills']} fills happened but {len(r['record_fills'])} "
                        f"reached paper.db")
    if any(f["symbol"] != OPEN_SYMBOL for f in r["record_fills"]):
        problems.append("a fill reached the record without its symbol")

    # The refusal, and it is checked as strictly as the fill. A gate that only proves the
    # door opens has not tested a door.
    if r["bad"]["state"] != "rejected":
        problems.append(f"{IMPOSTOR} has no US listing on this vendor and should have "
                        f"been refused; the registration is {r['bad']['state']}")
    elif "no US listing" not in (r["bad"]["reason"] or ""):
        problems.append(f"{IMPOSTOR} was refused for the wrong reason: "
                        f"{r['bad']['reason']}")
    if IMPOSTOR in paper_config.CLASS_OF:
        problems.append(f"{IMPOSTOR} was refused and admitted to the desk anyway")

    if problems:
        print("\n  THE OPEN-SYMBOL PATH IS BROKEN:")
        for p in problems:
            print(f"    - {p}")
        raise SystemExit(1)

    print(f"\n  open symbols work: ledger -> resolve -> admit -> running trader -> fill "
          f"-> book -> record."
          f"\n  and {IMPOSTOR}, which is a different company wearing the ticker, never "
          f"got an instrument.")


if __name__ == "__main__":
    main()
