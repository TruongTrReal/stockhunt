# CLAUDE.md

Guidance for Claude Code working in this directory. Read `../CLAUDE.md` first.
**No results here** — the `results/` CSVs and the dashboard own those.

## What this is

The live desk. Live vendor bars in, NautilusTrader simulated fills out, state published for
the dashboard to watch.

`SandboxExecutionClient` is the point of it: a real Nautilus execution client that prices
fills from the live feed instead of sending orders anywhere, so portfolio, position and P&L
accounting is the *same code* that would run against a real venue.

```
backtest   BacktestNode  + BacktestDataClient   + SimulatedExchange
paper      TradingNode   + TwelveDataLiveClient + SandboxExecutionClient   <- here
                         + DatabentoLiveClient
live       TradingNode   + the same two clients + Binance / IB exec client
```

**Two data clients, because the research has two vendors.** Twelve Data feeds four classes
and Databento feeds `cme_futures`; Nautilus keeps them apart by venue, and the section
below on the fifth leg is where that lives.

Going live is a change of execution client in `EXEC_CLIENTS` and nothing else.

**This is plumbing, not a trading recommendation.** What runs here runs to prove that bars
arrive, signals compute, orders fill and P&L accrues.

## Files

```
paper_config.py   sys.path bootstrap, UNIVERSE + class_of, venues, warm-up, top_rules()
store.py          results/paper.db - the record. fills, curve, sessions, gaps
run_paper.py      the desk: builds the TradingNode, starts LiveHub, runs
strategy.py       TalibRuleStrategy - one rule, traded to a target exposure
member_strategy.py  MemberStrategy - trades on INSTRUCTION, not on a rule
desk_control.py   the Nautilus Controller: reconcile registrations, drain orders
desk_orders.py    what the desk will and will not do with an order. No Nautilus import
fill_pnl.py       average cost per position, and what each fill CLOSED against it. Pure
                  arithmetic, no Nautilus and no store, so the live path, the v2->v3
                  migration and the tests all use the same definition
catalog.py        publishes catalog.json: which rules may be promoted to the desk
migrate_owner.py  inspect/apply/verify the account migration on paper.db
paper_state.py    the registry; serialises results/paper_state.json and publishes live.json
td_live.py        Twelve Data REST: bars, prices, market hours. Importable without Nautilus
td_nautilus.py    TwelveDataLiveClient + instrument factories + `instrument_for`, the ONE
                  dispatcher from class to instrument shape
db_live.py        Databento REST: the CME futures leg's WARM-UP, its marks, and the
                  fallback bar feed, ratio back-adjusted. The SECOND vendor, and the only
                  one this class may be asked. Importable without Nautilus, like td_live
db_stream.py      Databento's LIVE gateway: the same vendor over a socket, 0.01s behind a
                  bar's close against the archive's ~8 minutes. Owns its own reconnect,
                  because the SDK's drops the gap. Imports the `databento` SDK LAZILY, so
                  a box without it degrades to the poller instead of failing to start
db_nautilus.py    DatabentoLiveClient, bound to the `GLBX` venue, and the roll/forward-
                  factor arithmetic that keeps a live buffer continuous. Both feeds
                  publish through its `_emit`, so the adjustment has no branch in it
live_ws.py        LiveHub: the ONE Twelve Data tick socket -> paper_state.mark() ->
                  browser socket. Marks and feed health only; it makes no bars
backtest_paper.py the same strategy through a BacktestEngine on cached bars
parity_live.py    measures the rolling window each rule needs to match the full series
alpaca_mirror.py  A SECOND, OPTIONAL PROCESS. Drives Alpaca paper accounts to a scaled
                  copy of this desk's book, so a real broker's fills can be compared
                  against the sandbox's bar-close ones. Reads paper_state.json, imports
                  no Nautilus, and cannot affect the desk
alpaca_client.py  Alpaca REST. The paper host is a CONSTANT with no env override
alpaca_map.py     target - held = delta. Pure functions, the desk_orders.py pattern
alpaca_store.py   state/alpaca.db - what the broker did, and at what price
test_decide_early.py    a book that decides BEFORE the bell matches the backtest
test_store.py           a restart resumes instead of resetting
test_runtime_attach.py  a strategy can join a RUNNING trader. Gate for the whole design
test_member_desk.py     ledger -> running trader -> fill -> book -> record, end to end
test_accounts.py        two accounts on one cell keep separate books  (pytest)
test_desk_orders.py     the rules that decide whether money moves        (pytest)
test_fill_pnl.py        None vs 0.0: what closed nothing against what closed at cost
test_alpaca_map.py      the mirror's arithmetic, offline                 (pytest)
test_alpaca_mirror.py   the mirror converges, is idempotent, and refuses (pytest)
test_futures_leg.py     the CME leg: a fractional instrument, an unfeedable timeframe
                        refused at subscribe time, and the roll reproducing
                        `db_loader.back_adjust` up to one constant on BOTH feeds —
                        polled frames and gateway records. Also: a dropped stream
                        degrades loudly, a reconnect replays the gap    (pytest)
test_live_stream.py     the tick socket: reconnect resubscribes, a silent socket is
                        dropped and says so, arrival is stamped locally, and no
                        futures symbol reaches Twelve Data              (pytest)
```

## The four Twelve Data classes stream, and the stream deliberately makes no bars

`live_ws.LiveHub` holds **one** connection to `wss://ws.twelvedata.com/v1/quotes/price` for
the whole process. One, because the vendor meters concurrent sockets per API KEY and not
per process: a second client opened anywhere in this repo does not add a feed, it evicts
this one, and the eviction is indistinguishable from a flaky network. Anything that wants a
live price reads `hub.prices` or `hub.health()`.

**It marks; it does not decide.** The stream delivers ticks and the research is built on
the vendor's `/time_series` bars, so bars aggregated here would be *this desk's* bars —
close enough to the vendor's to pass every eye test, different enough that the forward
record would stop being comparable to the sheet, and nothing would say so. That is the same
property the Alpaca section above turns on: every fill lands at the signal bar's close,
which is the price the backtest assumed. So a tick never reaches a rule, a target or an
order. It revalues an *existing* position and reports whether the feed is alive.

Three defences against the failure this folder keeps paying for — a feed that has stopped
while every indicator still reads healthy:

* **An application heartbeat** every `HEARTBEAT_EVERY` (10s). It is not the `ping_interval`
  the socket library already sends: a protocol ping is answered by the vendor's edge, the
  heartbeat by the quote service behind it. A connection held open by pings alone reads
  `live` here while the service behind it has stopped publishing.
* **A watchdog**, timed from the last tick or — when there has never been one — from the
  CONNECT. That second half is the point: a subscription the vendor accepted and never
  served has no last tick, so the obvious version of this check never fires on it.
  `QUIET_AFTER` (90s) is reported and nothing more, because an equity book legitimately
  prints nothing overnight; `STALL_AFTER` (300s) closes the socket so the reconnect path
  resubscribes.
* **A published source.** `paper_state.set_feed(stream=..., marks=...)` says which of the
  two is actually carrying the desk, and `start_marker` prints both transitions. The REST
  `/price` poll stays on unconditionally at `--mark-seconds`: it is one request a minute,
  and a safety net that only runs once something has noticed it is needed is not one.

**`cme_futures` may never be sent to this socket**, and the guard is in `live_ws.streamable`
— at the capability, not only at the call sites. `run_paper.start_feed_tracker` already
split the running book by vendor, but the hub's CONSTRUCTOR was handed `build_plan`'s
symbols unfiltered, so `--top 3` put the whole futures leg into the first subscription.
Twelve Data does not refuse `ES.v.0` loudly; it returns it in `subscribe-status.fails`
forever, and an unqualified `ES` there is Eversource Energy.

Measured 2026-08-28, 120s on the live key: 250 ticks over 6 subscribed symbols, `state`
`live` throughout with no reconnect, and a mark never more than ~1s old against the poll's
60. BTC/USD and ETH/USD print about once a second, XAU/USD about every two.

### The bar poll got faster only where it was measured

`td_nautilus.POLL_LAG` is 90s and stays 90s for every size except `1m` and `5m`
(`POLL_LAG_BY_TF`, 40s). At 1m the old lag fired the poll 30 seconds after the NEXT bar had
closed, so minute bars arrived late and in clumps of two — which `MEMBER_TIMEFRAMES`
gaining `1m` made worth fixing.

**Two ways of measuring this wrongly, both of which were hit first:**

* **The vendor serves the FORMING bar immediately.** "A bar with this stamp is present" is
  not "this bar has settled". The first attempt read 1.1s and was timing the bar's
  *appearance*. The settle instant is when its **close stops moving**.
* **The measuring machine's clock was 42 seconds slow**, against the vendor's own `Date`
  header. Every figure is fiction until that is removed — and a desk whose clock runs
  *fast* has a live problem, not a measurement one: `fetch_bars`' forming-bar guard
  compares `datetime.now(timezone.utc)` against the vendor's stamp, so a fast clock hands
  the desk a bar that is still forming.

Corrected, polling once a second across BTC/USD, ETH/USD, XAU/USD and XAG/USD: a bar's
close stopped moving **19.7–24.0s** after its true close, at 1m and at 5m alike — a flat
~20s that reads as the vendor's own aggregation window rather than as network variance.

**A lag shorter than the settle is a look-ahead, not merely an early request.** At close +
15s the interval HAS fully elapsed, so the forming-bar guard keeps the row, and the vendor
then moves its close for another five seconds. 40s is the worst of those readings plus
~65% headroom, sized by what it has to cover rather than by taste: the equity classes were
never in the sample, and at 1m a 40s lag still lands the bar inside its own minute, which
is the property that was broken. `test_live_stream.py` holds the constant above the
measured settle so it cannot be lowered back into that window by accident.

`15m` and up were not measured and are not changed. **The tick stream is deliberately not
consulted by the poll**: a socket does know when a bar truly closed, but the vendor settles
on a fixed delay regardless, so reading the hub would buy nothing and would let a wedged
WebSocket delay a bar — the one thing `live_ws` may never do.

## The second record: Alpaca

This desk fills its own orders. `SandboxExecutionClient` prices a fill from the Twelve Data
bar that produced the signal, so every fill lands at that bar's **close** — the same price
the backtest assumed, which is what makes the live record comparable to the sheet and what
makes the live record unable to check itself. Whether those fills exist at a real venue, at
a real spread, at a real time of day, is not a question the sandbox can answer about itself.

`alpaca_mirror.py` asks it. It is a **separate process** that reads `paper_state.json`,
computes this desk's net exposure per symbol, and drives an Alpaca paper account to a
proportional copy. Nothing in `paper.db`, on the board, or in any published number changes:
Alpaca is a second record, not a replacement for the first.

Four things about it are load-bearing:

* **It reconciles a position, it does not forward an order.** Alpaca paper hands out a
  random partial fill 10% of the time and rejects on asset eligibility, so a fill-forwarder
  drifts within a day and nothing says which fill diverged. Computing a target and sending
  the difference makes a missed cycle cost latency and never correctness, and makes a
  rejected order retry itself next pass. It is `desk_control.tick()`'s design, one process out.
* **Out of process, deliberately.** No HTTP client enters the Nautilus node, and an Alpaca
  outage or rate limit cannot stop the desk. Same separation `stockhunt/deskdb.py` buys
  between the API and the desk.
* **Three classes, three accounts, three key pairs.** `us_stocks`, `us_etfs` and `crypto`
  each get their own Alpaca paper account, so their buying power is separate and their P&Ls
  are separately readable. `commodities` and `cme_futures` are **not mirrored at all** —
  Alpaca sells no spot metals, no FX and no futures — and `alpaca_client.UNSUPPORTED` says
  so by name rather than leaving them merely absent.
* **The target is scaled.** Each class runs six $100,000 books against one $100,000 account,
  so every target is multiplied by `alpaca_equity / desk_equity`. Without it the mirror is a
  stream of rejected buys.
* **The position it reconciles against is `held + pending`, never `held` alone.**
  `/v2/positions` does not know about an order that has not filled yet, so a cycle that
  looks only at positions concludes the trade never happened and sends it again — once per
  `--interval`, for as long as the fill takes. That is not a rare race: market orders sent
  into the opening auction do not print for minutes, so the first cycles of a session
  re-ordered the whole book two and three times, Alpaca refused part of it with
  `potential wash trade detected` (403), and the excess had to be sold back the same
  morning. The round-trip spread on that churn lands in `slip_bp`, which is the one column
  this process exists to measure — so the bug corrupts the measurement, not just the book.
  `alpaca_map.pending_units` reads the unfilled REMAINDER of each open order, so a partial
  fill is counted once.

**The decide-early books have one honest divergence and it is recorded, not hidden.** They
compute at 15:55 ET and trade the close; Alpaca **rejects `cls` (market-on-close) orders
after 15:50 ET**, so the mirror can only send a market `day` order that fills at the 15:55
price rather than in the closing auction. Do not "fix" this by moving
`DECIDE_EARLY_MINUTES` — 5 is what was measured against the 5m cache, and changing it to
suit a broker's cutoff breaks the correspondence with the backtest. The gap goes in
`alpaca.db`'s `slip_bp` column instead, which is the number the whole process exists for.

Outside the session the mirror sends **no** equity order; it records the plan and applies it
on the next open. A market order queued overnight fills at a price nobody decided on, which
is the mistake `desk_orders.STALE_BARS` refuses one process over. Crypto has no such gate.

```powershell
python alpaca_mirror.py --check                 # credentials, equity, symbol coverage
python alpaca_mirror.py --once --dry-run        # plan every class, send nothing
python alpaca_mirror.py --once --class us_etfs  # one class, for real
python alpaca_mirror.py --report                # fill price against the desk's mark
```

Credentials come from the environment, falling back to `.env.local` at the repo root — the
same order `td_live.api_key` uses: `ALPACA_STOCKS_KEY_ID`/`_SECRET`,
`ALPACA_ETFS_KEY_ID`/`_SECRET`, `ALPACA_CRYPTO_KEY_ID`/`_SECRET`.

The long run goes through bash, detached, for the reason in the root `CLAUDE.md`:

```bash
nohup python -u alpaca_mirror.py > logs/alpaca_mirror.log 2>&1 &
```

## Two kinds of strategy, one record

```
TalibRuleStrategy   bar -> signal -> target -> order      the house's own rules
MemberStrategy      order from the ledger -> order        somebody else's logic
```

A manager's code never comes near this process. It runs on their machine, decides what it
wants, and calls the API; `desk_control` drains `stockhunt.deskdb` and calls `place()`. So
there is no sandbox to build, no dependency of theirs to install, and their edge stays
theirs — which is the thing a manager actually cares about.

Both kinds are rows in one `registrations` table with one lifecycle, and everything
downstream — the record, the curve, the dashboard — cannot tell them apart. Promoting a
rule off a walk-forward sheet is `kind='house_rule'` on account `00`; a manager's is
`kind='member'`. Only `desk_control._build` differs.

**A member strategy holds several instruments and one book.** `_cash` is shared across the
registration and `_units` is per symbol, and both are its own — never the Nautilus venue
account, which nets every strategy on an instrument together.

## The controller is not optional

`Trader.add_strategy` contains:

```python
if self.is_running and not self._has_controller:
    self._log.error("Cannot add a strategy to a running trader")
    return
```

It **returns rather than raises** — without a controller the call is a silent no-op, the
strategy never trades, and one line in the log is all there is. `_has_controller` is fixed
when the Trader is constructed from `NautilusKernelConfig.controller`, so **it cannot be
switched on later**: `run_paper.py` builds the node with a `DeskController` whether or not
anything is registered, or a registration can never go live without a full restart and a
re-warm of every system.

`remove_strategy`, `start_strategy` and `stop_strategy` carry no such guard.

**A runtime-attached strategy is handed a brand-new clock** (`self._clock.__class__()`).
Under a LiveClock that is fine; under a TestClock it starts at the epoch until the engine
next advances it. That is why `strategy.py` reads `self.clock.utc_now()` and not
`datetime.now()` — the wall-clock version asks for a bar range in the future and Nautilus
refuses it outright with `start was > now`.

## The tick is the control plane's whole latency, so it is one second

A member presses Retire, the API writes `want='retired'`, and **nothing happens until
`desk_control` comes round**. At the old 30s that put a click up to half a minute behind
the desk, with the console unable to say whether the wait was normal.

Polling the same ledger faster is the fix that changes no guarantee: the ledger stays the
only channel, a missed pass costs latency and never correctness, and the desk still
survives the web layer being wedged or gone. **A nudge from the API into this process is
deliberately not the fix** — a socket from an HTTP server into a live trading node is the
coupling the split exists to prevent, and it would not even remove the timer, since a
notification that can be dropped needs a sweep behind it anyway.

`tick()` therefore runs **two lanes at two cadences**:

| lane | every | what |
|---|---|---|
| fast | `TICK_SECONDS` = 1 | reconcile, then drain. Two indexed SELECTs; both empty on an idle desk |
| slow | `UNIVERSE_SECONDS` = 60 | `_refresh_universes`, a pandas CSV load **per running book** against a table that changes a handful of names a year |

Gated on `self.clock.utc_now()`, never `datetime.now()` — a runtime-attached component is
handed a fresh clock, and under a TestClock the wall clock is years from the data.

**The pass stamps `deskdb.beat` when it finishes**, and only when it finishes: a heartbeat
written at the start would beat steadily through a reconcile that throws every time, which
is the failure it exists to expose. That is what lets the console tell "not yet" from "not
running" — two states the registrations table renders identically.

### Two states used to never converge, and both read as a live book

`_reconcile` drives off `_running`, which is right for a restart and blind in one
direction: it cannot act on a registration this process never held.

* **Retired while the desk was down.** `active_registrations` filters those out by `want`,
  so no loop ever looked at the row again — `state` stayed `live` forever for a strategy
  nothing was running. `deskdb.unapplied_retirements()` is the second pass that closes it.
* **Paused while the desk was down.** The pause branch needs a running strategy to stop,
  and after a restart there is not one, so `want=paused, state=live` was permanent. Not
  running *is* what paused means, so the state is marked true.

Neither lost anything — in both cases the strategy correctly was not trading — but the
record claimed a dead book was live, which is the kind of wrong that gets believed.

## Orders: what binds, and where

`desk_orders` is pure functions over dicts and imports no `nautilus_trader`, so the rules
that decide whether somebody's money moves are exhaustively testable in milliseconds. The
API runs its own checks earlier and cheaper; **these are the ones that bind**, because the
API cannot see the book.

**Buying power is reserved across a batch.** A batch is validated before any of it reaches
the exchange, so without reservation every order is checked against the same starting cash
and ten individually affordable buys collectively overspend. It also makes "buy 5 then sell
3 in one tick" coherent, which a manager will do on their first day.

**Staleness is time-in-force applied to the queue.** An order that waited while the desk was
down and then filled at a price the manager never saw is worse than one rejected outright: a
rejection can be retried, a fill cannot be undone. The window is one bar of the strategy's
own timeframe (`STALE_BARS`), so a two-minute restart rejects nothing and only a real outage
does.

**No leverage on this path.** A buy that would take cash below zero is refused. Arriving at
margin by accident because nobody checked is how a paper track record stops meaning anything.

## The record grew two columns, and both are about deduplication

`paper.db` is at schema v2; `store._migrate` runs automatically from `connect()` and
`migrate_owner.py --check` is how a human inspects it. `sid` is now `{account}:{...}` — the
account in FRONT, because Nautilus caps `order_id_tag` at 36 characters and a suffix is what
truncation eats.

`fills` gained `symbol` and `ref`, and the two kinds of strategy need **opposite** things
from them:

| | |
|---|---|
| house rules | leave `ref` empty. A restart replays warm-up bars and the strategy re-emits fills it already reported, so identical fills MUST collapse |
| members | pass the venue's trade id. A manager can legitimately send the same order twice on one bar, and those are two real fills — collapsing them loses half the position. Their orders are never replayed, because the desk drains only past its watermark |

**A migration step with nothing to test for must be guarded on the version.** The sid prefix
is not idempotent: re-running it produces `00:00:spy-…` and orphans everything a second
time. It is guarded on `version < 1`, not on the target version, so every future schema bump
does not trip it.

## A fill has two P&Ls and they are not interchangeable (schema v3)

```
book_pnl        equity - capital at the moment of the fill. A snapshot of the WHOLE book.
realised_pnl    what THIS fill closed, against the average cost of what it closed.
                NULL when the fill opened or added.
```

Only `book_pnl` existed, and the board printed it under the heading **Realised P&L** while
`liveMetrics` counted a closed trade as `pnl != 0`. Neither half of that survives contact
with a book:

* **A snapshot is not a trade result.** Every name filling in one second carries the same
  value, so one book mark became several "trades" — and a fill's own price moves cash and
  units by equal and opposite amounts, so `book_pnl` is *zero* on almost every fill and
  nonzero only when some **unrelated** name has been marked since. "Closed trades" was
  therefore "fills that happened while something else moved".
* **It has the wrong sign.** On the IBS equity book every completed round trip had made
  money and the page reported a 13% win rate and a profit factor of 0.04, because a
  profitable sell that happened while the rest of the book was down carries a negative
  snapshot. A page can be wrong about the direction of its own record.

Three things follow, and each is load-bearing:

**`realised_pnl` is NULL, not 0.0, when nothing closed.** A scratch that closed exactly at
cost realised zero and *is* a closed trade; an opening buy realised nothing and is not one.
Collapsing those is the original bug in miniature, so the board filters on the null
(`t.realised != null`) and `fill_pnl.apply_fill` returns `None` rather than `0.0`.

**`realised_pnl` is NOT part of the fill's natural key.** It is a consequence of a fill, not
part of its identity, and a warm-up replay computed against a re-warmed book can carry a
different one — putting it in the UNIQUE would switch off the deduplication `test_store.py`
exists to protect.

**`_entry` is the average cost now, not the opening price.** A position scaled into over
three bars has three prices behind it and only their weighted average prices what a partial
sell just closed. That was a second, quieter error in the same place: the per-name `pnl_pct`
on the board measured against the opening fill and ignored every add. One basis, two
figures, `fill_pnl` owns it. `member_strategy` kept no basis at all and now does.

The v2 -> v3 migration **backfills the column by replaying each symbol's own fills**, so a
record written before it existed is recovered exactly rather than starting blank. It is
guarded on the column being absent rather than on the version, so it can never overwrite
what the live desk has since written.

## Commands

```powershell
python backtest_paper.py --symbols SOXL --bars 800   # prove fills, offline. do this first
python backtest_paper.py --symbols SPY --write-state # + a paper_state.json to inspect
python parity_live.py --tf 1d                        # re-measure the warm-up window
python run_paper.py                                  # the live desk: top 3 per class
python run_paper.py --top 0 --rule SMA_200           # the smoke path: one boring rule
python run_paper.py --dry-run                        # build and validate config, no connect
python run_paper.py --futures-feed poll              # the CME leg on the 8-minute archive
python db_stream.py ES.v.0,CL.v.0 240                # the LIVE gateway, measured, then
                                                     #   checked against the archive
python db_live.py                                    # the REST path, against the cache
```

**One symbol per `backtest_paper.py` process.** Nautilus initialises its Rust logger once
per process and a second `BacktestEngine` panics with `attempted to set a logger after the
logging system was already initialized`. Passing two symbols runs the first and dies on the
second. Pre-existing, unrelated to the strategy — loop in the shell if you need several.

Run from **this** directory: `run_paper.py` instantiates the strategy by string path
(`"strategy:TalibRuleStrategy"`), so `strategy.py` must be importable by bare name from the
process cwd. Renaming that file breaks the node at startup, not at import.

The venv is `..\.venv` — it carries `nautilus_trader` and `websockets`. Not `.venv-nautilus`,
which belongs to `engine-bakeoff/`.

## Warm-up is a correctness parameter, not a performance one

A live feed has no "full series". The strategy recomputes its indicator over a rolling
window of the last N bars, and a recursive indicator (EMA, RSI, anything Wilder-smoothed,
ADX) depends on all prior history and only converges asymptotically. Too small a window
means the desk trades **a different signal from the backtest, with nothing to indicate it**.

So N is **measured**, by `parity_live.py`, not assumed. As of 2026-08-06, the smallest
trailing window reproducing the full-series position exactly on 200/200 recent bars:

| rule | window |
|---|---|
| SMA/MA/MIDPRICE_200, HT_TRENDMODE, ADXR | 250 |
| RSI, ADX, ATR, NATR, MACD | 120 |
| EMA_200 | 500 |
| DEMA_200 | 750 |
| **TEMA_200** | **1000** ← the binding rule |

`DEFAULT_WINDOW_BARS = 1500` is that worst case plus 50% headroom. 750 was the old default
and was wrong for TEMA_200. Re-run `parity_live.py` after adding any rule.

## A book can decide before the bell, and for IBS it must

A rule computed from a bar's own high, low and close and then filled **at that same close**
assumes a print that was not knowable until it happened. The sandbox will happily sell you
it — `on_bar` fires on the finished bar and the exchange prices the fill from that bar — so
the paper desk reproduced the backtest's look-ahead rather than testing it away. Measured
on this desk's own record: 126 of 126 `us_stocks-1d-ibs` fills were at the signal bar's own
close, and none at the next bar's open.

**The fix is not to delay the fill by a bar. It is to compute the signal earlier and still
trade near the close.** `BookStrategyConfig.signal_tf` is that: set it and the book

* subscribes to `signal_tf` bars (`5m`) instead of `tf` bars,
* folds each day into one row holding the range **so far**, cut at
  `decide_lead_min` before the bell (`paper_config.SESSION_CLOSE`),
* and acts once a day, on that bar, at that bar's price.

Every row in the frame is a partial session, history included — a rule whose past is
full-day bars and whose newest row is partial is comparing two different statistics, and
for a state machine like IBS's that silently changes which state it is in.

Four things are load-bearing:

**The decision instant is built from the wall clock, then localised.** Local midnight plus
sixteen hours is 17:00 on the spring-forward Sunday, so the naive arithmetic is right for
363 days a year and trades the wrong bar on the other two.

**One decision per name per session.** 78 five-minute bars a day must not become 78
rebalances, and `_export` is gated with it so the curve stays one point a session — the
same shape the daily books' record has. `_decided` holds the last session each name acted
on.

**Warm-up is counted in SESSIONS.** `min_warmup_sessions` (30) with
`DECIDE_EARLY_WINDOW_BARS` (3000 = ~38 sessions) behind it. Truncation-tested with the
same method as `parity_live.py`: every one of the 21 symbols in `data/stocks/5m`
reproduces the full-history IBS state from 20 sessions or fewer. IBS has no lookback at
all — its only memory is the entry/exit state machine — so re-measure before running a
rule here that does.

**The class must have a bell.** Crypto and spot commodities trade around the clock, so
"five minutes before the close" names no instant; `desk_control._build` refuses it there
with a sentence rather than letting a `ValueError` die inside a Nautilus task, which is the
failure mode this folder has already paid fifteen hours for once.

`test_decide_early.py` is the gate — a `__main__` script, run directly:

```powershell
..\.venv\Scripts\python test_decide_early.py
```

It asserts the fold never contains a post-cutoff price, that every decision matches an
offline reference exactly, that no session is decided twice, and that the book identity
still holds. The reference is spelled out in the test rather than imported, because one
that calls the implementation cannot disagree with it.

`deskdb.registrations.signal_tf` carries it to the desk; NULL is the old behaviour and is
what every existing row has. As of 2026-08-20 the desk carries **both** versions of the two
daily IBS books — `us_stocks-1d-ibs` and `us_stocks-1d-ibs-early`, and the same pair on
`us_etfs` — so the peek and the honest signal run on identical bars and can be compared
forward rather than only in backtest.

## The record survives restarts

`results/paper.db` (SQLite, WAL) is the source of truth; `paper_state.json` is a projection
rendered from it. Before this existed the JSON was the only copy and nothing ever read it
back, so every start began empty — a forward test that resets on restart is a series of
unrelated day-one snapshots.

**Events, not state.** The tables hold what happened — `fills`, `curve`, `sessions`, `gaps`.
A state blob cannot be merged across restarts without guessing; an append-only log can, and
it also answers later questions ("what did this rule do in March") without having kept a
special-purpose file for it.

**Idempotent by construction.** Nautilus replays warm-up bars on restart and the strategy
re-emits fills it already reported. Every insert is `INSERT OR IGNORE` against a natural key
— `(sid, ts, side, qty, price)` for fills, `(sid, ts)` for curve points, where `ts` is the
**bar's** time, not the wall clock. Correctness does not depend on the caller remembering
what it already sent.

**Identity is `sid = "{symbol}-{tf}-{rule}"`.** Stable across restarts, which is what lets
history reattach. Renaming a rule starts a new record, correctly — it is a different system.

**Gaps are measured, never smoothed.** While the desk is down the strategy holds nothing, so
its return over the gap is genuinely 0. The benchmark's is not, and chaining both at 0 would
flatter the strategy through every drawdown it was not present for. So the actual benchmark
move across each gap is fetched (`td_live.return_between`) and stored; when it cannot be
fetched the gap keeps `bench_pct = NULL` and is counted in `unknown_gaps` rather than quietly
becoming zero. A gap shorter than one bar closed no bar, so it is 0.0 by measurement, not
assumption.

One lookup per `(symbol, timeframe, gap start)`, memoised — 330 systems over 33 symbols all
ask the same question, and doing it per strategy made startup 330 sequential API calls.

`paper_curve` is the **chained lifetime** series, so the dashboard shows the record since
inception rather than since the last restart. `curve_breaks` carries the indices the chart
cuts the line at, and `gaps` counts them, so the caption and the picture agree.

**A break is a MISSED BAR, not a restart** (`store._missed_a_bar`). This desk restarts far
more often than a bar closes, so marking every session boundary made each point its own
one-point segment and the chart drew a field of disconnected dots with no line anywhere.
Two conditions must now BOTH hold: the desk was down longer than one bar of the strategy's
timeframe, and the record itself skips more than one bar. Downtime alone cuts the line at
every overnight restart of an intraday system, where the market was shut and there was no
bar to miss; the record's own spacing alone cuts it at every weekend, restart or not,
because a 4h series has a 20-hour hole in it each night by construction.

## What this folder writes, and where

- `results/paper.db` — **the record.** Everything else can be rebuilt from it. This is the
  one file in the repo worth backing up; it is tracked in git for that reason, while its
  `-wal`/`-shm` sidecars are transient and ignored.
- `results/paper_state.json` — a projection of the database, for the dashboard.
- `paper_config.PUBLISH_DIR / "live.json"` — a mirror, for the dashboard. This is the
  **only** write outside this folder. It is declared in `paper_config.py` rather than
  computed in `paper_state.py` so the coupling is visible; set `STOCKHUNT_PUBLISH_DIR` to
  redirect it, or to an empty string to publish nothing. Publishing failing never stops the
  desk trading.
- `logs/` — the Nautilus log directory. It grows fast: one overnight run produced 127 MB.

It **reads** `../walk-forward optimization/results/wf_summary_*.csv` to pick its rules, and
`../data/` for cached bars via the engine's `td_loader`. It reads nothing else.

## The universe: five classes, five legs, five leaderboards

Each leg trades the rules from **its own** sheet. `paper_config.UNIVERSE` is the whole
declaration, and `class_of()` is the reverse lookup that decides which sheet a symbol selects
from, which venue it trades on, which vendor feeds it, and how it is grouped on the dashboard.

| leg | symbols | selects from | venue | fed by |
|---|---|---|---|---|
| `us_stocks` | MEGA20 + SPY, SOXL, TQQQ | `wf_summary_us_stocks_*` | `SANDBOX` | Twelve Data |
| `us_etfs` | QQQ, IWM, XLK, TLT, GLD | `wf_summary_us_etfs_*` | `SANDBOX` | Twelve Data |
| `crypto` | the top 10 by market cap | `wf_summary_crypto_*` | `BINANCE` | Twelve Data |
| `commodities` | XAU, XAG, XPT, XPD, WTI | `wf_summary_commodities_*` | `SPOT` | Twelve Data |
| `cme_futures` | the 19 screened CME roots | `wf_summary_cme_futures_1d` | `GLBX` | **Databento** |

Three rules per leg per timeframe (`TOP_N_RULES`), two timeframes.

## The fifth leg has its own vendor, and Nautilus routes it by venue

Twelve Data carries no CME contract at all and does not answer "no" — `ES` there is
Eversource Energy and `CL` is Colgate-Palmolive, returned as clean, plausible, entirely
wrong series. So `cme_futures` names its own source, and the seam is Nautilus's own:

    DataEngine.register_client   venue=None      -> _default_client   (TWELVEDATA)
                                 venue=GLBX      -> _routing_map      (DATABENTO)

`TwelveDataLiveClient` passes `venue=None` and stays the default; `DatabentoLiveClient`
passes `Venue("GLBX")` and receives exactly the futures subscriptions. No routing code was
written, and no futures bar can reach the wrong vendor by accident.

**The Databento client is registered even with no key on the box**, and that is
deliberate. Leaving it out does not turn the leg off, it hands the leg back to the default
client — the one that would ask Twelve Data for `ES.v.0`. A registered client with no
credential refuses visibly; an absent one mis-routes silently.

### There are two Databento feeds now, and the leg says which one it is on

The archive lags real time by about eight minutes — measured 2026-08-27 at 12:58:10 UTC,
`metadata.get_dataset_range` for `GLBX.MDP3` reported the dataset ending at 12:50:00 UTC,
and `ohlcv-1h`/`ohlcv-1d` report that same fact rounded down to their own bar boundary. A
REST poller therefore works, at $0.00, and that was the whole feed until 2026-08-28.
`db_nautilus.POLL_LAG` is 15 minutes, sized against that measurement: poll inside the lag
and the vendor has nothing, `drop_forming` correctly discards what it does have, and the
bar is skipped in silence.

**Eight minutes stopped being good enough when `1m` was opened to member registrations**,
because there it is eight bars. `db_stream.py` is Databento's LIVE gateway, and the two
measured side by side on this key:

| feed | behind a bar's close | cost |
|---|---|---|
| `db_live` REST poller | ~8 min (`ARCHIVE_LAG_SECONDS`) | $0.00 |
| `db_stream` live gateway | **0.01 s** | **$0.00** |

`metadata.list_unit_prices` carries a `live` mode block for `GLBX.MDP3` and
`get_cost(..., mode="live")` prices `ohlcv-1m`, `ohlcv-1h` and `ohlcv-1d` at $0.00. **The
reason there had been no live feed was neither cost nor capability** — every Databento call
in this repo went out over `requests`, the `databento` SDK was not a dependency, and nobody
had written one.

Five things about the split are load-bearing:

* **Warm-up stays on REST, always.** A live gateway serves no history. `_request_bars` is
  unchanged and still runs its window through `db_loader.back_adjust`.
* **Both feeds publish through `_emit`.** `db_stream` produces a `(front, behind)` pair in
  `db_live.fetch_raw`'s exact shape and hands it to the poller's own publish path, so the
  roll arithmetic below has no branch in it for which feed produced the bar. The gateway
  delivers RAW continuous prices exactly as the raw fetch does; an unadjusted roll on
  either path is the same +37% nobody earned.
* **1d is polled on purpose, and not because of latency.** `db_loader.merge_session_stubs`
  folds Sunday's two-hour sliver into the session it opens — or drops it when the weekend
  carried the roll, which 41% of them do — by looking at the NEXT session's contract. A
  stream has no next bar, so it would publish a stub as a day, and `IBS` is `(C-L)/(H-L)`.
* **The poller is the fallback, and the downgrade is loud.** No SDK, no key, a gateway that
  will not come back after `db_stream.MAX_ATTEMPTS` — each lands on the timer with a
  sentence in the log, `db_live.FEED_MODE` set to `poll`, and `futures_feed` published into
  `paper_state`. It does not flap back: a feed alternating between a hundredth of a
  second and eight
  minutes puts two meanings under one published number. A restart is the recovery.
* **The reconnect is owned here rather than delegated.** `databento.live.session._reconnect`
  re-subscribes with `start=None`, so the SDK's own policy resumes live and the outage's
  bars are gone. `db_stream` uses `ReconnectPolicy.NONE` and rebuilds the session with
  `start = <the last bar it saw>`, which the gateway replays and then runs on into live —
  measured, a 20-minute replay delivered in ~5s with no hole and no duplicate. The same
  mechanism closes the hole between a stale REST warm-up and a socket that would otherwise
  begin at "now".

`--futures-feed stream|poll` (or `STOCKHUNT_FUTURES_FEED`) chooses deliberately.
`python db_stream.py` is the smoke run: it streams real roots, reports the measured
latency, and then checks the same minutes against the REST archive once it catches up.

Two things a stream needs that a poller does not, and both are decisions:

* **Liveness is measured on the SESSION, never on bars.** The gateway emits an OHLCV
  record only for an interval the instrument traded in, so silence on `LE.v.0` overnight
  is correct and silence on the whole session is a dead socket. Heartbeats
  (`HEARTBEAT_SECONDS`) are what separate them; reading liveness off bars is
  `td_nautilus.timeframe_of`'s fifteen-hour failure wearing a socket.
* **A subscription added after `Live.start()` cannot carry a replay.** The SDK says so and
  the gateway enforces it, so a root registered later would have a hole between its stale
  warm-up and its first live bar. The supervisor rebuilds the session when the wanted set
  grows, debounced by `SUBSCRIBE_DEBOUNCE_SECONDS` because the desk's subscriptions arrive
  as one burst at start-up.

Two schema notes that decide what this leg can run at:

* **`ohlcv-1d` and `ohlcv-1h`, and nothing else.** The GLBX archive has no 15m or 4h
  schema at all, and its 1m bars carry the folded-session defect before 2016. The 15m and
  4h research sheets were cut from *cached* 1m files, which a live poll cannot ask for.
  `db_live.can_feed` is the capability; `db_nautilus.timeframe_of` refuses anything else
  at subscribe time, and `desk_control._feedable` refuses it one step earlier so the
  refusal reaches the registration's owner rather than a Nautilus task's log.
* **`db_live.available_end` is to the MINUTE, not to the day.** `db_loader.available_end`
  rounds to a date and memoises forever, which is right for a fetch job and breaks a
  poller twice: measured 2026-08-27 at 12:58 UTC, `ohlcv-1h` ended at **12:00** that day,
  so a date-truncated request loses the whole current session — and a permanently
  memoised end asks for the same window for the life of the process.

### A "contract" on this leg is not a contract

The instrument is a fractional `CurrencyPair`, not a `FuturesContract`, and
`td_nautilus.futures_instrument` says at length why. Short version: `FuturesContract` has
no `size_increment`, `BOOK_CAPITAL` is $100,000 across 19 names, and $5,263 against ES at
~$385,000 of index exposure per real contract rounds to **zero**. The whole book would sit
flat while every log line read healthy.

So a unit here is a fractional notional unit of a back-adjusted continuous series. The
multiplier, quote scale and tick in `futures_specs.CME_CONTRACTS` are **not** used, and a
quantity on this leg must not be read as a contract count.

### The roll is the one genuinely new piece of arithmetic

`data/futures/**` is ratio back-adjusted; a live poll is not. On a roll the raw continuous
series steps to a different contract's level — WTI printed 18.12 and then 24.76 in April
2020 — and `book_strategy` appends live bars to a rolling buffer, so an unhandled roll
feeds that fabricated return straight into a live signal.

Back-adjustment is multiplication by a constant and every price indicator here is
equivariant under a common scale, so **the anchor does not matter and internal consistency
does**:

| | |
|---|---|
| warm-up | `db_live.fetch_bars` runs the window through `db_loader.back_adjust` — the same code, the same rank-0/rank-1 same-bar ratio the cache was built with — anchored at the newest bar |
| afterwards | a per-symbol cumulative FORWARD factor, 1.0 at warm-up. A roll is detected by the `instrument_id` behind the continuous symbol changing, its ratio comes from `db_live.roll_ratios` (which is `back_adjust`), and the factor is **divided** by it |

Divided, not multiplied: `back_adjust` scales history *up* to the newest contract, and
here history is already published, so it is the new bars that come *down* to the warm-up's
anchor. Same adjustment, opposite end.

An inexact roll — rank 1 was not the contract rank 0 became, because ranks can skip a
month — falls back to the close-to-close splice `db_loader` labels in its ledger, and says
so as a WARNING. **An unadjusted bar is never emitted across a roll.**

`db_live.FORWARD_FACTORS` publishes that anchor so the MARK matches the FILLS. A fresh
vendor read anchors at *its* newest bar, so after a roll a naive mark differs from the
desk's own scale by that roll's ratio — a median 0.56% on this universe, applied to the
whole position, showing up as a P&L step nobody traded.

### Mark-to-market is split by vendor, in two threads

`start_marker` batches every marked symbol into one Twelve Data `/price` call, which is
exactly why futures cannot go through it. `run_paper._split_by_feed` divides them, and
`start_futures_marker` prices the CME leg from Databento in its own daemon thread at
`FUTURES_MARK_SECONDS` (300, against the equity leg's 60) — a Databento window costs ~25
seconds of server-side symbology resolution whatever it carries, and a stall there must not
delay the four classes Twelve Data prices. The Twelve Data **tick socket** gets the same
split: `ES.v.0` there does not fail, it comes back in `subscribe-status.fails` forever.

### There is no 4h sheet for this class, and there cannot be one

`FORWARD_TIMEFRAMES` is `1d, 4h` and `wf_summary_cme_futures_4h.csv` does not exist.
`paper_config.has_sheet` is what makes that survivable: `top_rules` raises `SystemExit` on
a missing sheet — right for a research script, fatal for a desk — so `run_paper.build_plan`
asks first and skips the cell with a printed line. `catalog.py` already skipped it.

`BOOK_TIMEFRAMES` still carries `4h` and `5m` for the other four legs; a futures book at
either is refused by `desk_control._feedable` with a sentence.

### What the VPS needs that it does not have

Two things, and they fail at different depths.

**`DATABENTO_API_KEY` in `/opt/stockhunt/.env.local`.** Until it is there the desk runs
normally on four classes and every futures subscription is refused with that sentence in
the log — verified, not assumed. `db_live.have_key` exists precisely so a missing secret is
answered rather than raised: `run_paper.py` runs under systemd with a restart policy, and a
`RuntimeError` at node build or in a poll task would restart-loop books that have nothing to
do with futures.

**`pip install databento` in the desk's venv.** The SDK was never a dependency of this
repo — every Databento call went out over `requests` — so a box that has not been
re-provisioned has `db_loader` working and no SDK, and the futures leg falls back to the
eight-minute poller with `db_stream.NO_SDK` in the log and `futures_feed: poll` in the
published state. `db_stream.have_sdk` answers instead of raising for exactly the reason
`have_key` does, and `import databento` is inside `_open` rather than at module top so
`db_live`, `db_nautilus` and `test_futures_leg.py` all still import without it. Verified
with `--dry-run` against both a keyless box and an SDK-less one.

## The house runs two timeframes; a member may run six

`BOOK_TIMEFRAMES` is `1d, 4h` and stays there — a book follows a walk-forward sheet, and
there is no sheet at 15m. `MEMBER_TIMEFRAMES` is `1d, 4h, 2h, 1h, 15m, 5m`, because a
member's strategy decides for itself and needs the desk only to mark a book and fill an
order. Neither of those needs a leaderboard behind it.

**`BAR_SPEC` and `td_live.INTERVALS` are both derived from `bt_config.TIMEFRAMES` now, not
listed.** Two hand-written subsets of one list is how the desk came to be able to accept a
registration at a timeframe it could not build a bar type for: the check lived in
`api_config`, the capability lived here, and nothing tied them together. `desk_control` now
refuses a member timeframe outside `MEMBER_TIMEFRAMES` with a sentence, where it previously
raised `KeyError('30m')` from inside `on_start` — leaving the registration `pending` with
nothing said to its owner. `paper_config` also raises at import if any member timeframe has
no bar spec.

**That check proves a bar type can be SPELLED, not that it can be SUBSCRIBED TO, and the
gap between those two cost fifteen hours of a forward test** (2026-08-17).
`td_nautilus.timeframe_of` was two hardcoded branches — `1d` and `4h` — while this list
offered six and `/v1/limits` advertised six. A member registering at `5m` got 201 from the
API, a strategy that attached and logged `RUNNING`, and a registration marked **`live`**;
the `ValueError` was raised inside `_subscribe_bars`, in a Nautilus task, where it is
logged as an ERROR and goes nowhere. No bar ever arrived, so `_last_price` stayed empty, so
every order that strategy ever sent was refused with *"no price for BTC/USD yet — try again
after the next 5m close"*. Two strategies sat like that overnight, green in the console.

Three things now stand where nothing stood:

- **`timeframe_of` derives from `td_live.INTERVALS`** instead of listing branches. Nothing
  else had to change to make `5m` work — the poller already knew `5min`, `_interval_delta`
  already read the step from that table, and `_seconds_to_next_close` is modular arithmetic
  over it. The two branches were the whole obstacle.
- **`td_nautilus` raises `SystemExit` at import** if any `MEMBER_TIMEFRAMES` entry is
  missing from `td_live.INTERVALS`. The check lives there rather than in `paper_config`
  because the capability does, and because `paper_config` is imported by
  `Stockhunt Dashboard/` and may not pull in the trading stack. **A guard belongs next to
  the capability it guards, not next to the list it reads.**
- **`DeskController._watch_feeds` reports silence.** A member strategy attached for more
  than `FEED_SILENCE_BARS` (3) of its own bars with no price at all gets a `reason` written
  onto its registration, which `/v1/strategies` already carries and the console already
  renders under *Desk says*. `state` is left at `live`, because `live` is true and
  `_reconcile` owns that column. The reason is cleared if prices start arriving.

`test_feed_timeframes.py` is the regression test: every offered timeframe is feedable, and
every one **round-trips** — a spec that maps to the wrong key polls on the wrong cadence,
which is quieter still.

**`1m` is on offer since 2026-08-28, and what guards it is a CEILING, not its absence.**
The old reason for excluding it was cost and not capability, and that is still exactly
right: `td_nautilus` runs one poll task per subscription aligned to the bar close, so a
minute book is one Twelve Data request per symbol **per minute** against the 610/minute
budget `td_live` is quoted for. What changed is the arithmetic. `MEMBER_TIMEFRAMES`
governs member registrations, which name at most 20 symbols each, and subscriptions are
shared by (symbol, timeframe) — so the bill is the count of **distinct symbols at 1m**,
not the count of strategies. A handful of member books is tens of requests a minute.

The worst case is still real, so it is enforced rather than assumed:
`MAX_MEMBER_STRATEGIES` is 60 and sixty books naming twenty distinct symbols each would be
1,200 requests a minute — which would not degrade the offending book, it would take the
feed down for every book on the desk. `paper_config.MAX_1M_SYMBOLS` (120, ~20% of the
budget) is checked in `DeskController._minute_budget_exceeded` before a 1m registration
attaches. Three properties of that check are load-bearing:

* **It counts SYMBOLS, not registrations.** Three members on the same twenty tickers cost
  twenty polls between them. Counting registrations would refuse the cheap case and admit
  the expensive one — the exact inversion.
* **It reads the LEDGER, not `self._running`**, so a registration applied but not yet
  started still counts. Otherwise a burst arriving in one tick each sees an empty desk.
* **It fails CLOSED.** A ledger this process cannot read refuses the registration and says
  so, because an unbudgeted 1m subscription admitted by accident is a desk-wide outage.

`1m` is deliberately **not** in `BOOK_TIMEFRAMES`. A book holds the whole class, and
`book_universe("us_stocks")` is the live top 100 — one promotion would be 100 requests a
minute on its own, which is the regime this paragraph has always been about.

### `cme_futures` runs at 1m too, and on the poller its bars arrive late

`db_live.SCHEMA` is `1d`, `1h` and — since 2026-08-28 — `1m`. It was the first two, and
`_feedable` refused a futures registration at 1m on the reasoning that the bar would be
stale. **The bar IS stale and that was still the wrong call**, because it answers a
question a member strategy does not ask: it does not compute its signal from this feed.
The signal arrives over the webhook from TradingView's own real-time data, and what the
desk needs a bar for is a price to fill against and a mark. So the honest answer is not
"no", it is "yes, and here is what is stale about it".

**How late, measured.** The archive frontier was sampled every 20s for two minutes on
2026-08-28: `ohlcv-1m`'s end ran **5.5 to 7.3 minutes** behind real time, mean 6.4. So
`POLL_LAG_BY_TF["1m"]` is 8 minutes — the worst reading plus headroom — against the 15 the
other sizes use, because fifteen minutes is right for a daily bar and is fifteen BARS at
1m. Verified end to end: a 20-bar `fetch_bars("ES.v.0", "1m")` returns in ~12s with its
newest bar 6.0 minutes old.

**Read the per-schema `end` carefully if you re-measure this.** `ohlcv-1d` reports 00:00
and `ohlcv-1h` reports the last completed hour, so at 02:25 they read 145 and 25 minutes
behind while the frontier was 5. Only the finest schema tracks the frontier, and
`metadata.get_dataset_range`'s top-level `end` is the frontier itself. Sizing the poll lag
off the hourly reading is how 8 minutes became 15.

**The caveat is written into `reason`, beside `live`** (`desk_control._caveat`), a column
that until then only ever carried a refusal. A member whose fills are priced off a bar
eight minutes old has to be told, and the row they are already looking at is where they
will see it — the alternative is a system that runs, fills and publishes while quietly
meaning something other than what its owner thinks. Keep that column rare: a caveat on
every row is a caveat nobody reads.

**Since 2026-08-28 that caveat is READ, not written.** `db_stream.py` puts the leg on the
live gateway at 0.01 seconds, where the sentence above is simply untrue and `_caveat`
returns `""` — the row reads `live` and nothing else. It is still true whenever the leg
has fallen back to the poller, and the fallback can happen at any moment, so the sentence
is built from `db_live.FEED_MODE` at the instant the registration is marked and it names
*why* the desk is polling. Everything in this section still describes the fallback exactly;
none of it describes the normal case any more.

`4h`, `15m` and `5m` are still refused, and for a genuinely different reason — the GLBX
ohlcv archive has no such schema at all, and the research sheets at those sizes were cut
from cached 1m bars offline, which a live poll cannot ask for. The refusal names the size
that was asked for rather than a hardcoded pair; it explained `4h` and `15m` to everyone,
including 1m askers, and then named 1m as the thing those sheets came from, which reads as
a yes.

`cme_futures` therefore cannot reach the 1m symbol ceiling by a different route than it
used to: `MAX_1M_SYMBOLS` applies to it like anything else, and the 19-name universe is
well under it.

**The legs must stay disjoint** — `paper_config` raises at import if they are not. SPY, SOXL
and TQQQ are on the *equity* leg because that is the transfer test (rules ranked on
mega-caps, run on funds the research never held), so the ETF leg deliberately holds five
other names. One instrument under two rule lists would read on the dashboard as two systems
agreeing when it is one asset counted twice.

**The universe lists are pinned, not read from `bt_config`.** They were the same list until
the research universe grew on 2026-08-09; `bt_config.US_STOCKS` is now the 216-name
point-in-time TOP 100 (it was 751 S&P names between 2026-08-09 and 2026-08-12) and crypto is
`CRYPTO_TOP20`, screened down from 34. Widening the desk should be a deliberate act, not a
side effect of re-running `sp500_membership.py`, `top100_membership.py` or
`universe_screen.py`.

**One of those pins was not a pin.** `CRYPTO_SYMBOLS` was
`CLASSES["crypto"]["symbols"][:10]` — it read the live research universe and took whatever
ten names sat at its front, so any reordering of that list would have re-pointed the live
desk with no diff to review. The 2026-08-12 screen reordered it and the roster survived by
luck, not design. It is now `bt_config.CRYPTO_DEEP`, a stable named list holding exactly the
ten the desk has always traded. If a pin here is meant to hold, it has to be a literal or a
list that exists for its own reason — never a slice of something that moves.

The ETF leg's `XLK` is the loose end the screen left: `universe_screen.py` dropped it from
`us_etfs` at 19.8 tradable years against a 20-year gate, so that leg is now ranked on a
sheet its own instrument is absent from. `XLF`, `XLV` or `XLE` is the like-for-like swap,
and it is a deliberate act, so it has not been made here.

**`XAU/USD` is not crypto.** Routing used to be `"/" in symbol`, which was right for two
classes and silently wrong for the third — a metal priced against the Binance book. It is a
class lookup now, and commodities are a `CurrencyPair` on their own venue because `XAU/USD`
settles into XAU exactly as `BTC/USD` settles into BTC. Nautilus's currency registry already
knows XAU, XAG, XPT, XPD and WTI.

**And `XAU/USD` was not on UTC either.** Twelve Data stamps commodity intraday bars in
`Australia/Sydney` and declares nothing — `meta.exchange_timezone` is `null` for the class
(see `config.INTRADAY_CLOCK` and `../backtest engine/CLAUDE.md` for how that was measured).
Read as UTC, every commodity bar was 10-11 hours in the **future**, and two things here
believed it:

* **`td_live.fetch_bars` discarded the newest bar on every single read.** The forming-bar
  guard asks whether a full interval has elapsed since the bar opened, and against a future
  stamp it never has, so **the commodity legs ran permanently one bar behind** — silently,
  with no error, for as long as the leg has existed. This is the reason the guard is called
  the most important line in that file, and the reason a clock error is worse than a crash.
* **`td_nautilus._to_bar` stamped `ts_event` from the same value**, so those bars are
  recorded in the future in `results/paper.db`.

`fetch_bars` now returns a tz-aware **UTC** index for every class, from
`config.INTRADAY_CLOCK` via `paper_config.vendor_tz`, so the desk and the sheet it selected
from mean the same thing by "the bar". `_seconds_to_next_close` is modular arithmetic on the
epoch and was therefore also wrong against the vendor's Sydney-anchored 4h commodity grid —
whole hours are fine at `1m`..`1h`, and `10 % 4 == 2` is not.

**The existing record is NOT rewritten.** 24 fills and 18 curve points across six
`commodities-1d` and `commodities-4h` systems, all on 2026-08-14/15, carry `ts` on the
Sydney clock; their 4h stamps read 03:00/07:00/23:00, which is that grid. The prices and
the fills are real — only their timestamps are shifted — and a trading record edited after
the fact is worth less than one with a documented defect. Read those six sids' timestamps
as Sydney wall clock, or ignore them.

## Selection

`paper_config.top_rules()` reads the walk-forward sheet rather than a hard-coded list, so the
desk reflects the current sweep instead of going stale silently — re-running `walkforward.py`
re-picks the desk on the next start, with nothing to remember to update. It lives in
`paper_config` and not `run_paper` because the dashboard picks the same rules to draw the
same systems, and importing `run_paper` for one function would pull the whole
`nautilus_trader` stack into a page builder.

Restricted to `wf_mode == "fixed"`: the re-selected rows (`IS#1`, the `[WF]` families) are a
different rule in every fold and have no single definition to trade live.

**Duplicates are measured, not assumed.** `_same_idea` collapses rules that are one idea
under two names, and each alias carries the fraction of bars on which the two hold the
identical position. `MA_n`/`SMA_n` and `SAR`/`SAREXT` collapse; `MAXINDEX`/`MININDEX` and
`LINEARREG_n`/`TSF_n` are deliberately left separate, because collapsing those would be an
opinion about the indicators rather than an observation about their output.

**A stale sheet warns at selection time.** `_warn_if_stale` compares the sheet's mtime against
`data/reference/quarantine.csv`, the youngest artifact `td_loader.load` consults. A sheet
older than that ranked rules over a different *sample*, and nothing in the CSV records it. It
warns rather than refuses — a stale ranking still exercises the order path, and stopping the
forward record over a day-old research artifact trades one problem for a worse one.

**`--dry-run` does not touch the record.** It builds and validates the node and exits.
Publishing — `paper_state.reset()`, the session row, the venue totals — happens *after* that
exit. It used to happen first, so validating a config blanked the published projection and
opened a `sessions` row that only the shutdown path closes, which a dry run never reaches. An
unclosed session is the expensive half: downtime is measured between sessions, and `gaps` is
the one thing here that cannot be recomputed later.

## Gotchas

- **Capital is per system, not per venue.** Nautilus gives one account per venue, so without
  splitting it every system sizes against the same balance and they collectively try to
  deploy N times the capital that exists.
- **`order_id_tag`, not `strategy_id`.** Through an `ImportableStrategyConfig`, msgspec
  decodes `strategy_id` into a `StrategyId` and `Strategy.__init__` passes it to a `name`
  parameter typed `str` — TypeError at node build. Nautilus 1.230.0.
- **The rule is part of the tag**, not just the instrument: five rules on one symbol would
  otherwise share an id and Nautilus rejects the duplicate registration.
- **The sandbox adapter has a bar-subscription bug**; `route_bars_to_sandbox` in
  `run_paper.py` is the workaround.
- **`safe()` here is not `config.safe_symbol`.** A Nautilus `Symbol` cannot carry a separator
  (`BTC/USD` → `BTCUSD`); a cache filename keeps one (`BTC_USD.parquet`).
