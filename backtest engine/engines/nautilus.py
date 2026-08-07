"""NautilusTrader leg: an event-driven third opinion, and the realism stage.

Two distinct jobs, deliberately not conflated:

**parity** (`final_equity`) — a fractional-quantity instrument, zero fees, trading to the
same target *fraction* of equity the other two engines model. This isolates the engine's
order/fill/accounting model, which is the only thing worth cross-checking. In
`../engine-bakeoff/` this configuration landed within 5.7e-6 of the reference across 200
runs, the residual being Nautilus's own quantisation: positions round to 6dp and cash to
the cent, ~69 times a run.

**validate** (`validate_run`) — a real `Equity` instrument with `size_precision=0`, so
whole-share rounding is enforced, plus an explicit commission. This is what the survivors
get, and it is the only place in the project where a number is allowed to differ from
the vectorised sweep — because here the difference *is* the finding.

Cost parity is checked between `vector` and `reference` across the whole cost grid;
Nautilus parity is checked at **zero cost only**. That is not laziness: a venue fee is
charged on traded notional (`|d shares| * price`), while this project's cost model
charges on target change (`|d target| * equity`). Under constant-fraction rebalancing a
short re-sizes its share count every bar without changing its target, so the two models
legitimately disagree the moment a fee is switched on. Comparing them at zero cost tests
the accounting; comparing them at non-zero cost would only re-measure that known
difference.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

VENUE_NAME = "SIM"
PRICE_PRECISION = 6
SIZE_PRECISION = 6

# Bar spacing is declared truthfully so any time-aware machinery inside the engine sees
# the right grid. It does not affect the arithmetic — bars are processed in sequence.
BAR_SPEC = {
    "1m": "1-MINUTE", "5m": "5-MINUTE", "15m": "15-MINUTE",
    "1h": "1-HOUR", "2h": "2-HOUR", "4h": "4-HOUR", "1d": "1-DAY",
}


def _imports():
    """Imported lazily — nautilus_trader is heavy and the sweep never needs it."""
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
    from nautilus_trader.model.instruments import CurrencyPair, Equity
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy
    return locals()


_NT = None


def nt():
    global _NT
    if _NT is None:
        _NT = _imports()
    return _NT


def _safe(symbol: str) -> str:
    return symbol.replace("/", "").replace("-", "")


def make_instrument(symbol: str, fractional: bool):
    n = nt()
    iid = n["InstrumentId"](n["Symbol"](_safe(symbol)), n["Venue"](VENUE_NAME))
    if fractional:
        # A real Equity has size_precision=0 and would force whole-share rounding.
        # Rounding is a realism feature, not an error — but it is not what the parity
        # check is measuring, so it is taken off the table here.
        return n["CurrencyPair"](
            instrument_id=iid, raw_symbol=n["Symbol"](_safe(symbol)),
            base_currency=n["USD"], quote_currency=n["USD"],
            price_precision=PRICE_PRECISION, size_precision=SIZE_PRECISION,
            price_increment=n["Price"](10 ** -PRICE_PRECISION, PRICE_PRECISION),
            size_increment=n["Quantity"](10 ** -SIZE_PRECISION, SIZE_PRECISION),
            lot_size=None, max_quantity=None, min_quantity=None,
            max_notional=None, min_notional=None,
            max_price=None, min_price=None,
            margin_init=Decimal(0), margin_maint=Decimal(0),
            maker_fee=Decimal(0), taker_fee=Decimal(0),
            ts_event=0, ts_init=0,
        )
    return n["Equity"](
        instrument_id=iid, raw_symbol=n["Symbol"](_safe(symbol)),
        currency=n["USD"], price_precision=PRICE_PRECISION,
        price_increment=n["Price"](10 ** -PRICE_PRECISION, PRICE_PRECISION),
        lot_size=n["Quantity"](1, 0), isin=None,
        margin_init=Decimal(0), margin_maint=Decimal(0),
        maker_fee=Decimal(0), taker_fee=Decimal(0),
        ts_event=0, ts_init=0,
    )


def _make_strategy_cls():
    n = nt()

    class TargetFractionStrategy(n["Strategy"]):
        """Hold `targets[i]` as a fraction of current equity, trading at each close."""

        def __init__(self, config=None):
            super().__init__(config)
            self.instrument_id = None
            self.bar_type = None
            self.targets = None
            self.size_precision = SIZE_PRECISION
            self.i = 0
            self.n_fills = 0
            self.prev_target = 0.0
            self.equity_curve = []

        def on_start(self):
            self.subscribe_bars(self.bar_type)

        def _equity(self) -> float:
            # A margin account's cash balance does not fall when a position opens, so
            # equity is cash plus what the open position is currently worth above cost.
            # Flat -> unrealized is None, not zero.
            acct = self.portfolio.account(n["Venue"](VENUE_NAME))
            cash = acct.balance_total(n["USD"]).as_double()
            unreal = self.portfolio.unrealized_pnl(self.instrument_id)
            return cash + (unreal.as_double() if unreal is not None else 0.0)

        def on_bar(self, bar):
            if self.i >= len(self.targets):
                return
            target = float(self.targets[self.i])
            self.i += 1

            price = bar.close.as_double()
            equity = self._equity()
            self.equity_curve.append(equity)

            net = float(self.portfolio.net_position(self.instrument_id))
            # A long at full investment needs no re-sizing: its share count is already
            # value/price at every bar. A short does — its required share count drifts
            # with equity every bar, and that drift is the whole difference between
            # constant-fraction and hold-the-shares. Re-sizing only when it is needed
            # keeps the order count (and the runtime) down by orders of magnitude.
            needs_resize = target != self.prev_target or target < 0.0
            self.prev_target = target
            if not needs_resize:
                return

            want_qty = target * equity / price
            delta = want_qty - net
            if abs(delta) < 10 ** -self.size_precision:
                return

            side = n["OrderSide"].BUY if delta > 0 else n["OrderSide"].SELL
            self.submit_order(self.order_factory.market(
                instrument_id=self.instrument_id, order_side=side,
                quantity=n["Quantity"](round(abs(delta), self.size_precision),
                                       self.size_precision)))

        def on_order_filled(self, event):
            self.n_fills += 1

    return TargetFractionStrategy


def _build_bars(df: pd.DataFrame, bar_type):
    n = nt()
    # .astype("int64") on a millisecond index yields milliseconds, not the nanoseconds
    # Nautilus expects — hence the explicit as_unit("ns").
    idx = pd.to_datetime(df.index, utc=True).as_unit("ns")
    ts = idx.astype("int64").to_numpy()
    vol = df["Volume"].to_numpy()
    vol = np.where(np.isfinite(vol), vol, 0.0)      # crypto has no volume
    return [
        n["Bar"](bar_type=bar_type,
                 open=n["Price"](o, PRICE_PRECISION), high=n["Price"](h, PRICE_PRECISION),
                 low=n["Price"](lo, PRICE_PRECISION), close=n["Price"](c, PRICE_PRECISION),
                 volume=n["Quantity"](v, SIZE_PRECISION),
                 ts_event=int(t), ts_init=int(t))
        for o, h, lo, c, v, t in zip(
            df["Open"].to_numpy(), df["High"].to_numpy(), df["Low"].to_numpy(),
            df["Close"].to_numpy(), vol, ts)
    ]


def run(symbol: str, df: pd.DataFrame, position: np.ndarray, capital: float,
        timeframe: str = "1d", fractional: bool = True) -> dict:
    """Run one (symbol, rule) cell through Nautilus. Zero fees."""
    n = nt()
    engine = n["BacktestEngine"](config=n["BacktestEngineConfig"](
        trader_id=n["TraderId"]("MASTER-001"),
        logging=n["LoggingConfig"](bypass_logging=True),
    ))
    engine.add_venue(
        venue=n["Venue"](VENUE_NAME), oms_type=n["OmsType"].NETTING,
        account_type=n["AccountType"].MARGIN, base_currency=n["USD"],
        starting_balances=[n["Money"](capital, n["USD"])],
    )
    instrument = make_instrument(symbol, fractional)
    engine.add_instrument(instrument)

    spec = BAR_SPEC.get(timeframe, "1-DAY")
    bar_type = n["BarType"].from_str(
        f"{_safe(symbol)}.{VENUE_NAME}-{spec}-LAST-EXTERNAL")
    engine.add_data(_build_bars(df, bar_type))

    strat = _make_strategy_cls()()
    strat.instrument_id = instrument.id
    strat.bar_type = bar_type
    strat.targets = np.asarray(position, dtype="float64")
    strat.size_precision = SIZE_PRECISION if fractional else 0
    engine.add_strategy(strat)

    engine.run()

    account = engine.portfolio.account(n["Venue"](VENUE_NAME))
    cash = account.balance_total(n["USD"]).as_double()
    unreal = engine.portfolio.unrealized_pnl(instrument.id)
    equity = cash + (unreal.as_double() if unreal is not None else 0.0)
    out = {"final_equity": float(equity), "n_fills": strat.n_fills,
           "n_bars": len(df)}
    engine.dispose()
    return out


def final_equity(position: np.ndarray, close: np.ndarray, cost_bps: float,
                 capital: float, df: pd.DataFrame = None,
                 symbol: str = "PARITY", timeframe: str = "1d") -> float:
    """Signature-compatible with the other two engines, for the parity harness.

    `cost_bps` must be 0 — see the module docstring for why a venue fee and this
    project's turnover charge are not the same quantity.
    """
    if cost_bps:
        raise ValueError("nautilus parity runs at zero cost only; see module docstring")
    if df is None:
        idx = pd.date_range("2015-01-02", periods=len(close), freq="D", tz="UTC")
        df = pd.DataFrame({"Open": close, "High": close, "Low": close,
                           "Close": close, "Volume": np.zeros(len(close))}, index=idx)
    return run(symbol, df, position, capital, timeframe, fractional=True)["final_equity"]
