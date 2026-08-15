"""A Nautilus strategy that trades on instruction rather than on a rule.

`TalibRuleStrategy` computes a signal and trades towards it. This one computes nothing:
it holds a book, and it does what the order ledger tells it. That is the whole difference
between the desk running its own research and the desk running somebody else's.

    TalibRuleStrategy   bar -> signal -> target -> order        the house's rules
    MemberStrategy      order from the ledger -> order          a manager's own logic

The manager's code never comes near this process. It runs on their machine, decides what
it wants, and calls the API; `desk_control` drains the ledger and calls `place()` here.
So there is no sandbox to build and no dependency of theirs to install — and their edge
stays theirs, which is the thing a manager will actually care about.

**One strategy, several instruments, one book.** A house leg is one symbol and one rule.
A registration may name several symbols and the manager wants *the strategy's* P&L, not a
per-symbol breakdown they would have to add up. So `_cash` is shared across the
registration and `_units` is per symbol, and both are this strategy's own — never the
Nautilus venue account, which nets every strategy on an instrument together and would let
one manager's position size another's.

**Every fill is reported with its symbol.** The fill table's natural key includes it; two
names bought at the same size and price on the same bar would otherwise deduplicate into
one row and half the position would vanish from the record.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

import paper_config
import paper_state
import store
import td_nautilus
from stockhunt import deskdb

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy


class MemberStrategyConfig(StrategyConfig, frozen=True):
    # `deskdb`'s id for this registration — the join between the ledger and the node.
    registration_id: str
    account: str
    name: str
    cls: str
    tf: str
    # Plain strings, not InstrumentIds: this config is built by `desk_control` from a
    # database row, and keeping it to primitives means it can also be declared up front in
    # a `TradingNodeConfig` without a custom encoder.
    symbols: tuple[str, ...] = ()
    venue: str = "SANDBOX"
    capital: float = 10_000.0
    allow_short: bool = False
    # Declared at registration or absent. A house rule benchmarks against buy-and-hold of
    # its own symbol; a multi-symbol strategy has no obvious baseline, and choosing one on
    # the manager's behalf would put this desk's opinion inside their track record.
    benchmark: str | None = None
    export_state: bool = True
    note: str = ""


class MemberStrategy(Strategy):
    def __init__(self, config: MemberStrategyConfig) -> None:
        super().__init__(config)
        self._sid = store.sid_for(config.account, config.name)
        self._cash = float(config.capital)
        self._units: dict[str, float] = {s: 0.0 for s in config.symbols}
        self._last_price: dict[str, float] = {}
        self._n_fills = 0
        self._start_ts: pd.Timestamp | None = None
        self._bench_start: float | None = None

        # Nautilus mints its own ClientOrderId when an order is created, so the ledger's
        # `seq` has to be carried alongside rather than encoded in it. Both directions are
        # needed: fills arrive keyed on the Nautilus id, and a cancel arrives keyed on the
        # manager's own `client_order_id`.
        self._seq_of: dict[str, int] = {}
        self._nautilus_id_of: dict[str, str] = {}

    # ------------------------------------------------------------------ instruments
    def _instrument_for(self, symbol: str):
        if self.config.cls in paper_config.PAIR_CLASSES:
            return td_nautilus.pair_instrument(symbol, self.config.venue)
        return td_nautilus.equity_instrument(symbol, self.config.venue)

    def _instrument_id(self, symbol: str) -> InstrumentId:
        return self._instrument_for(symbol).id

    def _symbol_of(self, instrument_id: InstrumentId) -> str:
        """Back from a Nautilus symbol to the vendor's spelling: `BTCUSD` -> `BTC/USD`."""
        raw = str(instrument_id.symbol)
        return paper_config.SAFE_TO_VENDOR.get(raw, raw)

    # ------------------------------------------------------------------ lifecycle
    def on_start(self) -> None:
        # The benchmark is subscribed but never traded. Without its bars the second line
        # on the chart cannot be drawn, and drawing it from the traded symbols instead
        # would make the comparison meaningless.
        watch = list(self.config.symbols)
        if self.config.benchmark and self.config.benchmark not in watch:
            watch.append(self.config.benchmark)

        for symbol in watch:
            iid = self._instrument_id(symbol)
            if self.cache.instrument(iid) is None:
                self.cache.add_instrument(self._instrument_for(symbol))
            self.subscribe_bars(
                BarType.from_str(f"{iid}-{paper_config.BAR_SPEC[self.config.tf]}"))

        self.log.info(f"member strategy {self.config.registration_id} live on "
                      f"{', '.join(self.config.symbols)} at {self.config.tf}, "
                      f"capital {self.config.capital:,.0f}")

        if self.config.export_state:
            paper_state.register(
                self._sid, account=self.config.account, kind="member",
                symbol=", ".join(self.config.symbols), venue=self.config.venue,
                cls=self.config.cls, tf=self.config.tf, rule=self.config.name,
                benchmark=self.config.benchmark,
                state="flat", status="running",
                since=self.clock.utc_now().strftime("%Y-%m-%d"), days=0,
                paper_pnl_pct=0.0, paper_trades=0, position_units=0, entry=None,
                capital=self.config.capital, cash=self.config.capital, units=0.0,
                equity=self.config.capital, turnover=0.0, note=self.config.note)
            paper_state.flush()

    # ------------------------------------------------------------------ the book
    def book(self) -> dict:
        """This strategy's own cash and holdings, in the shape `desk_orders` validates."""
        return {"cash": self._cash, "units": dict(self._units)}

    def prices(self) -> dict:
        return dict(self._last_price)

    def equity(self) -> float:
        return self._cash + sum(self._units.get(s, 0.0) * self._last_price.get(s, 0.0)
                                for s in self._units)

    # ------------------------------------------------------------------ instructions
    def place(self, row: dict) -> tuple[bool, str]:
        """Submit one ledger row. Returns `(submitted, reason)`.

        Validation has already happened in `desk_orders.partition` against this book; what
        is left here is the part that can only fail at the venue — an instrument the cache
        does not know, or a size that rounds to nothing.
        """
        if row["action"] == "cancel":
            return self._cancel(row)

        symbol = row["symbol"]
        inst = self.cache.instrument(self._instrument_id(symbol))
        if inst is None:
            return False, f"no instrument for {symbol} on {self.config.venue}"

        try:
            qty = inst.make_qty(abs(float(row["qty"])))
        except Exception as exc:
            return False, f"cannot size {row['qty']} {symbol}: {exc}"
        if qty.as_double() <= 0:
            # Whole-share instruments round down. Saying so beats a silent no-op, which
            # would leave the order 'accepted' forever with nothing having happened.
            return False, (f"{row['qty']} {symbol} rounds to zero at this instrument's "
                           f"size increment")

        side = OrderSide.BUY if row["side"].lower() == "buy" else OrderSide.SELL
        try:
            if (row.get("order_type") or "market").lower() == "limit":
                order = self.order_factory.limit(
                    instrument_id=inst.id, order_side=side, quantity=qty,
                    price=inst.make_price(float(row["limit_price"])))
            else:
                order = self.order_factory.market(
                    instrument_id=inst.id, order_side=side, quantity=qty)
        except Exception as exc:
            return False, f"could not build the order: {exc}"

        # Recorded BEFORE submitting. A fill can be delivered synchronously inside
        # `submit_order` for a market order against a bar that is already on the book, and
        # a mapping written afterwards would be written too late to attribute it.
        self._seq_of[str(order.client_order_id)] = int(row["seq"])
        self._nautilus_id_of[row["client_order_id"]] = str(order.client_order_id)

        self.submit_order(order)
        return True, ""

    def _cancel(self, row: dict) -> tuple[bool, str]:
        """Cancel by the manager's own id, which is the only one they know.

        The Nautilus `ClientOrderId` is minted inside this node and never leaves it, so
        the mapping built at submission time is the only way back from what the caller
        said to what the venue holds.
        """
        wanted = row.get("target_coid") or ""
        if not wanted:
            return False, "a cancel must name the order it cancels"
        nautilus_id = self._nautilus_id_of.get(wanted)
        if nautilus_id is None:
            return False, f"no live order called {wanted!r} on this strategy"

        from nautilus_trader.model.identifiers import ClientOrderId
        order = self.cache.order(ClientOrderId(nautilus_id))
        if order is None:
            return False, f"{wanted!r} is no longer on the book"
        if order.is_closed:
            return False, f"{wanted!r} has already finished; nothing to cancel"
        self.cancel_order(order)
        return True, ""

    # ------------------------------------------------------------------ events
    def on_bar(self, bar: Bar) -> None:
        symbol = self._symbol_of(bar.bar_type.instrument_id)
        price = float(bar.close)
        if price > 0:
            self._last_price[symbol] = price
        if self.config.export_state:
            self._export(bar)

    def on_order_filled(self, event) -> None:
        self._n_fills += 1
        symbol = self._symbol_of(event.instrument_id)
        # `.as_double()`, not `float()`: a Nautilus Quantity carries its own precision and
        # `float()` on a fractional one does not round-trip — 0.014691 BTC came back as
        # 1e-06, exactly one size increment.
        price = event.last_px.as_double()
        qty = event.last_qty.as_double()
        side = event.order_side.name if hasattr(event.order_side, "name") \
            else str(event.order_side)
        signed = qty if side == "BUY" else -qty

        self._units[symbol] = self._units.get(symbol, 0.0) + signed
        self._cash -= signed * price
        self._last_price.setdefault(symbol, price)

        seq = self._seq_of.get(str(event.client_order_id))
        if seq is not None:
            order = self.cache.order(event.client_order_id)
            filled = order.filled_qty.as_double() if order is not None else qty
            done = order is not None and order.is_closed
            deskdb.mark_order(seq, "filled" if done else "partially_filled",
                              filled_qty=filled, avg_price=price)

        self.log.info(f"FILLED {side} {qty} {symbol} @ {price} -> "
                      f"{self._units[symbol]:.6f}, cash {self._cash:,.2f}")

        if self.config.export_state:
            paper_state.push_trade(
                self._sid,
                pd.Timestamp(event.ts_event, unit="ns", tz="UTC").strftime("%Y-%m-%d %H:%M"),
                side, qty, price, round(self.equity() - self.config.capital, 2),
                symbol=symbol,
                # The venue's own id for this fill. A manager may legitimately send the
                # same order twice on one bar; without this the two collapse to one row
                # and half the position disappears from the record.
                ref=str(getattr(event, "trade_id", "") or ""))
            paper_state.update(
                self._sid, paper_trades=self._n_fills,
                position_units=round(sum(self._units.values()), 8),
                state=self._state_word(),
                paper_pnl_pct=round((self.equity() / self.config.capital - 1) * 100, 3),
                equity=round(self.equity(), 2), cash=round(self._cash, 4),
                units=sum(self._units.values()), capital=self.config.capital)
            paper_state.flush(force=True)

    def on_order_rejected(self, event) -> None:
        self._fail(event, "rejected")

    def on_order_denied(self, event) -> None:
        self._fail(event, "rejected")

    def on_order_canceled(self, event) -> None:
        self._fail(event, "canceled")

    def on_order_expired(self, event) -> None:
        self._fail(event, "expired")

    def _fail(self, event, state: str) -> None:
        """Tell the ledger. Without this a manager polling their order sees `accepted`
        forever and has no way to learn the venue refused it."""
        seq = self._seq_of.get(str(getattr(event, "client_order_id", "")))
        if seq is None:
            return
        deskdb.mark_order(seq, state, reason=str(getattr(event, "reason", "") or state))

    def _state_word(self) -> str:
        total = sum(self._units.values())
        return "long" if total > 0 else "short" if total < 0 else "flat"

    # ------------------------------------------------------------------ the curve
    def _export(self, bar: Bar) -> None:
        equity = self.equity()
        now = pd.Timestamp(bar.ts_event, unit="ns", tz="UTC")
        if self._start_ts is None:
            self._start_ts = now

        bench_pct = 0.0
        if self.config.benchmark:
            bench_price = self._last_price.get(self.config.benchmark)
            if bench_price:
                if self._bench_start is None:
                    self._bench_start = bench_price
                bench_pct = (bench_price / self._bench_start - 1.0) * 100.0

        pnl_pct = (equity / self.config.capital - 1.0) * 100.0
        days = max(int((now - self._start_ts).total_seconds() // 86400), 0)
        paper_state.push_point(self._sid, pnl_pct, bench_pct,
                               ts=now.isoformat(timespec="seconds"))
        paper_state.update(
            self._sid, state=self._state_word(), status="running", days=days,
            paper_pnl_pct=round(pnl_pct, 3), paper_trades=self._n_fills,
            position_units=round(sum(self._units.values()), 6),
            equity=round(equity, 2), cash=round(self._cash, 4),
            units=sum(self._units.values()), capital=self.config.capital)
        paper_state.flush()

    def on_stop(self) -> None:
        self.log.info(f"member strategy {self.config.registration_id} stopped, "
                      f"equity {self.equity():,.2f} after {self._n_fills} fills")
