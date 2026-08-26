# Conversions

Where the strategies in `published/` came from when they came from somebody else's code
rather than from a paper. **No results here** — the `results/` CSVs and the dashboard own
those. This file records provenance and, just as importantly, *what was dropped in
translation*, because a converted rule that quietly lost a filter is not the rule anybody
published.

## The contract a conversion has to fit

Every strategy in this repo is one function returning **one target exposure per bar**:

```python
fn(df, close, bpy, **params) -> np.ndarray      # -1..1, bar t may read bars <= t only
```

That contract is narrower than a TradingView `strategy()` or a freqtrade `IStrategy`, and
the gap is where conversions go wrong. Four things do not fit through it, and each one is
a reason a source file below was either adapted with a note or left alone:

| does not fit | why |
|---|---|
| **intrabar stops and targets** | the series says what you hold over a bar, not what happened inside it. A percentage stop can be re-expressed on closes; a trailing stop filled at the tick cannot |
| **trade-level state** | `custom_exit(trade, current_profit)` is legal to reproduce — entry price is causally known — but only by walking the bars. Anything reading `trade.max_rate` at tick resolution is not |
| **position sizing** | DCA / safety orders / `pyramiding` scale the stake mid-trade. A -1..1 exposure has no room for "add 1.4x at -1.8%" |
| **cross-asset inputs** | the strategy layer sees one symbol. A guard on BTC's own move, or on the rest of the book, has nowhere to read from |

The fifth constraint is data, not contract: this repo holds **daily and 4-hour bars**, plus
an old 5-minute cache for twenty mega-caps only. A rule keyed to 1-minute ES futures has no
bars to run on here whatever its logic looks like.

### One conversion is NOT in `published/`, on purpose

`walk-forward optimization/rotation.py` ports a monthly best-of-basket ETF rotation from
`papertoprofit.substack.com` (2025-04-05) and its author's `portwine` library. It is the
fourth row of the table above in its purest form: *"hold whichever of these seven is
best this month"* is a **cross-sectional** rule, and no per-symbol exposure function can
express it — the choice only exists once you can see the panel. So it carries its own
loader, its own book, its own controls and its own truncation gate rather than being
squeezed into `position()`. Look for it there, not here.

## Converted (13)

Batch of 2026-08-18, from `Strategies to convert.zip`. All 13 pass
`strategies/tests/test_causality.py` by truncation, at every cell of their grids, and all
53 cells were pre-registered in `data/reference/trials.csv` before anything was scored —
50 with the batch, 3 as the follow-up described below.

| file | source artefact | what it is |
|---|---|---|
| `bar_updn.py` | `2nd-bar.rtf` | TradingView's own `BarUpDn` sample: a three-bar pattern |
| `pivot_center.py` | `3 SH-LH.rtf` | confirmed-pivot centre line, crossed one bar late |
| `range_filter.py` | `vumanchu.rtf` | DonovanWall's Range Filter — a ratcheting trend line |
| `range_filter_macd.py` | `2 RF-100 Inverse off-no false .rtf` | the same filter with a slow MACD gate on entry |
| `ema_cross_sniper.py` | `snipper.rtf` | TradersPost's EMA 8/21 cross |
| `bb_outside_in.py` | `SH-BOSS high accuracy.rtf` | Bollinger pierce, then a midline recross |
| `ssl_hybrid.py` | `SH-SSL-HB v4.rtf` | SSL channel + Keltner baseline + two QQE lines |
| `lorentzian_knn.py` | `SH-Machine v1.rtf` | jdehorty's Lorentzian Classification, libraries included |
| `heikin_reversal.py` | `Marwo_heiken_pure.py` | first green synthetic candle after a bearish stretch |
| `sma_fan_dip.py` | `EVA2.py` | 5/10/25/60 SMA fan break, bought at a discount |
| `vwma_offset_dip.py` | `Cenderawasih_3_kucoin.py` | percentage offsets under a VWMA and two EMAs |
| `ema_fan_align.py` | `ichiV1_Marius.py` | the `tesla` entry: a seven-deep EMA fan, still widening |
| `renko_delta.py` | `renkotrading.ipynb`, `renko_v4.ipynb` | percentage renko bricks, run-counted |

The two notebooks are **one** strategy: the second is a refactor of the first with a
parameter grid bolted on, and shipping them as two files would double-count the trial.

### What was dropped, per file

Each file's `NOTE` carries this too — this is the index, the file is the record.

* **`bar_updn`** — `strategy.risk.max_intraday_loss(1%)`, an equity-curve breaker.
* **`ema_cross_sniper`** — nothing. Its two `barmerge.lookahead_on` 30-minute EMAs are real
  look-ahead but are plotted only and reach no condition, so the causal version *is* the
  published one. Its stop and target inputs both default to 0, i.e. off.
* **`bb_outside_in`** — the HullMA direction filter, which ships with `filter = false` and
  is dead code at the published settings.
* **`ssl_hybrid`** — the SSL2/JMA continuation line and its ATR criterion, which reach no
  condition. Two *defects* are reproduced rather than fixed: the short leg tests the
  **upper** Keltner band where symmetry wants the lower one, and that asymmetry is most of
  the difference between the two legs.
* **`lorentzian_knn`** — only `bar_index >= last_bar_index - maxBarsBack`, which gates
  computation on where the chart happens to end and is therefore not causal. Three
  properties that look like bugs are reproduced deliberately: the training label is
  **backwards** (it describes the four bars that ended at t, not the four that follow),
  the neighbour search only ever scans the **oldest** 2000 bars, and the neighbour buffer
  **persists across bars**.
* **`heikin_reversal`** — `minimal_roi = {"0": 0.04}`, a 4% take-profit. Its stoploss was
  -99%, i.e. off.
* **`sma_fan_dip`** — the "5% profit within 10 minutes" exit, unreachable when the finest
  bar available is a day. The -25% stop is kept but measured on the close, where freqtrade
  measures it intrabar.
* **`vwma_offset_dip`** — the trailing `custom_stoploss`, and the listing-age and liquidity
  screens (this repo screens its universe centrally). Its 15-minute informative EMA is
  approximated by scaling the length on the native frame — an analogue, not the same
  series. **Restricted to `us_stocks` and `us_etfs`**: the vendor serves no volume for
  crypto or commodities, so a VWMA there is a silently unweighted mean.
* **`ema_fan_align`** — a BTC-relative guard (cross-asset), a pump/dump guard built on a
  15-minute informative frame, and `rsi > rsi_1h`, which becomes a fast-RSI-over-slow-RSI
  analogue on the native frame. The first two are protections and only cut the trade count;
  the third is a signal term and the substitution is named in the file. **Restricted to the
  two classes with volume**, because its MFI term is a hard gate.
* **`renko_delta`** — the volatility filter, inert at the published threshold of 1e8. The
  notebooks fill at the next bar's open; this repo owns fill timing at book level via
  `--fill`, so the position is emitted on the signal bar.

## The long/flat cell is not optional

Pine's `strategy.entry(short)` **reverses**; it does not close to cash. Eight of the
thirteen therefore ship as long/short rules, and on this repo's benchmark that single
property dominates everything else they do — a reversing rule is charged the benchmark's
drift twice over every downtrend it is wrong about.

So every convertible rule carries an `allow_short` parameter with `allow_short=1` in
`grid[0]` (the published behaviour, bare label preserved) and an `allow_short=0` cell
appended. `ssl_hybrid`, `lorentzian_knn` and `range_filter_macd` got theirs on
2026-08-18 as a follow-up, registered as their own trials because they were added *after*
seeing the first pass — that is itself a look at the data and the ledger has to say so.

Five of the thirteen never needed one: `heikin_reversal`, `renko_delta`, `sma_fan_dip`,
`vwma_offset_dip` and `ema_fan_align` come from freqtrade and notebooks, which sell to
cash and have no short side at all.

**Read the long/flat cell before drawing any conclusion about a converted Pine rule.**
Quoting the reversing cell alone measures the short leg, not the signal.

## Not converted (5), and why

These are in the same archive and are deliberately absent from `published/`. Shipping a
plausible-looking rewrite of any of them would put a number on the leaderboard under a name
that did not earn it.

### `es-1m new.txt` — "Snag - Trailing Futures", 1-minute ES
### `NQM2024-5M-futures.txt` — "NQM2024 - Trailing Futures", 5-minute NQ

The same script, tuned to two contracts. Every load-bearing element is outside the
contract at once:

* a **session entry window** (`0630-1300` in a named timezone) and a per-day filled-order
  cap — the rule is defined on the intraday clock, and there is no intraday clock in a
  daily bar;
* **absolute point distances** — `HighPrice - LowPrice >= 35`, `point_entry_call = 18` /
  `12`, `HighPrice - close >= 25`. These are ES and NQ index points. They are not
  percentages and they do not survive being pointed at a $40 stock;
* **percentage trailing stops** under `calc_on_every_tick = true`, i.e. filled intrabar;
* opening-range levels rebuilt each session from the first bars of the day.

And there are no bars: the futures class in this repo is **daily only** (the vendor's
hourly CME archive collapses whole sessions before 2013), and the 5-minute equity cache
holds the old mega-20, not ES or NQ.

### `1 SH VIP- ES 1m Strategy Candle.rtf` — 1-minute ES

Two independent blockers.

**It is gated on look-ahead.** `up10m` and `down8m` come from
`request.security(..., timeframe="12"/"11", lookahead=barmerge.lookahead_on)` — a 12-minute
and an 11-minute higher-timeframe close, requested with look-ahead ON, so the bar's closing
price is known from its first minute. `longCondition` cannot fire without `up10m` and
`shortCondition` cannot fire without `down8m`, so **every** trade in that script is
conditioned on a price that had not printed. Removing the leak does not yield a causal
version of the strategy; it yields a different strategy that has never been tested.

**Its thresholds are absolute.** `math.abs(wma13 - wma48) < 0.005` on an instrument quoted
near 4,500 is a condition that is essentially never true — the "sideways" veto it is
supposed to implement is inert as shipped, and would fire constantly on a low-priced name.

Worth recording separately, because it is the interesting part: the script's actual
behaviour is a **one-bar hold**. `closelong` and `closeshort` are computed at length and
then never wired to an order; what closes every position is the unconditional
`if strategy.position_size > 0: strategy.close("call")` at the bottom of the file. The 400
lines above it decide the entry; the exit is "next bar".

### `newstrategy53.py` — freqtrade, 5m
### `BB_RPB_TSL_BI.py` — freqtrade, 5m

NostalgiaForInfinity-lineage bots, not strategies. Both:

* set `populate_exit_trend` to **0** — there is no signal exit at all. Every exit comes
  from a `custom_sell` of roughly a hundred branches reading `trade.max_rate`,
  `trade.min_rate` and `current_profit`, plus a `custom_stoploss` that steps as profit
  grows. Reproducing the entry and inventing an exit produces a rule whose equity curve is
  mostly the exit I wrote;
* stack **eight to twenty ORed entry branches**, each hyperopt-fitted on 5-minute crypto —
  `is_dip`, `is_break`, `is_local_uptrend`, `is_ewo`, `is_cofi`, `is_nfi_13`, `is_nfi_32`,
  and more. The `n_trials` that honestly prices such a search is not something this repo
  can reconstruct, and pre-registering "one strategy" for it would be a lie to
  `metrics.deflated_sharpe`;
* read a **1-hour informative frame** and, in `newstrategy53`, BTC's own drawdown;
* `newstrategy53` additionally runs **DCA safety orders** — up to 8, at
  `safety_order_volume_scale = 1.4` — which is position sizing inside a trade and has no
  expression at all in a -1..1 exposure.

If these are wanted, the honest route is not a conversion: it is to pick **one** named
entry branch, register it as its own trial, give it a stated exit, and let it be scored as
what it is — a new rule inspired by a bot, not a replication of one.

## The megacellar forecaster batch (130), added 2026-08-21

A second kind of conversion, and the difference matters more than the count. The thirteen
above came from **somebody's code** — a Pine `strategy()` or a freqtrade `IStrategy` — so
the question was what got dropped fitting it through `fn(df, close, bpy)`. These 130 came
from **somebody's prose**. There is no code.

Source: `megacellar-browser.onrender.com`, a static site titled *164 Trading Strategy
Browser*. It publishes, per rule, one paragraph of description, a total return at each of
eight rebalance horizons, and a benchmark. It publishes no implementation.

### Why the source's own ranking cannot be used

Its benchmark is **one number, 9.7497x, reused unchanged at every one of the eight
cells**, while the rules rebalance weekly or monthly. That is the weighting-and-schedule
mismatch the root `CLAUDE.md` puts second in its table of five, and it is enough on its
own to explain the result:

    1,150 of 1,310 cells beat it                     88%
    164 of 164 rules beat it on at least one horizon  100%
    median best-cell outperformance                  +103.9x

No costs and no fill convention are documented. The curve shapes carry the other tell:
the top-ranked rules capture ~140% of the benchmark's up-moves and ~73% of its down-moves
*simultaneously*, and the effect scales with rank — which for 164 unrelated rules is the
signature of same-bar information, not of 164 separate edges.

**So "it beat the benchmark there" is the reason a rule is in this batch, and is not
evidence of anything.** The batch exists to price that claim on a matched benchmark.

### CORRECTION, 2026-08-21: the `timeframe` key is `<lookback>_<schedule>`

The browser JSON exposes eight `timeframe` values per rule -- `7_weekly`, `14_weekly`,
`21_weekly`, `28_weekly`, `30_monthly`, `60_monthly`, `90_monthly`, `180_monthly` -- and
nothing anywhere in it says what they mean. The first pass read them as eight rebalance
horizons. **They are a lookback crossed with a rebalance schedule**: the number is the
LOOKBACK IN DAYS and the word is how often the book trades. The source's own blog post
gives it away by listing "90-day Lookback / Monthly Rebalancing" beside a chart whose
underlying cell is `90_monthly`, and the total returns tie out exactly.

Two things in the first pass are wrong because of it, and both are in the flattering
direction for this repo rather than for the source:

**The source does specify a parameter, and it sweeps it.** The first pass fixed
`days=60.0` and told every file "the source specifies no parameters at all". That is one
of the eight lookbacks the source actually searched, and not the one its leaderboard
reports -- its headline figure per rule is the **maximum over eight lookbacks**. So the
first pass under-tested every rule (it saw one cell where the source saw eight) *and*
under-counted the source's own selection (taking the best of eight is exactly the search
this repo charges trials for).

**Fixed for all 130 on 2026-08-21 (pass 2).** Every rule now carries the source's own
eight `(lookback, schedule)` cells, and `grid[0]` is **the cell the source's leaderboard
reports** -- or, for the three its blog names, the cell the blog advertises. Those are not
the same thing: the blog picks monthly cells deliberately, while `EntropyDivergence` peaks
at `21_weekly` and `RecursiveEnvelope` at `7_weekly` on the source's own numbers. Pinning
the advertised cell is what makes the no-fitting row a direct test of the claim, and
carrying all eight is what lets the walk-forward re-pick the lookback out of sample rather
than inherit the source's in-sample choice. 1,028 cells in total.

**Nothing in the batch rebalanced on the source's schedule.** The first pass had every
rule re-deciding its exposure on every bar; the source trades weekly or monthly. Measured
on AAPL that was 16.6 turns a year against 4.0 for the same signal held monthly, and the
median across the batch has come down from ~21 to 11.4 turns a year now that each cell
carries its own schedule.

The decimation has one implementation, `_forecast.decimate`, reached two ways: every
`mc_*` rule passes its `rebal` parameter through `expose`, and
`strategies/overlays/hold.py` (`hold:<days>:<base label>`) wraps any other label the same
way. The signal still sees every bar -- only the act of trading is decimated, which is not
the same thing as sampling the price weekly. The rebalance grid is anchored at bar 0, not
at the end of the series, which is the whole of what makes it causal and is what the
truncation gate checks.

**Ten rules had no lookback to sweep and were re-parameterised**, each by the standard
identity for its own filter, recorded in its `NOTE`: an EMA alpha becomes
`span=win(bpy, days)` (`mc_adaptive_midpoint`, `mc_recursive_midpoint`,
`mc_max_pain_pivot`, `mc_liquidity_weighted_price`, `mc_recursive_envelope`), an RLS
forgetting factor becomes `lam = 1 - 1/win(bpy, days)` (`mc_recursive_least_squares`), and
an Ehlers cutoff becomes `alpha = 2*sin(pi/win(bpy, days))` (`mc_ehlers_homodyne_filter`).
Three more sweep a differently-named window (`mc_local_linear_regression` on `local_days`,
`mc_rolling_window_smoothed_max` on `smooth_days`, `mc_holt_winters_seasonal` on
`season_len`).

**Two rules have no window at all and say so.** `mc_kalman_filter_price` is a recursive
filter over all history and `mc_round_number_magnet` reads only the current price, so the
source's lookback axis has no expression in either. They carry the two rebalance schedules
and nothing else -- two cells, not eight. Inventing a window to fill the grid would not be
a replication of them.

**What survives unchanged.** The benchmark criticism: a baseline that never rebalances,
compared against a book that rebalances weekly or monthly, is still the unmatched-schedule
failure, and it still explains an 88% win rate on its own.

### The Top 3 the source advertises

Its blog names three rules as "clear winners, all achieving over 20% CAGR":

| source name | in `published/` | claimed cell | claimed CAGR after fees | site total |
|---|---|---|---|---|
| `SquaredDiffMean` | `mc_smaema_difference` | 90-day, monthly | 21.80% | 196.9x |
| `EntropyDivergence` | `mc_entropy_divergence` | 90-day, monthly | 20.95% | 171.9x |
| `RecursiveEnvelope` | `mc_recursive_envelope` | 180-day, monthly | 20.38% | 132.1x |

**`SquaredDiffMean` does not exist in the source's own data** -- it is absent from the
browser's 164-name list and from all 259 descriptions. Its blog description is word for
word the description the browser publishes for `SMAEMADifference`, whose `90_monthly` cell
returns 196.9x, matching the blog's chart. The two are the same rule under two names; the
name on the blog is not queryable.

`mc_recursive_envelope` was re-parameterised on 2026-08-21 as part of this: the source
states its smoothing as an `alpha`, but its published cell is a 180-day lookback, which
the first port's `alpha=0.1` (a ~19-bar span) could not express. Its earlier scored numbers
are superseded.

### What was dropped: 34 of the 164

| dropped | count | why |
|---|---|---|
| cross-sectional / multi-asset | 31 | `CrossAsset*`, `CompositeCrossAsset*`, PCA, cointegration, copula, pairwise-spread, basket-relative, breadth. The strategy layer sees ONE symbol — the fourth row of the contract table above. This includes the source's own #1 rule, `CrossAssetMomentumClusters` |
| market-relative | 3 | `CopulaTailDependence`, `CrossSigmaDivergence`, `RollingPairSpreadZScore` read an asset against the market or against every other asset. Same reason, caught on a second pass |

The 31 are not "hard"; they are unexpressible here. Building them would mean a
cross-sectional layer this repo does not have, and faking one per-symbol would produce a
rule the source did not publish.

### What every file in the batch assumes

Four things, and none of them come from the source.

**The signal-to-position map.** These are price FORECASTERS: each emits a guess at the
next price, not an exposure. `strategies/_forecast.expose` is the single definition of
how that becomes a trade — long when the forecast is above the same bar's close, flat
otherwise. Long/flat rather than long/short, following the section above. **Only the sign
of `forecast - close` survives**, so every magnitude the source's formula produces is
discarded, and a rule forecasting a 200% move scores identically to one forecasting a
tick. Several rules collapse under this: see the degeneracy list below.

**The lookback.** The source names no parameters at all. Every window is 60 days unless
the description states otherwise, converted through `_bars(bpy, days * D)` like every
other rule here, so it means the same span on the 1d and 4h sheets. Because nothing was
fitted, **each rule ships a one-cell GRID** — 130 cells, not 130 x a grid. Widening it
would be search this batch has no licence for.

**The fill.** The repo's `close` convention, identical to every other rule on the sheet
and carrying the same look-ahead the root `CLAUDE.md` describes. The source's convention
is undocumented, so the two are not comparable even where the logic matches.

**Three ambiguities were resolved by choice, and each says so in its own NOTE**:
`mc_cumulative_sum_reversion` (the literal CUSUM is identically zero at the window's last
bar), `mc_quantile_atr_hybrid` (the direction of the ATR push), `mc_tukey_lambda_reversion`
(no estimator is given). Two rules quantise a continuously-varying window to four
candidates because a per-bar window length cannot be vectorised:
`mc_adaptive_std_band`, `mc_adaptive_window_sma`.

### Reproduced defects, not repaired

Same principle as `lorentzian_knn` and `ssl_hybrid` above — a fixed version of a published
rule is not a replication of it.

* **`mc_adxr_persistence`** reads a rising ADXR as a rising price. ADXR is unsigned, so
  the rule is long into strengthening downtrends.
* **`mc_peak_to_peak_forecast`** computes the peak-to-peak distance and never uses it.
* **`mc_recursive_envelope`** computes an envelope width and predicts only the centre.
* **`mc_liquidity_weighted_price`** uses no volume; the weighting is recency alone.
* **`mc_smaema_difference`** is long when the SMA is above the EMA, which is the recent
  trend being DOWN.

### The degeneracy list — read it before reading any score

Under a long/flat map some of these rules are not rules. Their forecast cannot cross the
close in one direction, so they are constants wearing a strategy's name. They are kept,
by name, because each was a trial and because discovering this on the sheet later is
worse than declaring it here:

* **Structurally always long** — the forecast is `close + something non-negative`:
  `mc_bollinger_bandwidth`, `mc_composite_range_momentum`, `mc_directional_change_rate`,
  `mc_min_max_slope`, `mc_rolling_max_drawdown_reversion`, `mc_rolling_window_l2_norm`,
  `mc_wave_height_momentum`, `mc_williams_r_reactive`, `mc_down_day_reversion`,
  `mc_mean_of_top_k_returns`. Two of these ranked in the source's top 5.
* **Structurally always flat** — the forecast can never exceed the close:
  `mc_current_run_reversion`.
* **Duplicated mechanism** — the source ships the same rule twice under two names, and
  both are carried so the trial count reflects what was actually searched:
  `mc_adaptive_midpoint` / `mc_recursive_midpoint`, and
  `mc_normalized_band_deviation_reversion` / `mc_rolling_window_z_score_mean`.

`mc_chaikin_money_flow_gradient` and `mc_chaikin_oscillator_spread` set `CLASSES` to
exclude `crypto` and `commodities`: the vendor serves no volume for either class — the
column exists and is identically zero — so the rules are undefined there and are counted
as skipped rather than scored as rules that never trade.

### Bookkeeping

All 130 pass `strategies/tests/test_causality.py` by truncation, at their published cell
and under the `volregime:`, `ha:` and `chart:` overlays. All 1,162 (rule, scope) trials
were written to `data/reference/trials.csv` **before** anything was scored — nine scopes,
five classes at 1d and 4h, `cme_futures` at 1d only.

**The batch is a plausible rewrite from a description, and its results must be read that
way.** A number on this sheet is evidence about the rule as described, not about the rule
as the source ran it, and the two can differ without either being wrong.

## The chart these were actually run on (added 2026-08-20)

The August batch was scored at **1d**, on real candles, with this repo's day-denominated
window convention. None of those three matches how the scripts were being run: the owner
had them on **1-to-3-minute TradingView charts, mostly on Heikin-Ashi candles**. That is
not a detail of presentation — it changes the signal, the window lengths and, on a chart
platform, the fill. So the eight Pine conversions were re-registered and re-run on their
own terms, 384 cells, pre-registered before anything was scored.

Three substitutions separate the two readings, and each is its own trial:

**The bars.** `2m` and `3m` are not vendor intervals; `backtest engine/resample_intraday.py`
aggregates them out of the cached 1m. `1m` is fetched.

**The candles.** `ha:` computes the base rule's signal on Heikin-Ashi candles. **The money
still settles on real prices**, and that is the whole point of the overlay: an HA close is
`(O+H+L+C)/4`, an average, not a price anybody is filled at. TradingView's emulator fills
at it by default, and since the synthetic close is pulled toward the middle of the bar,
that default alone buys below where you could have bought. Every published HA result from
a chart platform carries it. This one does not, and the two numbers are not comparable.

**The windows.** This repo reads `ema_cross_sniper` as "8-DAY against 21-DAY EMA" on every
sheet; Pine reads the same script on a 1m chart as 8-BAR against 21-BAR. `chart:` pins the
Pine reading. On the intraday sheets it is also the only buildable one — the day form
implies windows of tens of thousands of bars and TA-Lib rejects several outright
(`range_filter_macd` and `ssl_hybrid` both fail to build at 1m without it).

The controls are the point of the design. `chart:X` and `ha:chart:X` run side by side on
every sheet, so the HA cell has a plain-candle twin at the same timeframe with the same
fills — without it, a good HA number could not be told apart from a timeframe effect.
`BUYHOLD`, `RANDOM_50` and `RANDOM_75` price the exposure handicap as everywhere else.

**Read the cost line before reading anything else on these sheets.** At 1-3 minute bars a
reversing rule trades hundreds of thousands of times, and the fee schedules are real
(`retail` for equities, Binance spot taker for crypto). Cost drag on the order of the
whole return is expected here, not a bug — it is most of what the study is measuring.

## The SP100 Momentum Stockpicker (1), added 2026-08-26

Source: `sp100_momentum_strategy.py`, a NautilusTrader implementation of StrategyQuant's
*SP100 Momentum* (an AlgoCloud Stockpicker template), supplied 2026-08-26. Unlike the
thirteen above it arrived as **working code against a real engine**, so nothing had to be
guessed at — which makes what could not come across easier to state exactly.

It is the fourth row of the contract table and the third at the same time. The script is
a **Stockpicker**: on every bar it evaluates the whole SP100 at once, scores each
candidate by `ROC(close,20)[1]`, and fills the free slots of a ten-position book
best-first. Four things therefore have no expression in `fn(df, close, bpy) -> exposure`:

| dropped | why |
|---|---|
| the position score, `ROC(close,20)[1]` | it only ranks candidates *against each other*, and the strategy layer sees one symbol |
| `max_positions = 10` | the cap the ranking competes for. Without the panel there is nothing to cap |
| risk-percent sizing | `qty = equity * risk_pct / (entry * stop_pct)`. A -1..1 exposure has no stake to set |
| the SPY market filter | `SPY.Close[0] > SPY.SMA(200)[1]`, on **both** the entry gate and the exit. Cross-asset |

**What is left is the per-symbol entry and exit, taken every time it fires**, which is a
breadth rule where the source is a concentrated book. That difference is not a detail: the
original holds ten names and this holds every name that qualifies, so their exposure
profiles are not the same measurement and their drawdowns cannot be compared.

**One substitution, and it is named in the file.** The market filter becomes the
**symbol's own** close against its own `SMA200[1]`. That is an analogue in exactly the
sense `ema_fan_align`'s `rsi > rsi_1h` is one, and it is weaker in a specific way: on a
single name the own-200 test is much noisier than an index-level regime read, and it
overlaps the 50/200 cross the rule already carries. `regime=0` is in the grid so the
cost of the substitution is a column on the sheet rather than an argument.

**One deviation.** The 20% stop is measured on the **close**; the source submits a
`stop_market` order that fills intrabar. Same deviation, same reason, as `sma_fan_dip`.

**One property reproduced rather than smoothed.** StrategyQuant indexes `[0]` as the bar
that just closed and `[1]` as the one before it, and the script snapshots each indicator
*before* feeding it the new bar. So `SMA50`, `SMA200` and `ATR14` are read one bar late
while `Close` and `RSI(2)` are read on the current bar. That is stricter than this repo's
default and it is kept.

36 cells (4 grid cells x 9 scopes) were registered in `data/reference/trials.csv` before
anything was scored, and it passes `strategies/tests/test_causality.py` by truncation.
