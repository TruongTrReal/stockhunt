"""Paper-trade the forward test: live Twelve Data bars, Nautilus simulated fills.

`SandboxExecutionClient` is the point of this file. It is a real Nautilus execution
client that prices fills from the live data feed instead of sending orders anywhere, so
the portfolio, position and P&L accounting is the *same code* that runs against a real
venue. Going live is a change of execution client in `EXEC_CLIENTS` below and nothing
else — the strategy, the data client and the signal layer are untouched.

    backtest   BacktestNode      + BacktestDataClient  + SimulatedExchange
    paper      TradingNode       + TwelveDataLiveClient + SandboxExecutionClient   <- here
    live       TradingNode       + TwelveDataLiveClient + Binance / IB exec client

**This is plumbing, not a trading recommendation.** Nothing on any of the four walk-forward
sheets clears an acceptance gate, and three of the four are led by rules that are in the
market ~86% of the time — a leaderboard measuring capital deployment, not skill. The job of
a run is to prove that bars arrive, signals compute, orders fill and P&L accrues. Do not
read the P&L as evidence of anything.

**By default the desk runs what has been REGISTERED, and nothing else** — the $100,000
class books promoted from the backtest page, and managers' own strategies. It starts with
no strategies at all and picks them up on its first control tick, which is why an empty
plan is legitimate rather than an error.

The old automatic legs — top three rules per class, one independent $10,000 book per
(symbol, rule, timeframe) — are a DIFFERENT accounting model, and running both put two
things on one board that look alike and are not. `--top 3` brings them back deliberately.

Run::

    python run_paper.py                       # registered books and strategies only
    python run_paper.py --top 3               # ...plus the old per-symbol legs
    python run_paper.py --top 0 --rule SMA_200 --symbols SOXL   # the smoke path
    python run_paper.py --dry-run             # build and validate config, do not connect
"""

from __future__ import annotations

import argparse
import threading
from datetime import datetime, timezone

import paper_config
import desk_control
import desk_orders
import live_ws
import paper_state
import store
import td_live
import td_nautilus
from strategy import TalibRuleConfig, TalibRuleStrategy

from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.config import (ImportableStrategyConfig, LoggingConfig,
                                    TradingNodeConfig)
from nautilus_trader.trading.config import ImportableControllerConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue

VENUES = paper_config.VENUES
# The crypto venue is singled out below when funding accounts: it is the one that must stay
# multi-currency, and `SPOT` has to be treated the same way for the same reason.
PAIR_VENUES = {VENUES[c] for c in paper_config.PAIR_CLASSES}
# Capital per SYSTEM, not per venue. Each venue is then funded with exactly what its
# systems need, so adding a symbol or a rule never shrinks everybody else's book.
#
# The size is not cosmetic. Equities round to whole shares, so a system's book has to be
# large enough that the rounding is a rounding rather than a decision: at $435 a slice,
# MA at $570 rounded 0.72 shares up to 1 and held 131% of its capital, while AAPL rounded
# 1.33 down to 1 and held 75%. At $10,000 those become 16.6 -> 17 and 30.5 -> 31 — a
# fraction of a percent either way, which is tracking error rather than distortion.
CAPITAL_PER_SYSTEM = 10_000.0

BAR_SPEC = paper_config.BAR_SPEC


def instrument_for(symbol: str, asset_class: str | None = None):
    """The Nautilus instrument for a desk symbol, by CLASS rather than by spelling.

    This used to key off `"/" in symbol`, which was exactly right while the desk traded
    equities and crypto and became wrong the moment commodities arrived: `XAU/USD` carries
    the same separator as `BTC/USD` and would have been built as a Binance spot pair — a
    metal quoted on a crypto venue, priced against a book it does not belong to, and
    reported under the wrong class on the dashboard. The class is looked up, not inferred.
    """
    cls = asset_class or paper_config.class_of(symbol)
    venue = VENUES[cls]
    if cls in paper_config.PAIR_CLASSES:
        return td_nautilus.pair_instrument(symbol, venue)
    return td_nautilus.equity_instrument(symbol, venue)


RESULTS = paper_config.WFO_RESULTS
HEADLINE = paper_config.HEADLINE


# `top_rules` and `_same_idea` live in paper_config so the dashboard can select the same
# rules without importing this module, which would drag nautilus_trader in with it.
top_rules = paper_config.top_rules
_same_idea = paper_config._same_idea


def build_node(plan: list[tuple], allow_short: bool, log_level: str,
               capital: float = CAPITAL_PER_SYSTEM) -> tuple[TradingNode, list]:
    """`plan` is a list of (symbol, rule, timeframe, asset_class) rows — the desk to run."""
    classes = {s: cls for s, _, _, cls in plan}
    symbols = sorted(classes)
    instruments = {s: instrument_for(s, classes[s]) for s in symbols}

    # The WHOLE universe, not just what this plan trades — and every venue, not just the
    # ones the plan lands on.
    #
    # `desk_control` attaches registrations to the running node: a promoted rule, or a
    # manager's strategy. Those may name any symbol on the desk's universe, and the
    # instrument has to already be at the venue for the exchange to fill against it —
    # `SandboxExecutionClient` reads this class-level list once, at connect, and there is
    # no way to add to it afterwards. Restricting it to the plan gave a registration that
    # attached, subscribed, received bars and then never filled, which is the worst
    # available failure: everything looks healthy.
    #
    # It is cheap. An instrument is a value object; a *subscription* is what costs a poll
    # task and a warm-up, and those are still created per bar type on demand by whichever
    # strategy asks. Nothing here reaches the vendor.
    # The pinned legs AND every class's book universe. A book on the top 100 holds names
    # that are in no pinned leg, and `SandboxExecutionClient` reads this list once at
    # connect — a name missing from it attaches, subscribes, receives bars and then never
    # fills, which is the worst available failure because everything looks healthy.
    universe = {s: instrument_for(s, cls)
                for cls, syms in paper_config.UNIVERSE.items() for s in syms}
    for cls in paper_config.UNIVERSE:
        try:
            for s in paper_config.book_universe(cls):
                universe.setdefault(s, instrument_for(s, cls))
        except Exception as exc:            # a missing membership table must not stop the
            print(f"  ! cannot resolve the {cls} book universe: {exc}")   # whole desk
    SandboxExecutionClient.INSTRUMENTS = list(universe.values())
    venues = sorted({str(i.id.venue) for i in universe.values()})

    # Split each venue's account across the systems trading on it. Nautilus gives one
    # account per venue, so without this every system sizes against the same balance and
    # they collectively try to deploy N times the capital that exists.
    per_venue = {v: 0 for v in venues}
    for s, _, _, _ in plan:
        per_venue[str(instruments[s].id.venue)] += 1
    capital_for = {v: capital for v in per_venue}
    # Plus headroom for what `desk_control` may attach later. A venue account has to be
    # funded before its client connects and cannot be topped up afterwards, so a member
    # strategy registering onto a venue the plan barely used would find nothing to trade
    # with. Over-funding a SANDBOX account costs nothing and is invisible: every system
    # sizes against its own book (`self._cash`), never the venue balance.
    headroom = desk_control.MAX_MEMBER_STRATEGIES
    funding = {v: capital * (n + headroom) for v, n in per_venue.items()}

    # Plus whatever is REGISTERED. A venue account is funded before its client connects
    # and cannot be topped up afterwards, so the headroom above — sixty systems at the
    # per-system size — has to cover the books too. It does not: twelve $100,000 equity
    # books are $1.2M against $600,000 of headroom, and the shortfall does not announce
    # itself, it just stops filling partway through.
    try:
        from stockhunt import deskdb
        for reg in deskdb.active_registrations():
            venue = paper_config.VENUES.get(reg["cls"])
            if venue in funding:
                funding[venue] += float(reg["capital"])
    except Exception as exc:
        print(f"  ! could not read the ledger to size the venues: {exc}")
    # Doubled, because a book's equity GROWS and Nautilus checks the account balance on
    # every order. A book that doubled would start being refused at exactly the point it
    # was working, which is the least helpful moment for a limit nobody set deliberately.
    funding = {v: f * 2 for v, f in funding.items()}
    print("  capital: " + ", ".join(
        f"{v} ${capital:,.0f} × {n} systems (+{headroom} spare) = ${funding[v]:,.0f}"
        for v, n in per_venue.items()))

    strategies = []
    for symbol, rule, timeframe, asset_class in plan:
        inst = instruments[symbol]
        bar_type = BarType.from_str(f"{inst.id}-{BAR_SPEC[timeframe]}")
        strategies.append(ImportableStrategyConfig(
            strategy_path="strategy:TalibRuleStrategy",
            config_path="strategy:TalibRuleConfig",
            config={
                # `order_id_tag`, NOT `strategy_id`. Through an ImportableStrategyConfig
                # msgspec decodes `strategy_id` into a StrategyId object, which
                # Strategy.__init__ then passes to a `name` parameter typed `str` —
                # TypeError on node build. Nautilus 1.230.0. Supplying the tag instead
                # lets it derive `TalibRuleStrategy-<tag>` itself, which is unique per
                # instrument and per timeframe.
                #
                # The RULE is part of the tag now, not just the instrument: five rules on
                # one symbol would otherwise share an id and Nautilus would reject the
                # duplicate registrations.
                "order_id_tag": f"{inst.id.symbol}-{timeframe}-{rule}"[:36],
                "instrument_id": inst.id,
                "bar_type": bar_type,
                "rule": rule,
                "allow_short": allow_short,
                "window_bars": paper_config.DEFAULT_WINDOW_BARS,
                "capital": capital_for[str(inst.id.venue)],
                # Display and provenance only — never consulted in a trading decision.
                "display_symbol": symbol,
                # The research class name — `us_stocks`, `us_etfs`, `crypto`,
                # `commodities` — not the old two-valued `equity`/`crypto`. It is what the
                # dashboard groups and filters by, and it is also the name of the sheet
                # this rule was selected from, so a system on screen says which leaderboard
                # it came off rather than only what shape of instrument it is.
                "asset_class": asset_class,
                "timeframe": timeframe,
                "note": f"{rule} on {symbol} {timeframe}, live Twelve Data bars into the "
                        f"Nautilus sandbox. Running to prove the pipeline.",
            },
        ))

    config = TradingNodeConfig(
        trader_id="FWD-001",
        logging=LoggingConfig(log_level=log_level,
                              log_directory=str(paper_config.LOG_DIR)),
        # The controller, and it is NOT optional even when nothing is registered.
        #
        # `Trader.add_strategy` refuses a running trader unless `_has_controller`, and
        # that flag is fixed when the Trader is constructed from this config — it cannot
        # be switched on later. So a node built without one can never accept a
        # registration, a promoted rule, or a manager's strategy without a full restart
        # and a re-warm of every system on the desk.
        #
        # Worse, the refusal is a `return`, not a raise: the call is a silent no-op with
        # one line in the log. `test_runtime_attach.py` is the gate that proves this path.
        controller=ImportableControllerConfig(
            controller_path="desk_control:DeskController",
            config_path="desk_control:DeskControllerConfig",
            config={"tick_seconds": desk_control.TICK_SECONDS,
                    "universe_seconds": desk_control.UNIVERSE_SECONDS,
                    "stale_bars": desk_orders.STALE_BARS,
                    "max_member_strategies": desk_control.MAX_MEMBER_STRATEGIES,
                    "export_state": True}),
        data_clients={
            "TWELVEDATA": td_nautilus.TwelveDataDataClientConfig(
                window_bars=paper_config.DEFAULT_WINDOW_BARS),
        },
        exec_clients={
            # `base_currency` is set for the equity venue and deliberately NOT for the
            # pair-quoted ones — crypto, and now commodities, whose `XAU/USD` settles into
            # XAU for exactly the same reason `BTC/USD` settles into BTC.
            # A single-currency USD account can hold the proceeds of an Equity
            # trade, which settles in USD — but a CurrencyPair trade converts USD into the
            # base asset, and an account that cannot hold BTC has nowhere to put it. The
            # exchange does not reject the order; it fills one size increment and stops, so
            # `BUY 0.014691 BTCUSD` came back as `last_qty=0.000001` and every crypto
            # position was reported at a millionth of its true size. Leaving the venue
            # multi-currency lets the balance move into the base asset properly.
            v: SandboxExecutionClientConfig(
                venue=v,
                starting_balances=[f"{funding[v]:.0f} USD"],
                **({} if v in PAIR_VENUES else {"base_currency": "USD"}),
                bar_execution=True,      # fill from bars: no tick feed needed at 1d/4h
            ) for v in venues
        },
        strategies=strategies,
    )

    node = TradingNode(config=config)
    node.add_data_client_factory("TWELVEDATA",
                                 td_nautilus.TwelveDataLiveDataClientFactory)
    for v in venues:
        node.add_exec_client_factory(v, SandboxLiveExecClientFactory)
    node.build()

    # Seed the cache before anything starts. Twelve Data has no instrument-reference
    # endpoint to discover from, and the sandbox execution client only publishes its
    # instruments once it connects — which is after `on_start` runs and asks the cache
    # for them. Without this the strategy finds nothing and stops itself.
    # The whole universe again, for the same reason: a strategy attached later asks the
    # cache for its instrument in `on_start`, and a miss makes it stop itself.
    for inst in universe.values():
        node.kernel.cache.add_instrument(inst)

    route_bars_to_sandbox(node)
    return node, list(instruments.values())


def route_bars_to_sandbox(node: TradingNode) -> int:
    """Deliver bars to the sandbox venue, which its own adapter does not do.

    `SandboxExecutionClient._connect` subscribes to `data.*.{venue}.*`. That pattern fits
    the topic Nautilus uses for quotes and trades — `data.quotes.{venue}.{symbol}` — but
    **not** the one it uses for bars, which is `data.bars.{bar_type}` and renders as
    `data.bars.SOXL.SANDBOX-1-DAY-LAST-EXTERNAL`. There is no dot after the venue there, so
    the subscription never matches and no bar ever reaches `on_data`.

    The consequence is silent and total: with `bar_execution=True` and a bars-only feed,
    the simulated exchange never receives a price, so every order is rejected with
    `no market for <symbol>` and the desk sits flat forever while the logs report healthy
    strategies. It is not a configuration mistake at our end — the adapter cannot fill from
    bars as shipped in 1.230.0.

    Subscribing `data.bars.*` and forwarding by venue is the smallest fix that keeps the
    adapter's own accounting. The venue filter matters: one process runs both SANDBOX and
    BINANCE, and handing a BINANCE bar to the SANDBOX exchange would price an instrument it
    does not own.
    """
    engine = node.kernel.exec_engine
    clients = getattr(engine, "_clients", {})
    wired = 0
    for client in clients.values():
        exchange = getattr(client, "exchange", None)
        if exchange is None:                      # not a sandbox client
            continue
        venue = client.venue

        def route(bar, _client=client, _venue=venue):
            try:
                if bar.bar_type.instrument_id.venue == _venue:
                    _client.on_data(bar)
            except Exception:                     # never let a bad bar kill the feed
                pass

        node.kernel.msgbus.subscribe("data.bars.*", handler=route)
        wired += 1
    print(f"routed bars to {wired} sandbox venue(s) — works around the adapter's "
          f"quote-shaped subscription pattern")
    return wired


def start_marker(symbols: list[str], every: int):
    """Revalue open positions on a timer, in a plain background thread.

    A daemon thread rather than a Nautilus timer on purpose: this is reporting, not
    trading, and it must not be able to touch the event loop, place an order, or delay a
    bar. It reads prices, updates the published state and does nothing else — so the worst
    a failure here can do is leave the dashboard stale.

    One batched `/price` call covers every symbol, so the credit cost is one request per
    interval regardless of how many systems are running.
    """
    if every <= 0:
        return lambda: None
    stop = threading.Event()

    def loop():
        while not stop.wait(every):
            try:
                paper_state.mark(td_live.fetch_prices(symbols))
                paper_state.set_feed(last_bar=datetime.now(timezone.utc)
                                     .strftime("%Y-%m-%d %H:%M UTC"))
                # Written on every pass, not only when something marked. `generated_at` is
                # the only evidence the dashboard has that this process is still breathing,
                # and a status field cannot report a death — a node that dies publishes
                # nothing further, so the page keeps showing whatever it said last. Writing
                # unconditionally makes the timestamp a heartbeat at this cadence: a file
                # older than a few marks means nobody is home, whatever the status claims.
                # It costs one atomic write a minute.
                paper_state.flush(force=True)
            except Exception as exc:
                print(f"mark-to-market failed (will retry): {exc}", flush=True)

    threading.Thread(target=loop, daemon=True, name="mark-to-market").start()
    print(f"marking {len(symbols)} symbols to market every {every}s")
    return stop.set


def build_plan(args) -> list[tuple]:
    """(symbol, rule, timeframe, asset_class) rows to trade.

    Rules are chosen per **class and per timeframe**, because the sheets are separate and
    they disagree: the best crypto rule at 1d is `CORREL`, at 4h it is `T3_200`, and neither
    appears in the other's top three. Carrying one list across both would paper-trade a rule
    on a horizon where the research never ranked it — and carrying one list across classes
    would be worse still, since the commodities sheet is led by `SAREXT` and the equities
    sheet has never ranked it in its top fifty.

    Grouping is by `paper_config.class_of`, so a symbol trades the rules from its own
    leaderboard. Four classes now, and the two new ones are why this cannot be a split on
    the ticker: `GLD` and `SPY` are both ETFs but only one of them is on the ETF leg here,
    and `XAU/USD` and `BTC/USD` are spelled alike and ranked on different sheets.
    """
    # No automatic legs unless asked for. The desk's own top-3-per-class selection is the
    # OLD accounting model — one independent $10,000 book per (symbol, rule, timeframe) —
    # and running it beside the $100,000 class books puts two things on one board that
    # look alike and are not. What the desk runs now is what has been REGISTERED: books
    # promoted off the research, and managers' own strategies.
    #
    # `--top N` brings the automatic legs back, and `--top 0 --rule SMA_200` is still the
    # smoke path: one boring rule everywhere, to prove bars arrive and orders fill.
    if not args.top and not args.rule:
        return []

    by_class: dict[str, list[str]] = {}
    for s in args.symbols:
        by_class.setdefault(paper_config.class_of(s), []).append(s)
    plan = []
    for tf in args.timeframes:
        if not args.top:
            plan += [(s, args.rule, tf, paper_config.class_of(s)) for s in args.symbols]
            continue
        for cls, group in by_class.items():
            for rule in top_rules(cls, args.top, tf):
                plan += [(s, rule, tf, cls) for s in group]
    return plan


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+",
                    default=[s for syms in paper_config.UNIVERSE.values() for s in syms])
    ap.add_argument("--timeframes", nargs="+", default=list(paper_config.BOOK_TIMEFRAMES),
                    choices=list(BAR_SPEC))
    # Empty by default. With no `--rule` and no `--top`, the desk runs only what has been
    # registered — which is the point of the switch on the backtest page.
    ap.add_argument("--rule", default="")
    # Defaults to the top three per class rather than to a single hard-coded rule. The
    # desk's job is to forward-test what the research actually selected, and `--top 0
    # --rule SMA_200` is the smoke-test path — one boring rule, to prove bars arrive and
    # orders fill — not the configuration anyone should be watching.
    ap.add_argument("--top", type=int, default=0,
                    help="trade the top N rules per asset class from the sweep; 0 falls "
                         "back to the single --rule")
    ap.add_argument("--capital", type=float, default=CAPITAL_PER_SYSTEM,
                    help="notional per system; each venue is funded with "
                         "this times the number of systems on it")
    ap.add_argument("--ws-port", type=int, default=live_ws.DEFAULT_PORT,
                    help="port for the browser tick stream (0 disables streaming and "
                         "falls back to --mark-seconds polling)")
    ap.add_argument("--mark-seconds", type=int, default=60,
                    help="how often to revalue open positions (0 disables)")
    ap.add_argument("--allow-short", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and validate the node, then exit without connecting")
    args = ap.parse_args()

    plan = build_plan(args)
    # An EMPTY plan is now legitimate and common: the desk starts with no automatic legs
    # and picks up whatever the ledger holds on its first control tick. Refusing to start
    # would mean a book could never be the first thing on the desk.
    if not plan:
        pending = 0
        try:
            from stockhunt import deskdb
            pending = len([r for r in deskdb.registrations()
                           if r["state"] not in deskdb.REGISTRATION_DONE])
        except Exception:
            pass
        print(f"  no automatic legs — the desk runs what is registered "
              f"({pending} registration(s) waiting). Flip a switch on the backtest page, "
              f"or pass --top 3 for the old per-symbol legs.")

    node, instruments = build_node(plan, args.allow_short, args.log_level,
                                   args.capital)
    print(f"built node: {len(plan)} strategies over {len(instruments)} instruments, "
          f"timeframes {'+'.join(args.timeframes)}, "
          f"warmup {paper_config.DEFAULT_WINDOW_BARS} bars")
    by: dict[tuple, list[str]] = {}
    for s, r, tf, cls in plan:
        by.setdefault((cls, tf, r), []).append(s)
    for (cls, tf, rule), syms in sorted(by.items()):
        print(f"  {cls:<12} {tf:>3}  {rule:<26} {len(syms):>2} × {', '.join(syms[:4])}"
              f"{' …' if len(syms) > 4 else ''}")
    if args.dry_run:
        print("dry run — not connecting")
        node.dispose()
        return

    # Publishing happens HERE, after the dry-run exit, and not before the plan is built.
    #
    # It used to be the first thing `main` did, which made `--dry-run` destructive in two
    # ways that a flag whose whole promise is "validate and exit" should not be. `reset()`
    # blanks `paper_state.json` — recoverable, since the database is the record and the JSON
    # is only a projection of it — and it also opens a `sessions` row that only the `finally`
    # block below ever closes, which the dry-run path returns before reaching. An unclosed
    # session is worse than a blank projection: downtime is measured between sessions, so a
    # row that starts and never ends leaves the next real start unable to say when the desk
    # was actually down, and `gaps` is the one thing here that cannot be recomputed later.
    #
    # Clearing is still right at this point: a restart must not resurrect a strategy that is
    # no longer configured, and history reattaches from the database by `sid` when each
    # strategy registers.
    paper_state.reset()
    paper_state.set_feed(source="Twelve Data", plan="pro",
                         status="starting", last_bar="—")
    total = args.capital * len(plan)
    paper_state.set_venue(name="Nautilus sandbox", balance=total, equity=total)
    paper_state.flush()

    symbols = sorted({s for s, _, _, _ in plan})
    hub = None
    if args.ws_port:
        hub = live_ws.LiveHub(symbols, args.ws_port).start()
    # The poller stays on as a slow safety net even when streaming: it costs one request a
    # minute and it is what keeps the marks honest if the upstream socket silently stalls.
    stop_marking = start_marker(symbols, args.mark_seconds)
    paper_state.set_feed(status="ok")
    # `force`, because this write lands milliseconds after the "starting" one above and
    # `flush` is debounced at MIN_FLUSH_SECONDS — un-forced it does not write, it schedules
    # a *daemon* timer to write in two seconds. Anything that kills the process inside that
    # window leaves "starting" as the desk's last published word forever, which is exactly
    # what a dead node looked like on the dashboard: perpetually starting up, no strategies,
    # no way to tell it from a node still warming its indicators.
    paper_state.flush(force=True)
    print(f"paper state -> {paper_state.STATE_PATH}")
    print("regenerate the site with: (cd web && python build_web_data.py)")
    try:
        node.run()
    finally:
        # The dashboard must not keep showing "live" for a process that has exited. The
        # numbers stay — they were real — but the status tells the truth about the feed.
        stop_marking()
        if hub is not None:
            hub.stop()
        paper_state.set_feed(status="stopped")
        paper_state.flush(force=True)   # the last write of the process; a timer will not run
        # Close the session row so the next start can measure the gap from here. Without
        # it the record still works -- the gap is taken from the last curve point -- but
        # the downtime window would be unattributable to a particular run.
        store.end_session()
        node.dispose()


if __name__ == "__main__":
    main()
