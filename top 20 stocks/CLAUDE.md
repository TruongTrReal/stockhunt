# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **depth** counterpart to the S&P-wide sweep in `../test research/`. That study asked whether any of
TA-Lib's ~231 indicator variants beats buy-and-hold across 501 tickers on daily bars (answer: no).
This one trades breadth for depth — 20 mega-cap tickers, three timeframes (`1d` / `1h` / `5m`), with
transaction costs and end-of-day flattening added — to see whether an edge hides somewhere the daily
sweep could not look.

Five files, no package, no tests, no CI. Offline research plus one self-contained HTML artifact.

Read `../test research/CLAUDE.md` too: the signal layer, the backtest conventions, and the
"never mix price sources" rule all come from there and apply here unchanged.

**Current state: a null result.** 0 rules beat buy-and-hold on average excess Sharpe at any cost
level on `1d` or `5m`; `1h` has 2 at 0bps and 0 once costs are charged. Finer timeframes are strictly
worse. Treat a run that suddenly produces winners as a bug until proven otherwise.

## Setup and commands

The venv lives two levels up at the repo root and is shared with `../test research/`:

```powershell
..\..\.venv\Scripts\Activate.ps1
```

TA-Lib's Python wrapper needs the compiled TA-Lib C library already installed on the system.
The Twelve Data key is read from `TWELVEDATA_API_KEY`, falling back to `../.env.local` (gitignored).

Run everything **from this directory** — the modules import each other by bare name (`from config
import ...`), so `cd` here or set `PYTHONPATH`:

```powershell
python td_loader.py              # fetch all three timeframes -> data/cache_{1d,1h,5m}/*.parquet
python td_loader.py 5m           # just one timeframe
python sweep.py                  # backtest all timeframes -> results/*.csv
python sweep.py 1d 1h            # a subset (unrecognised args are silently ignored)
python build_report.py           # results/*.csv -> report.html
python etf_wf.py --tf 1d 4h      # SOXL/TQQQ walk-forward -> results/etf_wf_*.csv
```

## The SOXL / TQQQ sheet (`etf_wf.py`, added 2026-08-05)

A separate universe (`config.ETF_UNIVERSE`) and a separate runner, because two 3x
leveraged ETFs must not share a leaderboard with twenty unlevered mega-caps. It is
**walk-forward** (3y in-sample / 12m out-of-sample, rolling) and ranks on information
ratio against buy-and-hold, matching `../backtest master/` rather than this project's
older single-split excess-Sharpe design — so read `etf_wf_summary_*.csv` against that
project's sheets, not against `summary_1d.csv`.

**Result: null on both timeframes.** Best rule at 5bps is `HT_TRENDMODE` at IR **-0.072**
(1d, 13.5 OOS years) and `MININDEX` at **-0.181** (4h, 3.4 years); both sit far under
their noise ceilings (+0.72 and +1.42). The honest `IS#1` number — the rule re-selected
on each in-sample window — is **-0.662** on 1d, materially worse than any fixed rule,
which is the usual sign that the leaderboard is noise.

Two things worth carrying forward:

- **The decay trap did not fire, but check it every time.** The worry with a 3x
  daily-reset ETF is that volatility decay makes buy-and-hold a weak benchmark, letting a
  rule post a positive excess just by sitting out. Measured here it did not: SOXL and TQQQ
  buy-and-hold both score Sharpe **0.89** against SPY's 0.86 over the same window — the
  leverage scaled return and risk together. What it did do is take the drawdown to
  **-90.5%** (SOXL) and -81.6% (TQQQ), so equal Sharpe hides a completely different
  tradeability. `etf_wf.py` prints the buy-and-hold reference block for exactly this
  reason; do not read an IR on these names without it.
- **`corr(IR, time-long)` is +0.65 on 1d and +0.50 on 4h**, the same artifact found in
  `../backtest master/combo_wf.py`: the leaderboard is largely ranking how much of the
  time a rule is invested, and IR approaches 0 from below as a rule approaches
  always-long. 0 is the ceiling, not a win.

`td_loader.py` is expensive and cached; `sweep.py` reads only the parquet cache and never hits the
network. A full `sweep.py` run is minutes, dominated by `5m` (~70k bars × 20 tickers × 231 rules ×
4 cost levels).

## Architecture

```
config.py      universe, timeframe specs, cost grid, paths — and sys.path injection
   |
td_loader.py   Twelve Data -> data/cache_<tf>/<TICKER>.parquet   (network, cached)
   |
sweep.py       talib_signals rules x tickers x cost grid -> results/{per_ticker,summary}_<tf>.csv
   |
build_report.py  results CSVs -> report.html (template.html with JSON substituted in)
```

**`config.py` is load-bearing beyond configuration.** It prepends `../test research/src` to
`sys.path`, which is the only reason `from talib_signals import ...` resolves in `sweep.py`. The
signal layer is *shared, not copied*: editing `../test research/src/talib_signals.py` changes the
rules in both studies, and moving or renaming that sibling directory breaks this one.

**`td_loader.py`** paginates because Twelve Data caps a `time_series` response at 5000 bars per
symbol — hence `window_days` per timeframe in `config.TIMEFRAMES`, sized so one request stays under
the cap. Three API behaviours are already handled and should not be "simplified" away: batches are
capped at 20 symbols and report overflow as `414 URI Too Long` (handled by recursive bisection);
share classes are dot-separated (`BRK.B`, not `BRK-B`); and `adjust=all` is mandatory — the API
default is split-adjusted but *not* dividend-adjusted. Windows are half-open but the API is inclusive
at both ends, so boundary bars are deduplicated on concat.

**`sweep.py`** is the whole backtest. Per timeframe it runs every indicator against every cached
ticker at every cost level, writes per-ticker rows, then `summarise()` merges each row against the
`BUYHOLD` row for the same (timeframe, ticker, cost) and derives the `excess_*` columns the
leaderboard actually ranks on.

**`build_report.py` / `template.html` / `report.html`.** `report.html` is generated — it is
`template.html` with the `__REPORT_DATA__` placeholder replaced by a JSON blob. Edit `template.html`
(styles, JS, layout) or `build_payload()` (data), never `report.html`; it is overwritten on every
build. The page is fully self-contained: no backend, no build step, no external requests.

## Conventions that carry real meaning

- **Rank on excess over buy-and-hold, never raw Sharpe.** In a rising market raw Sharpe mostly
  measures how long-biased a rule is; that is how the earlier sweep manufactured "winners" that were
  just beta. `avg_excess_sharpe` at `HEADLINE_COST_BPS` (5bps) is the headline metric.
- **A gross-only result is not evidence.** Costs are charged on `|position.diff()|`, so flat→long
  costs one side and long→short costs two. Any new metric must be reported across the whole
  `COST_BPS_GRID`, and the headline must use a non-zero cost — a 5-minute rule can turn over hundreds
  of times a year.
- **`flatten_eod` applies at 5m only, and never to `BUYHOLD`.** Flattening the benchmark would turn
  it into a different strategy; not flattening the rules would let "day trading" quietly collect the
  overnight drift buy-and-hold already earns.
- Positions are 1/0/-1 and `shift(1)` before multiplying by returns; net returns are clipped at
  `-0.999` before compounding. Both mirror `../test research/` — departing from either makes results
  incomparable with everything already learned.
- **Annualisation is measured, not assumed.** `bars_per_year()` derives the factor from the actual
  index span rather than 252/1638/19656, because the vendor's intraday grid is not uniform (holidays,
  half days, two different hourly grids in this data).
- `MIN_SHARPE_COVERAGE` (0.8) gates the `rankable` flag: a rule must produce a valid Sharpe on 80% of
  its tickers. Rules that sit flat otherwise win on a couple of tickers' worth of noise. Always filter
  on `rankable & ~is_baseline` before ranking, as `sweep.main()` and `build_payload()` both do.
- SPY is fetched and cached alongside the 20 names but is **not** part of the universe — it exists as
  the benchmark input for the `BETA` and `CORREL` rules.

## Gotchas

- `results/summary_all.csv` and `per_ticker_all.csv` only contain the timeframes of the *last*
  `sweep.py` invocation, so `python sweep.py 5m` leaves them holding 5m alone while the per-timeframe
  CSVs stay complete. `build_report.py` reads the per-timeframe files; ad-hoc analysis that reaches
  for `*_all.csv` will silently see a subset.
- `sweep.py` swallows per-rule exceptions with a bare `except: continue`, so a broken indicator
  disappears from the leaderboard instead of failing the run. If a rule count drops below ~215, that
  is the reason.
- Nothing under this directory is gitignored — `data/` is ~71MB of regenerable parquet and would be
  committed as-is. Add a local `.gitignore` (as `../test research/` has) before the first commit here.
