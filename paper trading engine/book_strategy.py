"""One rule, one book, many names: $100,000 traded across a whole asset class.

`TalibRuleStrategy` is one rule on one symbol with its own $10,000. That is the right
shape for checking an order path and the wrong shape for the question "is this rule any
good", because the research never scored a rule on one name — `ir_net` is a mean across
the class, and the board's headline figures come from `portfolio_wf.py`, which holds the
whole universe as a single account.

This is that account, live:

    TalibRuleStrategy   1 rule x 1 symbol   x $10,000 each, independent
    BookStrategy        1 rule x N symbols  x $100,000 TOTAL, one book   <- here

Four rules decide every number it produces, and they were chosen deliberately:

**The slice is the book divided by the live names.** 100 names, $100,000, so $1,000 each —
and as the book grows so does the slice, because the target is `equity / n` rather than a
fixed dollar figure. That is what makes the headline "$100,000 became X" mean compounding
rather than a sum of unrelated bets.

**A name the rule is out of holds cash.** Its slice is not pushed into the names that are
signalled. This is exactly how every figure on the dashboard was computed — each asset
compounds on its own and idle capital is idle — so the forward record stays comparable
with the backtest it came from. It also means the book is often half in cash, and
`held_count()` exists so the screen can say so rather than let it read as underperformance.

**It rebalances when something changes, not on a schedule.** A signal flip, or a
membership change, or drift past a deadband. `strategy.py` learned this the expensive way:
re-sizing to an exact fraction every bar produced 637 fills where the research measured
two or three round trips a year — "not a more precise implementation of the backtest, a
different strategy that pays a spread on every bar".

**It can decide before the bell.** With `signal_tf` set the book reads 5-minute bars,
rebuilds each day as the range *so far*, and trades once — five minutes before the close.
A rule computed from a bar's own high, low and close cannot honestly be filled at that
same close, because the close is not known until it has printed; the fix is not to delay
the fill by a day but to compute the signal slightly earlier and still trade near the
close. See `paper_config.SESSION_CLOSE`.

**A replacement inherits what the departing name was worth.** Selling a name that left the
index returns its value to cash, and the next sizing spreads that cash over whoever is in
the list now. No money appears or disappears at a membership change, so the $100,000 line
is continuous through it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import fill_pnl
import live_signal
import paper_config
import paper_state
import store

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.trading.strategy import Strategy


class BookStrategyConfig(StrategyConfig, frozen=True):
    rule: str
    account: str = "00"
    name: str = ""                       # defaults to the rule
    cls: str = "us_stocks"
    tf: str = "1d"
    symbols: tuple[str, ...] = ()
    venue: str = "SANDBOX"
    capital: float = 100_000.0
    allow_short: bool = False
    # Leave headroom so a rounded order still fits inside the slice. Whole-share equities
    # round up as often as down, and a slice sized to the last cent cannot absorb it.
    target_fraction: float = 0.98
    # How far a held position may drift from its nominal slice before being trimmed. The
    # guard against re-sizing every name on every bar as the book's equity moves.
    rebalance_deadband: float = 0.20
    min_warmup_bars: int = paper_config.MIN_WARMUP_BARS
    window_bars: int = paper_config.DEFAULT_WINDOW_BARS
    # DECIDE EARLY. When set, the book subscribes to `signal_tf` bars instead of `tf`
    # bars, folds them into one row per SESSION SO FAR, and trades once a day at
    # `decide_lead_min` before the bell. `tf` still names the horizon the rule was scored
    # on and still picks the sheet; only how the bar is observed changes.
    signal_tf: str | None = None
    decide_lead_min: int = paper_config.DEFAULT_DECIDE_LEAD_MIN
    min_warmup_sessions: int = paper_config.MIN_WARMUP_SESSIONS
    benchmark: str | None = None
    export_state: bool = True
    note: str = ""


class BookStrategy(Strategy):
    def __init__(self, config: BookStrategyConfig) -> None:
        super().__init__(config)
        # The registration's `name` is already `{cls}-{tf}-{rule}` — the desk names a book
        # for what it is, since it holds no single symbol. Re-adding the class and
        # timeframe produced `00:us_stocks-1d-us_stocks-1d-ibs`, which is not wrong so much
        # as unreadable, and the sid is the record's identity in four tables.
        name = config.name or f"{config.cls}-{config.tf}-{config.rule.lower()}"
        self._sid = store.sid_for(config.account, name)

        # THE book. One cash balance for the whole strategy, and units per name.
        self._cash = float(config.capital)
        self._units: dict[str, float] = {}
        self._last_price: dict[str, float] = {}

        self._bars: dict[str, list[dict]] = {}
        self._target: dict[str, float] = {}          # what the rule wants, in {-1,0,+1}
        self._traded: dict[str, float] = {}          # what was last actually traded to

        # Who is in the book right now. Held separately from `config.symbols` because
        # membership moves: `set_universe` is how a January re-rank reaches a running
        # strategy without restarting it.
        self._live: list[str] = list(config.symbols)

        # AVERAGE COST per name: what the slice currently held actually cost, so the board
        # can show a per-name return and `fill_pnl` can price what each sell closed.
        # Cleared when a name goes flat. It used to be the opening fill's price and to
        # ignore every add, which understates or overstates a name the rule scaled into.
        self._entry: dict[str, float] = {}
        self._fills_by: dict[str, int] = {}
        self._n_fills = 0
        # Decide-early bookkeeping: the last session each name was decided for, so 78
        # five-minute bars a day produce one decision and not seventy-eight.
        self._decided: dict[str, object] = {}
        self._no_bell_warned = False
        self._start_ts: pd.Timestamp | None = None
        self._bench_start: float | None = None
        self._warned = False

    # ------------------------------------------------------------------ the book
    def equity(self) -> float:
        """Cash plus every open slice at its last mark."""
        return self._cash + sum(
            u * self._last_price.get(s, 0.0) for s, u in self._units.items())

    def held_count(self) -> int:
        return sum(1 for u in self._units.values() if abs(u) > 1e-12)

    def holdings(self) -> list[dict]:
        """Every name in the book, held or not — the row-per-name the board expands into.

        All of them, not just the ones with a position: "46 of 100 held" is only readable
        if the other 54 are visible as *waiting* rather than absent. A name the rule is out
        of is holding its slice in cash, which is a state, not a gap.
        """
        out = []
        for symbol in self._live:
            units = self._units.get(symbol, 0.0)
            price = self._last_price.get(symbol)
            entry = self._entry.get(symbol)
            held = abs(units) > 1e-12
            out.append({
                "symbol": symbol,
                "state": "long" if units > 0 else "short" if units < 0 else "flat",
                "units": round(units, 6),
                "entry": entry,
                "mark": price,
                "value": round(units * price, 2) if (held and price) else 0.0,
                # Per name, against what it was bought at. `None` rather than 0.0 when
                # there is nothing to measure — a flat name has no return, and printing
                # 0.00% would read as "tried and made nothing".
                "pnl_pct": (round((price / entry - 1.0) * 100.0, 3)
                            if held and price and entry else None),
                "trades": self._fills_by.get(symbol, 0),
                "warming": symbol not in self._last_price,
            })
        return out

    def slice_value(self) -> float:
        """What one name is entitled to right now: the book divided by the live count.

        Divided by the LIVE count and not by the original one, so a book whose index
        shrank does not leave a permanently unallocated hole.
        """
        n = max(len(self._live), 1)
        return self.equity() * self.config.target_fraction / n

    # ------------------------------------------------------------------ membership
    def set_universe(self, symbols: list[str]) -> list[str]:
        """Replace the live list. Returns the names that left and must be sold.

        Selling is not done here — the caller does it through `_rebalance`, on the next
        bar for that name, because an order needs a price and this may be called from a
        control tick that has none.
        """
        gone = [s for s in self._live if s not in symbols]
        self._live = list(symbols)
        for s in gone:
            self._target[s] = 0.0       # the rule's opinion no longer matters; it is out
        return gone

    # ------------------------------------------------------------------ lifecycle
    def on_start(self) -> None:
        watch = list(self._live)
        if self.config.benchmark and self.config.benchmark not in watch:
            watch.append(self.config.benchmark)

        import td_nautilus
        from datetime import timedelta
        # The bars this book OBSERVES, which is not always the horizon it trades. Deciding
        # before the bell means watching 5-minute bars and folding them back into sessions.
        feed_tf = self.config.signal_tf or self.config.tf
        span = {"1d": timedelta(days=1), "4h": timedelta(hours=4),
                "2h": timedelta(hours=2), "1h": timedelta(hours=1),
                "15m": timedelta(minutes=15), "5m": timedelta(minutes=5),
                "1m": timedelta(minutes=1)}[feed_tf]
        start = self.clock.utc_now() - span * self.config.window_bars * 2

        for symbol in watch:
            inst = (td_nautilus.pair_instrument(symbol, self.config.venue)
                    if self.config.cls in paper_config.PAIR_CLASSES
                    else td_nautilus.equity_instrument(symbol, self.config.venue))
            if self.cache.instrument(inst.id) is None:
                self.cache.add_instrument(inst)
            bar_type = BarType.from_str(
                f"{inst.id}-{paper_config.BAR_SPEC[feed_tf]}")
            # HISTORY FIRST, then the live feed. Without this the book holds no bars at
            # all and cannot reach `min_warmup_bars` until that many bars have closed in
            # real time — 250 trading days on a daily book, so it would sit warming for a
            # year while looking perfectly healthy. `strategy.py` has always requested it;
            # this omission is why the first live book did nothing.
            #
            # `self.clock.utc_now()`, not the wall clock: a strategy attached to a running
            # trader is handed a fresh clock, and a wall-clock window asks for a range in
            # the future, which Nautilus rejects outright.
            self.request_bars(bar_type, start=start, limit=self.config.window_bars)
            self.subscribe_bars(bar_type)

        how = (f" deciding {self.config.decide_lead_min}m before the bell off "
               f"{feed_tf} bars" if self.decide_early else "")
        self.log.info(f"book {self._sid}: {self.config.rule} over {len(self._live)} "
                      f"names at {self.config.tf}, ${self.config.capital:,.0f} "
                      f"(${self.config.capital / max(len(self._live), 1):,.0f} a name)"
                      f"{how}")

        if self.config.export_state:
            paper_state.register(
                self._sid, account=self.config.account, kind="book",
                symbol=f"{len(self._live)} names", venue=self.config.venue,
                cls=self.config.cls, tf=self.config.tf, rule=self.config.rule,
                benchmark=self.config.benchmark, state="flat", status="warming",
                since=self.clock.utc_now().strftime("%Y-%m-%d"), days=0,
                paper_pnl_pct=0.0, paper_trades=0, position_units=0, entry=None,
                capital=self.config.capital, cash=self.config.capital, units=0.0,
                equity=self.config.capital, turnover=0.0, note=self.config.note,
                # Published at registration, not left until the first bar. `_export` runs
                # on `on_bar`, so a book still warming up reported `held=None, names=None`
                # and the board printed "1 assets" — one row, read as one instrument.
                held=0, names=len(self._live), holdings=self.holdings())
            paper_state.flush()

    def on_historical_data(self, data) -> None:
        """Warm-up arrives here, per instrument, before any live bar.

        Buffered only — no order is sent. The sandbox venue prices fills from bars on the
        message bus and never sees a request response, so an order placed now is rejected
        with `no market for <instrument>`. The first live bar reaches both this strategy
        and the exchange, and `on_bar` trades it then.
        """
        bars = data if isinstance(data, list) else [data]
        for bar in bars:
            if isinstance(bar, Bar):
                symbol = self._symbol_of(bar)
                self._append(symbol, bar)
                price = float(bar.close)
                if price > 0:
                    self._last_price.setdefault(symbol, price)

    # ------------------------------------------------------------------ data
    def _symbol_of(self, bar: Bar) -> str:
        raw = str(bar.bar_type.instrument_id.symbol)
        return paper_config.SAFE_TO_VENDOR.get(raw, raw)

    def _append(self, symbol: str, bar: Bar) -> bool:
        ts = pd.Timestamp(bar.ts_event, unit="ns", tz="UTC")
        buf = self._bars.setdefault(symbol, [])
        if buf and ts <= buf[-1]["ts"]:
            return False                       # a replayed or revised bar; see strategy.py
        buf.append({"Open": float(bar.open), "High": float(bar.high),
                    "Low": float(bar.low), "Close": float(bar.close),
                    "Volume": float(bar.volume), "ts": ts})
        if len(buf) > self.config.window_bars:
            del buf[:-self.config.window_bars]
        return True

    # -------------------------------------------------------- deciding before the bell
    @property
    def decide_early(self) -> bool:
        return bool(self.config.signal_tf)

    def _bell(self):
        """(tz, close time) for this book's class, or None if the class has no bell."""
        return paper_config.SESSION_CLOSE.get(self.config.cls)

    def _decision_instants(self, idx: pd.DatetimeIndex, tz: str, hh: int, mm: int):
        """The decision instant for each bar's own session, as a tz-aware index.

        Built from the WALL CLOCK and then localised, not by adding sixteen hours to local
        midnight. On the spring-forward Sunday an hour does not exist, so midnight plus
        sixteen hours of elapsed time is 17:00, not 16:00 — an arithmetic that is right for
        363 days a year and silently trades the wrong bar on the other two.
        """
        naive = idx.tz_convert(tz).tz_localize(None).normalize()
        naive = naive + pd.Timedelta(hours=hh, minutes=mm) - pd.Timedelta(
            minutes=self.config.decide_lead_min)
        return naive.tz_localize(tz)

    def _session_of(self, ts: pd.Timestamp):
        """(session date, decision instant) for a bar close, both in market local time.

        The bar carries its CLOSE timestamp (`td_nautilus._to_bar`), so a 5-minute bar
        stamped 15:55 is the one covering 15:50-15:55 — the last bar whose contents are
        known five minutes before a 16:00 bell, and therefore the decision bar itself.
        """
        bell = self._bell()
        if bell is None:
            return None, None
        tz, (hh, mm) = bell
        idx = pd.DatetimeIndex([ts])
        return idx.tz_convert(tz)[0].date(), self._decision_instants(idx, tz, hh, mm)[0]

    def _sessions(self, symbol: str) -> pd.DataFrame | None:
        """One row per session, built from the part of it that had happened by decision time.

        This is the frame the rule sees, and every row in it — history included — is a
        *partial* session. That is deliberate and it is the point: a rule whose history is
        full-day bars and whose latest row is a partial one is being asked to compare two
        different statistics, and for a state machine like IBS's that quietly changes which
        state it is in.
        """
        buf = self._bars.get(symbol) or []
        bell = self._bell()
        if not buf or bell is None:
            return None
        tz, (hh, mm) = bell
        raw = pd.DataFrame(buf)
        idx = pd.DatetimeIndex(raw["ts"])
        decide = self._decision_instants(idx, tz, hh, mm)
        # Vectorised, because this runs once per name per session over a buffer thousands
        # of bars long, and a per-bar Python loop over tz-aware timestamps is the slowest
        # thing this strategy could possibly do.
        keep = idx.tz_convert(tz) <= decide
        if not keep.any():
            return None
        raw = raw[keep]
        g = raw.groupby(decide[keep])
        out = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(),
                            "Low": g["Low"].min(), "Close": g["Close"].last(),
                            "Volume": g["Volume"].sum()})
        out.index.name = "ts"
        return out

    def _is_decision_bar(self, symbol: str, ts: pd.Timestamp) -> bool:
        """Is this the bar to act on — and has this session not been acted on already?

        Fires at or after the decision instant rather than exactly on it. The exact bar is
        the normal case; the `>` is for a session whose 15:55 print never arrives, where
        acting at the bell on the same pre-bell information is better than skipping the day
        entirely. Either way the SIGNAL only ever reads bars up to the cutoff, so no
        version of this sees a price it could not have seen.
        """
        day, decide = self._session_of(ts)
        if day is None:
            if not self._no_bell_warned:
                self._no_bell_warned = True
                self.log.error(f"{self.config.cls} has no SESSION_CLOSE; "
                               f"cannot decide before the bell")
            return False
        if self._decided.get(symbol) == day:
            return False
        if ts.tz_convert(decide.tz) < decide:
            return False
        self._decided[symbol] = day
        return True

    def _signal(self, symbol: str) -> float | None:
        if self.decide_early:
            df = self._sessions(symbol)
            if df is None or len(df) < self.config.min_warmup_sessions:
                return None
        else:
            buf = self._bars.get(symbol) or []
            if len(buf) < self.config.min_warmup_bars:
                return None
            df = pd.DataFrame(buf).set_index("ts")[
                ["Open", "High", "Low", "Close", "Volume"]]
        try:
            raw = live_signal.position_for(self.config.rule, df, symbol)
        except Exception as exc:
            self.log.error(f"{self.config.rule} on {symbol}: {exc}")
            return None
        if raw is None:
            if not self._warned:
                self._warned = True
                self.log.error(f"no dispatcher can build {self.config.rule!r}")
            return None
        arr = np.nan_to_num(np.asarray(raw, dtype="float64"), nan=0.0,
                            posinf=0.0, neginf=0.0)
        if arr.size != len(df):
            return None
        pos = float(arr[-1])
        return max(pos, 0.0) if not self.config.allow_short else pos

    # ------------------------------------------------------------------ trading
    def on_bar(self, bar: Bar) -> None:
        symbol = self._symbol_of(bar)
        price = float(bar.close)
        if price > 0:
            self._last_price[symbol] = price
        self._append(symbol, bar)

        # The benchmark is watched, never traded.
        if symbol not in self._live:
            if symbol == self.config.benchmark:
                if self._bench_start is None and price > 0:
                    self._bench_start = price
            else:
                # A name that has left the index still has a position to unwind, and it
                # needs a price to do it — which is this bar. Unwound at the same instant
                # of the day as everything else when deciding early, not on the first
                # five-minute bar that happens to arrive.
                if abs(self._units.get(symbol, 0.0)) > 1e-12 and (
                        not self.decide_early or self._is_decision_bar(
                            symbol, pd.Timestamp(bar.ts_event, unit="ns", tz="UTC"))):
                    self._rebalance(symbol, price)
            if not self.decide_early:
                self._export(bar)
            return

        # Deciding early means most bars are only a price update. Marks move, the buffer
        # grows, and nothing trades until the one bar a day this book acts on — which is
        # also the only bar it writes a curve point for, so the record stays one point per
        # session exactly as the daily books' does.
        if self.decide_early and not self._is_decision_bar(
                symbol, pd.Timestamp(bar.ts_event, unit="ns", tz="UTC")):
            return

        signal = self._signal(symbol)
        if signal is not None:
            self._target[symbol] = signal
        self._rebalance(symbol, price)
        self._export(bar)

    def _rebalance(self, symbol: str, price: float) -> None:
        """Trade one name toward its slice — but only when it is worth trading.

        Three things justify an order and nothing else does: the rule changed its mind,
        the target is short (whose share count genuinely moves with equity), or drift has
        passed the deadband. Without that last guard the slice moves every bar, because
        the slice is a fraction of a book whose value moves every bar.
        """
        if price <= 0:
            return
        import td_nautilus
        inst_factory = (td_nautilus.pair_instrument
                        if self.config.cls in paper_config.PAIR_CLASSES
                        else td_nautilus.equity_instrument)
        inst = self.cache.instrument(inst_factory(symbol, self.config.venue).id)
        if inst is None:
            return

        target = 0.0 if symbol not in self._live else self._target.get(symbol, 0.0)
        want_units = target * self.slice_value() / price
        have_units = self._units.get(symbol, 0.0)
        delta = want_units - have_units

        changed = target != self._traded.get(symbol)
        drift = abs(delta) / abs(want_units) if want_units else (
            1.0 if abs(have_units) > 1e-12 else 0.0)
        if not (changed or target < 0.0 or drift > self.config.rebalance_deadband):
            return

        # No leverage, ever. `target_fraction` leaves headroom for whole-share rounding,
        # but headroom is not a guarantee: a slice of $1,000 against a $500 share rounds by
        # up to half a share, and a hundred of those can outrun 2% of the book. Capping the
        # BUY at the cash actually on hand is what makes "no leverage" a property rather
        # than an intention — it went to −$354 on an eight-name book before this existed.
        if delta > 0:
            affordable = max(self._cash, 0.0) / price
            if affordable <= 0:
                return
            delta = min(delta, affordable)

        try:
            qty = inst.make_qty(abs(delta))
        except Exception:
            return
        if qty.as_double() <= 0:
            return
        # `make_qty` rounds, and on a whole-share instrument it can round UP past what was
        # just capped. Step back one increment rather than overspend by a share.
        if delta > 0 and qty.as_double() * price > self._cash:
            stepped = qty.as_double() - float(inst.size_increment)
            if stepped <= 0:
                return
            qty = inst.make_qty(stepped)
            if qty.as_double() <= 0 or qty.as_double() * price > self._cash:
                return

        self._traded[symbol] = target
        order = self.order_factory.market(
            instrument_id=inst.id,
            order_side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
            quantity=qty)
        self.submit_order(order)

    def on_order_filled(self, event) -> None:
        self._n_fills += 1
        symbol = self._symbol_of_id(event.instrument_id)
        price = event.last_px.as_double()
        qty = event.last_qty.as_double()
        side = event.order_side.name if hasattr(event.order_side, "name") \
            else str(event.order_side)
        signed = qty if side == "BUY" else -qty

        # The whole book moves together: cash pays for what any name bought and receives
        # what any name sold, so `equity = cash + sum(units x price)` stays right through
        # partial fills, reversals and membership swaps without consulting the venue.
        before = self._units.get(symbol, 0.0)
        # What this fill CLOSED, against the average cost of what was already held, and
        # the basis the remainder carries on. `_entry` is that basis now: a slice built
        # over three bars has three prices behind it, and the opening one — which is what
        # this used to keep — is neither what the part just sold cost nor what the board's
        # per-name return should measure against.
        realised, basis = fill_pnl.apply_fill(
            before, self._entry.get(symbol), signed, price)
        if basis is None:
            self._entry.pop(symbol, None)
        else:
            self._entry[symbol] = basis
        self._units[symbol] = before + signed
        self._cash -= signed * price
        self._last_price.setdefault(symbol, price)
        self._fills_by[symbol] = self._fills_by.get(symbol, 0) + 1

        if self.config.export_state:
            paper_state.push_trade(
                self._sid,
                pd.Timestamp(event.ts_event, unit="ns", tz="UTC").strftime("%Y-%m-%d %H:%M"),
                side, qty, price, round(self.equity() - self.config.capital, 2),
                symbol=symbol, ref=str(getattr(event, "trade_id", "") or ""),
                realised=None if realised is None else round(realised, 2))

    def _symbol_of_id(self, instrument_id) -> str:
        raw = str(instrument_id.symbol)
        return paper_config.SAFE_TO_VENDOR.get(raw, raw)

    def on_order_rejected(self, event) -> None:
        """A rejection is not a completed trade. `_traded` is what suppresses duplicate
        orders, so leaving it set would keep the book flat while the log claimed a
        target."""
        symbol = self._symbol_of_id(event.instrument_id)
        self._traded[symbol] = float("nan")     # NaN != anything, so the next bar retries

    def on_order_denied(self, event) -> None:
        self.on_order_rejected(event)

    # ------------------------------------------------------------------ the curve
    def _export(self, bar: Bar) -> None:
        if not self.config.export_state:
            return
        equity = self.equity()
        now = pd.Timestamp(bar.ts_event, unit="ns", tz="UTC")
        if self._start_ts is None:
            self._start_ts = now

        bench_pct = 0.0
        if self.config.benchmark and self._bench_start:
            here = self._last_price.get(self.config.benchmark)
            if here:
                bench_pct = (here / self._bench_start - 1.0) * 100.0

        pnl_pct = (equity / self.config.capital - 1.0) * 100.0
        days = max(int((now - self._start_ts).total_seconds() // 86400), 0)
        paper_state.push_point(self._sid, pnl_pct, bench_pct,
                               ts=now.isoformat(timespec="seconds"))
        paper_state.update(
            self._sid, state="long" if self.held_count() else "flat", status="running",
            days=days, paper_pnl_pct=round(pnl_pct, 3), paper_trades=self._n_fills,
            position_units=self.held_count(), equity=round(equity, 2),
            cash=round(self._cash, 4), units=self.held_count(),
            capital=self.config.capital,
            # What the screen needs to explain a book that trails a rising market.
            held=self.held_count(), names=len(self._live),
            holdings=self.holdings())
        paper_state.flush()

    def on_stop(self) -> None:
        self.log.info(f"book {self._sid} stopped: ${self.equity():,.2f} from "
                      f"${self.config.capital:,.2f} after {self._n_fills} fills")
