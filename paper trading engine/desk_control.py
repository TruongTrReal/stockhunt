"""The desk's half of the seam: read the ledger, act, write back what happened.

This is a Nautilus `Controller`, and it has to be, for a reason that is easy to miss.
`Trader.add_strategy` contains:

    if self.is_running and not self._has_controller:
        self._log.error("Cannot add a strategy to a running trader")
        return

It *returns* rather than raising — without a controller registered the call is a silent
no-op, the strategy never trades, and one line in the log is the only evidence. And
`_has_controller` is fixed when the Trader is constructed, from
`NautilusKernelConfig.controller`, so it cannot be switched on later: **the node must be
built with a controller whether or not anything is attached at start-up.**
`test_runtime_attach.py` is the gate that proves the whole path works.

Two loops run on every tick, in this order and never the other way round:

1. **Reconcile registrations.** Start what is newly registered, stop what was paused,
   retire what was retired. Ordering matters: an order for a strategy that was registered
   in the same tick must find it running.
2. **Drain orders.** Oldest `seq` first, so a cancel can never overtake its order.

The watermark only advances once a batch has been dealt with, so a desk that dies
mid-batch re-reads rather than loses. Losing an order is the one failure a trading system
may not have; re-reading is safe because a rejected order stays rejected and a submitted
one is already past the watermark.

**The tick is the whole latency of the control plane, so it is one second.** A member
presses Retire, the API writes a row, and nothing happens until this loop comes round —
which at the old 30s meant a click could sit half a minute behind the desk with the
console unable to say whether that was normal. Polling faster is the fix that keeps every
existing guarantee: the ledger stays the single channel, a lost pass costs latency and
never correctness, and the desk still survives the web layer being wedged or gone.

**What is deliberately NOT here is a nudge from the API.** A socket or a callback from the
web process into a live trading node is the one coupling this design exists to prevent —
and it would not even remove the timer, because a notification that can be dropped needs a
sweep behind it anyway. Reading the same table more often does the same job with no new
failure mode.

Ticking that fast has one cost, and `tick` splits the loop to avoid it: the membership
refresh is a file read per running book against a table that changes a handful of names a
year, so it runs on its own slower cadence while the two loops anyone is waiting on run
every pass.

The tick also stamps `deskdb.beat` when it finishes, which is what lets the console tell
"the desk has not got to it yet" apart from "the desk is not running" — two states that
look identical in the registrations table and had been reported as the same sentence.
"""

from __future__ import annotations

from datetime import timedelta

import desk_orders
import paper_config
import paper_state
from member_strategy import MemberStrategy, MemberStrategyConfig
from stockhunt import deskdb
from strategy import TalibRuleConfig, TalibRuleStrategy

from nautilus_trader.live.config import ControllerConfig
from nautilus_trader.trading.controller import Controller

# How often the desk looks at the ledger — the FAST lane, and the whole latency a member
# sees between pressing a button and the desk acting on it.
#
# It was 30s, which put a retire up to half a minute behind the click that asked for it and
# left the console with nothing honest to say in the meantime. One second is affordable
# because the fast lane is two indexed SELECTs against a small table on a local file: the
# work is proportional to what actually CHANGED, and on an idle desk both come back empty.
TICK_SECONDS = 1

# The SLOW lane, and it must not share the fast one's cadence. `_refresh_universes` calls
# `paper_config.book_universe`, which for us_stocks is a pandas CSV load and a datetime
# parse PER RUNNING BOOK — against a membership table that is re-ranked each January and
# changes a handful of names a year. At one second that is ~86,000 reads a day to learn
# nothing; at sixty it is the same answer for 1/60th of the cost.
UNIVERSE_SECONDS = 60

# Capital is per system, not per venue: Nautilus gives one account per venue, so without
# splitting it every strategy sizes against the same balance and they collectively try to
# deploy N times the money that exists.
DEFAULT_CAPITAL = 10_000.0

# A ceiling on what members can collectively ask the sandbox venue to fund. It is not a
# risk limit — nothing here is real money — it is a guard against one runaway script
# registering strategies until the venue account is meaningless.
MAX_MEMBER_STRATEGIES = 60


# Nautilus caps `order_id_tag` at 36 characters and REJECTS a duplicate outright, so a
# plain slice is a collision waiting for two long names that share a prefix.
TAG_MAX = 36
TAG_HASH = 8


def _order_id_tag(strategy_id: str) -> str:
    """A unique 36-character tag for a registration of any length.

    Slicing to 36 was fine while names were `00:spy-1d-sma_200` and broke on the first
    pair: `str_00_commodities-1d-minindex~sarext|vote` and `…|and` are identical for 36
    characters, so the second one Nautilus saw was refused with *Already registered a
    strategy with ID*. The whole rule was lost for a reason that had nothing to do with
    the rule.

    Readable head, hashed tail. The head keeps the account and the class visible in a log;
    the hash is over the WHOLE id, so two registrations differing anywhere differ here.
    """
    if len(strategy_id) <= TAG_MAX:
        return strategy_id
    import hashlib
    digest = hashlib.sha256(strategy_id.encode("utf-8")).hexdigest()[:TAG_HASH]
    return strategy_id[:TAG_MAX - TAG_HASH] + digest


class DeskControllerConfig(ControllerConfig, frozen=True):
    tick_seconds: int = TICK_SECONDS
    universe_seconds: int = UNIVERSE_SECONDS
    stale_bars: float = desk_orders.STALE_BARS
    max_member_strategies: int = MAX_MEMBER_STRATEGIES
    export_state: bool = True


class DeskController(Controller):
    """Owns every strategy the ledger asked for. Never places an order itself."""

    def __init__(self, config: DeskControllerConfig, trader) -> None:
        super().__init__(trader=trader, config=config)
        self._running: dict[str, object] = {}       # registration_id -> strategy instance
        self._universe_at = None                    # when the slow lane last ran
        self.ticks = 0
        self.applied = 0
        self.rejected = 0

    # ------------------------------------------------------------------ lifecycle
    def on_start(self) -> None:
        deskdb.connect()
        # An immediate pass so a desk starting with registrations already on file does not
        # sit idle for the first interval.
        self.tick()
        self.clock.set_timer(
            name="desk-control",
            interval=timedelta(seconds=self.config.tick_seconds),
            callback=lambda _event: self.tick())

    def on_stop(self) -> None:
        try:
            self.clock.cancel_timer("desk-control")
        except Exception:
            pass          # already gone; stopping must not raise

    # ------------------------------------------------------------------ the tick
    def tick(self) -> None:
        """One pass. Public so a test can drive it deterministically instead of waiting
        on timer semantics, which differ between a TestClock and a LiveClock.

        Two lanes at two cadences. Reconcile and drain are the FAST lane and run every
        tick, because both are somebody waiting on an answer. The membership refresh is the
        SLOW lane and is gated on elapsed time — it is the only expensive thing here and
        the only one nobody is watching.
        """
        self.ticks += 1
        now = self.clock.utc_now()
        failed = None
        try:
            self._reconcile()
        except Exception as exc:                     # never let one bad row stop the desk
            self.log.error(f"reconcile failed: {exc}")
            failed = f"reconcile failed: {exc}"
        if self._universe_due(now):
            try:
                self._refresh_universes()
            except Exception as exc:
                self.log.error(f"membership refresh failed: {exc}")
                failed = failed or f"membership refresh failed: {exc}"
        try:
            self._drain()
        except Exception as exc:
            self.log.error(f"drain failed: {exc}")
            failed = failed or f"drain failed: {exc}"
        # Last, and carrying whatever went wrong. Each lane above is guarded on its own so
        # a bad row cannot stop the desk — which means a pass that fails identically every
        # time still completes and still beats. Without `failed` on it, a desk that is up
        # and getting nowhere is indistinguishable from a healthy one, and the console
        # would sit there reporting `live` at somebody waiting for a change that is
        # throwing on every attempt. It goes to the log too; the log is not on screen.
        try:
            deskdb.beat(self.ticks, node=str(getattr(self._trader, "id", "") or ""),
                        at=now.isoformat(timespec="seconds"), error=failed)
        except Exception as exc:
            self.log.error(f"heartbeat failed: {exc}")

    def _universe_due(self, now) -> bool:
        """Has the slow lane come round again?

        Measured on the desk's own clock, never `datetime.now()`: a runtime-attached
        component is handed a fresh clock, and under a TestClock the wall clock is years
        away from the data — the same trap `strategy.py` documents.
        """
        every = timedelta(seconds=self.config.universe_seconds)
        if self._universe_at is not None and now - self._universe_at < every:
            return False
        self._universe_at = now
        return True

    def _refresh_universes(self) -> None:
        """Keep every running book on the current index.

        The top 100 is re-ranked each January with a 120-rank buffer on incumbents, so
        this changes a handful of names a year and does nothing on almost every tick. It
        runs on the tick anyway because the alternative is a restart to pick up a swap,
        and a restart re-warms every system on the desk.

        Departing names are NOT sold here. `set_universe` marks them, and the book unwinds
        each on its own next bar — an order needs a price, and this tick has none.
        """
        from book_strategy import BookStrategy
        for rid, strat in self._running.items():
            if not isinstance(strat, BookStrategy):
                continue
            try:
                names = paper_config.book_universe(strat.config.cls)
            except Exception as exc:
                self.log.error(f"{rid}: cannot read membership: {exc}")
                continue
            if not names or names == strat._live:
                continue
            gone = strat.set_universe(names)
            if gone:
                self.log.info(
                    f"{rid}: index change — {len(gone)} out ({', '.join(gone[:5])}"
                    f"{' …' if len(gone) > 5 else ''}), now {len(names)} names. "
                    f"Each is sold on its next bar and its value funds the rest.")

    # ------------------------------------------------------------------ registrations
    def _reconcile(self) -> None:
        """Make what this process holds match what the ledger asks for.

        Driven by `_running` — what this node ACTUALLY has — rather than by the stored
        `state`, which is a claim about whichever process wrote it. On a restart every
        registration still reads `live` while the new node holds nothing, so a reconciler
        that trusted `state` brought the desk up empty and reported it as running.
        """
        wanted = {r["strategy_id"]: r for r in deskdb.active_registrations()}

        # Anything running that is no longer wanted — retired, or gone from the ledger.
        for rid in [r for r in self._running if r not in wanted]:
            try:
                self._retire(rid)
                deskdb.mark_registration(rid, "retired")
            except Exception as exc:
                self.log.error(f"{rid}: could not retire: {exc}")

        # ...and anything retired while the desk was DOWN, which the loop above structurally
        # cannot see: it iterates what this process holds, and this process never held it.
        # `active_registrations` filters those rows out by `want`, so without this pass no
        # loop looks at them again and the row keeps `state='live'` forever — the owner
        # retired it, the desk honoured that by never starting it, and the record went on
        # saying it was trading. Anything still in `_running` here failed to stop a moment
        # ago and is left for the next tick rather than marked done on a lie.
        for reg in deskdb.unapplied_retirements():
            if reg["strategy_id"] not in self._running:
                deskdb.mark_registration(reg["strategy_id"], "retired")

        for rid, reg in wanted.items():
            try:
                running = rid in self._running
                if reg["want"] == "paused":
                    if running:
                        self._trader.stop_strategy(self._running[rid].id)
                        deskdb.mark_registration(rid, "paused")
                    elif reg["state"] != "paused":
                        # Paused while the desk was down. It is not running, which is what
                        # paused MEANS, so the state is already true and only the record
                        # disagrees. Left alone it never converges: the branch above needs
                        # a running strategy to stop, and there will never be one.
                        deskdb.mark_registration(rid, "paused")
                elif not running:
                    self._launch(reg)
                elif reg["state"] != "live":
                    deskdb.mark_registration(rid, "live")
            except Exception as exc:
                self.log.error(f"{rid}: {exc}")
                deskdb.mark_registration(rid, "rejected", reason=str(exc))

    def _launch(self, reg: dict) -> None:
        rid = reg["strategy_id"]

        if rid in self._running:                     # paused, and now resuming
            self._trader.start_strategy(self._running[rid].id)
            deskdb.mark_registration(rid, "live")
            return

        live_members = sum(1 for r in deskdb.registrations()
                           if r["kind"] == "member" and r["state"] == "live")
        if reg["kind"] == "member" and live_members >= self.config.max_member_strategies:
            raise RuntimeError(
                f"the desk is at its ceiling of {self.config.max_member_strategies} "
                f"member strategies")

        cls = reg["cls"]
        if cls not in paper_config.VENUES:
            raise RuntimeError(f"unknown asset class {cls!r}")

        if reg["kind"] == "book":
            # A book names no symbols: it holds whoever is in the class right now, and
            # `_refresh_universes` keeps that current while it runs. Checking a stored
            # list would freeze the roster at registration time, which is the opposite of
            # what a point-in-time top 100 is for.
            if reg["tf"] not in paper_config.BOOK_TIMEFRAMES:
                raise RuntimeError(
                    f"books run at {', '.join(paper_config.BOOK_TIMEFRAMES)} only for now")
        else:
            unknown = [s for s in reg["symbols"] if s not in paper_config.CLASS_OF]
            if unknown:
                raise RuntimeError(
                    f"{', '.join(unknown)} is not in the desk's universe. Only symbols "
                    f"the desk already subscribes to can be traded — a new one costs an "
                    f"instrument, a subscription and a full warm-up.")
            # Checked here rather than left to `BAR_SPEC[tf]` in the strategy's `on_start`,
            # where the same mistake surfaces as a bare `KeyError('30m')` in the desk log
            # with the registration stuck at `pending` and nothing said to its owner.
            if reg["tf"] not in paper_config.MEMBER_TIMEFRAMES:
                raise RuntimeError(
                    f"{reg['tf']} is not a timeframe this desk can feed. It runs "
                    f"{', '.join(paper_config.MEMBER_TIMEFRAMES)} — each one costs a poll "
                    f"task per symbol against the vendor, so the list is deliberate.")

        strategy = self._build(reg, paper_config.VENUES[cls])
        # Start it ourselves ONLY if the trader is already running. The controller's first
        # tick happens inside `on_start`, i.e. during the trader's own start sequence — so
        # a strategy added there is started again a moment later by `Trader._start`, and
        # the second one logs `InvalidStateTrigger('RUNNING -> START')`. Harmless, and
        # exactly the kind of red herring that gets investigated at 2am.
        self.create_strategy(strategy, start=self._trader.is_running)
        self._running[rid] = strategy
        deskdb.mark_registration(rid, "live")
        self.log.info(f"started {rid} ({reg['kind']}) on {', '.join(reg['symbols'])}")

    def _build(self, reg: dict, venue: str):
        """A member's registration and a rule promoted off a backtest differ only here.

        Both are rows in the same table with the same lifecycle, and everything downstream
        — the record, the curve, the dashboard — cannot tell them apart. Only the thing
        that decides what to trade changes.
        """
        tag = _order_id_tag(reg["strategy_id"])

        if reg["kind"] == "book":
            from book_strategy import BookStrategy, BookStrategyConfig
            names = paper_config.book_universe(reg["cls"])
            if not names:
                raise RuntimeError(f"no names live in {reg['cls']} right now")
            return BookStrategy(config=BookStrategyConfig(
                order_id_tag=tag, rule=reg["rule"], name=reg["name"],
                account=reg["account"], cls=reg["cls"], tf=reg["tf"],
                symbols=tuple(names), venue=venue,
                capital=float(reg["capital"]), allow_short=bool(reg["allow_short"]),
                benchmark=reg["benchmark"],
                window_bars=paper_config.DEFAULT_WINDOW_BARS,
                export_state=self.config.export_state,
                note=f"{reg['rule']} held as one book of "
                     f"${float(reg['capital']):,.0f} across {len(names)} "
                     f"{reg['cls']} names at {reg['tf']}."))

        if reg["kind"] == "house_rule":
            symbol = reg["symbols"][0]
            inst = (td_pair(symbol, venue) if reg["cls"] in paper_config.PAIR_CLASSES
                    else td_equity(symbol, venue))
            from nautilus_trader.model.data import BarType
            return TalibRuleStrategy(config=TalibRuleConfig(
                order_id_tag=tag,
                instrument_id=inst.id,
                bar_type=BarType.from_str(
                    f"{inst.id}-{paper_config.BAR_SPEC[reg['tf']]}"),
                rule=reg["rule"], account=reg["account"],
                allow_short=bool(reg["allow_short"]),
                window_bars=paper_config.DEFAULT_WINDOW_BARS,
                capital=float(reg["capital"]), display_symbol=symbol,
                asset_class=reg["cls"], timeframe=reg["tf"],
                export_state=self.config.export_state,
                note=f"{reg['rule']} on {symbol} {reg['tf']}, promoted from the "
                     f"walk-forward sheet."))

        return MemberStrategy(config=MemberStrategyConfig(
            order_id_tag=tag,
            registration_id=reg["strategy_id"], account=reg["account"],
            name=reg["name"], cls=reg["cls"], tf=reg["tf"],
            symbols=tuple(reg["symbols"]), venue=venue,
            capital=float(reg["capital"]), allow_short=bool(reg["allow_short"]),
            benchmark=reg["benchmark"], export_state=self.config.export_state,
            note=f"{reg['name']}: orders arrive over the API. The desk executes and "
                 f"accounts; the strategy itself runs on the manager's own machine."))

    def _retire(self, rid: str) -> None:
        strategy = self._running.pop(rid, None)
        if strategy is None:
            return
        # `remove_strategy` stops it first. The record is deliberately NOT deleted: a
        # forward test somebody can erase is not a record, and a manager who could remove
        # a losing run could remove the evidence of it.
        self._trader.remove_strategy(strategy.id)
        self.log.info(f"retired {rid}; its record is kept")

    # ------------------------------------------------------------------ orders
    def _drain(self) -> None:
        batch = deskdb.drain()
        if not batch:
            return

        regs = {r["strategy_id"]: r for r in deskdb.registrations()}
        books, prices = {}, {}
        for rid, strat in self._running.items():
            if isinstance(strat, MemberStrategy):
                books[rid] = strat.book()
                prices.update(strat.prices())

        submit, reject = desk_orders.partition(
            batch, regs, books, prices, stale_bars=self.config.stale_bars)

        for row, reason in reject:
            deskdb.mark_order(row["seq"], "rejected", reason=reason)
            self.rejected += 1

        for row in submit:
            strat = self._running.get(row["strategy_id"])
            if not isinstance(strat, MemberStrategy):
                deskdb.mark_order(row["seq"], "rejected",
                                  reason="this strategy does not take orders; it trades "
                                         "a rule the desk selected")
                self.rejected += 1
                continue
            ok, why = strat.place(row)
            if ok:
                deskdb.mark_order(row["seq"], "working")
                self.applied += 1
            else:
                deskdb.mark_order(row["seq"], "rejected", reason=why)
                self.rejected += 1

        # Last, and only last. A watermark advanced before the batch was handled would
        # lose every order in it if the process died here.
        deskdb.commit_drain(batch[-1]["seq"])


# Imported lazily by name so this module can be read without pulling the instrument
# factories in at import time — they reach the vendor's symbol conventions.
def td_equity(symbol: str, venue: str):
    import td_nautilus
    return td_nautilus.equity_instrument(symbol, venue)


def td_pair(symbol: str, venue: str):
    import td_nautilus
    return td_nautilus.pair_instrument(symbol, venue)
