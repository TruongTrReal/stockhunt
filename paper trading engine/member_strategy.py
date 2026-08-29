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

**...and since 2026-08-29 those instruments may come from several ASSET CLASSES.** A
registration is already a portfolio, and confining it to one class was a restriction that
came from the row's `cls` column deciding the venue rather than from anything about the
book. `symbol_classes` is the fix: class is a property of each symbol now, so `_venue_of`
and `_instrument_for` are asked per name and one book can hold `AAPL` on `SANDBOX`,
`BTC/USD` on `BINANCE` and `ES.v.0` on `GLBX` — which also routes each of them to the
right VENDOR, because Nautilus routes data clients by venue and that is the whole
mechanism keeping Twelve Data from ever being asked for a CME contract.

The cash is still ONE pot across all of them. The venue accounts are funded separately by
`run_paper` and are not the book; `desk_orders` bounds the book, `_cash` is the book, and
a venue balance is only the sandbox's own bookkeeping behind the fills.

**Every fill is reported with its symbol.** The fill table's natural key includes it; two
names bought at the same size and price on the same bar would otherwise deduplicate into
one row and half the position would vanish from the record.

**...and so is the book, name by name.** `holdings()` publishes one entry per registered
symbol in exactly the shape `BookStrategy.holdings()` uses. The per-symbol state was
always tracked here and simply never left the process, so the board had only the joined
`symbol` string to key a row on and drew three instruments as one row with one units
figure and em-dashes for cost, mark and value. `symbol` is unchanged and still the joined
string — the record, `paper_curves` and the fills table all key off it — the breakdown is
published ALONGSIDE it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

import fill_pnl
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
    # Symbol -> asset class, as a tuple of pairs. **This is what lets one registration hold
    # instruments from several classes**, and it is the only thing that does: `cls` and
    # `venue` above are the book's HOME leg, and they used to decide the instrument shape,
    # the venue and therefore the vendor for every symbol the registration named — so a
    # book holding `AAPL` and `BTC/USD` was not expressible. One of the two would have been
    # built as the wrong instrument on the wrong venue and marked from a feed that does not
    # carry it.
    #
    # Empty means "every symbol is `cls`", which is exactly what every registration written
    # before this field existed means, so nothing already running changes shape.
    #
    # Pairs and not a dict, because `StrategyConfig` is a FROZEN msgspec Struct: frozen
    # structs get a generated `__hash__` over their fields, and a dict field makes the
    # whole config unhashable at whatever point Nautilus first hashes it — which is not
    # here, and not obviously related to this line when it happens.
    symbol_classes: tuple[tuple[str, str], ...] = ()
    capital: float = 10_000.0
    allow_short: bool = False
    # Carried for the RECORD, not for the arithmetic. `desk_orders.validate` is what
    # enforces the ceiling, off the ledger row, before an order ever reaches `place()` —
    # this copy exists so the published book says under what terms it was run. A curve at
    # 2x and a curve at 1x are not the same measurement and the document has to say which.
    leverage: float = 1.0
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
        # Unpacked once. The config carries pairs so it stays hashable; every lookup after
        # this is a dict lookup on a bar, which is the hot path.
        self._classes: dict[str, str] = dict(config.symbol_classes or ())
        self._cash = float(config.capital)
        self._units: dict[str, float] = {s: 0.0 for s in config.symbols}
        # Average cost per name, so a sell can be priced against what it actually closed.
        # Dropped when a name goes flat.
        self._cost: dict[str, float] = {}
        self._last_price: dict[str, float] = {}
        # Fills per name, for the board's per-symbol row. `_n_fills` is the book's total
        # and cannot be split after the fact, so a three-name book reported its one ETH
        # fill against all three names or against none of them.
        self._fills_by: dict[str, int] = {}
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
    def _class_of(self, symbol: str) -> str:
        """This SYMBOL's class, not the registration's.

        The two are the same on every single-class book and on every registration written
        before `symbol_classes` existed, which is what makes the change invisible there.
        On a mixed book they differ, and everything that follows from the class — the
        venue, the instrument shape, and therefore which vendor Nautilus routes the
        subscription to — has to follow the symbol.
        """
        return self._classes.get(symbol) or self.config.cls

    def _venue_of(self, symbol: str) -> str:
        """Which sandbox venue this symbol settles on.

        Read from `paper_config.VENUES` by the symbol's own class rather than from
        `config.venue`, because the venue is what Nautilus routes DATA by: a `GLBX`
        instrument id reaches `DatabentoLiveClient` and everything else reaches the Twelve
        Data default client. Putting a futures symbol on `SANDBOX` would ask Twelve Data
        for `ES.v.0`, which is the one thing this desk may never do — it answers with
        Eversource Energy rather than with an error.
        """
        return paper_config.VENUES.get(self._class_of(symbol), self.config.venue)

    def _instrument_for(self, symbol: str):
        return td_nautilus.instrument_for(
            symbol, self._class_of(symbol), self._venue_of(symbol))

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
                      f"{', '.join(f'{s} ({self._class_of(s)} on {self._venue_of(s)})' for s in self.config.symbols)} "
                      f"at {self.config.tf}, capital {self.config.capital:,.0f}, "
                      f"leverage {self.config.leverage:g}x")

        if self.config.export_state:
            paper_state.register(
                self._sid, account=self.config.account, kind="member",
                symbol=", ".join(self.config.symbols), venue=self.config.venue,
                cls=self.config.cls, tf=self.config.tf, rule=self.config.name,
                # Every class this book actually holds, and `cls` above is its HOME leg.
                # The board groups on `cls`, so a mixed book is filed under one of its legs
                # rather than under a sixth pseudo-class called `mixed` — which would put
                # it in no class pill at all and remove it from the board entirely. This is
                # the disclosure that keeps that grouping honest, and it is published even
                # when there is one class so "single-class" and "an older desk that never
                # published this" cannot read alike.
                classes=self.classes(),
                benchmark=self.config.benchmark,
                state="flat", status="running",
                since=self.clock.utc_now().strftime("%Y-%m-%d"), days=0,
                paper_pnl_pct=0.0, paper_trades=0, position_units=0, entry=None,
                capital=self.config.capital, cash=self.config.capital, units=0.0,
                equity=self.config.capital, turnover=0.0,
                # Published at REGISTRATION, not left until the first fill. A member's
                # `symbol` is the joined string of everything they registered, so without
                # this the board counted the rows it had — one — and printed "1 assets"
                # over a book holding three. `BookStrategy.on_start` publishes these for
                # the same reason and under the same names.
                #
                # `names` also moves one published number: `paper_state._set_turnover`
                # divides lifetime fills by it, so a member's turnover is now round trips
                # PER NAME rather than for the whole book. That is the unit the
                # walk-forward sheets report and the unit every other row on the board
                # already carries, so the change makes the column comparable rather than
                # merely different — but it does mean a three-name member's figure is a
                # third of what the desk used to print for it.
                held=0, names=len(self.config.symbols), holdings=self.holdings(),
                # PUBLISHED, not merely held. `paper_pnl_pct` is `equity / capital - 1` for
                # a levered book exactly as it is for an unlevered one — the base is the
                # money the member put up either way, so the percentage is not on a
                # different base and does not need re-scaling. What differs is the RISK
                # behind it, and two rows reading `+8%` with nothing to separate 1x from 4x
                # is a board that looks like it is comparing like with like. So the number
                # travels with its leverage.
                leverage=self.config.leverage,
                note=self.config.note)
            paper_state.flush()

    # ------------------------------------------------------------------ the book
    def book(self) -> dict:
        """This strategy's own cash and holdings, in the shape `desk_orders` validates."""
        return {"cash": self._cash, "units": dict(self._units)}

    def prices(self) -> dict:
        return dict(self._last_price)

    def held_count(self) -> int:
        """How many NAMES carry a position. Not the units total.

        A long and a short of equal size sum to zero units and are two open positions,
        so the board's "with a position" figure has to count names rather than add them
        up — which is also why this is published beside `position_units` rather than
        being derived from it downstream.
        """
        return sum(1 for u in self._units.values() if abs(u) > 1e-12)

    def holdings(self) -> list[dict]:
        """Every registered symbol, held or not — the row-per-name the board draws.

        **Same shape and same field names as `BookStrategy.holdings()`, deliberately.**
        Both kinds of strategy are rows on one board and `app.js` renders them with one
        function; a parallel vocabulary here would buy a second renderer and two places
        for the same bug.

        The data was always here — `_units`, `_cost` and `_last_price` are all keyed on
        the symbol — and only the publishing was missing. Without it the board had
        nothing to key a row on and fell back to the joined `symbol` STRING, so a book
        holding `BTC/USD`, `ETH/USD` and `BNB/USD` drew ONE row headed with all three
        names, one units figure that was their sum, and an em-dash everywhere a per-name
        cost, mark or value belonged.

        **Every registered symbol, not only the held ones.** "BNB/USD, flat" is a fact:
        the member registered it, the desk is watching it, and it is holding its share of
        the book in cash. Dropping the row would say this strategy holds two names when
        it holds three — the silent narrowing this repo keeps having to undo.

        Three fields carry a member-specific meaning under the shared name:

        * `entry` is `_cost`, the AVERAGE cost of what is currently held, so a partial
          sell is priced against what it actually closed. `BookStrategy._entry` is the
          same quantity under the same name.
        * `pnl_pct` is signed by DIRECTION. A book is long/flat by construction and can
          use `price / entry - 1` unconditionally; a member with `allow_short` cannot —
          a short whose price has fallen has made money, and printing that as a loss
          would make the colour on this page mean something other than gained or lost.
          The sign convention is `fill_pnl.apply_fill`'s, so the percentage and the
          realised figure on the fills table cannot disagree about who is winning.
        * `warming` is "the desk has seen no price for this name yet". A member computes
          nothing and needs exactly one bar rather than a warm-up window, so it clears
          far sooner here than on a book — but it answers the same question, which is
          why it keeps the same name.

        The symbol's CLASS is deliberately NOT on the row. A member's names may come from
        several asset classes since 2026-08-29 and a book's never can, but that
        disclosure already travels on the registration as `classes`, and a per-row copy
        would be a second place for one fact to go stale.
        """
        out = []
        for symbol in self.config.symbols:
            units = self._units.get(symbol, 0.0)
            price = self._last_price.get(symbol)
            entry = self._cost.get(symbol)
            held = abs(units) > 1e-12
            ret = None
            if held and price and entry:
                ret = (price / entry - 1.0) * (1.0 if units > 0 else -1.0) * 100.0
            out.append({
                "symbol": symbol,
                "state": "long" if units > 0 else "short" if units < 0 else "flat",
                "units": round(units, 6),
                "entry": entry,
                "mark": price,
                # Signed, so a short reads as the liability it is. `gross()` is the
                # absolute version and is what the leverage ceiling is measured on.
                "value": round(units * price, 2) if (held and price) else 0.0,
                "pnl_pct": None if ret is None else round(ret, 3),
                "trades": self._fills_by.get(symbol, 0),
                "warming": symbol not in self._last_price,
            })
        return out

    def classes(self) -> list[str]:
        """Every asset class this book holds, sorted. One entry on a single-class book."""
        return sorted({self._class_of(s) for s in self.config.symbols})

    def venues(self) -> list[str]:
        """Every venue this book settles on. `run_paper` funds each of them."""
        return sorted({self._venue_of(s) for s in self.config.symbols})

    def equity(self) -> float:
        return self._cash + sum(self._units.get(s, 0.0) * self._last_price.get(s, 0.0)
                                for s in self._units)

    def gross(self) -> float:
        """Total notional held, long plus short — what the leverage ceiling bounds.

        Reported rather than enforced: `desk_orders.headroom` is what refuses an order, and
        it works on the same `book()`/`prices()` pair this is computed from, so the figure
        on the board and the figure in a refusal cannot disagree. Absolute values, because
        a $5,000 long against a $5,000 short is $10,000 of market risk and nets to zero
        only on paper.
        """
        return sum(abs(self._units.get(s, 0.0)) * self._last_price.get(s, 0.0)
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
            # The symbol's OWN venue, not the book's. On a mixed book those differ, and a
            # message naming the wrong one sends somebody looking at the wrong exchange.
            return False, f"no instrument for {symbol} on {self._venue_of(symbol)}"

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

        self._fills_by[symbol] = self._fills_by.get(symbol, 0) + 1

        before = self._units.get(symbol, 0.0)
        # What this fill CLOSED, against the average cost of the position it closed. A
        # member's book kept no cost basis at all before this, so every fill it reported
        # went into the record with nothing but the whole book's mark beside it.
        realised, basis = fill_pnl.apply_fill(
            before, self._cost.get(symbol), signed, price)
        if basis is None:
            self._cost.pop(symbol, None)
        else:
            self._cost[symbol] = basis
        self._units[symbol] = before + signed
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
                ref=str(getattr(event, "trade_id", "") or ""),
                realised=None if realised is None else round(realised, 2))
            paper_state.update(
                self._sid, paper_trades=self._n_fills,
                position_units=round(sum(self._units.values()), 8),
                state=self._state_word(),
                paper_pnl_pct=round((self.equity() / self.config.capital - 1) * 100, 3),
                equity=round(self.equity(), 2), cash=round(self._cash, 4),
                units=sum(self._units.values()), capital=self.config.capital,
                leverage=self.config.leverage, gross=round(self.gross(), 2),
                held=self.held_count(), names=len(self.config.symbols),
                holdings=self.holdings())
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
            units=sum(self._units.values()), capital=self.config.capital,
            leverage=self.config.leverage, gross=round(self.gross(), 2),
            # Re-published on every bar, because `mark` and `warming` move with the feed
            # even when nothing has traded: a name the desk has just seen its first price
            # for stops being "waiting for bars" without a fill anywhere.
            held=self.held_count(), names=len(self.config.symbols),
            holdings=self.holdings())
        paper_state.flush()

    def on_stop(self) -> None:
        self.log.info(f"member strategy {self.config.registration_id} stopped, "
                      f"equity {self.equity():,.2f} after {self._n_fills} fills")
