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

import os
from datetime import timedelta

import desk_orders
import paper_config
import paper_state
import symbol_resolve
import venue_instruments
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

# A ceiling on how many member strategies the desk will hold at once.
#
# **The mechanism is kept and the number is not a limit any more.** It was 60, and what it
# guarded against is real and still is: a runaway script registering until the venue
# account is meaningless, the board is unreadable and the ledger is full. The owner has
# asked for no practical ceiling, so it is a number no real use reaches rather than a
# check that was deleted — a bug that used to stop at 60 would otherwise fill the ledger
# with nothing anywhere to stop it, and the refusal it raises is the only evidence such a
# loop would ever produce.
#
# **What actually protects the desk now is the FEED budget and the rate limits, not this
# count**, and they are the numbers to reason about before widening anything else:
#
#   `paper_config.MAX_1M_SYMBOLS`   120 distinct symbols at 1m — one vendor request per
#                                   symbol per minute against a 610/minute budget
#   `paper_config.MAX_OPEN_SYMBOLS` 200 names from outside the pinned legs
#   `api_config.MAX_ORDERS_PER_MINUTE`  60 orders a minute, per account, counted from the
#                                   store so restarting the API hands nobody a fresh
#                                   allowance
#
# Each of those is per SYMBOL or per MINUTE, which is the unit that costs something. A
# strategy count never was: a hundred books on twenty tickers cost twenty subscriptions.
MAX_MEMBER_STRATEGIES = int(os.environ.get("STOCKHUNT_MAX_MEMBER_STRATEGIES") or 100_000)

# How many not-yet-registered member books `run_paper` pre-funds each venue account for.
#
# It was `MAX_MEMBER_STRATEGIES`, and that stopped being a usable number the moment the
# ceiling above became 100,000: a venue account has to be funded before its client
# connects, so the headroom is multiplied by the per-system capital, and a Nautilus `Money`
# refuses anything above 9,223,372,036 — the node would fail to BUILD, on every start, for
# a limit nobody set deliberately.
#
# Separated rather than shrunk, because the two were never the same quantity. This is a
# GUESS at how many books might attach between now and the next restart; that is a REFUSAL
# ceiling. Books already in the ledger are funded exactly, on top of this, so the guess
# only has to cover what arrives while the desk is up.
FUNDING_HEADROOM_STRATEGIES = 60

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



def book_refusal(reg: dict) -> str:
    """Why this book may not attach, or "" if it may.

    A module-level function rather than an inline check inside `_launch`, because these
    two refusals are the only thing standing between the leaderboard and a book that
    trades a different strategy from the one that was scored — and a check that can only
    be exercised by building a whole `TradingNode` is a check nobody writes a test for.

    **This is the bind, and `catalog.cells` is the courtesy.** The catalog marks a row
    untradable so the picker can say why instead of offering it, but a registration
    reaches this path from an older `catalog.json`, a hand-written row or a member's API
    call, none of which consulted the build that made the mark.
    """
    if reg.get("tf") not in paper_config.BOOK_TIMEFRAMES:
        return (f"books run at {', '.join(paper_config.BOOK_TIMEFRAMES)} only for now")
    # Buildable and still not runnable. A book recomputes over a ROLLING buffer, so a rule
    # whose value depends on the first bar of the series trades something the backtest
    # never scored — silently, with a filling order path and a healthy log.
    return paper_config.unpromotable_reason(reg.get("rule") or "")


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
        self._underwater: set[str] = set()          # ...and as having no equity left
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
        # Guarded on its own, like every other lane: a book whose marks are momentarily
        # unreadable must not stop the drain, which is where the orders somebody is waiting
        # on actually move.
        try:
            self._watch_equity()
        except Exception as exc:
            self.log.error(f"equity watch failed: {exc}")
            failed = failed or f"equity watch failed: {exc}"
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

        # THE CEILING BINDS HERE, not in `desk_orders`.
        #
        # The API bounds `leverage` on the way in, and that check is the fast, kind one:
        # it answers a caller in their own request. This is the one that binds, for the
        # reason the whole module docstring gives — a row in the ledger is untrusted input
        # whatever the API said about it, and this process is the only one that knows
        # `paper_config.MAX_LEVERAGE` first-hand.
        #
        # It REFUSES rather than clamping. Clamping would run somebody's book on terms
        # their own registration does not record, so the ledger and the desk would disagree
        # about what was permitted and every refusal afterwards would read as a bug.
        refusal = _leverage_refusal(reg)
        if refusal:
            raise RuntimeError(refusal)

        # Bound here rather than only inside the `else` below, so the admit near the end
        # of this method cannot depend on two guards agreeing about what a book is.
        # `opened` is symbol -> the class the desk decided it is, not symbol -> `cls`.
        opened: dict[str, str] = {}

        if reg["kind"] == "book":
            # A book names no symbols: it holds whoever is in the class right now, and
            # `_refresh_universes` keeps that current while it runs. Checking a stored
            # list would freeze the roster at registration time, which is the opposite of
            # what a point-in-time top 100 is for.
            refusal = book_refusal(reg)
            if refusal:
                raise RuntimeError(refusal)
        else:
            # A symbol outside the pinned legs is RESOLVED, not refused. Nothing is
            # admitted yet — see `_resolve_open` for why the two halves are apart.
            opened = self._resolve_open(reg)
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

        # WHICH CLASS IS EACH SYMBOL ON? Every symbol already on the desk answers for
        # itself; anything `_resolve_open` just decided answers with what it decided; and
        # a `book`, which names no symbols at all, produces an empty map that means "the
        # registration's own class, as it always did".
        held = symbol_classes(reg, opened)

        # Both kinds, and after both of the lists above, because a timeframe can be on the
        # desk's offer and still be one a CLASS's vendor cannot serve. `cme_futures` at 4h
        # is exactly that, permanently. Refused here so the owner reads a sentence, rather
        # than in `_subscribe_bars` where it is logged into a Nautilus task and goes
        # nowhere.
        #
        # **Asked PER CLASS, and the refusal names the symbol.** A mixed book is refused
        # WHOLE rather than having the unfeedable leg dropped, and that is the deliberate
        # half: silently trading four of somebody's five symbols is a book that is not the
        # book they registered, and nothing downstream — the curve, the P&L, the record —
        # would say which one it is. So the answer is no, and it names the symbol and its
        # class so the fix is obvious, rather than naming a class the registration may not
        # even mention.
        for sym_cls in sorted(set(held.values()) | {cls}):
            can, why = _feedable(sym_cls, reg["tf"])
            if not can:
                named = sorted(s for s, c in held.items() if c == sym_cls)
                where = f" ({', '.join(named)})" if named else ""
                raise RuntimeError(f"{why}{where}")

        # LAST, and only once every refusal above has passed. Admitting is a side effect
        # on process-wide state — `paper_config.CLASS_OF` grows, an instrument reaches the
        # cache and the venue, and the symbol counts against `MAX_OPEN_SYMBOLS` — so doing
        # it before the timeframe and feed checks would leak a symbol per rejected
        # registration, and sixty rejected 4h futures books would exhaust a ceiling that
        # exists to bound the FEED. `opened` is empty for a book and for a registration
        # that named nothing new, so this needs no second guard on the kind.
        self._admit_open(opened)

        strategy = self._build(reg, held)
        # Start it ourselves ONLY if the trader is already running. The controller's first
        # tick happens inside `on_start`, i.e. during the trader's own start sequence — so
        # a strategy added there is started again a moment later by `Trader._start`, and
        # the second one logs `InvalidStateTrigger('RUNNING -> START')`. Harmless, and
        # exactly the kind of red herring that gets investigated at 2am.
        self.create_strategy(strategy, start=self._trader.is_running)
        self._running[rid] = strategy
        self._attached_at[rid] = self.clock.utc_now()
        self._quiet.discard(rid)
        self._underwater.discard(rid)
        # A caveat, not a refusal — `reason` beside `live`. See `_caveat`.
        #
        # Caught here rather than inside the caveat builders: a caveat is a courtesy and
        # must never fail an attach, but a courtesy that fails SILENTLY reads exactly like
        # one with nothing to say. So it is non-fatal and loud, in that order.
        try:
            caveat = _caveat(reg)
        except Exception as exc:                   # noqa: BLE001 - reported, never fatal
            self.log.warning(f"{rid}: could not build the registration caveat "
                             f"({type(exc).__name__}: {exc}); attaching without one")
            caveat = ""
        deskdb.mark_registration(rid, "live", caveat or None)
        self.log.info(f"started {rid} ({reg['kind']}) on {', '.join(reg['symbols'])}")

    # --------------------------------------------------------------- open symbols
    def _resolve_open(self, reg: dict) -> dict[str, str]:
        """Which of this registration's symbols are new, and what class each of them is.

        **The desk used to refuse anything outside `paper_config.CLASS_OF` and the reason
        it gave was wrong.** It said a new symbol "costs an instrument, a subscription and
        a full warm-up", and only the middle third of that is true. An instrument is a
        value object. A warm-up is what a RULE needs — `TalibRuleStrategy` recomputes a
        recursive indicator over `DEFAULT_WINDOW_BARS` and is a different signal without
        it — and a `MemberStrategy` computes nothing: it trades on instruction and needs
        `_last_price`, which is one bar. So a member naming `ARKK` was refused for a cost
        their strategy does not incur.

        The subscription is the real cost, and it is bounded by `MAX_OPEN_SYMBOLS` in
        `paper_config.admit` rather than by a roster.

        **That distinction is exactly where `house_rule` parts company**, and it is not
        blurred here. A promoted rule is selected off `wf_summary_<class>_<tf>.csv`, which
        ranks rules over that class's research universe; running one on a symbol outside
        that universe trades a ranking that was never computed on the instrument, and it
        needs the full warm-up as well. So the open path is for members only and a house
        rule on an unknown symbol is still refused — with the reason, not with the old
        sentence about instruments.

        Resolution itself is `symbol_resolve.classify`, which is where the identity guard
        lives; this only decides who to ask about and turns a refusal into an exception
        `_reconcile` will write onto the registration.

        **`classify`, not `resolve`, and that is the mixed-class change.** `resolve` asks
        "is this symbol the instrument THIS class means", which needs a declared class and
        a book holding `AAPL` and `BTC/USD` has no single one to offer. `classify` decides
        the class from the symbol and returns it on the verdict, falling back to the
        registration's declared class only where the symbol itself cannot say — a bare
        ticker, which is an equity or an ETF and shares a venue, an instrument shape and a
        vendor either way. Every existing single-class registration therefore resolves
        exactly as it did.

        **Nothing is admitted here.** The caller admits after every other refusal has
        passed, so a registration rejected for its timeframe does not leave a symbol on
        the desk's books.
        """
        unknown = [s for s in reg["symbols"] if s not in paper_config.CLASS_OF]
        if not unknown:
            return {}
        if reg["kind"] != "member":
            raise RuntimeError(
                f"{', '.join(unknown)} is not in the desk's {reg['cls']} universe, and a "
                f"promoted rule may only run on symbols that are. The rule was ranked on "
                f"wf_summary_{reg['cls']}_{reg['tf']}.csv, which scores that universe and "
                f"nothing else, so a ranking on another instrument does not exist. A "
                f"member strategy can trade it — it decides for itself and needs the desk "
                f"only to fill and mark.")
        out: dict[str, str] = {}
        for symbol in unknown:
            verdict = symbol_resolve.classify(symbol, reg["cls"])
            if not verdict.ok:
                raise RuntimeError(verdict.reason)
            out[symbol] = verdict.asset_class
            self.log.info(f"{reg['strategy_id']}: {verdict.reason}"
                          + (f" — trading it as {verdict.asset_class}"
                             if verdict.asset_class != reg["cls"] else "")
                          + ("  [cached verdict]" if verdict.cached else ""))
        return out

    def _admit_open(self, symbols: dict[str, str]) -> None:
        """Put a resolved symbol on the desk: the registry, the cache, and the venue.

        All three, in that order, and each one prevents a different silent failure:

        * `paper_config.admit` — without it `run_paper._split_by_feed` and
          `live_ws.streamable` both read `CLASS_OF.get(symbol)` as `None` and put the
          symbol on the Twelve Data side of the vendor split. For a futures name that is
          the one thing this desk may never do.
        * `self.cache.add_instrument` — `SimulatedExchange.process_bar` builds its
          matching engine by looking the instrument up HERE. `MemberStrategy.on_start`
          does the same thing a moment later, and doing it now as well is what makes the
          venue call below possible at all.
        * `venue_instruments.publish` — builds the matching engine at attach instead of on
          the first bar. The lazy path raises `RuntimeError: No matching engine found`
          inside `run_paper.route_bars_to_sandbox`'s handler, which catches everything so
          that one bad bar cannot kill the feed — so a miss there is not an error, it is a
          book that receives bars and fills nothing while every log line reads healthy.

        **The class comes in with each symbol, not once for the batch.** It used to be one
        argument for the whole registration, which put every admitted name on the
        registration's venue — so a `BTC/USD` registered alongside equities would have been
        admitted as `us_stocks`, built as a whole-share `Equity` on `SANDBOX`, and — the
        part that matters most — filed in `CLASS_OF` as an equity, which is what
        `run_paper._split_by_feed` and `live_ws.streamable` read to keep `cme_futures` away
        from Twelve Data. Getting that map wrong for a futures name is the one thing this
        desk may never do.
        """
        for symbol, cls in sorted(symbols.items()):
            venue = paper_config.VENUES[cls]
            paper_config.admit(symbol, cls)
            inst = instrument_for(symbol, cls, venue)
            if self.cache.instrument(inst.id) is None:
                self.cache.add_instrument(inst)
            at_venue = venue_instruments.publish(inst)
            self.log.info(
                f"admitted {symbol} to the {cls} leg on {venue} "
                f"({'venue notified' if at_venue else 'cache only — no running venue'}); "
                f"{len(paper_config.open_symbols())} of "
                f"{paper_config.MAX_OPEN_SYMBOLS} open symbols in use")

    def _flatten(self, rid: str, strategy) -> int:
        """Close everything a strategy holds, before it is detached. Returns how many.

        Asked of the CACHE rather than of the strategy's own book, because the two answer
        different questions and only one of them binds: `BookStrategy` tracks intended
        units in `self._cash`/`holdings()`, while the venue holds whatever actually
        filled. It is the venue's position that would be stranded, so it is the venue's
        position that is closed.

        **Failures are logged and never raised.** This runs inside `tick()`, which the
        controller's timer drives — an exception here would abort the pass, so the
        registration would never be marked `retired`, so the next tick would try to retire
        it again, forever, with the strategy already gone from `self._running`. A position
        that could not be closed is a thing to report loudly; it is not a reason to wedge
        the desk.

        The close is a market order, so on this venue it fills against the next BAR rather
        than instantly. "Retired" therefore means "told to close" for one bar's width, and
        on a 1d book that is a day. Saying so in the log is the honest version.
        """
        closed = 0
        try:
            # `self.cache`, not `self._trader.cache` — a `Trader` exposes no cache, and
            # a Controller is an Actor, which does.
            positions = self.cache.positions_open(strategy_id=strategy.id)
        except Exception as exc:                       # noqa: BLE001 - never fatal
            self.log.error(f"{rid}: could not read open positions to flatten: {exc}")
            return 0
        for pos in positions:
            try:
                strategy.close_position(pos)
                closed += 1
            except Exception as exc:                   # noqa: BLE001 - never fatal
                self.log.error(
                    f"{rid}: could not close {pos.instrument_id} ({pos.quantity}): {exc}. "
                    f"IT IS STILL OPEN at the venue and nothing owns it now.")
        if closed:
            self.log.info(f"{rid}: closing {closed} position(s) before retiring — market "
                          f"orders, so they fill on the next bar, not instantly")
        return closed

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

    def _build(self, reg: dict, held: dict[str, str] | None = None):
        """A member's registration and a rule promoted off a backtest differ only here.

        Both are rows in the same table with the same lifecycle, and everything downstream
        — the record, the curve, the dashboard — cannot tell them apart. Only the thing
        that decides what to trade changes.

        `held` is symbol -> class, and it is the whole of the mixed-class change at this
        level. A `book` and a `house_rule` ignore it: both trade off a walk-forward sheet
        that is scored on ONE class, so their venue is still the registration's own. A
        `MemberStrategy` carries it, because that is the only kind whose symbols may come
        from several legs at once.
        """
        tag = _order_id_tag(reg["strategy_id"])
        held = dict(held or {})
        venue = paper_config.VENUES[reg["cls"]]

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
                # Sized off the TIMEFRAME, not off one constant. 1,500 bars is six years
                # at 1d and ten weeks at 15m, and a rule whose lookback is expressed in
                # days does not shrink to fit — it computes nothing and holds flat while
                # reading as healthy. `cache_warmup` is what makes the deeper numbers
                # reachable; without a cache the vendor caps the fill at 5,000 and the
                # book says so in its log.
                window_bars=(paper_config.DECIDE_EARLY_WINDOW_BARS if signal_tf
                             else paper_config.window_bars(reg["tf"])),
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

        lev = desk_orders.leverage_of(reg)
        # The benchmark is subscribed and marked but never traded, so it needs a class of
        # its own for exactly the same reason every held symbol does — it is an instrument
        # on a venue, and `MemberStrategy.on_start` builds it the same way.
        watch = dict(held)
        if reg.get("benchmark"):
            watch.setdefault(reg["benchmark"],
                             paper_config.class_of_symbol(reg["benchmark"], reg["cls"]))
        classes = sorted({c for s, c in watch.items() if s in reg["symbols"]}
                         or {reg["cls"]})
        return MemberStrategy(config=MemberStrategyConfig(
            order_id_tag=tag,
            registration_id=reg["strategy_id"], account=reg["account"],
            name=reg["name"], cls=reg["cls"], tf=reg["tf"],
            symbols=tuple(reg["symbols"]), venue=venue,
            # Symbol -> class, as PAIRS rather than as a dict: a `StrategyConfig` is a
            # frozen msgspec Struct and frozen structs are hashable, so a dict field makes
            # the config itself unhashable — which fails wherever Nautilus hashes a config,
            # not here where it would be found. Empty means "every symbol is `cls`", which
            # is what every registration written before this existed means.
            symbol_classes=tuple(sorted(watch.items())),
            capital=float(reg["capital"]), allow_short=bool(reg["allow_short"]),
            leverage=lev,
            benchmark=reg["benchmark"], export_state=self.config.export_state,
            note=f"{reg['name']}: orders arrive over the API. The desk executes and "
                 f"accounts; the strategy itself runs on the manager's own machine."
                 # Named, because a mixed book's `cls` column is its HOME leg and no longer
                 # a description of what it holds. See `symbol_classes` below for why that
                 # column is not rewritten to `mixed`.
                 + ("" if len(classes) < 2 else
                    f" Holds {', '.join(_CLASS_WORDS.get(c, c) for c in classes)} "
                    f"instruments in one book, each on its own venue and vendor, filed on "
                    f"the board under {reg['cls']}.")
                 + ("" if lev == desk_orders.NO_LEVERAGE else
                    f" Levered {lev:g}x — gross exposure up to {lev:g} times equity, so "
                    f"this curve is not comparable to an unlevered one or to the "
                    f"research, which scores unlevered books.")))

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
                    self._say(rid)
                    self.log.info(f"{rid}: prices are arriving again")
                continue
            if rid in self._quiet:
                continue
            if now - since < _bar_interval(strat.config.tf) * FEED_SILENCE_BARS:
                continue
            self._quiet.add(rid)
            self._say(rid)
            self.log.error(f"{rid}: {self._watch_reasons(rid)[0]}")

    # ----------------------------------------------------------------- is it solvent?
    def _watch_equity(self) -> None:
        """Say so when a book's equity reaches zero, instead of leaving it to be inferred.

        **This is what a levered book blowing up looks like, and until it is written down
        it looks like nothing.** `desk_orders` bounds gross exposure at `leverage x equity`,
        so equity falling to zero collapses the ceiling to zero and every order that would
        add exposure is refused. That is the correct behaviour and it is completely silent:
        the registration still reads `live`, the strategy is still attached, bars still
        arrive, and the only symptom the owner gets is that their orders stop working — one
        refusal at a time, in a column they have to go looking for.

        An unlevered book reaches the same state and it matters less there, because it
        cannot be short more than its equity by construction and gets there only by losing
        everything. It is watched all the same: "you have no money left" is not a fact that
        should depend on a setting.

        `state` is deliberately untouched. The book IS live — it can still close what it
        holds, and closing is the one thing somebody at zero equity needs to be able to do.
        Demoting it would also race `_reconcile`, which owns that column.
        """
        for rid, strat in self._running.items():
            if not isinstance(strat, MemberStrategy):
                continue
            # Before the first bar a book is worth its cash and nothing is measurable —
            # `_watch_feeds` owns that state and reporting both would put two sentences on
            # one row for one problem.
            if not any(p for p in strat.prices().values()):
                continue
            equity = strat.equity()
            if equity > 0:
                if rid in self._underwater:
                    self._underwater.discard(rid)
                    self._say(rid)
                    self.log.info(f"{rid}: equity is positive again ({equity:,.2f})")
                continue
            if rid in self._underwater:
                continue
            self._underwater.add(rid)
            self._say(rid)
            self.log.error(f"{rid}: {self._watch_reasons(rid)[-1]}")

    # ------------------------------------------------------- one writer, one column
    #
    # `reason` is a single column and both watches above have something to put in it. Two
    # writers meant the second condition to fire overwrote the first, and — worse — either
    # one CLEARING itself wrote `None` over the other's live warning: a book that was both
    # unfed and broke stopped reporting either the moment one of them recovered. So the
    # watches keep flags and this composes the column from all of them.
    def _watch_reasons(self, rid: str) -> list[str]:
        strat = self._running.get(rid)
        out = []
        if rid in self._quiet and strat is not None:
            out.append(f"attached, but no {strat.config.tf} bar has arrived for "
                       f"{', '.join(strat.config.symbols)} since it started. Orders will "
                       f"be refused for want of a price until one does. Check the desk "
                       f"log for a failed subscription.")
        if rid in self._underwater and strat is not None:
            lev = getattr(strat.config, "leverage", 1.0)
            out.append(f"this book's equity has reached {strat.equity():,.2f}. Its gross "
                       f"exposure may not exceed {lev:g}x equity, so at zero the "
                       f"desk will accept only orders that REDUCE what it holds — every "
                       f"order that would open or add is refused until the book is worth "
                       f"something again. Close the positions, or retire it: retiring "
                       f"flattens, and keeps the record.")
        return out

    def _say(self, rid: str) -> None:
        """Publish whatever the watches currently hold. Empty clears the column."""
        reasons = self._watch_reasons(rid)
        deskdb.mark_registration(rid, "live", "\n\n".join(reasons) or None)

    def _retire(self, rid: str) -> None:
        self._attached_at.pop(rid, None)
        self._quiet.discard(rid)
        self._underwater.discard(rid)
        strategy = self._running.pop(rid, None)
        if strategy is None:
            return
        # FLATTEN FIRST, and this was missing until 2026-08-28.
        #
        # `remove_strategy` stops the strategy and detaches it, and stopping a strategy
        # does not close what it holds — neither `BookStrategy.on_stop` nor
        # `MemberStrategy.on_stop` ever did. So retiring left an open position at the
        # venue with NOTHING managing it: no strategy to mark it, none to size it, none to
        # exit it, and no way to reach it again short of restarting the node. It sat in
        # the venue account distorting every balance drawn from it, and "retired" on the
        # console read as flat when it was not.
        #
        # Retiring is the owner saying "stop trading this", and a position is trading. So
        # the book is closed before the strategy that owns it goes away — while it can
        # still be closed by the thing that opened it.
        self._flatten(rid, strategy)
        # The record is deliberately NOT deleted: a forward test somebody can erase is not
        # a record, and a manager who could remove a losing run could remove the evidence
        # of it. Flattening ENDS the position; it does not erase the history of it.
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

        # Say on the BOARD that orders arrived and were refused.
        #
        # The desk writes a complete, well-worded reason onto every refused order and for a
        # long time nothing rendered any of it. The manager console reads the ledger and now
        # shows them; the paper BOARD reads `live.json`, which carries fills and not orders
        # — so a system with 127 refusals printed *"No fills yet — this system has not
        # opened a position"*, which is true and useless. That sentence and "nothing has
        # ever been sent to this book" are completely different situations.
        #
        # Only the COUNT crosses over, not the reasons: the board is a public-shaped
        # document and a refusal names sizes and cash balances. It points at the console.
        #
        # Read from the LEDGER rather than from a counter kept here, because a counter
        # resets on restart and would publish a lifetime figure that is not one — the same
        # class of quietly-wrong number this folder keeps paying for. It costs two grouped
        # queries and runs only on a pass that actually rejected something, which on a
        # healthy desk is never.
        if reject:
            self._publish_refusals({row["account"] for row, _ in reject})

        # Last, and only last. A watermark advanced before the batch was handled would
        # lose every order in it if the process died here.
        deskdb.commit_drain(batch[-1]["seq"])

    def _publish_refusals(self, accounts: set[str]) -> None:
        """Put each affected book's order counts on the published record. Never fatal."""
        for account in accounts:
            try:
                summary = deskdb.order_summary(account)
            except Exception as exc:                   # noqa: BLE001 - never fatal
                self.log.error(f"could not read the order summary for {account}: {exc}")
                continue
            for rid, stats in summary.items():
                strat = self._running.get(rid)
                sid = getattr(strat, "_sid", None)
                if not sid or not getattr(strat, "config", None) \
                        or not strat.config.export_state:
                    continue
                paper_state.update(
                    sid,
                    orders_total=int(stats.get("total") or 0),
                    orders_refused=int((stats.get("by_state") or {}).get("rejected") or 0))
        # Debounced, NOT forced. A bot in a rejection loop is exactly the case that reaches
        # this line, and it reaches it on every one-second tick — forcing the write would
        # serialise the whole published document once a second for a display counter. A
        # fill forces its own flush because a trade is a record; this is not one.
        paper_state.flush()


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


def symbol_classes(reg: dict, opened: dict[str, str] | None = None) -> dict[str, str]:
    """Which leg each of this registration's symbols trades on.

    **This is where "class is a property of the symbol, not of the registration" is
    actually written down.** `cls` on the row used to decide the venue, the instrument
    shape and the vendor for everything the registration named. Those three are per symbol
    now and come from here; `cls` keeps a narrower job (see below).

    Three sources, in order, and each is the cheapest one that can answer:

      `opened`             what `_resolve_open` just decided about a symbol the desk had
                           never seen. Passed in rather than re-read, because it is the
                           only source that costs a vendor round trip
      `paper_config.CLASS_OF`   every pinned leg, plus everything `admit` has ever let in
      `reg["cls"]`         the fallback, which is the old behaviour exactly — so a
                           single-class registration comes out of here mapping every one
                           of its symbols to the class it declared, as before

    **`cls` on the row is not dropped and does not become `mixed`.** It is the book's HOME
    LEG: the class the board files it under, the default above, and the venue a `book` or a
    `house_rule` still runs on whole. A literal `mixed` was the obvious alternative and is
    worse — the dashboard's class filter is built from the five research classes, so a
    sixth value puts the book in no pill at all and it disappears from the board rather
    than being grouped imperfectly. The majority class is worse still: it would move the
    book between legs as its holdings change, and a grouping that is not stable is not a
    grouping. So the row stays honest by DISCLOSURE — `_build`'s note names every class the
    book holds, and `MemberStrategy` publishes them.
    """
    out = {}
    for symbol in reg.get("symbols") or ():
        out[symbol] = ((opened or {}).get(symbol)
                       or paper_config.class_of_symbol(symbol, reg["cls"]))
    return out


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
      What is missing is FRESHNESS: this desk reads the historical archive, whose frontier
      has been measured 3.5 to 13 minutes behind real time, and the poll waits for that
      frontier rather than guessing at it. A minute bar delivered thirteen minutes
      after it closed is thirteen bars stale, which is not a slow feed — it is a different
      strategy from the one anybody backtested. Fixing it needs Databento's LIVE product,
      which is a subscription decision rather than code.
    """
    import db_live
    servable = ", ".join(sorted(db_live.SCHEMA))
    return (f"{tf} is not a bar Databento can serve for CME futures. The leg runs "
            f"{servable} — the GLBX ohlcv archive has no {tf} schema at all, and the "
            f"research sheets at that size were cut from cached 1m bars offline, which a "
            f"live poll cannot ask for.")


def _leverage_refusal(reg: dict) -> str:
    """Why this registration may not lever as far as it asked. Empty when it may.

    Two separate refusals, and they are not the same mistake:

    * **Above the desk's ceiling.** `paper_config.MAX_LEVERAGE` is now ONE range for every
      class, 1x to 125x, so this no longer names a class or a venue — it was per class and
      each number was anchored to what a real venue permits, and `paper_config` keeps that
      history and the two facts from it still worth knowing.
    * **Levered, and not a member strategy.** A `house_rule` and a `book` trade a rule off
      a walk-forward sheet, and every one of those sheets scores an UNLEVERED book. Running
      one at 2x would put a number on the board under a rule's name that the rule's own
      research does not describe, and nothing downstream would say so. `desk_orders`
      enforces its ceiling on the order path, **which those two kinds do not use at all**,
      so a levered promotion would not even be bounded — it would simply be unenforced.
      That is unchanged by the ceiling being uniform: it was never about the number.
    """
    lev = desk_orders.leverage_of(reg)
    if lev == desk_orders.NO_LEVERAGE:
        return ""
    if reg.get("kind") != "member":
        return (f"a {reg.get('kind')} cannot be levered. It trades a rule selected off "
                f"wf_summary_{reg['cls']}_{reg['tf']}.csv, and every sheet in this repo "
                f"scores an UNLEVERED book — a levered one would publish a curve under "
                f"that rule's name that the rule's own research does not describe. It also "
                f"never touches the order path where the ceiling is enforced, so it would "
                f"be unenforced rather than merely incomparable. "
                f"Register it at 1x, or run it as a member strategy you send orders to.")
    ceiling = paper_config.max_leverage(reg["cls"])
    if lev > ceiling:
        return (f"{lev:g}x is beyond what this desk will run: the ceiling is {ceiling:g}x "
                f"on every class. At {ceiling:g}x an adverse move of "
                f"{paper_config.wipeout_move_pct(ceiling):.2g}% takes a book sitting on "
                f"its ceiling to zero equity, which is where this number stops.")
    return ""


def _caveat(reg: dict) -> str:
    """Everything TRUE but surprising about a registration the desk is about to ACCEPT.

    Composed from the individual caveats rather than written as one, because they are
    independent: a futures book can be on the slow feed, or unable to afford a unit, or
    both, or neither. Joined with a blank line so a row carrying two still reads.

    **The feed caveat is asked of every class the book HOLDS, not of the class it declared.**
    A mixed book filed under `us_stocks` that also carries `ES.v.0` at 1m runs half of
    itself off Databento's archive, and a caveat keyed on the row's `cls` would have said
    nothing about it — which is this column's whole failure mode, one class over.
    """
    held = symbol_classes(reg)
    feeds = [_feed_caveat(c, reg["tf"]) for c in sorted(set(held.values()) | {reg["cls"]})]
    parts = [c for c in (*feeds,
                         _leverage_caveat(reg),
                         _affordability_caveat(reg)) if c]
    return "\n\n".join(parts)


def _leverage_caveat(reg: dict) -> str:
    """What a member has actually agreed to by asking for leverage.

    Not a refusal — `_leverage_refusal` already ran and let this through — and not a
    warning about risk, which is the member's business. Two things the member cannot see
    from their own side, and both are arithmetic rather than opinion:

    * **This book's forward record stops being comparable to anything else on the board.**
      `portfolio_wf` and `riskmatch_wf` score UNLEVERED books, so a levered curve beside a
      research number is two different measurements printed in one column.
    * **How far the market has to move to end the book.** The ceiling is
      `leverage x equity`, so a book sitting on it loses everything to a move of
      `100 / leverage` per cent — **0.8% at 125x**. That is the honest number for what a
      leverage setting costs, it is one division, and it is the thing a member is least
      likely to have worked out. It replaces the old per-class sentence, which could lean
      on "the venue behind this class allows this much" and no longer can: since
      2026-08-29 the ceiling is 125x on every class and is the OWNER's number, not a
      venue's, so the row has to carry the consequence instead of the provenance.

    Said on every levered row rather than only above some threshold: this column is kept
    rare by only appearing on a book that ASKED for leverage, and at 2x the same sentence
    reads "a 50% adverse move", which is true, unalarming, and tells the reader exactly
    what the scale is before they ever type a larger number into it.
    """
    lev = desk_orders.leverage_of(reg)
    if lev == desk_orders.NO_LEVERAGE:
        return ""
    capital = float(reg.get("capital") or 0.0)
    move = paper_config.wipeout_move_pct(lev)
    return (f"Running at {lev:g}x. Gross exposure — longs plus shorts, counted at their "
            f"absolute size — may reach {lev:g} times this book's equity, so about "
            f"${capital * lev:,.0f} while it is still worth the ${capital:,.0f} it "
            f"started with, and less as it loses. An order past that is refused with the "
            f"numbers in it. The ceiling moves with equity rather than with capital, so a "
            f"book at zero equity may only close positions, never open one. "
            f"AT {lev:g}x A {move:.2g}% ADVERSE MOVE ON A FULLY DEPLOYED BOOK TAKES IT TO "
            f"ZERO — that is 100/{lev:g}, not an estimate. Its published return is still "
            f"measured on the capital you put up, so it is comparable to nothing else on "
            f"the board: the research sheets score unlevered books.")


def _affordability_caveat(reg: dict) -> str:
    """Say it now if this book cannot afford one unit of what it just registered.

    **This is the failure that produced it, and it was invisible for hours.** A member
    pointed a TradingView strategy at `cme_futures` with the standard $10,000 book and an
    alert sending `{{strategy.order.contracts}}`, which is an INTEGER. On this leg a unit
    is a fractional notional unit of a back-adjusted continuous series, so one `NQ.v.0` is
    ~$29,600 and one `YM.v.0` ~$53,700. Every order was refused for want of cash, the
    refusal was correct and well-worded — *"not enough cash: NQ.v.0 2 at 29,616.50 costs
    59,233.00"* — and it was written into the ORDER row, which nobody reads until they
    already suspect something. The registration itself said `live`.

    So the arithmetic is done once, at attach, against the row the owner is looking at.
    It is a caveat and never a refusal: a book that cannot afford a whole unit can still
    afford a fractional one, and choosing sizes is the member's business, not the desk's.

    Priced off the CACHED daily close, not off the vendor. It runs inside `tick()`, so a
    network call here would let a slow vendor stall the controller — and a price a day old
    is ample for "can $10,000 buy something that costs $29,600".

    **It is measured against `capital x leverage`, not against capital**, since 2026-08-29.
    Buying power is what decides whether a whole unit fits, and for a levered book that is
    no longer the capital: a $10,000 book at 4x carries one `NQ.v.0` comfortably, and
    warning that it cannot would be actively wrong — it would tell a member to send
    fractional sizes on the one leg where they had just paid for the ability not to. The
    mechanism is kept; only the number it compares against changed.
    """
    symbols = list(reg.get("symbols") or [])
    capital = float(reg.get("capital") or 0.0)
    leverage = desk_orders.leverage_of(reg)
    # What one order can actually reach on an untouched book. `desk_orders.headroom` on a
    # flat book is exactly `leverage * cash`, and cash on a flat book is the capital, so
    # this is that same number rather than a second opinion about it.
    buying_power = capital * leverage
    if not symbols or buying_power <= 0:
        return ""
    # NOT wrapped in a broad `except` any more, and the reason is that the broad one bit
    # me while writing this: a `NameError` in the test's own stub was swallowed and the
    # function returned "" three runs in a row, reading exactly like "this book can afford
    # it". A courtesy that cannot fail an attach is right; a courtesy that hides a
    # programming error is the same silent-success failure this file is full of warnings
    # about. `_attach` catches and LOGS instead — expected absence returns "" below, and
    # anything unexpected is somebody's bug and should be visible.
    import td_loader
    # Loaded PER CLASS, because `td_loader.load` reads `data/<class>/1d/` and a mixed book
    # has symbols in more than one of those directories. Asking for all of them under the
    # registration's own class returns empty frames for the ones that live elsewhere,
    # which reads as "nothing to say" — and this function's whole job is to say something.
    unit = {}
    by_class: dict[str, list[str]] = {}
    for sym in symbols:
        by_class.setdefault(
            paper_config.class_of_symbol(sym, reg["cls"]), []).append(sym)
    for cls, names in by_class.items():
        bars = td_loader.load(cls, "1d", names)
        for sym in names:
            frame = bars.get(sym)
            # An absent symbol or an empty frame is expected — the cache need not hold a
            # name the desk trades live — and is the one case that is genuinely nothing to
            # say.
            if frame is not None and len(frame):
                unit[sym] = float(frame["Close"].iloc[-1])
    dear = {s: p for s, p in unit.items() if p > buying_power}
    if not dear:
        return ""
    worst, price = max(dear.items(), key=lambda kv: kv[1])
    affordable = buying_power / price
    names = ", ".join(sorted(dear))
    # The book is described by what it can SPEND, and by the two numbers that produced it
    # when they differ. Saying "this book is $10,000" on a 4x registration would understate
    # its reach by four, and a member reading a caveat that contradicts their own leverage
    # setting learns to ignore the caveat.
    size = (f"${capital:,.0f}" if leverage == desk_orders.NO_LEVERAGE else
            f"${capital:,.0f} at {leverage:g}x, so ${buying_power:,.0f} of buying power")
    return (f"Running, but note the size this book can carry: one unit of {worst} costs "
            f"about ${price:,.0f} and this book is {size}, so the most it can "
            f"hold is {affordable:.2f}. On {'these' if len(dear) > 1 else 'this'} symbol"
            f"{'s' if len(dear) > 1 else ''} — {names} — a whole-number size will be "
            f"refused for want of cash. A unit here is a fractional notional unit, not a "
            f"contract, so send fractional sizes or register with more capital.")


def _feed_caveat(cls: str, tf: str) -> str:
    """What is TRUE but surprising about the FEED a registration will run on.

    Written into `reason` beside `live`, a column that until now only ever carried a
    refusal. A caveat is not a refusal and must not read as one — but a member whose fills
    are priced off a bar eight minutes old has to be told, and the row they are already
    looking at is where they will see it. The alternative is a system that runs, fills,
    publishes, and quietly means something other than what its owner thinks it means.

    This is the whole reason 1m is ALLOWED on the CME leg rather than refused. A member
    strategy does not compute its signal from this feed — it arrives over the webhook from
    TradingView's own real-time data, and the desk needs a bar to price a fill and mark a
    book. So the honest answer was never "no", it was "yes, and here is what is stale
    about it".

    **Since 2026-08-28 the answer depends on which feed is running, so it is read rather
    than written.** `db_stream.py` puts the leg on Databento's LIVE gateway, measured at
    0.01 seconds behind a bar's close against the archive's 3.5-13 minutes, and on that feed
    the old caveat is simply untrue. It is still true whenever the leg has fallen back to
    the poller, and that fallback can happen at any moment — a dead socket, a missing SDK
    — so the sentence has to be built from `db_live.FEED_MODE` at the moment the
    registration is marked, not from a constant. Keep this column rare: a caveat on every
    row is a caveat nobody reads, which is why the streaming case returns "" and the row
    reads simply `live`.
    """
    if cls != "cme_futures" or tf != "1m":
        return ""
    import db_live
    if db_live.feed_mode() == "stream":
        # Nothing surprising is true right now: the bar is a fraction of a second old,
        # a shorter delay than the member's own webhook round trip. A caveat here would
        # be noise, and this column is only useful while it stays rare.
        return ""
    return (f"Running. One thing to know about 1m on this class: the desk is polling "
            f"Databento's HISTORICAL archive rather than its live gateway "
            f"({db_live.FEED_MODE.get('why', 'reason unrecorded')}). The archive runs "
            f"up to {db_live.ARCHIVE_LAG_SECONDS // 60} minutes behind real time, so a "
            f"minute bar reaches the desk up to "
            f"{db_live.ARCHIVE_LAG_SECONDS // 60} minutes after it closed and your orders "
            f"fill against it. Your SIGNAL is as timely as whatever sends it; the desk's "
            f"fill PRICE on this class is not until the live feed is back.")


def _bar_interval(tf: str):
    """How long one of this timeframe's bars is. Lazily, for the same reason as above:
    `td_live` is the vendor client, and this module must stay readable without it.

    `td_live.INTERVALS` is the one place a timeframe's length is written down — the same
    table `td_nautilus.timeframe_of` now derives from — so the watchdog and the poller
    cannot disagree about how long "three bars" is.
    """
    import td_live
    return td_live.INTERVALS[tf][1]

