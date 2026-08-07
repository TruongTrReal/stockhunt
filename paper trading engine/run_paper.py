"""Paper-trade the forward test: live Twelve Data bars, Nautilus simulated fills.

`SandboxExecutionClient` is the point of this file. It is a real Nautilus execution
client that prices fills from the live data feed instead of sending orders anywhere, so
the portfolio, position and P&L accounting is the *same code* that runs against a real
venue. Going live is a change of execution client in `EXEC_CLIENTS` below and nothing
else — the strategy, the data client and the signal layer are untouched.

    backtest   BacktestNode      + BacktestDataClient  + SimulatedExchange
    paper      TradingNode       + TwelveDataLiveClient + SandboxExecutionClient   <- here
    live       TradingNode       + TwelveDataLiveClient + Binance / IB exec client

**This is plumbing, not a trading recommendation.** Every rule the research measured is
negative on information ratio against buy-and-hold — best is -0.048 on crypto 1d and
-0.072 on SOXL 1d, both under their noise ceilings. The default rule here is `SMA_200`
precisely because it is boring and well understood: the job of the first run is to prove
that bars arrive, signals compute, orders fill and P&L accrues. Do not read the P&L as
evidence of anything.

Run::

    python run_paper.py                       # SOXL + TQQQ, 1d, SMA_200
    python run_paper.py --symbols SOXL --rule EMA_200 --timeframe 4h
    python run_paper.py --dry-run             # build and validate config, do not connect
"""

from __future__ import annotations

import argparse
import threading
from datetime import datetime, timezone

import paper_config
import live_ws
import paper_state
import td_live
import td_nautilus
from strategy import TalibRuleConfig, TalibRuleStrategy

from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.config import (ImportableStrategyConfig, LoggingConfig,
                                    TradingNodeConfig)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue

EQUITY_VENUE = "SANDBOX"
CRYPTO_VENUE = "BINANCE"
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


def is_crypto(symbol: str) -> bool:
    return "/" in symbol


def instrument_for(symbol: str):
    if is_crypto(symbol):
        return td_nautilus.crypto_instrument(symbol, CRYPTO_VENUE)
    return td_nautilus.equity_instrument(symbol, EQUITY_VENUE)


# The sheets this desk selects from are walk-forward output, so they live with the
# walk-forward stage, not with the engine that produced the single-split leaderboards.
RESULTS = paper_config.WFO / "results"
HEADLINE = paper_config.HEADLINE


def top_rules(asset_class: str, n: int, timeframe: str = "1d") -> list[str]:
    """The n best rules on a sheet, by walk-forward out-of-sample IR.

    Read from `wf_summary_*.csv` rather than hard-coded, so the paper desk always reflects
    the current sweep instead of a list that silently goes stale the next time the research
    is re-run. Restricted to `wf_mode == "fixed"`: the re-selected rows (`IS#1`, the
    `[WF]` families) are a different rule in every fold and have no single definition to
    trade live.

    **Ranking is not passing.** Nothing on either sheet clears a single acceptance gate,
    and on equities the best rule has positive IR on 15% of assets. These are the least-bad
    candidates, which is not the same as good ones; they are here to exercise the pipeline.
    """
    import pandas as pd
    p = RESULTS / f"wf_summary_{asset_class}_{timeframe}.csv"
    if not p.exists():
        raise SystemExit(f"no sweep results at {p} — run walkforward.py first")
    df = pd.read_csv(p)
    h = df[(df.scenario == HEADLINE[asset_class]) & df.rankable & ~df.is_baseline]
    h = h[(h.wf_mode == "fixed") & ~h.rule.astype(str).str.startswith("IS#1")]
    out: list[str] = []
    for r in h.nlargest(n * 3, "ir_net").itertuples():
        # MA_50 and SMA_50 are the same series (TA-Lib's MA defaults to a simple average)
        # and score identically. Trading both would double the exposure to one idea while
        # looking like diversification.
        if any(_same_idea(r.rule, k) for k in out):
            continue
        out.append(str(r.rule))
        if len(out) == n:
            break
    return out


def _same_idea(a: str, b: str) -> bool:
    norm = lambda s: s.replace("SMA_", "MA_")
    return norm(a) == norm(b)


def build_node(plan: list[tuple], allow_short: bool, log_level: str,
               capital: float = CAPITAL_PER_SYSTEM) -> tuple[TradingNode, list]:
    """`plan` is a list of (symbol, rule, timeframe) triples — the desk to run."""
    symbols = sorted({s for s, _, _ in plan})
    instruments = {s: instrument_for(s) for s in symbols}
    venues = sorted({str(i.id.venue) for i in instruments.values()})

    # The sandbox venue must exist before its execution client starts, and the client
    # reads its instruments from this class-level list rather than an instrument provider.
    SandboxExecutionClient.INSTRUMENTS = list(instruments.values())

    # Split each venue's account across the systems trading on it. Nautilus gives one
    # account per venue, so without this every system sizes against the same balance and
    # they collectively try to deploy N times the capital that exists.
    per_venue = {}
    for s, _, _ in plan:
        per_venue[str(instruments[s].id.venue)] = per_venue.get(
            str(instruments[s].id.venue), 0) + 1
    capital_for = {v: capital for v in per_venue}
    funding = {v: capital * n for v, n in per_venue.items()}
    print("  capital: " + ", ".join(
        f"{v} ${capital:,.0f} × {n} systems = ${funding[v]:,.0f}"
        for v, n in per_venue.items()))

    strategies = []
    for symbol, rule, timeframe in plan:
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
                "asset_class": "crypto" if is_crypto(symbol) else "equity",
                "timeframe": timeframe,
                "note": f"{rule} on {symbol} {timeframe}, live Twelve Data bars into the "
                        f"Nautilus sandbox. Running to prove the pipeline.",
            },
        ))

    config = TradingNodeConfig(
        trader_id="FWD-001",
        logging=LoggingConfig(log_level=log_level,
                              log_directory=str(paper_config.LOG_DIR)),
        data_clients={
            "TWELVEDATA": td_nautilus.TwelveDataDataClientConfig(
                window_bars=paper_config.DEFAULT_WINDOW_BARS),
        },
        exec_clients={
            # `base_currency` is set for the equity venue and deliberately NOT for the
            # crypto one. A single-currency USD account can hold the proceeds of an Equity
            # trade, which settles in USD — but a CurrencyPair trade converts USD into the
            # base asset, and an account that cannot hold BTC has nowhere to put it. The
            # exchange does not reject the order; it fills one size increment and stops, so
            # `BUY 0.014691 BTCUSD` came back as `last_qty=0.000001` and every crypto
            # position was reported at a millionth of its true size. Leaving the venue
            # multi-currency lets the balance move into the base asset properly.
            v: SandboxExecutionClientConfig(
                venue=v,
                starting_balances=[f"{funding[v]:.0f} USD"],
                **({} if v == CRYPTO_VENUE else {"base_currency": "USD"}),
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
    for inst in instruments.values():
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
                marked = paper_state.mark(td_live.fetch_prices(symbols))
                paper_state.set_feed(last_bar=datetime.now(timezone.utc)
                                     .strftime("%Y-%m-%d %H:%M UTC"))
                if marked:
                    paper_state.flush(force=True)
            except Exception as exc:
                print(f"mark-to-market failed (will retry): {exc}", flush=True)

    threading.Thread(target=loop, daemon=True, name="mark-to-market").start()
    print(f"marking {len(symbols)} symbols to market every {every}s")
    return stop.set


def build_plan(args) -> list[tuple]:
    """(symbol, rule, timeframe) triples to trade.

    Rules are chosen per **class and per timeframe**, because the sheets are separate and
    they disagree: the best crypto rule at 1d is `SUM`, at 4h it is `T3_200`, and neither
    appears in the other's top five. Carrying one list across both would paper-trade a rule
    on a horizon where the research never ranked it.
    """
    equities = [s for s in args.symbols if not is_crypto(s)]
    cryptos = [s for s in args.symbols if is_crypto(s)]
    plan = []
    for tf in args.timeframes:
        if not args.top:
            plan += [(s, args.rule, tf) for s in args.symbols]
            continue
        for group, cls in ((equities, "us_stocks"), (cryptos, "crypto")):
            if not group:
                continue
            for rule in top_rules(cls, args.top, tf):
                plan += [(s, rule, tf) for s in group]
    return plan


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+",
                    default=paper_config.EQUITY_SYMBOLS + paper_config.CRYPTO_SYMBOLS)
    ap.add_argument("--timeframes", nargs="+", default=paper_config.FORWARD_TIMEFRAMES,
                    choices=list(BAR_SPEC))
    ap.add_argument("--rule", default="SMA_200")
    ap.add_argument("--top", type=int, default=0,
                    help="trade the top N rules per asset class from the sweep, instead "
                         "of the single --rule")
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

    # Clear whatever the last run (or a `backtest_paper.py --write-state` replay) left
    # behind. Merging would leave a strategy on the dashboard that is not in this node,
    # reported as live, with a P&L that stopped updating whenever the old process died.
    paper_state.reset()
    paper_state.set_feed(source="Twelve Data", plan="pro",
                         status="starting", last_bar="—")

    plan = build_plan(args)
    if not plan:
        raise SystemExit("nothing to trade")

    # The desk's total is derived from the plan, so it has to be built first.
    total = args.capital * len(plan)
    paper_state.set_venue(name="Nautilus sandbox", balance=total, equity=total)
    paper_state.flush()
    node, instruments = build_node(plan, args.allow_short, args.log_level,
                                   args.capital)
    print(f"built node: {len(plan)} strategies over {len(instruments)} instruments, "
          f"timeframes {'+'.join(args.timeframes)}, "
          f"warmup {paper_config.DEFAULT_WINDOW_BARS} bars")
    by: dict[tuple, list[str]] = {}
    for s, r, tf in plan:
        by.setdefault((tf, r), []).append(s)
    for (tf, rule), syms in sorted(by.items()):
        print(f"  {tf:>3}  {rule:<26} {len(syms):>2} × {', '.join(syms[:4])}"
              f"{' …' if len(syms) > 4 else ''}")
    if args.dry_run:
        print("dry run — not connecting")
        node.dispose()
        return

    symbols = sorted({s for s, _, _ in plan})
    hub = None
    if args.ws_port:
        hub = live_ws.LiveHub(symbols, args.ws_port).start()
    # The poller stays on as a slow safety net even when streaming: it costs one request a
    # minute and it is what keeps the marks honest if the upstream socket silently stalls.
    stop_marking = start_marker(symbols, args.mark_seconds)
    paper_state.set_feed(status="ok")
    paper_state.flush()
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
        paper_state.flush()
        node.dispose()


if __name__ == "__main__":
    main()
