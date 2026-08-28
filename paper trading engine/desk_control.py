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

# How many of its own bars a member strategy may go without a single price before the desk
# says so in the ledger. Three, because one missed poll is normal — `td_nautilus` retries,
# and a vendor that is slow to settle a bar produces exactly one gap — while three in a row
# is a subscription that is not working rather than a feed that is late.
FEED_SILENCE_BARS = 3


# Nautilus caps `order_id_tag` at 36 characters and REJECTS a duplicate outright, so a
# plain slice is a collision waiting for two long names that share a prefix.
TAG_MAX = 36
TAG_HASH = 8


# The class keys in words, for text a reader sees. Deliberately singular: every use
# is an adjective in front of a noun ("across 100 US stock names").
_CLASS_WORDS = {
    "us_stocks": "US stock",
    "us_etfs": "ETF",
    "crypto": "crypto",
    "commodities": "commodity",
    "cme_futures": "CME futures",
}


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
        self._attached_at: dict[str, object] = {}   # registration_id -> when it went live
        self._quiet: set[str] = set()               # already reported as receiving nothing
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
            self._watch_feeds(now)
        except Exception as exc:
            self.log.error(f"feed watch failed: {exc}")
            failed = failed or f"feed watch failed: {exc}"
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
            # 1m is the one timeframe whose cost is not amortised by the bar being long.
            # Every other size polls once per symbol per bar and a bar is minutes or
            # hours; at 1m it is once per symbol per MINUTE, so the desk's whole vendor
            # budget is spent by a few dozen tickers. Refused here, with the number, so
            # the answer is a sentence its owner can act on rather than a feed that
            # degrades for every book on the desk including the ones already running.
            if reg["tf"] == "1m":
                over = self._minute_budget_exceeded(reg)
                if over:
                    raise RuntimeError(over)

        # Both kinds, and after both of the lists above, because a timeframe can be on the
        # desk's offer and still be one this CLASS's vendor cannot serve. `cme_futures` at
        # 4h is exactly that, permanently. Refused here so the owner reads a sentence,
        # rather than in `_subscribe_bars` where it is logged into a Nautilus task and goes
        # nowhere.
        can, why = _feedable(cls, reg["tf"])
        if not can:
            raise RuntimeError(why)

        strategy = self._build(reg, paper_config.VENUES[cls])
        # Start it ourselves ONLY if the trader is already running. The controller's first
        # tick happens inside `on_start`, i.e. during the trader's own start sequence — so
        # a strategy added there is started again a moment later by `Trader._start`, and
        # the second one logs `InvalidStateTrigger('RUNNING -> START')`. Harmless, and
        # exactly the kind of red herring that gets investigated at 2am.
        self.create_strategy(strategy, start=self._trader.is_running)
        self._running[rid] = strategy
        self._attached_at[rid] = self.clock.utc_now()
        self._quiet.discard(rid)
        deskdb.mark_registration(rid, "live")
        self.log.info(f"started {rid} ({reg['kind']}) on {', '.join(reg['symbols'])}")

    def _minute_budget_exceeded(self, reg: dict) -> str:
        """Would attaching this 1m registration take the desk past `MAX_1M_SYMBOLS`?

        Counted over DISTINCT symbols already subscribed at 1m, not over registrations,
        because `td_nautilus` keys its poll tasks on the bar type: three members trading
        the same twenty tickers cost twenty polls between them, not sixty. Counting
        registrations would refuse the cheap case and wave the expensive one through.

        Read off the LEDGER rather than off `self._running`, so a registration that is
        applied but not yet started still counts against the budget — otherwise a burst
        arriving in one tick each sees an empty desk and they are all admitted.
        """
        wanted = {s for s in reg["symbols"]}
        live: set[str] = set()
        try:
            for other in deskdb.active_registrations():
                if other.get("tf") != "1m":
                    continue
                if other.get("strategy_id") == reg.get("strategy_id"):
                    continue
                live.update(other.get("symbols") or [])
        except Exception as exc:                       # noqa: BLE001 - reported, not fatal
            # A ledger this process cannot read is not a reason to admit an unbudgeted
            # subscription: fail closed, and say which way it failed.
            return (f"could not price this 1m registration against the desk's budget "
                    f"({type(exc).__name__}: {exc}); refusing rather than guessing")
        total = len(live | wanted)
        if total <= paper_config.MAX_1M_SYMBOLS:
            return ""
        return (f"this would take the desk to {total} distinct symbols at 1m and the "
                f"ceiling is {paper_config.MAX_1M_SYMBOLS}. One minute bar costs one "
                f"vendor request per symbol per minute, so 1m is the one size where a "
                f"few dozen tickers spend the whole feed's budget — {len(live)} are "
                f"already subscribed. Trade fewer names, or use 5m.")

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
            # Refused here rather than at `on_start`, where a ValueError inside a Nautilus
            # task is logged and goes nowhere — the failure this desk has already paid
            # fifteen hours for once. A class with no bell has no "before the close".
            signal_tf = reg.get("signal_tf") or None
            if signal_tf:
                if signal_tf not in paper_config.MEMBER_TIMEFRAMES:
                    raise RuntimeError(
                        f"signal_tf {signal_tf!r} is not a timeframe the desk can feed")
                if reg["cls"] not in paper_config.SESSION_CLOSE:
                    raise RuntimeError(
                        f"{reg['cls']} trades around the clock, so there is no bell to "
                        f"decide before; signal_tf only applies to a class with a session")
            return BookStrategy(config=BookStrategyConfig(
                order_id_tag=tag, rule=reg["rule"], name=reg["name"],
                account=reg["account"], cls=reg["cls"], tf=reg["tf"],
                symbols=tuple(names), venue=venue,
                capital=float(reg["capital"]), allow_short=bool(reg["allow_short"]),
                benchmark=reg["benchmark"],
                # A book that DECIDES EARLY watches a finer bar than it trades, so its
                # warmup is counted in those bars and not in sessions. Both knobs move
                # together or the book warms for weeks on a buffer sized for days.
                signal_tf=signal_tf,
                window_bars=(paper_config.DECIDE_EARLY_WINDOW_BARS if signal_tf
                             else paper_config.DEFAULT_WINDOW_BARS),
                export_state=self.config.export_state,
                # The note is read by people, on a page that gets shown to people, so
                # it says the class in words. `reg['cls']` is the internal key and was
                # printing straight through as "100 us_stocks names".
                note=f"{reg['rule']} held as one book of "
                     f"${float(reg['capital']):,.0f} across {len(names)} "
                     f"{_CLASS_WORDS.get(reg['cls'], reg['cls'])} names at {reg['tf']}"
                     + (f", deciding {paper_config.DEFAULT_DECIDE_LEAD_MIN}m before the "
                        f"close off {signal_tf} bars." if signal_tf else ".")))

        if reg["kind"] == "house_rule":
            symbol = reg["symbols"][0]
            inst = instrument_for(symbol, reg["cls"], venue)
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

    # ------------------------------------------------------------------ is it fed?
    def _watch_feeds(self, now) -> None:
        """Say so when a strategy is attached and no prices are arriving.

        A subscription that fails does so inside the data client, in a Nautilus task,
        where the exception is logged and goes no further. The registration stays `live`,
        the console stays green, and the only symptom reaches the manager as a refusal on
        every order — *"no price for BTC/USD yet ... try again after the next 5m close"* —
        which blames the next bar close for something no bar close will fix. Two member
        strategies sat exactly like that for fifteen hours, both reading `live`.

        So the desk checks the one thing it can observe from here: attached for longer
        than `FEED_SILENCE_BARS` of its own bars, and still not one price. It writes a
        REASON and leaves `state` alone — `live` is the truth, the strategy is attached
        and would trade the moment a bar arrived, and demoting it would race `_reconcile`,
        which owns that column. `reason` is already carried by `/v1/strategies` and
        rendered under *Desk says*, so it reaches the person who can act on it.

        Reported once per silence, and cleared when a price shows up, so a strategy that
        recovers stops being accused of it.
        """
        for rid, strat in self._running.items():
            if not isinstance(strat, MemberStrategy):
                continue
            since = self._attached_at.get(rid)
            if since is None:
                continue
            fed = any(p for p in strat.prices().values())
            if fed:
                if rid in self._quiet:
                    self._quiet.discard(rid)
                    deskdb.mark_registration(rid, "live", reason=None)
                    self.log.info(f"{rid}: prices are arriving again")
                continue
            if rid in self._quiet:
                continue
            if now - since < _bar_interval(strat.config.tf) * FEED_SILENCE_BARS:
                continue
            self._quiet.add(rid)
            reason = (f"attached, but no {strat.config.tf} bar has arrived for "
                      f"{', '.join(strat.config.symbols)} since it started. Orders will "
                      f"be refused for want of a price until one does. Check the desk log "
                      f"for a failed subscription.")
            deskdb.mark_registration(rid, "live", reason=reason)
            self.log.error(f"{rid}: {reason}")

    def _retire(self, rid: str) -> None:
        self._attached_at.pop(rid, None)
        self._quiet.discard(rid)
        strategy = self._running.pop(rid, None)
        if strategy is None:
            return
        # `remove_strategy` stops it first. The record is deliberately NOT deleted: a
        # forward test somebody can erase is not a record, and a manager who could remove
        # a losing run could remove the evidence of it.
        self._trader.remove_strategy(strategy.id)
        # The published projection is not the record: a retired book must leave the board
        # rather than sit on it frozen until the next restart happens to clear it.
        sid = getattr(strategy, "_sid", None)
        if sid:
            paper_state.unregister(sid)
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
def instrument_for(symbol: str, cls: str, venue: str):
    """One dispatcher, in `td_nautilus`, rather than a branch per call site.

    The branch was `pair if cls in PAIR_CLASSES else equity` in five places. `cme_futures`
    is neither — a whole-share equity rounds its book to nothing and a `pair_instrument`
    would read `ES.v.0` as a currency called `ES.v.0` — so a third arm had to exist
    everywhere at once or the leg would have been one shape here and another there.
    """
    import td_nautilus
    return td_nautilus.instrument_for(symbol, cls, venue)


def _feedable(cls: str, tf: str) -> tuple[bool, str]:
    """Can this class's live client actually subscribe to this timeframe?

    Lazily, and only for the class that has an answer to give. `db_live` is where the
    capability lives — the GLBX ohlcv archive has no 4h or 15m schema at all, and its 1m
    bars fold whole sessions before 2016 — and it is checked HERE so a registration is
    refused with a sentence its owner can read, rather than raising inside `_subscribe_bars`
    where a Nautilus task logs it and it goes nowhere. That silence is what left two member
    strategies reading `live` for fifteen hours with every order refused for want of a
    price. `paper_config` cannot make this check itself: it is imported by the dashboard
    builder and may not pull a vendor client in.
    """
    if cls != "cme_futures":
        return True, ""
    import db_live
    if not db_live.can_feed(tf):
        return False, _why_not_feedable(tf)
    if not db_live.have_key():
        return False, db_live.NO_KEY
    return True, ""


def _why_not_feedable(tf: str) -> str:
    """Say why THIS timeframe cannot be fed, not why some other one cannot.

    There was one sentence for every refusal and it explained `4h` and `15m` — so a member
    who asked for **1m** was told the GLBX archive "has no 4h or 15m schema at all, and the
    sheets at those sizes were cut from cached 1m bars", which answers a question they did
    not ask and then names 1m as the thing those sheets came FROM. The obvious reading is
    that 1m must therefore be available. It is the most confusing possible answer to the
    request that was actually made.

    The two cases are genuinely different and only one of them is about the archive:

    * **4h, 15m, 5m and the rest** — the GLBX ohlcv archive has no such schema. Nothing can
      be done about it here or anywhere; the sheets at those sizes were cut from cached 1m
      bars offline, which a live poll cannot do.
    * **1m** — the schema EXISTS (`db_intraday` fetches `ohlcv-1m` and the cache holds it).
      What is missing is FRESHNESS: this desk reads the historical archive, which lags real
      time by about eight minutes, so `db_nautilus.POLL_LAG` waits fifteen. A minute bar
      delivered fifteen minutes after it closed is fifteen bars stale, which is not a slow
      feed — it is a different strategy from the one anybody backtested. Fixing it needs
      Databento's LIVE product, which is a subscription decision rather than code.
    """
    import db_live
    import db_nautilus
    servable = ", ".join(sorted(db_live.SCHEMA))
    if tf == "1m":
        lag = db_live.ARCHIVE_LAG_SECONDS // 60
        poll = db_nautilus.POLL_LAG // 60
        return (
            f"1m is not a bar this desk can trade on CME futures, and the reason is "
            f"freshness rather than the archive: Databento does have `ohlcv-1m` — it is "
            f"what `data/futures/1m` was fetched from — but the HISTORICAL archive this "
            f"desk polls lags real time by about {lag} minutes, so a minute bar could not "
            f"be acted on until roughly {poll} minutes after it closed. That is ~{poll} "
            f"bars stale, which is a different strategy from the one you backtested, not "
            f"a slower version of it. Serving it properly needs Databento's LIVE feed, "
            f"which is a subscription decision. The leg runs {servable} today; at 1m, use "
            f"a class fed by Twelve Data (us_stocks, us_etfs, crypto, commodities).")
    return (f"{tf} is not a bar Databento can serve for CME futures. The leg runs "
            f"{servable} — the GLBX ohlcv archive has no {tf} schema at all, and the "
            f"research sheets at that size were cut from cached 1m bars offline, which a "
            f"live poll cannot ask for.")


def _bar_interval(tf: str):
    """How long one of this timeframe's bars is. Lazily, for the same reason as above:
    `td_live` is the vendor client, and this module must stay readable without it.

    `td_live.INTERVALS` is the one place a timeframe's length is written down — the same
    table `td_nautilus.timeframe_of` now derives from — so the watchdog and the poller
    cannot disagree about how long "three bars" is.
    """
    import td_live
    return td_live.INTERVALS[tf][1]

