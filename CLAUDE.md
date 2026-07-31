# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A quant research pipeline that backtests every TA-Lib indicator (161 functions, ~231 variants once
moving-average period sweeps are expanded) as a standalone long/flat/short trading rule across the
S&P 500 universe, then renders the results as a static, self-contained HTML report (`report/index.html`).
There is no live trading, no broker integration, and no web server — this is offline research plus a
static report artifact.

## Setup

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

TA-Lib's Python wrapper requires the compiled TA-Lib C library to already be installed on the system —
`pip install TA-Lib` alone will fail without it.

There is no test suite, linter, or CI config in this repo.

## Pipeline (run from `src/`, modules import each other by bare name — not a package)

```powershell
cd src
python sp500_tickers.py            # refresh data/sp500_constituents.csv from Wikipedia
python data_loader.py               # download/cache daily OHLCV for the whole S&P 500 -> data/cache/*.parquet
python indicator_backtest.py        # backtest all indicators, print/save ranked summary -> data/indicator_backtest_results.csv
python build_report_data.py         # build the full 1D report dataset -> data/report_data.json
python build_hv_report_data.py      # build the high-volatility-subset dataset -> data/report_data_hv.json
python build_intraday_report_data.py 5m   # -> data/report_data_5m.json (arg: "5m" or "1h")
python build_intraday_report_data.py 1h   # -> data/report_data_1h.json
```

Because a full pass over 501 tickers × ~231 indicators is expensive (~minutes), `patch_report_data.py`
and `patch_top_tickers.py` exist to cheaply regenerate only part of an existing `report_data*.json`
(e.g. add a new leaderboard field, or widen `trades` to more tickers per indicator) without rerunning
pass 1. When changing what per-ticker trade detail looks like, prefer extending one of these patch
scripts over a full rebuild, and keep them in sync with the corresponding fields in
`build_report_data.py`.

`report/index.html` is the final deliverable: `report/template.html` with `report/report.js` inlined
and the `__REPORT_DATA_JSON__` placeholder (in template.html) replaced by the built JSON. There is no
script in this repo that performs that assembly — treat it as a manual/external step, and don't assume
editing `template.html` or `report.js` alone updates `index.html`.

## Architecture

**Data layer** (`data_loader.py`, `intraday_loader.py`, `sp500_tickers.py`): downloads OHLCV from
Yahoo Finance via `yfinance` in batches with retry, caches one Parquet file per ticker under
`data/cache/` (daily, since 2015-01-01) or `data/cache_intraday/` (5m/1h, capped by Yahoo's free-tier
history limits — ~60 days for 5m, ~730 days for 1h). Every downstream script reads from this cache;
none re-hit the network except to fill cache misses.

**Signal layer** (`talib_signals.py`): the core mapping from a TA-Lib function name to a position
series (1 = long, -1 = short, 0 = flat). Each of TA-Lib's ~161 functions is bucketed into one of several
rule families (moving-average crossover, zero-line oscillator, two-line crossover, mean-reversion bands,
candlestick pattern with fixed hold, etc.) — see the module docstring and the category lists near the
top of the file. Functions with no natural directional meaning (ATR, ADX, pure math ops, ...) fall back
to a generic "value vs. its own trailing SMA" rule; these are enumerated in
`GENERIC_FALLBACK_FUNCTIONS` so downstream consumers can flag/weight them differently.
`get_all_indicator_names()` and `generate_position()` are the two functions everything else calls —
adding support for a new indicator means adding it to the right rule table here, not touching the
backtest scripts. `describe_signal()` generates the human-readable rule description shown in the report
UI directly from the same rule tables `generate_position()` uses, so the two can't drift out of sync.

**Backtest/report-builder layer** (`indicator_backtest.py`, `build_report_data.py`,
`build_hv_report_data.py`, `build_intraday_report_data.py`): apply every indicator's position rule to
every ticker with a fixed $10,000-per-ticker notional, compute trade-level and equity-curve stats
(CAGR, Sharpe, max drawdown, profit factor, win rate...), and aggregate into a ranked leaderboard.
`build_report_data.py` is canonical — the other `build_*` scripts import its helpers
(`compute_trades`, `ticker_equity_stats`, `_resample_curve`, `_sanitize_nans`, etc.) rather than
duplicating them, and layer on their own variation:
- `build_hv_report_data.py`: same daily bars, restricted to the 100 highest-volatility tickers since 2020.
- `build_intraday_report_data.py`: same trade/equity math (bar-size-agnostic), but on 5m/1h bars, with
  positions force-flattened at end-of-day for 5m (genuine day-trading, no overnight exposure) and left
  open across sessions for 1h. Annualization uses an empirically measured bars-per-year factor, not a
  theoretical constant, since Yahoo's bar spacing isn't perfectly uniform.

Every `build_*` script does two passes: pass 1 computes scalar stats + a pooled per-indicator
cumulative-$PnL curve for *every* ticker (cheap — no trade-level detail retained); pass 2 recomputes
full trade records only for the top-N ranked indicators' top-K tickers by PnL, for the report's
drill-down view. `BASELINE_NAME = "BUYHOLD"` is a synthetic, always-included, non-ranked row (buy day 1,
hold forever) that every real indicator is meant to beat.

**Report** (`report/`): a single static HTML page with embedded JSON (no backend, no build tooling).
`report.js` reads timeframe-keyed data (`1D`/`1H`/`5m`, plus the HV variant) out of the embedded
`<script id="report-data">` blob and renders the leaderboard, PnL curve overlay, and per-ticker
trade drill-down entirely client-side.

## Conventions worth knowing before editing

- Position series are always `1`/`-1`/`0` (long/short/flat), shifted by one bar before multiplying by
  returns (`position.shift(1)`) so a signal computed on bar *t*'s close only trades bar *t+1* — never
  look-ahead.
- Returns are floored at `-0.999` before compounding (`clip(lower=-0.999)`) so a short position that
  loses more than 100% in one bar can't drive equity negative and spuriously flip positive on the next
  multiply.
- `holding_days` in trade records is actual elapsed wall-clock time between entry/exit timestamps, not a
  bar count — comparable across daily/hourly/5-minute datasets, unlike a raw bar count would be.
- JSON output always goes through `_sanitize_nans()` before `json.dump(..., allow_nan=False)`, because
  Python's NaN/Infinity tokens aren't valid JSON and would break the browser's `JSON.parse`.
- `CAPITAL_PER_TICKER` (a fixed $10,000 notional per ticker per indicator) is the basis for all
  dollar-PnL figures — indicators are compared on pooled dollar PnL across tickers, not normalized
  returns, so an indicator's ranking is influenced by how many tickers it produced a valid signal on.
