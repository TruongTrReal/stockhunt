# CLAUDE.md

Guidance for Claude Code working in this directory. Read `../CLAUDE.md` first.

## What this is

The live desk. Twelve Data bars in, NautilusTrader simulated fills out, state published
for the dashboard to watch.

`SandboxExecutionClient` is the point of it: a real Nautilus execution client that prices
fills from the live feed instead of sending orders anywhere, so portfolio, position and
P&L accounting is the *same code* that would run against a real venue.

```
backtest   BacktestNode  + BacktestDataClient   + SimulatedExchange
paper      TradingNode   + TwelveDataLiveClient + SandboxExecutionClient   <- here
live       TradingNode   + TwelveDataLiveClient + Binance / IB exec client
```

Going live is a change of execution client in `EXEC_CLIENTS` and nothing else.

**This is plumbing, not a trading recommendation.** Every rule the research measured is
negative on IR against buy-and-hold; the best is -0.048 on crypto 1d, under its noise
ceiling. What runs here runs to prove that bars arrive, signals compute, orders fill and
P&L accrues. **Do not read the P&L as evidence of anything.**

## Files

```
paper_config.py   sys.path bootstrap, universe, warm-up constants, top_rules()
store.py          results/paper.db - the record. fills, curve, sessions, gaps
run_paper.py      the desk: builds the TradingNode, starts LiveHub, runs
strategy.py       TalibRuleStrategy - one rule, traded to a target exposure
paper_state.py    the registry; serialises results/paper_state.json and publishes live.json
td_live.py        Twelve Data REST: bars, prices, market hours. Importable without Nautilus
td_nautilus.py    TwelveDataLiveClient + instrument factories
live_ws.py        LiveHub: upstream tick socket -> paper_state.mark() -> browser socket
backtest_paper.py the same strategy through a BacktestEngine on cached bars
parity_live.py    measures the rolling window each rule needs to match the full series
test_store.py     end-to-end check that a restart resumes instead of resetting
```

## Commands

```powershell
python backtest_paper.py --symbols SOXL --bars 800   # prove fills, offline. do this first
python backtest_paper.py --symbols SPY --write-state # + a paper_state.json to inspect
python parity_live.py --tf 1d                        # re-measure the warm-up window
python run_paper.py --top 5                          # the live desk
python run_paper.py --dry-run                        # build and validate config, no connect
```

Run from **this** directory: `run_paper.py` instantiates the strategy by string path
(`"strategy:TalibRuleStrategy"`), so `strategy.py` must be importable by bare name from the
process cwd. Renaming that file breaks the node at startup, not at import.

The venv is `..\.venv` — it carries `nautilus_trader` and `websockets`. Not
`.venv-nautilus`, which belongs to `engine-bakeoff/`.

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

`results/paper.db` (SQLite, WAL) is the source of truth; `paper_state.json` is a
projection rendered from it. Before this existed the JSON was the only copy and nothing
ever read it back, so every start began empty and the forward record restarted at zero —
a forward test that resets on restart is a series of unrelated day-one snapshots.

**Events, not state.** The tables hold what happened — `fills`, `curve`, `sessions`,
`gaps`. A state blob cannot be merged across restarts without guessing; an append-only log
can, and it also answers later questions ("what did this rule do in March") without having
kept a special-purpose file for it.

**Idempotent by construction.** Nautilus replays warm-up bars on restart and the strategy
re-emits fills it already reported. Every insert is `INSERT OR IGNORE` against a natural
key — `(sid, ts, side, qty, price)` for fills, `(sid, ts)` for curve points, where `ts` is
the **bar's** time, not the wall clock. Correctness does not depend on the caller
remembering what it already sent. Verified live: three restarts, fills stayed at 200.

**Identity is `sid = "{symbol}-{tf}-{rule}"`.** Stable across restarts, which is what lets
history reattach. Renaming a rule starts a new record, correctly — it is a different
system.

**Gaps are measured, never smoothed.** While the desk is down the strategy holds nothing,
so its return over the gap is genuinely 0. The benchmark's is not, and chaining both at 0
would flatter the strategy through every drawdown it was not present for. So the actual
benchmark move across each gap is fetched (`td_live.return_between`) and stored; when it
cannot be fetched the gap keeps `bench_pct = NULL` and is counted in `unknown_gaps` rather
than quietly becoming zero. A gap shorter than one bar closed no bar, so it is 0.0 by
measurement, not assumption.

One lookup per `(symbol, timeframe, gap start)`, memoised — 330 systems over 33 symbols all
ask the same question, and doing it per strategy made startup 330 sequential API calls.

`paper_curve` is now the **chained lifetime** series, so the dashboard shows the record
since inception rather than since the last restart. `curve_breaks` carries the indices
where the desk was down; `app.js` does not yet break the line there, so the chart currently
draws straight through a gap it has the data to mark.

## What this folder writes, and where

- `results/paper.db` — **the record.** Everything else can be rebuilt from it. This is the
  one file in the repo worth backing up; it is tracked in git for that reason, while its
  `-wal`/`-shm` sidecars are transient and ignored.
- `results/paper_state.json` — a projection of the database, for the dashboard.
- `paper_config.PUBLISH_DIR / "live.json"` — a mirror, for the dashboard. This is the
  **only** write outside this folder. It is declared in `paper_config.py` rather than
  computed in `paper_state.py` so the coupling is visible; set `STOCKHUNT_PUBLISH_DIR` to
  redirect it, or to an empty string to publish nothing. Publishing failing never stops
  the desk trading.
- `logs/` — the Nautilus log directory. It grows fast: one overnight run produced 127 MB.

It **reads** `../walk-forward optimization/results/wf_summary_*.csv` to pick its rules, and
`../data/` for cached bars via the engine's `td_loader`. It reads nothing else.

## Selection

`paper_config.top_rules()` reads the walk-forward sheet rather than a hard-coded list, so
the desk reflects the current sweep instead of going stale silently. It lives in
`paper_config` and not `run_paper` because the dashboard picks the same rules to draw the
same systems, and importing `run_paper` for one function would pull the whole
`nautilus_trader` stack into a page builder.

Restricted to `wf_mode == "fixed"`: the re-selected rows (`IS#1`, the `[WF]` families) are
a different rule in every fold and have no single definition to trade live.

**Ranking is not passing.** Nothing on either sheet clears a single acceptance gate, and on
equities the best rule has positive IR on 3 of 20 assets. These are the least-bad
candidates, which is not the same as good ones.

## Gotchas

- **Capital is per system, not per venue.** Nautilus gives one account per venue, so
  without splitting it every system sizes against the same balance and they collectively
  try to deploy N times the capital that exists.
- **`order_id_tag`, not `strategy_id`.** Through an `ImportableStrategyConfig`, msgspec
  decodes `strategy_id` into a `StrategyId` and `Strategy.__init__` passes it to a `name`
  parameter typed `str` — TypeError at node build. Nautilus 1.230.0.
- **The rule is part of the tag**, not just the instrument: five rules on one symbol would
  otherwise share an id and Nautilus rejects the duplicate registration.
- **The sandbox adapter has a bar-subscription bug**; `route_bars_to_sandbox` in
  `run_paper.py` is the workaround.
- **`safe()` here is not `config.safe_symbol`.** A Nautilus `Symbol` cannot carry a
  separator (`BTC/USD` → `BTCUSD`); a cache filename keeps one (`BTC_USD.parquet`).
