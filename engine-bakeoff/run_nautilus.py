"""NautilusTrader leg of the bake-off.

Nautilus has no TA-Lib DSL to work around — a strategy is ordinary Python, so
``talib.RSI(...)`` can be called directly inside ``on_bar``. That flexibility is
the whole reason to consider it, so this script runs each rule two ways:

``inject``  The target position array is precomputed exactly as the reference
            computes it and the strategy just trades to it. Nothing about the
            indicator can differ, so any gap versus the reference is purely the
            engine's order/fill/accounting model. This is the accuracy test.

``native``  The strategy calls TA-Lib itself on each bar, over a bounded rolling
            window of the most recent bars — which is how a Nautilus strategy
            actually has to work, since it sees bars one at a time and cannot
            afford to recompute over all history forever. This measures the real
            cost of the event-driven path, and exposes the fact that recursively
            smoothed TA-Lib indicators (RSI, EMA, MACD) do not converge to their
            full-history values on a truncated window.

Both modes fill at the signal bar's close, which is what Nautilus does with bar
data: a market order submitted from ``on_bar`` transacts against that same bar's
close. That matches the reference's ``at_close`` convention and the main
project's ``position.shift(1)`` convention.
"""

from __future__ import annotations

import time
from decimal import Decimal

import numpy as np
import pandas as pd
import talib

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from common import (
    INITIAL_CAPITAL, RESULTS_DIR, RULES, UNIVERSE, WARMUP_BARS,
    load_bars, positions_for,
)

VENUE = Venue("SIM")
PRICE_PRECISION = 6
SIZE_PRECISION = 6

# How much history a native-mode strategy keeps to feed TA-Lib each bar. Long
# enough for every rule's stated lookback several times over, short enough that
# the per-bar cost stays constant instead of growing with the backtest.
ROLLING_WINDOW = 200


def make_instrument(symbol: str) -> CurrencyPair:
    """A fractional-quantity instrument, so sizing can match the reference.

    A real ``Equity`` instrument has ``size_precision=0`` and would force whole
    -share rounding. Rounding is a realism feature, not an error — but it is not
    what this test is trying to measure, so it is taken off the table here.
    """
    return CurrencyPair(
        instrument_id=InstrumentId(Symbol(symbol), VENUE),
        raw_symbol=Symbol(symbol),
        base_currency=USD,
        quote_currency=USD,
        price_precision=PRICE_PRECISION,
        size_precision=SIZE_PRECISION,
        price_increment=Price(10 ** -PRICE_PRECISION, PRICE_PRECISION),
        size_increment=Quantity(10 ** -SIZE_PRECISION, SIZE_PRECISION),
        lot_size=None,
        max_quantity=None, min_quantity=None,
        max_notional=None, min_notional=None,
        max_price=None, min_price=None,
        margin_init=Decimal(0), margin_maint=Decimal(0),
        maker_fee=Decimal(0), taker_fee=Decimal(0),
        ts_event=0, ts_init=0,
    )


class BakeoffStrategy(Strategy):
    """Trades a long/flat target to 100% of equity, at each bar's close."""

    def __init__(self, config=None):
        super().__init__(config)
        self.instrument_id = None
        self.bar_type = None
        self.mode = "inject"
        self.targets = None          # inject mode
        self.rule = None             # native mode
        self.i = 0
        self.n_fills = 0
        self.opens, self.highs, self.lows, self.closes = [], [], [], []

    def on_start(self):
        self.subscribe_bars(self.bar_type)

    # -- signal ---------------------------------------------------------
    def _target(self, bar: Bar) -> float:
        if self.mode == "inject":
            return float(self.targets[self.i])
        # native: recompute the rule from the rolling window this engine has seen.
        # Plain numpy arrays, not a DataFrame — the rules only ever index columns
        # and np.asarray them, and building a DataFrame per bar would make this
        # measure pandas construction rather than the engine.
        if len(self.closes) < WARMUP_BARS + 1:
            return 0.0
        window = {
            "Open": np.asarray(self.opens[-ROLLING_WINDOW:], dtype="float64"),
            "High": np.asarray(self.highs[-ROLLING_WINDOW:], dtype="float64"),
            "Low": np.asarray(self.lows[-ROLLING_WINDOW:], dtype="float64"),
            "Close": np.asarray(self.closes[-ROLLING_WINDOW:], dtype="float64"),
        }
        value = RULES[self.rule](window)[-1]
        return 0.0 if not np.isfinite(value) else float(value)

    # -- execution ------------------------------------------------------
    def on_bar(self, bar: Bar):
        self.opens.append(bar.open.as_double())
        self.highs.append(bar.high.as_double())
        self.lows.append(bar.low.as_double())
        self.closes.append(bar.close.as_double())

        target = self._target(bar)
        self.i += 1

        net = float(self.portfolio.net_position(self.instrument_id))
        want_long = target > 0.5
        if want_long and net == 0.0:
            equity = self.portfolio.account(VENUE).balance_total(USD).as_double()
            qty = equity / bar.close.as_double()
            self.submit_order(self.order_factory.market(
                instrument_id=self.instrument_id,
                order_side=OrderSide.BUY,
                quantity=Quantity(qty, SIZE_PRECISION)))
        elif net != 0.0 and not want_long:
            self.submit_order(self.order_factory.market(
                instrument_id=self.instrument_id,
                order_side=OrderSide.SELL,
                quantity=Quantity(abs(net), SIZE_PRECISION)))

    def on_order_filled(self, event):
        self.n_fills += 1


def build_bars(ticker: str, bars_df: pd.DataFrame, bar_type: BarType) -> list[Bar]:
    """Materialise the Nautilus Bar objects for one ticker.

    ``.astype("int64")`` on a millisecond-precision index yields milliseconds,
    not the nanoseconds Nautilus expects — hence the explicit ``as_unit("ns")``.
    """
    idx = pd.to_datetime(bars_df.index, utc=True).as_unit("ns")
    ts = idx.astype("int64").to_numpy()
    return [
        Bar(bar_type=bar_type,
            open=Price(o, PRICE_PRECISION), high=Price(h, PRICE_PRECISION),
            low=Price(l, PRICE_PRECISION), close=Price(c, PRICE_PRECISION),
            volume=Quantity(v, SIZE_PRECISION), ts_event=int(t), ts_init=int(t))
        for o, h, l, c, v, t in zip(
            bars_df["Open"].to_numpy(), bars_df["High"].to_numpy(),
            bars_df["Low"].to_numpy(), bars_df["Close"].to_numpy(),
            bars_df["Volume"].to_numpy(), ts)
    ]


def run_one(ticker: str, rule: str, mode: str, bars_df: pd.DataFrame,
            target: np.ndarray) -> dict:
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("BAKEOFF-001"),
        logging=LoggingConfig(bypass_logging=True),
    ))
    engine.add_venue(
        venue=VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(INITIAL_CAPITAL, USD)],
    )
    instrument = make_instrument(ticker)
    engine.add_instrument(instrument)

    bar_type = BarType.from_str(f"{ticker}.{VENUE}-1-DAY-LAST-EXTERNAL")
    engine.add_data(build_bars(ticker, bars_df, bar_type))

    strat = BakeoffStrategy()
    strat.instrument_id = instrument.id
    strat.bar_type = bar_type
    strat.mode = mode
    strat.targets = target
    strat.rule = rule
    engine.add_strategy(strat)

    engine.run()

    # A margin account's cash balance does not fall when a position is opened,
    # so equity is cash plus whatever the open position is currently worth
    # above its cost. Flat at the end -> unrealized is None, not zero.
    account = engine.portfolio.account(VENUE)
    cash = account.balance_total(USD).as_double()
    unrealized = engine.portfolio.unrealized_pnl(instrument.id)
    equity = cash + (unrealized.as_double() if unrealized is not None else 0.0)

    result = {
        "engine": f"nautilus_{mode}",
        "convention": "at_close",
        "ticker": ticker,
        "rule": rule,
        "n_bars": len(bars_df),
        "final_equity": equity,
        "total_return": equity / INITIAL_CAPITAL - 1.0,
        "n_trades": strat.n_fills,
    }
    engine.dispose()
    return result


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)

    prepared = {}
    for ticker in UNIVERSE:
        df = load_bars(ticker)
        pos = positions_for(ticker, df)
        prepared[ticker] = (df, df.loc[pos.index], pos)

    rows, timing = [], []
    for mode in ("inject", "native"):
        seconds = 0.0
        for ticker in UNIVERSE:
            full_df, bars_df, pos = prepared[ticker]
            # Native mode is fed the untrimmed history so its rolling window has
            # the same warmup the reference consumed off the front. It stays flat
            # until bar WARMUP_BARS, which is exactly where the trimmed series
            # begins — so both modes start trading on the same bar with the same
            # capital, and the only thing left to differ is the window truncation.
            feed = full_df if mode == "native" else bars_df
            for rule in RULES:
                t0 = time.perf_counter()
                res = run_one(ticker, rule, mode, feed, pos[rule].to_numpy())
                seconds += time.perf_counter() - t0
                rows.append(res)
        n = len(UNIVERSE) * len(RULES)
        timing.append({"engine": f"nautilus_{mode}", "n_runs": n,
                       "setup_seconds": 0.0, "engine_seconds": seconds,
                       "total_seconds": seconds})
        print(f"nautilus [{mode}]: {n} runs in {seconds:.1f}s "
              f"({seconds / n * 1000:.0f} ms/run)")

    pd.DataFrame(rows).to_csv(RESULTS_DIR / "nautilus.csv", index=False)
    pd.DataFrame(timing).to_csv(RESULTS_DIR / "timing_nautilus.csv", index=False)


if __name__ == "__main__":
    main()
