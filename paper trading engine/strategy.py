"""The live strategy: one `talib_signals` rule, traded to a target exposure.

The whole point is that this computes its position with the **same function the research
used** — `talib_signals.generate_position` — over a rolling buffer, rather than
reimplementing the rule for live. A reimplementation is where forward tests silently stop
testing the thing that was backtested.

Two correctness rules, both learned the hard way in this repo:

**Warm up to the measured window, not a guessed one.** `parity_live.py` measures the
smallest trailing window that reproduces the full-series position exactly; on daily ETFs
that is 1000 bars (TEMA_200 binds) and the default carries 50% headroom. A recursive
indicator fed too little history returns a *plausible* number that is not the backtest's
number, and nothing in the logs would say so. The strategy refuses to trade until its
buffer reaches `MIN_WARMUP_BARS` and warns if it is under the measured window.

**Act on the closed bar, trade the next one.** The research convention is signal at bar
*t*, fill at *t+1*. Nautilus delivers a bar after its close, so submitting on receipt is
exactly that convention — the fill happens against the following bar. `SandboxExecutionClient`
with `bar_execution=True` honours it.

Position sizing is deliberately crude: a target *fraction* of account equity, matching
`(1 + pos.shift(1) * ret).cumprod()` in the vectorised engine, which means constant
fraction and not constant share count. Anything cleverer would stop being comparable with
the backtest it exists to validate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import fill_pnl
import live_signal
import paper_config
import paper_state
import store

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import BarAggregation, OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

# `generate_position` is no longer imported here: it answers for the TA-Lib family only.
# `live_signal.position_for` asks it first and falls through to the published registry.


def _bar_duration(bar_type: BarType) -> timedelta:
    """Wall-clock length of one bar, for sizing the warmup request."""
    spec = bar_type.spec
    if spec.aggregation == BarAggregation.DAY:
        return timedelta(days=spec.step)
    if spec.aggregation == BarAggregation.HOUR:
        return timedelta(hours=spec.step)
    if spec.aggregation == BarAggregation.MINUTE:
        return timedelta(minutes=spec.step)
    raise ValueError(f"unsupported bar spec: {spec}")


class TalibRuleConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    rule: str = "SMA_200"
    target_fraction: float = 0.95      # leave headroom so a rounded order still fits
    window_bars: int = paper_config.DEFAULT_WINDOW_BARS
    min_warmup_bars: int = paper_config.MIN_WARMUP_BARS
    allow_short: bool = False
    # Relative drift that justifies re-sizing a position the rule did not ask to change.
    # 0.20 lets a held long run 20% away from its nominal size before being trimmed —
    # loose enough that ordinary price movement never triggers it, tight enough that a
    # position cannot end up sized for an account that no longer exists.
    rebalance_deadband: float = 0.20
    # This system's own notional. Every strategy on a venue shares one Nautilus account,
    # so sizing against the account balance means the first to trade claims 95% of it and
    # the rest split what is left — geometrically. With 150 systems on one venue the tail
    # was buying 0.000003 units. Each therefore gets a slice and keeps its own books.
    capital: float = 100_000.0
    # Research context, carried through so the dashboard can show the live number beside
    # the multi-year one. Not used in any trading decision.
    asset_class: str = "equity"
    timeframe: str = "1d"
    # How the pair is written for humans. A Nautilus Symbol cannot hold a separator, so
    # `BTC/USD` becomes `BTCUSD` on the instrument — fine for routing, wrong on a
    # dashboard a manager reads.
    display_symbol: str = ""
    # Whose book this is. '00' is the house — the rules the desk runs off its own
    # walk-forward sheets — and a member's registration carries their own two-character id.
    # It is only ever an id: the trading engine never sees an email address.
    account: str = "00"
    bt_ir: float | None = None
    bt_years: float | None = None
    note: str = ""
    export_state: bool = True


class TalibRuleStrategy(Strategy):
    def __init__(self, config: TalibRuleConfig) -> None:
        super().__init__(config)
        self._bars: list[dict] = []
        self._target: float = 0.0
        # The target as of the last order actually sent. `_target` alone cannot answer
        # "did the rule change its mind?", because it is overwritten every bar.
        self._last_traded_target: float = 0.0
        self._warned_window = False
        # Warmup can arrive in more than one callback; the opening position is taken once.
        self._primed = False
        # Paper-state bookkeeping. `_start_*` are fixed at the first traded bar so the
        # published curve means "since this strategy began", not "since the warmup data
        # vendor's earliest bar" — which would put years of history in a chart captioned
        # as days of paper trading.
        # Account-scoped, so two accounts running the identical cell keep separate books.
        # Without the prefix they share one sid, their fills merge, their curves merge and
        # neither number belongs to anybody.
        self._sid = store.sid_for(
            config.account,
            f"{str(config.instrument_id.symbol).lower()}-{config.timeframe}"
            f"-{config.rule.lower()}")
        self._start_equity: float | None = None
        self._start_price: float | None = None
        self._start_ts: str | None = None
        self._n_fills = 0
        # Average cost of the open exposure — what it cost, not where it opened. None when
        # flat. `fill_pnl` maintains it and prices every close against it.
        self._entry: float | None = None
        self._bar_count = 0
        # This system's own book, independent of the shared venue account. Nautilus nets
        # every strategy on an instrument into one position, so `portfolio.net_position`
        # answers "what does the whole desk hold in AAPL", not "what do I hold" — reading
        # it made each system see its neighbours' trades as its own and stop trading.
        self._cash = float(config.capital)
        self._units = 0.0

    # ------------------------------------------------------------------ lifecycle
    def on_start(self) -> None:
        inst = self.cache.instrument(self.config.instrument_id)
        if inst is None:
            self.log.error(f"no instrument {self.config.instrument_id}; stopping")
            self.stop()
            return
        self.log.info(
            f"rule={self.config.rule} window={self.config.window_bars} "
            f"(measured worst case {paper_config.MEASURED_WINDOW_BARS}) "
            f"allow_short={self.config.allow_short}")
        if self.config.window_bars < paper_config.MEASURED_WINDOW_BARS:
            self.log.warning(
                f"window_bars={self.config.window_bars} is BELOW the measured "
                f"{paper_config.MEASURED_WINDOW_BARS}: a recursive rule may not reproduce "
                f"the backtest. Re-run parity_live.py before trusting these results.")
        # `request_bars` requires an explicit start. The Twelve Data client answers from
        # `limit` rather than the range, but a real vendor adapter would honour it — so
        # compute an honest span instead of passing an arbitrary early date. The 2x
        # padding covers weekends and holidays: 1500 daily *bars* is ~2170 calendar days.
        #
        # `self.clock.utc_now()`, NOT `datetime.now()`. A strategy must read its own clock
        # or it cannot run anywhere except live. The two are the same thing under a
        # LiveClock, so this changes nothing on the desk — but a strategy attached to a
        # RUNNING trader is handed a brand-new clock (`Trader.add_strategy` does
        # `self._clock.__class__()`), and under a TestClock that clock starts at the epoch
        # until the engine next advances it. Asking for a window measured from the wall
        # clock then requests a range in the future and Nautilus rejects it outright with
        # `start was > now`. See `test_runtime_attach.py`, which is what found this.
        span = _bar_duration(self.config.bar_type)
        start = self.clock.utc_now() - span * self.config.window_bars * 2
        self.request_bars(self.config.bar_type, start=start,
                          limit=self.config.window_bars)
        self.subscribe_bars(self.config.bar_type)

        if self.config.export_state:
            paper_state.register(
                self._sid,
                account=self.config.account, kind="house_rule",
                symbol=self.config.display_symbol or str(self.config.instrument_id.symbol),
                venue=str(self.config.instrument_id.venue),
                cls=self.config.asset_class, tf=self.config.timeframe,
                rule=self.config.rule, state="flat", status="warming",
                since=datetime.now(timezone.utc).strftime("%Y-%m-%d"), days=0,
                paper_pnl_pct=0.0, paper_trades=0, position_units=0, entry=None,
                # The book is published at registration, not at the first bar, so a system
                # that is still warming up is still worth its capital to the desk total.
                capital=self.config.capital, cash=self.config.capital, units=0.0,
                equity=self.config.capital,
                bt_ir=self.config.bt_ir, bt_years=self.config.bt_years,
                bt_gates=[0, 0, 0, 0], turnover=0.0, note=self.config.note)
            paper_state.flush()

    def on_historical_data(self, data) -> None:
        """Warmup arrives here as a list of bars before the first live close.

        Once the buffer is deep enough, work out the position the rule already wants and
        set it as the target — but do **not** send the order here. The sandbox venue
        prices fills from bars on the message bus, and warmup arrives as a request
        response that the exchange never sees, so an order sent now is rejected with
        `no market for <instrument>`. The first live bar reaches both the strategy and the
        exchange, and `on_bar` then finds `_target` already differing from
        `_last_traded_target` and trades it.

        Priming still matters even though it sends nothing: it means the position taken on
        that first live bar is the one the last *closed* bar called for, rather than the
        strategy behaving as though it had just been born flat.
        """
        bars = data if isinstance(data, list) else [data]
        for bar in bars:
            if isinstance(bar, Bar):
                self._append(bar)
        self.log.info(f"warmup buffer now {len(self._bars)} bars")

        # Prime on "history reaches the present", not on a bar count.
        #
        # Nautilus hands historical bars to this callback incrementally, so a plain
        # `len(...) >= min_warmup_bars` fires when the buffer holds bars 1..250 of 1500 and
        # primes off a close from 2021 while the vendor had already sent data through
        # today. Requiring the full `window_bars` fixed that but introduced its own hole:
        # any response shorter than the request never primes at all, which is exactly what
        # stranded the first six 4h equity strategies at "warming up" while their nine
        # siblings on the same symbols ran.
        #
        # The real precondition is neither count: it is that the newest bar in the buffer
        # is the most recent close. Testing that directly is immune to both how many bars
        # the vendor returns and how they are delivered.
        if self._primed or len(self._bars) < self.config.min_warmup_bars:
            return
        span = _bar_duration(self.config.bar_type)
        age = self.clock.utc_now() - self._bars[-1]["ts"].to_pydatetime()   # own clock, as above
        # Two intervals of slack absorbs a weekend gap on a daily bar and a vendor that
        # has not yet settled the bar that just closed.
        if age > span * 2 + timedelta(days=3):
            return
        self._primed = True
        signal = self._signal()
        if signal is None:
            return
        last = self._bars[-1]
        self._target = signal
        self.log.info(f"primed from warmup: {self.config.rule} wants {signal:+.0f} "
                      f"as of {last['ts'].date()} close {last['Close']} — "
                      f"will fill on the first live bar")
        if self.config.export_state:
            paper_state.update(
                self._sid, status="running",
                note=f"{self.config.note} Warm-up complete; target {signal:+.0f} pending "
                     f"the first live {self.config.timeframe} bar.")
            paper_state.flush()

    # ------------------------------------------------------------------ data
    def _append(self, bar: Bar) -> bool:
        """Add a bar, unless it is one already held.

        The data client republishes the last closed bar once at start-up so the venue has
        a market to fill against, which means every strategy sees that bar twice — once in
        its warmup and once live. Appending it again would put a duplicate close at the end
        of the series and quietly shift any windowed indicator by one bar. Anything at or
        before the newest bar held is therefore dropped, which also covers a vendor
        revision and an early poll.
        """
        ts = pd.Timestamp(bar.ts_event, unit="ns", tz="UTC")
        if self._bars and ts <= self._bars[-1]["ts"]:
            return False
        self._bars.append({
            "Open": float(bar.open), "High": float(bar.high),
            "Low": float(bar.low), "Close": float(bar.close),
            "Volume": float(bar.volume), "ts": ts,
        })
        if len(self._bars) > self.config.window_bars:
            self._bars = self._bars[-self.config.window_bars:]
        return True

    def _frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self._bars).set_index("ts")
        return df[["Open", "High", "Low", "Close", "Volume"]]

    def _signal(self) -> float | None:
        """Position in {-1, 0, +1} from the shared research signal layer.

        Through `live_signal`, which asks BOTH dispatchers. This used to call
        `generate_position` directly, which knows only the 231 TA-Lib rules and raises
        `No signal rule registered` for anything in `strategies/published/` — so the desk
        could not trade `ibs` or `volmanaged`, which are the two rules at the top of the
        `us_stocks 1d` board. The rules the research liked best were exactly the ones the
        paper desk refused.
        """
        try:
            raw = live_signal.position_for(
                self.config.rule, self._frame(),
                self.config.display_symbol or str(self.config.instrument_id.symbol))
        except Exception as exc:
            self.log.error(f"{self.config.rule} failed: {exc}")
            return None
        if raw is None:
            self.log.error(f"no dispatcher can build {self.config.rule!r}")
            return None
        arr = np.nan_to_num(np.asarray(raw, dtype="float64"), nan=0.0,
                            posinf=0.0, neginf=0.0)
        if arr.size != len(self._bars):
            self.log.error(f"{self.config.rule} returned {arr.size} of {len(self._bars)}")
            return None
        pos = float(arr[-1])
        return max(pos, 0.0) if not self.config.allow_short else pos

    # ------------------------------------------------------------------ trading
    def on_bar(self, bar: Bar) -> None:
        self._append(bar)
        n = len(self._bars)
        if n < self.config.min_warmup_bars:
            self.log.info(f"warming up: {n}/{self.config.min_warmup_bars}")
            return
        if n < paper_config.MEASURED_WINDOW_BARS and not self._warned_window:
            self._warned_window = True
            self.log.warning(
                f"buffer {n} < measured window {paper_config.MEASURED_WINDOW_BARS}: "
                f"signals may differ from the backtest until it fills")

        signal = self._signal()
        if signal is None:
            return
        if signal != self._target:
            self.log.info(f"{self.config.rule}: target {self._target:+.0f} -> "
                          f"{signal:+.0f} on close {float(bar.close)}")
        self._target = signal
        self._rebalance(float(bar.close))
        if self.config.export_state:
            self._export(bar)

    # ------------------------------------------------------------------ state export
    def _equity(self) -> float | None:
        """Cash plus open-position mark. A margin account's cash does not fall when a
        position opens, so cash alone reports a flat line while the position moves."""
        inst = self.cache.instrument(self.config.instrument_id)
        account = self.portfolio.account(inst.venue) if inst else None
        if account is None:
            return None
        balance = account.balance_total(inst.quote_currency)
        if balance is None:
            return None
        unreal = self.portfolio.unrealized_pnl(self.config.instrument_id)
        return float(balance.as_double()) + (unreal.as_double() if unreal is not None else 0.0)

    def _export(self, bar: Bar) -> None:
        price = float(bar.close)
        if price <= 0:
            return
        equity = self._cash + self._units * price
        self._bar_count += 1
        if self._start_equity is None:
            self._start_equity, self._start_price = self.config.capital, price
            self._start_ts = pd.Timestamp(bar.ts_event, unit="ns", tz="UTC")

        pnl_pct = (equity / self._start_equity - 1.0) * 100.0
        bench_pct = (price / self._start_price - 1.0) * 100.0
        units = self._units
        now = pd.Timestamp(bar.ts_event, unit="ns", tz="UTC")
        days = max(int((now - self._start_ts).total_seconds() // 86400), 0)

        # Keyed on the BAR's time, not the wall clock: a warm-up replay re-emits
        # the same bars and must collapse onto the same rows, while two genuine
        # closes never collide however fast they arrive.
        paper_state.push_point(self._sid, pnl_pct, bench_pct,
                               ts=now.isoformat(timespec='seconds'))
        paper_state.update(
            self._sid,
            state="long" if units > 0 else "short" if units < 0 else "flat",
            status="running", days=days,
            since=self._start_ts.strftime("%Y-%m-%d"),
            paper_pnl_pct=round(pnl_pct, 3), paper_trades=self._n_fills,
            position_units=round(units, 6), entry=self._entry,
            # Round trips a year, annualised from what has actually happened. Its job is
            # to be compared against the backtest's turnover: a live rule churning far
            # faster than the research said it would is trading a different signal.
            turnover=round(self._n_fills / 2 / max(days / 365.25, 1 / 365.25), 2)
            if days else 0.0,
            equity=round(equity, 2),
            # The marker revalues from these between bars; without them a position is
            # invisible to it and its P&L freezes at whatever the last close implied.
            cash=round(self._cash, 4), units=self._units,
            capital=self.config.capital)
        paper_state.flush()

    def on_order_filled(self, event) -> None:
        self._n_fills += 1
        # `.as_double()`, not `float()`. A Nautilus Quantity carries its own precision and
        # `float()` on a fractional one does not round-trip — whole-share equities came
        # back correct while 0.014691 BTC arrived as 1e-06, exactly one size increment.
        price = event.last_px.as_double()
        qty = event.last_qty.as_double()
        side = event.order_side.name if hasattr(event.order_side, "name") else str(event.order_side)
        signed = qty if side == "BUY" else -qty
        # Keep this system's own book. Cash pays for what it bought and receives what it
        # sold, so equity = cash + units x price stays right through partial fills and
        # reversals without ever consulting the shared account.
        before = self._units
        # What this fill CLOSED, against the average cost of the exposure it closed, and
        # the basis the remainder carries on. `_entry` is that average cost now, not the
        # price of the fill that opened the position: a position scaled into has several
        # prices behind it and only their average prices what a partial sell just closed.
        realised, self._entry = fill_pnl.apply_fill(before, self._entry, signed, price)
        self._units += signed
        self._cash -= signed * price
        self.log.info(f"FILLED {side} {qty} @ {price} -> mine {self._units:.6f} "
                      f"cash {self._cash:.2f}")
        if not self.config.export_state:
            return
        equity = self._cash + self._units * price
        paper_state.update(
            self._sid, paper_trades=self._n_fills,
            position_units=round(self._units, 8), entry=self._entry,
            state="long" if self._units > 0 else "short" if self._units < 0 else "flat",
            paper_pnl_pct=round((equity / self.config.capital - 1.0) * 100.0, 3),
            equity=round(equity, 2),
            cash=round(self._cash, 4), units=self._units,
            capital=self.config.capital)
        paper_state.push_trade(
            self._sid, pd.Timestamp(event.ts_event, unit="ns", tz="UTC")
            .strftime("%Y-%m-%d %H:%M"), side, qty, price,
            round(equity - self.config.capital, 2),
            realised=None if realised is None else round(realised, 2))
        paper_state.flush(force=True)      # a fill is the event people watch for

    def _rebalance(self, price: float) -> None:
        """Trade towards the target exposure — but only when it is worth trading.

        The naive version of this method re-sized to an exact fraction of equity on every
        single bar, and it was wrong in a way that only showed up under `backtest_paper.py`:
        `SMA_200` on 1200 daily SOXL bars produced **637 fills**, against the two or three
        round trips a year the research measured. Every bar the price moved, the "correct"
        share count moved with it, so a position the rule never asked to change was
        nonetheless traded, all day, forever.

        It matters beyond noise in a log. The vectorised engine compounds
        `(1 + pos.shift(1) * ret)`, which for a held long is arithmetically identical to
        holding the share count fixed — so the drift-chasing version was not a more precise
        implementation of the backtest, it was a different strategy that pays a spread on
        every bar. Three conditions therefore justify an order, and nothing else does:

        * the **target changed** — the rule actually said something new;
        * the target is **short** — a short's required share count genuinely does move with
          equity every bar, and not re-sizing it is the constant-fraction/constant-share
          divergence the master study documents;
        * drift has exceeded the **deadband** — a long-run guard so a position that has
          doubled is not still sized for the account it was opened in.
        """
        # Sized on THIS system's equity — its own cash plus the mark on its own units —
        # not the venue account and not the netted position. Both of those are shared, and
        # using either made a system's size depend on how many neighbours happened to trade
        # the same instrument first.
        #
        # Equity, not cash, is the denominator: on a margin account cash does not move when
        # a position opens, so sizing off a cash balance pins the share count to the
        # account's *starting* value and the system never compounds a gain.
        if price <= 0:
            return
        inst = self.cache.instrument(self.config.instrument_id)
        if inst is None:
            return
        equity = self._cash + self._units * price

        want_units = self._target * self.config.target_fraction * equity / price
        have_units = self._units
        delta = want_units - have_units

        changed = self._target != self._last_traded_target
        drift = abs(delta) / abs(want_units) if want_units else (1.0 if have_units else 0.0)
        if not (changed or self._target < 0.0
                or drift > self.config.rebalance_deadband):
            return
        self._last_traded_target = self._target

        try:
            qty = inst.make_qty(abs(delta))
        except Exception:
            return
        if qty.as_double() <= 0:
            return

        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
            quantity=qty,
        )
        self.log.info(f"{'BUY' if delta > 0 else 'SELL'} {qty} "
                      f"(have {have_units:.4f} -> want {want_units:.4f})")
        self.submit_order(order)

    def on_order_rejected(self, event) -> None:
        """A rejection is not a completed trade, and `_last_traded_target` must not be
        left claiming otherwise — that flag is what suppresses duplicate orders, so a
        rejected order would leave the strategy permanently flat while its logs cheerfully
        reported the right target. Roll it back so the next bar retries."""
        # NaN compares unequal to everything, including itself, so the next bar's
        # `changed` test is guaranteed true and the order is re-attempted. Setting it back
        # to a number would risk matching the new target and swallowing the retry.
        self._last_traded_target = float("nan")
        self.log.warning(f"order rejected ({getattr(event, 'reason', '?')}); "
                         f"retrying on the next bar")

    def on_order_denied(self, event) -> None:
        self.on_order_rejected(event)

    def on_stop(self) -> None:
        self.log.info(f"stopped with {len(self._bars)} bars buffered, "
                      f"target {self._target:+.0f}")
