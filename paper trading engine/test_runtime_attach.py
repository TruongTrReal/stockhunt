"""Gate: a strategy can be added to, and removed from, a node that is already running.

This is the question the whole manager-desk design turned on. If a strategy can only be
declared before `node.build()`, then registering one — a manager's, or a rule promoted off
the walk-forward sheet — means restarting the desk and re-warming every system on it. If
it can be attached in place, a registration goes live in seconds.

**The answer is a `Controller`, and it is not optional.** `Trader.add_strategy` contains:

    if self.is_running and not self._has_controller:
        self._log.error("Cannot add a strategy to a running trader")
        return

Note that it *returns* rather than raising. Without a controller the call is a silent
no-op: the log carries one line, the strategy never trades, and nothing else says so.
`_has_controller` is fixed at Trader construction from `NautilusKernelConfig.controller`,
so it cannot be switched on afterwards — the node must be built with a controller from
the start, whether or not anything is attached later.

Removal and stop/start carry no such guard and work on a running trader either way.

Run from this directory::

    ..\\.venv\\Scripts\\python test_runtime_attach.py
    ..\\.venv\\Scripts\\python test_runtime_attach.py --symbol SPY --bars 1400

Nonzero exit means runtime attach does not work on this Nautilus build and the desk must
fall back to applying registrations at the next restart. Nothing else in the design
changes if that happens — see `desk_control.apply_pending`.

It runs offline on cached bars and writes nothing: `export_state=False` keeps
`paper_state` and `results/paper.db` out of it entirely, so this gate can never touch the
forward-test record.
"""

from __future__ import annotations

import argparse

import paper_config                        # noqa: F401  (wires sys.path)
from backtest_paper import (BAR_SPEC, CAPITAL, VENUE, build_bars, load_bars,
                            make_instrument, safe)
from strategy import TalibRuleConfig, TalibRuleStrategy

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money
# `ControllerConfig` and `ImportableControllerConfig` live in different modules —
# the first with the live node config, the second beside the strategy configs.
from nautilus_trader.live.config import ControllerConfig
from nautilus_trader.trading.config import ImportableControllerConfig
from nautilus_trader.trading.controller import Controller

# The controller is built by `ControllerFactory` from a string path — `resolve_path`
# IMPORTS this module to find the class. Running the file directly therefore loads it
# twice: once as `__main__` and once as `test_runtime_attach`, two module objects with
# two sets of globals. A module-level results list is written by one and read by the
# other, and the gate reports "never attached" while the attach is working perfectly.
#
# So everything the gate asserts on lives on the controller INSTANCE, which is one object
# whichever module defined its class, and `main` reaches it through the kernel. Anything
# in `desk_control` that needs to report back has the same constraint.


class AttachControllerConfig(ControllerConfig, frozen=True):
    """Every field is a primitive: msgspec decodes this from the `config` dict."""

    bar_type: str
    instrument_id: str
    rule: str
    timeframe: str
    display_symbol: str
    attach_at_bar: int
    remove_at_bar: int
    min_warmup_bars: int


class AttachController(Controller):
    """Counts bars, attaches a second strategy partway through, removes it later.

    Subclassing `Controller` rather than driving the trader directly is the whole point:
    `Controller.create_strategy` calls the same `Trader.add_strategy` that refuses a
    running trader, and it succeeds only because the kernel set `has_controller=True`
    when it saw this in the config.
    """

    def __init__(self, config: AttachControllerConfig, trader) -> None:
        super().__init__(trader=trader, config=config)
        self._bars_seen = 0
        self._attached: TalibRuleStrategy | None = None
        self._removed = False
        self.timeline: list[str] = []          # read back through the kernel, not a global

    def on_start(self) -> None:
        self.subscribe_bars(BarType.from_str(self.config.bar_type))

    def on_bar(self, bar: Bar) -> None:
        self._bars_seen += 1

        if self._bars_seen == self.config.attach_at_bar and self._attached is None:
            strat = TalibRuleStrategy(config=TalibRuleConfig(
                # Distinct from strategy one's tag. `add_strategy` raises on a duplicate
                # `order_id_tag`, which is the collision the account prefix in the real
                # design has to stay clear of.
                order_id_tag="ATTACHED-2",
                instrument_id=BarType.from_str(self.config.bar_type).instrument_id,
                bar_type=BarType.from_str(self.config.bar_type),
                rule=self.config.rule,
                min_warmup_bars=self.config.min_warmup_bars,
                capital=CAPITAL,
                timeframe=self.config.timeframe,
                display_symbol=self.config.display_symbol,
                export_state=False,       # never touch paper.db from a gate
                note="attached at runtime by the controller"))
            self.create_strategy(strat, start=True)
            self._attached = strat
            self.timeline.append(
                f"bar {self._bars_seen}: attached, trader now holds "
                f"{len(self._trader.strategies())} strategies")

        if (self._bars_seen == self.config.remove_at_bar
                and self._attached is not None and not self._removed):
            fills_before = self._attached._n_fills
            self.remove_strategy(self._attached)
            self._removed = True
            self.timeline.append(
                f"bar {self._bars_seen}: removed after {fills_before} fills, "
                f"trader now holds {len(self._trader.strategies())} strategies")


def run(symbol: str, timeframe: str, bars_limit: int, attach_at: int,
        remove_at: int, log_level: str) -> dict:
    df = load_bars(symbol, timeframe)
    if bars_limit:
        df = df.tail(bars_limit)
    n = len(df)
    if n < attach_at + 400:
        raise SystemExit(f"{symbol} has only {n} bars; need at least {attach_at + 400} so "
                         f"the attached strategy can warm up and still trade")

    inst = make_instrument(symbol)
    bar_type = BarType.from_str(f"{inst.id}-{BAR_SPEC[timeframe]}")
    # The attached strategy warms up from live bars only — it was not present for the
    # earlier ones — so its window has to fit in what is left after the attach point.
    warmup = min(paper_config.MIN_WARMUP_BARS, max((n - attach_at) // 3, 30))

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("ATTACH-001"),
        logging=LoggingConfig(bypass_logging=log_level == "OFF", log_level=log_level),
        controller=ImportableControllerConfig(
            controller_path="test_runtime_attach:AttachController",
            config_path="test_runtime_attach:AttachControllerConfig",
            config={
                "bar_type": str(bar_type),
                "instrument_id": str(inst.id),
                "rule": "SMA_50",
                "timeframe": timeframe,
                "display_symbol": symbol,
                "attach_at_bar": attach_at,
                "remove_at_bar": remove_at,
                "min_warmup_bars": warmup,
            }),
    ))
    engine.add_venue(venue=Venue(VENUE), oms_type=OmsType.NETTING,
                     account_type=AccountType.MARGIN, base_currency=USD,
                     starting_balances=[Money(CAPITAL, USD)])
    engine.add_instrument(inst)
    engine.add_data(build_bars(df, bar_type))

    # Strategy one goes in the ordinary way, before the run. It is the control: if it
    # trades and the attached one does not, the difference is the attach and nothing else.
    baseline = TalibRuleStrategy(config=TalibRuleConfig(
        order_id_tag=f"{safe(symbol)}-BASE",
        instrument_id=inst.id, bar_type=bar_type, rule="SMA_200",
        min_warmup_bars=min(paper_config.MIN_WARMUP_BARS, max(n // 4, 30)),
        capital=CAPITAL, timeframe=timeframe, display_symbol=symbol,
        export_state=False, note="declared before the run"))
    engine.add_strategy(baseline)

    engine.run()

    # The kernel holds the one controller instance the factory built. Reaching it here
    # rather than through a module global is what survives the double import.
    controller = engine.kernel._controller
    attached = controller._attached if controller is not None else None
    out = {
        "bars": n,
        "attach_at": attach_at,
        "remove_at": remove_at,
        "warmup": warmup,
        "baseline_fills": baseline._n_fills,
        "controller_saw_bars": controller._bars_seen if controller else 0,
        "attached_exists": attached is not None,
        "attached_bars": len(attached._bars) if attached else 0,
        "attached_fills": attached._n_fills if attached else 0,
        "attached_running_after_remove": bool(attached and attached.is_running),
        "timeline": list(controller.timeline) if controller is not None else [],
    }
    engine.dispose()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--timeframe", default="1d", choices=list(BAR_SPEC))
    ap.add_argument("--bars", type=int, default=1600)
    ap.add_argument("--attach-at", type=int, default=300)
    ap.add_argument("--remove-at", type=int, default=1500)
    ap.add_argument("--log-level", default="OFF")
    args = ap.parse_args()

    r = run(args.symbol, args.timeframe, args.bars, args.attach_at,
            args.remove_at, args.log_level)

    print(f"\n  {args.symbol} {args.timeframe}, {r['bars']} bars, "
          f"attach at {r['attach_at']}, remove at {r['remove_at']}, "
          f"warmup {r['warmup']}")
    for line in r["timeline"]:
        print(f"    {line}")
    print(f"\n  {'declared before run':<24} {r['baseline_fills']:>4} fills")
    print(f"  {'attached while running':<24} {r['attached_fills']:>4} fills "
          f"over {r['attached_bars']} bars buffered")

    problems = []
    if r["controller_saw_bars"] == 0:
        problems.append("the controller itself received no bars — it was never wired in, "
                        "so nothing below this is a verdict on attaching")
    if not r["attached_exists"]:
        problems.append("the controller never attached a strategy")
    if r["baseline_fills"] == 0:
        problems.append("the BASELINE did not fill — the harness is broken, not the attach")
    if r["attached_bars"] == 0:
        problems.append("the attached strategy received no bars: it was registered but "
                        "its subscription never delivered")
    if r["attached_fills"] == 0:
        problems.append("the attached strategy never filled an order")
    if len(r["timeline"]) < 2:
        problems.append("remove_strategy did not run on the live trader")
    if r["attached_running_after_remove"]:
        problems.append("the removed strategy is still RUNNING — removal does not stop it, "
                        "so a retired registration would keep trading")

    if problems:
        print("\n  RUNTIME ATTACH DOES NOT WORK HERE:")
        for p in problems:
            print(f"    - {p}")
        print("\n  The desk must apply registrations at the next restart instead.")
        raise SystemExit(1)

    print("\n  runtime attach works: a strategy joined a running trader, received bars, "
          "\n  filled orders, and was removed again without stopping the node.")


if __name__ == "__main__":
    main()
