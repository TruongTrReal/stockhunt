# NautilusTrader vs manifoldbt — bake-off results

**Tested:** `nautilus_trader 1.230.0`, `manifoldbt 0.14.1` (Community), TA-Lib 0.7.1,
Python 3.13.3, Windows 11.
**Data:** Twelve Data `time_series`, `interval=1day`, **`adjust=all`**, 2015-01-02 →
2026-07-31, 2,911 bars × 20 large-cap tickers. Re-run against the project's existing
yfinance cache as a control (2,908 bars).
**Workload:** 20 tickers × 5 TA-Lib rules, long/flat, fully invested, no fees, no slippage.
**Ground truth:** a share-level simulation in `common.py:simulate` — every fill priced and
every mark done explicitly, so both engines are scored against arithmetic rather than
against each other.

Everything below was re-run end to end on Twelve Data. **Every engine finding reproduced
on both data sources**, to the third significant figure.

---

## Verdict

| | manifoldbt | NautilusTrader |
|---|---|---|
| Accuracy (at-close execution) | **exact** — 100/100 runs, relative error identically 0 | correct — 100/100 within 5.7e-6 (rounding) |
| Accuracy (open-priced execution) | **wrong — see finding 1** | n/a (fills at bar close) |
| Speed | **4.4 ms/run** | 404–583 ms/run (**~92–133× slower**) |
| TA-Lib support | **none** — must precompute and inject | **native** — call `talib.*` inside `on_bar` |
| Daily bars | **broken — see finding 2** | works |

### What to stick to

**Data: Twelve Data, with `adjust=all`, as the single source.** It is the better source
here — 3 more recent bars per ticker, an explicit adjustment flag rather than an implicit
one, a documented quota, and a `status` field so failures are detectable. But see finding
6: it is *not* interchangeable with the yfinance cache, so pick it and re-derive, don't mix.

**Engine for the sweep: keep the vectorised pandas pipeline you already have.** The
reference simulation ran at **0.95 ms/run — 4.6× faster than manifoldbt and ~425× faster
than Nautilus** — and it is exact by construction. Extrapolated to the real 501 × 231
workload: **~0.03 h for the current pipeline, ~0.14 h for manifoldbt, ~13–19 h for
Nautilus.** Neither engine is an upgrade for the screening stage.

**Engine to build on for everything after the sweep: NautilusTrader.** Once the candidate
list is down to a handful, the question changes from "how fast" to "what happens with real
order types, commissions, slippage and partial fills" — and there Nautilus was correct
everywhere tested, runs TA-Lib natively, and its 400 ms/run stops mattering. manifoldbt is
the faster screener but it cannot see your indicators at all (finding 3), which is exactly
what this project is about.

So: **Twelve Data → your existing pandas sweep → Nautilus for validation of survivors.**
manifoldbt does not earn a place in that chain.

---

## Finding 1 — manifoldbt's open-priced execution is look-ahead biased

This is the serious one. With `execution_price` set to `AT_OPEN` or `NEXT_BAR_OPEN`,
manifoldbt sizes the position from the **close** of the bar it fills at the **open** of.

Direct evidence, AAPL / SMA_CROSS, `signal_delay=1, execution_price=AT_OPEN`, $10,000 capital:

```
qty[0]      = 362.284511
fill_price  = 27.729243     <- the bar's OPEN
qty * fill  = 10,045.8752   <- 100.46% of capital, on a 100%-max config
qty * close = 10,000.000000 <- exactly capital, at the bar's CLOSE
```

The share count is `capital / close`, but the trade transacts at `open`.

`lookahead_probe.py` pins the mechanism down on synthetic bars built to close exactly
10% above their open on every bar, so the ratio is known rather than inferred:

```
AT_CLOSE            fill=110.00  qty=90.9091  notional=10,000.00  invested=100.0%
AT_OPEN             fill=100.00  qty=90.9091  notional= 9,090.91  invested= 90.9%
AT_OPEN + delay=1   fill=100.00  qty=90.9091  notional= 9,090.91  invested= 90.9%
NEXT_BAR_OPEN       fill=100.00  qty=90.9091  notional= 9,090.91  invested= 90.9%
```

The quantity is **identical across every execution mode** — always `capital / close`.
Only the fill price changes. So the rule is: *the share count is always computed from
the bar's close, whatever price the fill actually happens at.* Under `AT_CLOSE` those
two are the same price and the result is exact; under any open-priced mode they are
not. Two things follow, and both are bad:

1. **Look-ahead.** The size is chosen using a price that had not happened yet at the
   moment of the fill.
2. **Wrong exposure.** Invested notional is `100% × (open / close)` of equity, despite
   `max_position_pct=1.0` — under-invested on up-bars (90.9% above), over-invested on
   down-bars. In the AAPL trade above, `open/close = 27.7292/27.6026 = 1.0046`, so it
   took 100.46% exposure and residual cash went *negative*.

Across the 100 next-open runs this moves final equity by a **median of 1.88%, up to 14.2%**
(TSLA/RSI_TREND: 129,386 reported vs 150,781 correct), with trade counts identical — so
nothing about it looks wrong from the outside. On the yfinance control the same figures
were 1.86% / 14.2%.

Related trap: **`NEXT_BAR_OPEN` and `NEXT_BAR_CLOSE` do not delay anything.** With the
default `signal_delay=0` the trade's `execution_timestamp` equals its `signal_timestamp` —
`NEXT_BAR_OPEN` fills at the *signal bar's own* open, using a signal derived from that
bar's close. A genuine next-bar fill needs `signal_delay=1` explicitly. `NEXT_BAR_CLOSE`
at delay 0 is simply identical to `AT_CLOSE`.

**Only `execution_price=AT_CLOSE` with `signal_delay=0` is trustworthy**, and there it is
flawless: 100 of 100 runs matched the reference to *identically zero* relative error, on
both data sources. That happens to be the convention your project already uses
(`position.shift(1)`).

## Finding 2 — manifoldbt cannot read data imported at `interval="1d"`

Every backtest over a store imported with `interval="1d"` fails with
`data error: backtest received empty bar dataset for symbol SymbolId(1)`, at every
`bar_interval` setting. `1h` and `1m` imports work fine with identical timestamps, so it
is the interval label, not the data. Ruled out as a directory-naming issue by cloning the
timeframe directory to `24h`, `1440m`, `86400s`, `D`, `bars_1d`, `day`, `daily` — all
still empty. `4h` fails too; the working set appears to be `1m` / `15m` / `1h`.

Workaround used here: import daily bars labelled `"1h"`. Bars are processed in sequence so
equity and trades are unaffected — but every time-annualised metric the engine reports
(CAGR, Sharpe, Calmar, volatility) is then computed against the wrong calendar and must be
discarded and recomputed from the equity curve. For a daily-bar research project this is a
standing hazard, not a one-off.

## Finding 3 — manifoldbt has no TA-Lib binding

Strategies are built from manifoldbt's own Rust expression DSL, which exposes ~40
indicators (`sma`, `ema`, `rsi`, `macd`, `cci`, `bollinger_bands`, `atr`, `adx`,
`supertrend`, `kalman`, …) against TA-Lib's 161. Nothing in the API accepts a Python
callable.

So a TA-Lib rule can only reach the engine as a precomputed target-position column
injected via `register_exo()`, which is what `run_manifoldbt.py` does. The consequence is
structural, not cosmetic: **the engine never sees the indicator**, only the answer. Any
feature that needs the indicator's *value* inside the engine — parameter sweeps over
TA-Lib periods, ATR-scaled stops, indicator-conditional exits — is off the table, and
`run_sweep` / `run_sweep_lite` / `run_walk_forward` become useless for TA-Lib work since
the thing you would sweep lives outside.

Nautilus has the opposite shape: `on_bar` is ordinary Python, so `talib.RSI(...)` is a
direct call. Measured cost of doing it live, on a 200-bar rolling window: 583 ms/run vs
404 ms/run for a pre-injected signal — **44% overhead, and zero accuracy cost**. Native and
injected runs produced identical results (same median 1.4e-6 error, same max 5.7e-6, zero
trade-count differences), i.e. a 200-bar window is enough for these five rules — including
the recursively smoothed ones (RSI, MACD) where that was not obvious.

## Finding 4 — Nautilus is correct, and its cost is intrinsic

All 200 Nautilus runs landed within **5.7e-6** of the reference, median **1.4e-6**, with
every trade count matching exactly. The residual is quantisation, not modelling: positions
round to 6 dp and cash balances to the cent, ~69 times per run.

Its per-run cost cannot be engineered away by reusing engines — of 210 ms for one AAPL
run, engine + venue + instrument construction is **1.7 ms** and building the 2,911 `Bar`
objects is **6.7 ms**; the remaining **~202 ms (96%)** is `engine.run()` itself. That is
the event loop doing its job — an order-book simulation per bar — and it is the price of
the realism you would be buying.

Two setup notes for later: a real `Equity` instrument has `size_precision=0`, so
whole-share rounding is enforced (this harness used a fractional instrument deliberately,
to measure accounting rather than rounding); and a single-currency `CASH` account rejects a
same-currency pair, so the account here is `MARGIN` with zero margin requirements.

## Finding 5 — manifoldbt's batch path does not help at this size

`run_batch_lite` (load bars once, evaluate N strategies across threads) came out at
**7.61 ms/run vs 4.39 ms/run** for plain `run()` — *slower*, since 5 strategies per call is
too few to cover the thread fan-out. Accuracy was unaffected (100/100 exact). Batching
would need far more strategies per data load to pay off, and Community tier caps a
batch/sweep at 500 combinations per call.

Other Community-tier limits worth knowing before committing: walk-forward optimisation,
cross-exchange runs, GPU sweeps and sub-daily `output_resolution` are all Pro-gated.

## Finding 6 — manifoldbt's own look-ahead detector cannot catch finding 1

manifoldbt advertises `bt.diagnostics.detect_lookahead()` ("tests every signal for
future data leakage using split-sample comparison"), and its sample output includes a
`Position sizing … no future data … ok` line. A reasonable person reads that as "sizing
is checked". Worth taking seriously, so it was tested rather than argued about.

**The claim is not false — it is answering a different question.** From its own source,
the method is: re-run the strategy on a truncated time range and compare the trades in
the overlapping period against the full run. Its docstring names the two targets — a
global statistic computed over all history (`np.mean(all_prices)`), and a signal at bar
T that reads bar T+1. Both are leaks *across* bars, and split-sample comparison is a
sound way to find them.

Finding 1's leak is *within* a single bar: sizing off bar t's close, filling at bar t's
open. Truncating the series never changes bar t's own open or close, so every
overlapping trade comes back bit-identical and there is nothing to flag. Replicating
their published method against the config finding 1 has just proven biased
(`lookahead_probe.py`, part 2):

```
AT_OPEN (proven biased)
  truncation (full -> 1/3): 26 overlapping trades compared -> CLEAN
  extension  (2/3 -> full): 44 overlapping trades compared -> CLEAN
```

So both statements hold at once: the detector does what it claims, and it is
structurally blind to leakage in the execution layer — the layer where this engine
actually has the problem.

**Caveat, stated plainly:** `detect_lookahead` is Pro-gated and raises `LicenseError`
on Community, so the *real* implementation could not be run. Part 2 replicates the
method documented in its own docstring and Python source; the native Rust
`py_detect_lookahead` may do more than that. What is certain is that split-sample trade
comparison — the method it describes — cannot see this bug. Confirming the shipped
detector's behaviour would need a Pro license.

## Finding 7 — Twelve Data and yfinance are not interchangeable

Not an engine finding, but the one most likely to bite in day-to-day work.
`validate_data.py` and `compare_sources.py` measure it:

| | |
|---|---|
| Calendar | 2,908 dates shared; Twelve Data has 3 more recent bars per ticker, yfinance has none Twelve Data lacks |
| Prices, shared dates | median relative difference **3.9e-4**, worst **9.7e-2** (PEP) |
| Signals | only **0.053%** of position-days differ (155 of 290,800) |
| **Resulting equity** | median **0.91%**, 90th percentile **6.2%**, worst **18.2%** (MSFT) |
| Rule ranking | **identical** under both execution conventions |

Read that table top to bottom: the rules almost never take a different position, and yet
final equity moves by up to 18%. The cause is dividend-adjustment methodology, not
disagreement about what happened — a high-yield name's whole price *level* drifts between
the two sources over eleven years, so identical positions earn different returns. Note the
default Twelve Data response (and `adjust=splits`) is split-adjusted but **not**
dividend-adjusted, which would be a much larger version of the same problem;
`adjust=all` is required to match what this project assumes.

Practical consequences:

- Pick one source and re-derive everything from it. Do not compare fresh Twelve Data
  results against the existing yfinance-based leaderboard — an 18% equity gap with zero
  signal changes will look like a strategy effect.
- Conclusions that are *relative* (which rule beats which, and by how much) survive the
  switch intact — the ranking was identical. Conclusions that are *absolute* (CAGR, dollar
  PnL, "beats buy-and-hold by X") do not.

---

## Caveats on scope

- Long/flat only, no shorting, no fees, no slippage, no partial fills. Those are exactly
  the areas where Nautilus's extra machinery would start to matter and where this harness
  gives it no credit.
- Stateless rules by design, so a single divergent bar can't cascade. A path-dependent
  strategy would likely widen every gap reported here.
- Daily bars only. Nautilus's per-bar cost is what it is, so an intraday sweep would scale
  the 13–19 h extrapolation up by the bar-count ratio.
- 20 tickers, 5 rules. The engine findings are mechanical (they reproduce per-run, on two
  independent data sources), but the data-source figures in finding 6 are specific to these
  20 names and would differ for a low-dividend or small-cap universe.
- One machine, single-threaded.
