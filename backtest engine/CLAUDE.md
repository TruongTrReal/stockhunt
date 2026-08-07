# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this directory.

## What this is

The third and widest study in this repo. `../test research/` asked whether any TA-Lib
rule beats buy-and-hold across 501 S&P tickers on daily bars (no). `../top 20 stocks/`
asked the same at depth on 20 mega-caps across 1d/1h/5m (no, and finer timeframes were
monotonically worse). This one widens to **two asset classes and seven timeframes**,
ranks on **information ratio against four acceptance gates** rather than excess Sharpe,
and — the real change — makes the backtest itself auditable by running three independent
engines against each other.

Read `../test research/CLAUDE.md` too: the signal layer and the position conventions come
from there and apply here unchanged.

**Current state: a null result on US stocks.** 0 of 230 rules cleared all four gates at
any of six timeframes or any cost level. Best net out-of-sample IR is **-0.21** (1d,
26 years of history); it degrades monotonically to -0.84 by 15m. Treat a run that
suddenly produces winners as a bug until `parity.py` and the multiplicity correction
have both been re-checked.

## Setup and commands

The venv is one level up (in the repo root) and shared with the sibling studies:

```powershell
..\.venv\Scripts\Activate.ps1
```

TA-Lib's Python wrapper needs the compiled TA-Lib C library already on the system. The
Twelve Data key comes from `TWELVEDATA_API_KEY`, falling back to `../.env.local`.

Run everything **from this directory** — modules import each other by bare name:

```powershell
python td_loader.py                       # fetch everything (~5h, ~13k credits)
python td_loader.py --class crypto --tf 1d
python check_data.py --fix                # OHLC integrity scan + repair
python parity.py --n 3                    # three-engine cross-check (gate on this)
python sweep.py --class us_stocks         # stage 1: singles
python walkforward.py --class us_stocks --tf 1d   # stage 1b: rolling re-optimisation
python variants.py --tf 1d 4h             # stage 1c: transforms on the IS shortlist
python prereg.py --tf 1d 4h --freeze      # stage 1d: 5 pre-registered published rules
python strategies.py --tf 1d 4h           # stage 1e: 25 published strategies + WFO
python strategies.py --list               # ...print the catalog and exit
python gate_calibration.py                # are the four gates coherent on this sample?
python combo_wf.py --tf 1d 4h             # stage 2b: walk-forward pairs (use this)
python combo_sweep.py                     # stage 2/3: single-split pairs (legacy)
python validate.py                        # Nautilus on survivors (if any)
python build_payload.py                   # results -> report/report_payload.json
python build_report.py                    # -> report/index.html
python build_report.py --demo             # synthetic payload, layout check only
```

## Architecture

```
config.py        universes, 7 timeframes, per-class cost grids, gates, sys.path
   |
td_loader.py     Twelve Data -> ../data/<stocks|crypto|etfs>/<tf>/<SYMBOL>.parquet
check_data.py    OHLC integrity scan and repair
   |
signals.py       the ONE way a rule name becomes a position series
   |
engines/         vector.py | reference.py | nautilus.py
parity.py        samples cells, runs all three, FAILS on disagreement
   |
sweep.py         stage 1: 231 singles x assets x timeframes x costs
walkforward.py   stage 1b: rolling re-optimisation + the IS#1 selection rule
variants.py      stage 1c: 8 transforms on the IS-shortlisted leaders
prereg.py        stage 1d: 5 published rules, no free parameters, no selection
strategies.py    stage 1e: 25 published strategies, 93 cells, + the exposure controls
gate_calibration.py  power check: is a gate below its own noise ceiling?
combo_wf.py      stage 2b: walk-forward pairs + leg-correlation diagnosis
combo_sweep.py   stage 2/3: pairs of train-shortlisted singles, 4 operators (legacy split)
metrics.py       IR, the four gates, breakeven, leave-one-out
   |
validate.py      Nautilus on survivors: whole shares, commission, slippage
build_payload.py results -> report/report_payload.json
build_report.py  template.html + report.js + payload -> report/index.html
```

**`config.py` is load-bearing beyond configuration.** It prepends
`../test research/src` to `sys.path`, which is the only reason `from talib_signals
import ...` resolves. The signal layer is *shared, not copied*: editing that file changes
all three studies, and moving or renaming the sibling directory breaks this one.

**`signals.py` is the single source of positions.** `parity.py`, `sweep.py`,
`combo_sweep.py` and `validate.py` must all produce byte-identical positions for the same
cell, or the parity harness is checking the wrong thing. Benchmark plumbing (BETA,
CORREL), the NaN policy and end-of-day flattening live there and nowhere else.

## Conventions that carry real meaning

- **Rank on IR against buy-and-hold on the same asset.** Not raw Sharpe (measures
  long-bias in a rising market) and not excess Sharpe (discards the benchmark
  correlation that decides detectability). A rule that sits flat has IR approximately
  *minus* the benchmark's Sharpe — so on the intraday sheets the "best" rules are the
  ones that most nearly do nothing, and that is a fact about the metric, not a finding.
- **Positions re-size to the target fraction every bar.** `(1 + pos.shift(1)*ret).cumprod()`
  means constant *fraction*, not constant share count. The two are identical for long and
  flat and differ badly for shorts — a static -1 short through four +10% bars ends at
  0.536x, a re-sized one at 0.9^4 = 0.656x. Any new engine must match this or parity
  will (correctly) fail.
- **Cost grids are per asset class.** Stocks 0/1/5/10bps headline 5; crypto 0/5/10/20
  headline 10, because major-exchange taker fees run ~10bps a side. Charging crypto an
  equity grid manufactures survivors.
- **The baseline is never charged and never flattened.** Flattening the benchmark turns
  it into a different strategy — precisely what made the old 5m "beat" an artifact.
- **Shortlisting happens on train-period IR only.** Sorting by a test column and reading
  the top rows is selection on test; this project has done it and had to retract.
- **A single split scores a rule; it does not score *choosing* a rule.** `sweep.py`'s
  leaderboard picks its winner once, with the whole test column already visible — nobody
  trading in 2015 had that table. `walkforward.py` prices the choice: parameters and rule
  identity are re-selected on each 3y in-sample window and applied to the next 12 months.
  Quote **IS#1** as the headline, not the best fixed rule. On 1d equities the gap is
  **-0.21 IR** (-0.447 vs -0.237), and per-fold parameter tuning lost on **all 14**
  re-optimisable families (mean -0.083). Selection is a cost here, never a free upgrade.
- **Annualisation is measured**, never a constant. A US equity 4h "day" is one 4h bar
  plus a 2.5h stub.
- `ddof=1` everywhere. pandas defaults to 1, numpy to 0; mixing them makes two backtests
  silently incomparable.
- **One source, one adjustment.** Twelve Data with `adjust=all`. Never compare against
  the yfinance-derived leaderboards: 0.05% of position-days differ but final equity moves
  up to 18%, which looks exactly like alpha.

- **Check the gates against the sample before believing a null.** `gate_calibration.py`
  reports the effective bar, `max(IR gate, 2/sqrt(years), noise_ceiling(N, years))`. Only
  **us_stocks 1d** is coherent, and only since the history fix below: at 41 OOS years its
  ceiling is +0.43 even after 327 trials, under the 0.50 gate. Every other sheet carries
  3.4-5.9 OOS years, where the t gate alone implies IR 0.82-1.09 and the ceiling at 96
  trials is +0.95 to +1.26 — so the stated 0.50 gate sits *below* what luck produces and
  cannot be passed meaningfully. Relaxing such a gate makes it easier and more worthless
  simultaneously; the coherent moves are more years or fewer trials.
- **On equities, read `long_frac` before believing any IR improvement.** `combo_wf.py`
  found the best daily combination at IR -0.057 against -0.224 for the best single — and
  it is long **96%** of the time. `MININDEX~MAXINDEX|or` is long **100%**: it is literally
  buy-and-hold. Across all 140 combinations `corr(IR, long_frac) = 0.881` on 1d and 0.692
  on 4h, so the whole leaderboard is a ranking of time-in-market, and `or` wins because it
  is the operator that spends the most. IR against buy-and-hold approaches 0 from below as
  a rule approaches always-long, and 0 is the *ceiling*, not a win. Crypto behaves
  differently (correlation -0.12 / -0.00), so the artifact is an equity-uptrend effect,
  not a property of the metric everywhere.
- **Daily equity history goes back to 1970, not 2000.** `WINDOWS[("us_stocks","1d")]`
  carried `start: 2000-01-01` annotated as the vendor's earliest timestamp. It was wrong:
  `/earliest_timestamp` returns 1970-01-02 for JNJ/KO/XOM/PG/CVX, 1980-12-12 for AAPL,
  1986-03-13 for MSFT. Refetching took the sheet from 24 to **54 folds** and median OOS
  years from 23.6 to 41.0, which cut the noise ceiling from +0.48 to +0.36 and is the
  single reason the gates are testable at all. History is the only lever on the ceiling —
  `metrics.se_ir` falls as 1/sqrt(years) and ignores how many assets or bars those years
  hold. The pre-2000 cache is preserved at `../data/_archive/stocks_1d_pre2000/`.

- **A negative IR does not mean a rule is worthless, and a leaderboard without exposure
  controls cannot tell you which.** Against a rising benchmark, IR is close to a linear
  function of time-in-market: `strategies.py` measures it directly with six signal-free
  controls (`ALWAYS_FLAT`, seeded block-random rules at 25/50/75/90%, `ALWAYS_LONG`) and
  on us_stocks 1d the curve runs -0.68 at zero exposure to 0.00 at full exposure. Rank on
  `ir_vs_random` — the row's IR minus that curve at its *own* exposure — before believing
  any ordering. It reverses the leaderboard: on equities every mean-reversion rule clears
  its control while the trend rules do not, and on crypto that flips exactly.
- **IR is arithmetic and blind to variance drag; on leveraged ETFs that matters.** `ibs`
  on SOXL scores IR -0.134 (a loss) and simultaneously compounds **+25.4%/yr more than
  buy-and-hold** (1,908x vs 230x), because it cuts annualised vol from 93.5% to 62.7% and
  the drag that removes is worth 24 points a year. Both numbers are correct.
  `strategies.excess_cagr` reports `excess_cagr` / `excess_cagr_min` / `excess_cagr_hit`
  alongside IR for this reason. Note `walkforward.leaderboard_row`'s `excess_return_pct`
  is a mean of per-asset *total* returns and is near-meaningless when SOXL compounds 230x
  and SPY 6.4x — annualise first, average second.
- **`np.nanmedian(whole_series)` is look-ahead, and it is in this repo.**
  `prereg.volmanaged`, `variants._vol_scale` and any Pruitt-style adaptive lookback
  normalise current volatility against the median of the *entire* series. It is one
  scalar rather than a per-bar signal, but it is still future data: truncating the series
  changed 11 of 93 cells' *past* positions. `strategies._causal_median` uses an expanding
  median instead. **The stage 1c and 1d results were not re-run** — their
  volatility-scaled rows carry the leak, and it is worth 0.084 IR on `volmanaged` at
  us_stocks 1d (the contaminated version scored -0.368, the clean one -0.284).
  Re-run those two stages before quoting their vol-scaled numbers again.
- **Test causality by truncation, not by reading the code.** Build positions on the full
  series and on the series minus the last N bars, then assert the overlapping region is
  identical. That is what caught the above; no amount of staring at `rolling()` calls did.

## Gotchas

- **Twelve Data serves no volume for crypto** — the field is absent, not zero. AD, ADOSC,
  MFI and OBV therefore cannot be evaluated on that class. `signals.usable_rules` skips
  and counts them; they are never fed NaN, because a volume rule on NaN produces a flat
  position that is indistinguishable on a leaderboard from a rule that does nothing.
- **The vendor ships broken intraday OHLC bars** — 284 of ~3.4M had `high < close` or
  `low > open`. The vectorised engine reads Close only and would consume them forever in
  silence; Nautilus refuses to load them, which is how they were found. Run
  `check_data.py --fix` after any fetch, before sweeping.
- **The vendor also ships decimal-point bad ticks in crypto intraday**, and these are far
  more dangerous. BTC prints 2.812 instead of 28,100 for one minute, then recovers. Raw
  returns self-cancel, so `prod(1+r)` still telescopes to the right answer and nothing
  looks wrong. But the **-0.999 return floor clips the crash leg and not the recovery**,
  so each spike pair multiplies equity by ~10x — 126 spikes on BTC 1-minute turned
  buy-and-hold into 1e125 and contaminated every IR on that sheet. `check_data.py` now
  detects them with a centred rolling median and **drops** the bars (a bar at 1/10,000 of
  the true price did not happen; interpolating would invent a trade). 182 across crypto,
  every timeframe except daily; zero in equities.
  **This was not caught by the OHLC check** — each bad bar is internally consistent, its
  own high/low correctly bracketing its own bogus close — **nor by structural payload
  validation, nor by jsdom.** It was caught by looking at the rendered page and seeing a
  buy-and-hold PnL of $2.9e129. Structural checks verify shape; sanity-check the actual
  numbers on the actual output.
- **Nautilus parity tolerance scales with sqrt(fills), not with equity.** Every fill
  rounds cash to the cent, and a short re-sizes every bar — one 7k-bar cell produced
  5,355 fills. A flat tolerance produces false failures; see the note in `parity.py`.
- **Nautilus parity runs at zero cost only.** A venue fee is charged on traded notional;
  this project's cost model charges on target change. Under constant-fraction rebalancing
  those legitimately differ. Cost modelling is verified between `vector` and `reference`
  across the whole grid instead.
- **Background jobs get killed at 10 minutes** by the harness timeout. Long fetches and
  sweeps must be launched detached (`Start-Process ... -WindowStyle Hidden`) with output
  redirected to `logs/*.log`.
- `report/index.html` is generated. Edit `template.html`, `report.js` or
  `build_payload.py` — never `index.html`, which is overwritten every build.
- **The report is forced to pure ASCII** by `build_report.py`, with different escaping
  per region (character references in HTML, backslash escapes in JS, and CSS `content:`
  must be ASCII outright). A raw `->` in the source renders as mojibake in any context
  where the charset cannot be declared.
