# Master backtest — design

Status: **built and run.** 13 of 14 (class, timeframe) pairs complete — crypto 1-minute
is still fetching. **Zero candidates cleared all four gates anywhere**: not on 20 US
stocks across 7 timeframes and 26 years, not on 10 crypto pairs across 6, not among 4,652
combos, and not at zero cost. See "Results" at the end.

The third study in this repo. `test research/` asked whether any TA-Lib rule beats buy-and-hold
across 501 tickers on daily bars (no). `top 20 stocks/` asked the same at depth on 20 mega-caps
across 1d/1h/5m (no, and finer timeframes were monotonically worse). This one widens to two asset
classes and seven timeframes, ranks on **information ratio** against the four acceptance gates
rather than on excess Sharpe, and — the real change — makes the backtest itself auditable by
running three independent implementations against each other.

---

## Decisions taken

| decision | choice | why |
|---|---|---|
| Engine | Triangulated hybrid | Vectorized sweep is exact by construction and was the *ground truth* in `engine-bakeoff`; Nautilus matched it to 5.7e-6, and that residual was Nautilus's own rounding. Nautilus validates survivors. |
| Futures | **Skipped** | Twelve Data serves zero CME contracts. `NYBOT` lists 0 symbols; `ES`/`CL`/`MES`/`KC` all resolve to equities. Recorded as a scope gap, not silently dropped. |
| Metrics | The 4 acceptance gates | IR (net, OOS), breadth, breakeven cost, `t = IR × √years`, plus leave-one-out. From `edge-acceptance-criteria`. |
| Combo scope | Staged shortlist | Full 231-choose-2 is 26,565 pairs × 30 assets × 7 timeframes. Bonferroni at 26k candidates needs t > 4.4 — nothing in this project has come close. More candidates raise the bar, not lower it. |

---

## Universe

Futures are out, so 30 assets in two classes. Each asset is benchmarked against **buy-and-hold on
itself**, so the two classes never need a common benchmark and are never compared on absolute return.

**Crypto (10).** `BTC ETH XRP BNB SOL DOGE ADA TRX AVAX LINK` — all `/USD`, all confirmed present.
`DOT` and `LTC` were considered and dropped: their 1-minute history starts 2023-05 and 2021-05
respectively, too short for the finest timeframe.

**US stocks (20).** The same 20 mega-caps as `top 20 stocks/`: `AAPL MSFT AMZN NVDA GOOGL META TSLA
JPM JNJ XOM UNH V PG HD MA CVX ABBV PEP KO WMT`. Deliberately identical so results are directly
comparable to the existing null result rather than a fresh universe with fresh biases.

`SPY` is fetched and cached but is **not** in the universe — it exists only as the benchmark input
for the `BETA` and `CORREL` rules, exactly as in `top 20 stocks/config.py`.

### Survivorship, stated up front

Today's top-10 crypto by market cap is a survivorship-biased sample and a far worse one than the
S&P 500 was — LUNA, FTT and a long tail of dead coins are missing by construction. Measured
survivorship bias in this repo already inflates buy-and-hold by 4.85pp of CAGR on equities.

The saving grace is structural: every metric here is **relative to buy-and-hold on the same asset**,
and both the strategy and its benchmark are computed on the same survivorship-biased series. The
bias inflates both sides and largely cancels in the IR. So absolute crypto CAGR figures in this
report are meaningless as a forecast; relative ones survive. This gets a banner in the artifact,
not a footnote.

---

## Timeframes and what the data actually supports

Seven timeframes. Twelve Data's intraday history is shallower than its daily history, and it differs
per interval — measured via `earliest_timestamp`, not assumed:

| tf | interval | stocks from | crypto from | bars/asset (stock) | bars/asset (crypto) |
|---|---|---|---|---|---|
| 1d | `1day` | 1980-12 | 2017-08 | ~11,500 | ~3,300 |
| 4h | `4h` | 2019-06 | 2020-01 | ~3,200 | ~14,500 |
| 2h | `2h` | 2019-06 | 2020-01 | ~6,300 | ~29,000 |
| 1h | `1h` | 2019-01 | 2020-01 | ~13,400 | ~58,000 |
| 15m | `15min` | 2019-09 | 2020-02 | ~45,000 | ~231,000 |
| 5m | `5min` | 2020-01 | 2020-03 | ~130,000 | ~690,000 |
| 1m | `1min` | 2020-03 | 2020-04 | ~630,000 | ~3,350,000 |

≈60M bars total. Two consequences that must not be papered over:

- **Daily runs to 1980 for equities and only 2017 for crypto.** `t = IR × √years` is the governing
  formula, so daily equities get √26 = 5.1 against crypto's √9 = 3.0. The report shows a *common
  window* and a *full window* side by side and never mixes them in one comparison.
- **4h and 2h bars are session-aligned and ragged.** A US equity 4h "day" is one 4h bar (09:30) plus
  a 2.5h stub (13:30) — verified on AAPL. Crypto 4h is uniform 24/7. Annualisation is therefore
  **measured** from the actual index span (`bars_per_year()`), never a theoretical constant. This is
  the same convention as `top 20 stocks/` and the reason it is there.

---

## Cost model — per asset class, not one grid

`top 20 stocks/` charged `[0, 1, 5, 10]` bps with a 5bps headline. Applying that to crypto would be
wrong: major-exchange taker fees are ~10bps a side before spread. Using an equity cost grid on crypto
would manufacture survivors.

| class | grid (bps) | headline | rationale |
|---|---|---|---|
| US stocks | 0, 1, 5, 10 | **5** | Unchanged from `top 20 stocks/`, so results stay comparable |
| Crypto | 0, 5, 10, 20 | **10** | Taker ~10bps/side; 20bps is the realistic round-trip |

Charged on `|position.diff()|` as before: flat→long costs one side, long→short costs two. Zero is
reported as the gross case only. **A gross-only result is not evidence** — the headline gate is
always evaluated at the non-zero headline cost.

---

## The four gates

Ranked on information ratio, `mean(r − r_bh) / std(r − r_bh) × √ppy`, where `r_bh` is buy-and-hold on
the same asset. Not raw Sharpe (measures long-bias in a rising market), not excess Sharpe (discards
the benchmark correlation that decides detectability).

| gate | bug | marginal | **target** | exceptional |
|---|---|---|---|---|
| IR, net + out-of-sample | > 2.0 | 0.3–0.5 | **0.5–1.0** | 1.0–1.5 |
| breadth (IR hit rate) | 100% or < 50% | 55–65% | **70–80%** | > 80% |
| breakeven cost | — | 1–2× real | **3–5×** | > 10× |
| `t = IR × √years` | > 6 | 1.5–2 | **2–3** | > 3 |

Plus leave-one-out: removing the single best asset must cost **< 20%** of the IR.

Breakeven cost is read off the IR-vs-cost line through the 0bps and lowest-nonzero-bps points, as
`combo_sweep.py` already does. Headroom is `breakeven / headline`, so it is class-aware for free.

The supporting columns from `top 20 stocks/` (`excess_cagr`, `excess_sharpe`, `beat_buyhold_rate`,
`avg_turnover`, `avg_exposure`, `rankable`) are still computed and still shown — they are useful for
diagnosis. They are just not what anything is ranked on.

---

## Pipeline

```
config.py        universe, timeframe specs, per-class cost grids, paths, sys.path -> test research/src
   |
td_loader.py     Twelve Data -> ../data/<class>/<tf>/<SYMBOL>.parquet (network, cached, paginated)
   |
engines/         vector.py  |  reference.py  |  nautilus.py          (three implementations)
   |
parity.py        samples cells, runs all three, FAILS the build on disagreement > 1e-5
   |
sweep.py         stage 1: 231 singles x 30 assets x 7 tf x 4 costs
   |
shortlist.py     top-40 singles per (class, tf) ranked on TRAIN IR only
   |
combo_sweep.py   stage 2: pairs over the shortlist; stage 3: operators over stage-2 survivors
   |
gates.py         the four gates + leave-one-out -> pass/fail per candidate
   |
validate.py      Nautilus on survivors: real Equity instrument, whole-share rounding,
   |             commission + slippage models
build_report.py  everything -> report/index.html (self-contained)
```

The signal layer is **shared, not copied**: `config.py` prepends `../test research/src` to
`sys.path`, so `talib_signals.py` and its 231-variant table are the same code driving all three
studies. Editing it changes all of them — which is the point, and also the hazard.

### Why three engines

The accuracy risk in this project has never been the engine. It is implementation bugs, and a single
engine cannot detect its own. So:

- `engines/vector.py` — the fast path. Vectorized numpy/TA-Lib, streams one rule at a time so peak
  memory is one `int8` position series (3.3 MB at 1m crypto), never a materialised tensor.
- `engines/reference.py` — share-level simulation ported from `engine-bakeoff/common.py`. Every fill
  priced, every mark explicit. Slow, obviously correct, the arbiter.
- `engines/nautilus.py` — event-driven, `talib.*` called natively inside `on_bar`.

`parity.py` samples random `(asset, timeframe, rule, cost)` cells, runs all three, and asserts final
equity agrees within `1e-5` relative. **Disagreement fails the build.**

One honest limitation: Nautilus costs ~0.14 ms/bar, so a full 3.35M-bar 1m series is ~470 s for a
single cell. Parity sampling is therefore **stratified** — full series on 1d/4h/2h/1h, and a fixed
20,000-bar window on 15m/5m/1m. The report states which cells were checked at full length and which
were windowed. A windowed parity check is still a real check; claiming it was full-length would not be.

### Compute and quota budget

- **Fetch:** ~12,000–13,000 credits at 610/min ≈ 21 minutes, dominated by 1m crypto (670 paginated
  requests × 10 symbols). ~1–2 GB of parquet.
- **Stage 1 sweep:** ~13.9B bar-rule evaluations. Signal generation is C-speed TA-Lib; the backtest
  math is chunked float32. Parallel over assets — a few hours, not days.
- **Nautilus validation:** survivors only, so bounded by how many there are. Likely minutes.

---

## Guardrails

Carried from the two prior studies because each one has already cost a result:

- **Shortlist on train only.** Sorting by a test column and reading the top rows is selection on
  test and it manufactures winners. `shortlist.py` never sees the test period.
- **Multiplicity is reported, not hidden.** Every leaderboard shows the candidate count it was
  selected from and the Bonferroni-corrected t threshold that count implies.
- **Positions are 1/0/−1, `shift(1)` before multiplying returns.** Signal on bar *t*'s close trades
  bar *t+1*. Never look-ahead.
- **Net returns clipped at −0.999** before compounding, so a short losing >100% in a bar cannot drive
  equity negative and flip positive on the next multiply.
- **The benchmark is never modified.** Flattening buy-and-hold turns it into a different strategy —
  that is precisely what made the old 5m "beat" an artifact. EOD flattening applies to intraday
  *rules* only, never to `BUYHOLD`.
- **One source, one adjustment.** Twelve Data with `adjust=all`, everywhere. No comparison against
  any yfinance-derived leaderboard: 0.053% of position-days differ but final equity moves up to
  18.2%, which looks exactly like alpha.
- **`ddof=1` everywhere.** pandas defaults to 1, numpy to 0; mixing them makes two backtests
  silently incomparable.
- **Minimum exposure inside the objective.** A ratio objective rewards doing nothing — a rule in the
  market 0.4% of the time can score 1.96. The exposure floor is a constraint in the objective, not a
  post-filter.
- **`prices.describe_source()` stamped into every artifact** that outlives the session.

## Expected outcome

Every prior run in this repo returned a null result, and the honest prior is that this one will too.
The artifact is therefore designed so that **zero survivors is a legible, well-presented answer** —
a funnel showing where candidates died, not an empty table that reads like a broken build. A run
that suddenly produces winners should be treated as a bug until the parity harness and the
multiplicity correction have both been re-checked.

---

## Results (run 2026-08-04)

**Zero of ~4,880 candidates cleared all four gates**, at any class, timeframe or cost.

**US stocks — the gross column is the finding.** Best net IR at 5bps ranges from -0.211
(1d) to -0.835 (15m); at **zero cost** it is still negative everywhere (-0.176 to -0.629).
Costs are not the binding constraint. And this is worse than "no signal": SE(IR) is
1/sqrt(test years) = 0.31 on daily, so the best of 231 *worthless* rules should reach
about +0.9 by luck. Landing at -0.176 means these rules are systematically worse than
holding — which is what happens when a rule that sits flat part of the time gives up the
equity risk premium.

**Crypto behaves differently and it does not help.** Gross IR is positive — +0.209 (1d),
+0.705 (1h) — but the noise ceiling there is ~+1.8 (227 candidates, only ~2.6 test years),
so it sits well inside chance, and costs erase it by 1h. A shorter sample raises the bar
rather than lowering it; crypto and equity numbers are not comparable without it.

**Combos changed nothing.** 4,652 pairs under four operators, shortlisted on train IR
only. The one positive result is equity daily at +0.076 gross / +0.029 net, against a
ceiling of +0.96 for 1,104 candidates. Every other timeframe tracks its singles closely.

**Three defects the triangulated design caught**, none of which a single engine could:

1. **The vendor ships broken intraday OHLC bars** — 284 across equities (0.005-0.03%) plus
   3,530 in 1-minute (0.029%), with `high < close` or `low > open`. Nautilus refuses to
   load them; the vectorised engine reads Close only and would have consumed them in
   silence forever. Crypto had none and daily had none — the rate scales with granularity.
2. **Constant-fraction vs fixed-share accounting diverges on shorts** by 23% over four
   bars. Identical for long and flat, which is why the long/flat bake-off never saw it.
3. **Parity tolerance must scale with sqrt(fills)**, not with equity — a short re-sizes
   every bar, and one 7k-bar cell produced 5,355 cent-rounding events.

Final parity: vector vs reference agree to **8.2e-13** across 84 real cells; all three
engines pass.
