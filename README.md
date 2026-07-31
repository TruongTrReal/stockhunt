# stockhunt

Offline quant research pipeline: turns every TA-Lib indicator into a trading rule, backtests
it across the S&P 500, searches combinations of them, and renders the results as a static
self-contained HTML report.

There is no live trading, no broker integration, and no web server — this is batch research
plus a static artifact.

---

## Setup

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

TA-Lib's Python wrapper needs the compiled **TA-Lib C library already installed on the
system** — `pip install TA-Lib` alone fails without it.

There is no test suite, linter, or CI in this repo.

All scripts run **from `src/`** and import each other by bare name (`from data_loader import …`).
It is deliberately not a package, so `cd src` first or set `PYTHONPATH=src`.

---

## Directory layout

```
src/            47 scripts, ~7.8k lines. Everything below.
data/           gitignored. Price caches (parquet), signal tensors (npz), result CSVs/JSON.
report/         template.html + report.js -> index.html (the deliverable)
CLAUDE.md       working notes and conventions for AI-assisted edits
```

`data/` is ~420MB and fully regenerable from `src/`. Nothing in it is version-controlled.

---

## Architecture

Five layers. Each consumes the one above and writes to `data/`.

```
  sp500_tickers ──► data_loader ──► talib_signals ──► signal_tensor ──► searches
   (universe)         (OHLCV)        (rules)          (cached positions)
                                          │
                                          └──► indicator_backtest ──► build_report_data ──► report/
```

### 1. Data layer

| module | role |
|---|---|
| `sp500_tickers.py` | Fetch/cache the S&P 500 constituent list from Wikipedia |
| `data_loader.py` | Download daily OHLCV via `yfinance` in batches with retry; one parquet per ticker in `data/cache/` |
| `intraday_loader.py` | Same for 5m/1h bars into `data/cache_intraday/` (Yahoo caps history: ~60d for 5m, ~730d for 1h) |
| `pit_universe.py` | Reconstruct *point-in-time* index membership by replaying Wikipedia's index-change table backwards |

> **Gotcha:** `load_universe()` is **cache-only and never downloads**. Call
> `update_universe(tickers)` first, or it silently returns nothing for uncached names.

### 2. Signal layer

`talib_signals.py` is the core. It maps a TA-Lib function name to a position series
(`1` long / `-1` short / `0` flat). Each of TA-Lib's ~161 functions is bucketed into a rule
family — moving-average crossover, zero-line oscillator, two-line crossover, mean-reversion
bands, candlestick pattern with fixed hold, etc. Functions with no natural direction (ATR,
ADX, pure math ops) fall back to a generic *"value vs. its own trailing SMA"* rule and are
listed in `GENERIC_FALLBACK_FUNCTIONS` so consumers can flag them.

Two functions are the whole public surface: `get_all_indicator_names()` and
`generate_position()`. **Adding an indicator means adding it to the right rule table here**,
not touching any backtest script. `describe_signal()` generates the human-readable rule text
from the same tables, so the report and the code cannot drift apart.

Variant builders extend that set by pre-computing position tensors:

| module | output | what it varies |
|---|---|---|
| `signal_tensor.py` | `signal_tensor.npz` | the 231 baseline rules — 231 × 2908 × 501 `int8` |
| `param_variants.py` | `param_variant_tensor.npz` | ADOSC/AD/VAR/STDDEV periods and self-trend windows |
| `broad_variants.py` | `broad_variant_tensor.npz` | 38 functions × 8 periods (3–200) |
| `weekly_variants.py` | `weekly_variant_tensor.npz` | indicators on weekly/monthly resampled bars, held across the period |

The tensor is the key performance trick: generating positions is the expensive step
(~231 × 501 TA-Lib calls), so it is done **once** and every search afterwards is array math.

### 3. Backtest layer

| module | role |
|---|---|
| `indicator_backtest.py` | Canonical single-indicator backtest → ranked leaderboard CSV. Owns `_ticker_stats()`, the reference metric implementation |
| `cross_sectional_backtest.py` | Market-neutral variant: daily cross-sectional demeaning, dollar-neutral, unit gross |
| `pit_backtest.py` | Re-runs strategies against point-in-time index membership |
| `backtest.py`, `indicators.py` | Earlier standalone prototypes, superseded by the above |

### 4. Search layer

All share the same shape: build candidates → score on **train (2015–2021)** → validate on
**test (2022–2026)**. They differ in the space searched and the objective.

| module | space / objective |
|---|---|
| `combo_backtest.py` | First pass: 2–3 indicator combos by sum-then-sign |
| `beat_buyhold_search.py` | Transforms × gates × 1–4 combos, objective = CAGR |
| `beat_buyhold_search2.py` | Adds vote thresholds and whipsaw confirmation |
| `ensemble_search.py` | Consensus across many indicators at once |
| `sharpe_search.py` | Objective = average per-ticker Sharpe |
| `portfolio_search.py` | Objective = **net-of-cost portfolio Sharpe** (the one that matters) |
| `param_search.py` | Searches parameter variants and hedge weight jointly |
| `alien_search.py` | Bans the winning family; one beam per TA-Lib group |
| `operator_search.py` | New combination *operators*: strict-AND, asymmetric entry/exit, regime switching, persistence ramping |
| `blend_search.py` | Continuous graded blends and mean–variance (tangency) weights |

### 5. Validation and analysis

Search finds candidates; these decide whether to believe them.

| module | role |
|---|---|
| `sharpe_validate.py` | Adversarial battery: extra-bar-of-lag, breakeven cost, robustness, multiplicity-corrected z |
| `sharpe_finalists.py` | Final report on survivors, with cost curves |
| `combo_diagnostics.py` | What a strategy actually does — drawdown episodes, exposure vs regime, trade behaviour |
| `combo_layers.py` | Risk overlays: volatility targeting, regime gates, drawdown control |
| `combo_blend.py`, `combo_hedge.py` | Diversifier blending and trend-hedge validation |
| `hedge_attribution.py` | Decomposes a hedge into beta-stripping vs timing; leave-one-year-out jackknife |
| `tilt_search.py`, `tilt_smoothed.py`, `tilt_validate.py` | Exposure-neutral tilt experiments |
| `leverage_test.py`, `leverage_portfolio.py` | Per-ticker vs portfolio-level levering, with financing and cash interest |
| `rank_combos_full_period.py` | Scores combos on the full period and merges them into the leaderboard |

### 6. Report layer

`report/index.html` is the deliverable: `template.html` with `report.js` inlined and the
`__REPORT_DATA_JSON__` placeholder replaced by the built JSON. It is a single static page —
no backend, no build tooling — that renders the leaderboard, a pooled PnL overlay chart, and a
per-ticker trade drill-down entirely client-side.

| module | role |
|---|---|
| `build_report_data.py` | **Canonical builder** → `report_data.json`. Other builders import its helpers rather than duplicating them |
| `build_hv_report_data.py` | Same, restricted to the 100 highest-volatility tickers since 2020 |
| `build_intraday_report_data.py` | Same maths on 5m/1h bars (arg: `5m` or `1h`) |
| `patch_*.py`, `fix_combo_tldr.py` | Cheap partial regeneration — add rows or fields without a full rebuild |
| `build_index_html.py` | Assembles `index.html` and compacts the payload |

A full pass is ~500 tickers × ~231 indicators and takes minutes, so **prefer extending a patch
script over a full rebuild** when only part of the dataset changes.

---

## Running the pipeline

```powershell
cd src

python sp500_tickers.py                    # refresh data/sp500_constituents.csv
python data_loader.py                      # download/cache daily OHLCV
python signal_tensor.py                    # build the position tensor (~90s)

python indicator_backtest.py               # single-indicator leaderboard
python build_report_data.py                # -> data/report_data.json
python build_intraday_report_data.py 1h    # optional intraday tabs
python build_index_html.py                 # assemble report/index.html
```

Searches are independent and can be run in any order once the tensor exists.

---

## Conventions

These hold everywhere; breaking one silently corrupts results.

- **No look-ahead.** Position series are `1`/`-1`/`0` and always shifted one bar
  (`position.shift(1)`) before multiplying by returns, so a signal computed on bar *t*'s close
  trades bar *t+1*.
- **Returns floored at `-0.999`** (`clip(lower=-0.999)`) so a short losing more than 100% in a
  bar cannot drive equity negative and spuriously flip positive on the next multiply.
- **`holding_days` is elapsed wall-clock time**, not a bar count, so it is comparable across
  daily / hourly / 5-minute datasets.
- **JSON goes through `_sanitize_nans()`** before `json.dump(..., allow_nan=False)`; Python's
  bare `NaN`/`Infinity` tokens are invalid JSON and break the browser's `JSON.parse`.
- **`CAPITAL_PER_TICKER` = $10,000** per ticker per indicator is the basis for every dollar
  figure. Indicators are compared on pooled dollar PnL, so ranking is influenced by how many
  tickers produced a valid signal.
- **`ddof=1` everywhere** for standard deviations. pandas defaults to 1 and numpy to 0; mixing
  them makes two backtests silently incomparable.
- **Selection on train only.** Sorting a results table by a test column and reading the top
  rows is selection on test, and it will manufacture winners.
- **Materiality floors on both sides of exposure.** A ratio objective like Sharpe rewards
  doing nothing — a rule in the market 0.4% of the time can score 1.96 — so any search needs a
  minimum exposure constraint *inside* the objective, not as a post-filter.
