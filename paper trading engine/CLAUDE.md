# CLAUDE.md

Guidance for Claude Code working in this directory. Read `../CLAUDE.md` first.
**No results here** — the `results/` CSVs and the dashboard own those.

## What this is

The live desk. Twelve Data bars in, NautilusTrader simulated fills out, state published for
the dashboard to watch.

`SandboxExecutionClient` is the point of it: a real Nautilus execution client that prices
fills from the live feed instead of sending orders anywhere, so portfolio, position and P&L
accounting is the *same code* that would run against a real venue.

```
backtest   BacktestNode  + BacktestDataClient   + SimulatedExchange
paper      TradingNode   + TwelveDataLiveClient + SandboxExecutionClient   <- here
live       TradingNode   + TwelveDataLiveClient + Binance / IB exec client
```

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
catalog.py        publishes catalog.json: which rules may be promoted to the desk
migrate_owner.py  inspect/apply/verify the account migration on paper.db
paper_state.py    the registry; serialises results/paper_state.json and publishes live.json
td_live.py        Twelve Data REST: bars, prices, market hours. Importable without Nautilus
td_nautilus.py    TwelveDataLiveClient + instrument factories
live_ws.py        LiveHub: upstream tick socket -> paper_state.mark() -> browser socket
backtest_paper.py the same strategy through a BacktestEngine on cached bars
parity_live.py    measures the rolling window each rule needs to match the full series
test_store.py           a restart resumes instead of resetting
test_runtime_attach.py  a strategy can join a RUNNING trader. Gate for the whole design
test_member_desk.py     ledger -> running trader -> fill -> book -> record, end to end
test_accounts.py        two accounts on one cell keep separate books  (pytest)
test_desk_orders.py     the rules that decide whether money moves        (pytest)
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

## Commands

```powershell
python backtest_paper.py --symbols SOXL --bars 800   # prove fills, offline. do this first
python backtest_paper.py --symbols SPY --write-state # + a paper_state.json to inspect
python parity_live.py --tf 1d                        # re-measure the warm-up window
python run_paper.py                                  # the live desk: top 3 per class
python run_paper.py --top 0 --rule SMA_200           # the smoke path: one boring rule
python run_paper.py --dry-run                        # build and validate config, no connect
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
inception rather than since the last restart. `curve_breaks` carries the indices where the
desk was down; `app.js` does not yet break the line there.

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

## The universe: four classes, four legs, four leaderboards

Each leg trades the rules from **its own** sheet. `paper_config.UNIVERSE` is the whole
declaration, and `class_of()` is the reverse lookup that decides which sheet a symbol selects
from, which venue it trades on, and how it is grouped on the dashboard.

| leg | symbols | selects from | venue |
|---|---|---|---|
| `us_stocks` | MEGA20 + SPY, SOXL, TQQQ | `wf_summary_us_stocks_*` | `SANDBOX` |
| `us_etfs` | QQQ, IWM, XLK, TLT, GLD | `wf_summary_us_etfs_*` | `SANDBOX` |
| `crypto` | the top 10 by market cap | `wf_summary_crypto_*` | `BINANCE` |
| `commodities` | XAU, XAG, XPT, XPD, WTI | `wf_summary_commodities_*` | `SPOT` |

Three rules per leg per timeframe (`TOP_N_RULES`), two timeframes: **24 systems, 258
deployments**.

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

**`1m` is excluded on purpose, and the reason is cost, not capability.** `td_nautilus` runs
one poll task per subscription aligned to the bar close, so a minute book of ten symbols is
ten Twelve Data requests every minute — a different credit regime, not a faster version of
the same one. Adding it is one entry in `MEMBER_TIMEFRAMES` and one in `API_TIMEFRAMES`
when the vendor plan can carry it.

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
