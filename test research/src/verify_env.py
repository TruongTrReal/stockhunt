"""Prove the go-forward stack actually works end to end, in one command.

Checks, in order of how expensive they are to discover the hard way:

1. every library in the stack imports, in one interpreter;
2. the Twelve Data cache is *structurally* identical to the yfinance cache, so
   existing pipeline code cannot tell them apart;
3. `talib_signals.generate_position` — the module everything else is built on —
   runs against Twelve Data bars and produces sane positions;
4. a NautilusTrader backtest runs on those same bars and agrees with the
   project's own arithmetic.

Run it after any environment change::

    python verify_env.py
"""

import sys
from decimal import Decimal

import numpy as np
import pandas as pd

CHECKS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def check_imports() -> None:
    print("\n1. libraries")
    import numpy, pandas, pyarrow, requests, talib  # noqa: F401
    import nautilus_trader
    record("core stack imports", True,
           f"nautilus {nautilus_trader.__version__}, pandas {pandas.__version__}, "
           f"numpy {numpy.__version__}, TA-Lib {talib.__version__}")


def check_cache_shape() -> pd.DataFrame:
    print("\n2. price cache")
    import prices
    from data_loader import load_universe as load_yf

    record("source selected", True, prices.describe_source())

    td = prices.load_universe(["AAPL"])
    if "AAPL" not in td:
        record("twelvedata cache populated", False,
               "no AAPL — run: python twelvedata_loader.py")
        raise SystemExit(1)
    td = td["AAPL"]

    yf = load_yf(["AAPL"]).get("AAPL")
    if yf is not None:
        same_cols = list(td.columns) == list(yf.columns)
        same_dtypes = td.dtypes.astype(str).tolist() == yf.dtypes.astype(str).tolist()
        same_index = type(td.index) is type(yf.index)
        record("shape matches yfinance cache", same_cols and same_dtypes and same_index,
               f"columns={list(td.columns)}, index={type(td.index).__name__}")
        # Same shape, deliberately different values — see prices.py.
        shared = td.index.intersection(yf.index)
        drift = float(np.median(np.abs(
            td.loc[shared, "Close"].to_numpy() / yf.loc[shared, "Close"].to_numpy() - 1)))
        record("values differ from yfinance (expected)", drift > 0,
               f"median close drift {drift:.2e} — caches are NOT interchangeable")
    else:
        record("yfinance cache present for comparison", False, "skipped")

    record("bars loaded", len(td) > 1000,
           f"{len(td)} bars, {td.index[0].date()} -> {td.index[-1].date()}")

    # Coverage, not just shape. A partial download leaves a cache that is
    # perfectly well-formed and almost empty — every other check here passed
    # against a 7-ticker cache once, which is exactly the failure this catches.
    from sp500_tickers import get_sp500_tickers

    expected = set(get_sp500_tickers())
    cached = {p.stem for p in prices.CACHE_DIR.glob("*.parquet")}
    missing = expected - cached
    coverage = 1.0 - len(missing) / max(1, len(expected))
    record("cache covers the universe", coverage >= 0.98,
           f"{len(cached & expected)}/{len(expected)} S&P 500 tickers "
           f"({coverage:.1%})" +
           (f" — missing {sorted(missing)[:8]}{'...' if len(missing) > 8 else ''}"
            if missing else ""))
    return td


def check_signals(bars: pd.DataFrame) -> pd.Series:
    print("\n3. signal layer")
    from talib_signals import generate_position, get_all_indicator_names

    names = get_all_indicator_names()
    record("indicator table loaded", len(names) > 100, f"{len(names)} indicators")

    # Names are the expanded variant list (e.g. period sweeps), not raw TA-Lib
    # function names — so pick the sample from the table rather than hardcoding.
    ok, working = 0, []
    for name in names[:40]:
        try:
            pos = generate_position(name, bars)
        except Exception:
            continue
        if pos is not None and len(pos) == len(bars):
            ok += 1
            if 0.0 < float((pos != 0).mean()) <= 1.0:
                working.append((name, pos))
    record("generate_position runs on Twelve Data bars", ok > 30,
           f"{ok}/40 sampled indicators produced a full-length position series")

    if not working:
        record("a sampled rule takes a position", False, "all sampled rules stayed flat")
        raise SystemExit(1)
    name, position = working[0]
    exposure = float((position != 0).mean())
    record("sample rule has sane exposure", True, f"{name} exposure {exposure:.1%}")
    return position


def check_nautilus(bars: pd.DataFrame, position: pd.Series) -> None:
    print("\n4. nautilus execution")
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
    from nautilus_trader.model.instruments import CurrencyPair
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    venue, capital, prec = Venue("SIM"), 10_000.0, 6
    target = (position.to_numpy() > 0).astype(float)

    class Runner(Strategy):
        def __init__(self):
            super().__init__()
            self.instrument_id = None
            self.bar_type = None
            self.targets = None
            self.i = 0
            self.fills = 0

        def on_start(self):
            self.subscribe_bars(self.bar_type)

        def on_bar(self, bar):
            want = self.targets[self.i] > 0.5
            self.i += 1
            net = float(self.portfolio.net_position(self.instrument_id))
            if want and net == 0.0:
                equity = self.portfolio.account(venue).balance_total(USD).as_double()
                self.submit_order(self.order_factory.market(
                    instrument_id=self.instrument_id, order_side=OrderSide.BUY,
                    quantity=Quantity(equity / bar.close.as_double(), prec)))
            elif net != 0.0 and not want:
                self.submit_order(self.order_factory.market(
                    instrument_id=self.instrument_id, order_side=OrderSide.SELL,
                    quantity=Quantity(abs(net), prec)))

        def on_order_filled(self, event):
            self.fills += 1

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("VERIFY-001"), logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(venue=venue, oms_type=OmsType.NETTING,
                     account_type=AccountType.MARGIN, base_currency=USD,
                     starting_balances=[Money(capital, USD)])
    instrument = CurrencyPair(
        instrument_id=InstrumentId(Symbol("AAPL"), venue), raw_symbol=Symbol("AAPL"),
        base_currency=USD, quote_currency=USD,
        price_precision=prec, size_precision=prec,
        price_increment=Price(1e-6, prec), size_increment=Quantity(1e-6, prec),
        lot_size=None, max_quantity=None, min_quantity=None,
        max_notional=None, min_notional=None, max_price=None, min_price=None,
        margin_init=Decimal(0), margin_maint=Decimal(0),
        maker_fee=Decimal(0), taker_fee=Decimal(0), ts_event=0, ts_init=0)
    engine.add_instrument(instrument)

    bar_type = BarType.from_str(f"AAPL.{venue}-1-DAY-LAST-EXTERNAL")
    ts = pd.to_datetime(bars.index, utc=True).as_unit("ns").astype("int64").to_numpy()
    engine.add_data([
        Bar(bar_type=bar_type, open=Price(o, prec), high=Price(h, prec),
            low=Price(l, prec), close=Price(c, prec), volume=Quantity(float(v), prec),
            ts_event=int(t), ts_init=int(t))
        for o, h, l, c, v, t in zip(bars["Open"], bars["High"], bars["Low"],
                                    bars["Close"], bars["Volume"], ts)])

    strat = Runner()
    strat.instrument_id = instrument.id
    strat.bar_type = bar_type
    strat.targets = target
    engine.add_strategy(strat)
    engine.run()

    account = engine.portfolio.account(venue)
    unrealized = engine.portfolio.unrealized_pnl(instrument.id)
    equity = account.balance_total(USD).as_double() + (
        unrealized.as_double() if unrealized is not None else 0.0)
    engine.dispose()

    # The project's own arithmetic for the same long/flat, fully-invested rule.
    close = bars["Close"].to_numpy()
    cash, shares = capital, 0.0
    prev = 0.0
    for i in range(len(close)):
        if target[i] != prev:
            value = cash + shares * close[i]
            shares = target[i] * value / close[i]
            cash = value - shares * close[i]
            prev = target[i]
    expected = cash + shares * close[-1]

    rel = abs(equity / expected - 1.0)
    record("nautilus backtest runs", strat.fills > 0, f"{strat.fills} fills")
    record("nautilus agrees with reference", rel < 1e-4,
           f"equity {equity:,.2f} vs {expected:,.2f} (rel {rel:.2e})")


def main() -> None:
    print("=" * 70)
    print("stockhunt environment check — Twelve Data -> pandas/TA-Lib -> Nautilus")
    print("=" * 70)
    check_imports()
    bars = check_cache_shape()
    position = check_signals(bars)
    check_nautilus(bars, position)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print("\n" + "=" * 70)
    if failed:
        print(f"{len(failed)} of {len(CHECKS)} checks FAILED: {failed}")
        sys.exit(1)
    print(f"all {len(CHECKS)} checks passed — stack is ready")


if __name__ == "__main__":
    main()
