# engine-bakeoff

A head-to-head of **NautilusTrader** and **manifoldbt** as backtest engines for the TA-Lib
indicator research in `../test research/`, scored on accuracy and speed against an analytic
reference, on **Twelve Data** prices with the existing yfinance cache as a control.

**Read [ANALYSIS.md](ANALYSIS.md) for the results and the recommendation.**

## Data source

Daily bars come from Twelve Data's `time_series` endpoint with **`adjust=all`**. That flag
is not optional — the default response is split-adjusted but *not* dividend-adjusted, a
different price basis from the one this project assumes.

The API key is read from `TWELVEDATA_API_KEY`, falling back to `.env.local` in this
directory (gitignored, never committed):

```
TWELVEDATA_API_KEY=your_key_here
```

Fill or refresh the cache in `data_td/`:

```powershell
..\.venv-bakeoff\Scripts\python.exe twelvedata_loader.py
```

Every runner reads `BAKEOFF_DATA` to choose its source — `twelvedata` (default) or
`yfinance` (the `../test research/data/cache/` parquet). Results go to
`results/<source>/`, so the two never overwrite each other.

> **Status: concluded.** The go-forward stack chosen from these results — Twelve Data →
> the pandas/TA-Lib sweep → NautilusTrader for validating survivors — now lives in the
> main `../.venv` and `../test research/`. This directory is kept only so the findings
> stay reproducible; nothing in the live pipeline depends on it, and manifoldbt is not
> part of the stack.

## Why two virtualenvs

Each engine gets its own environment at the repo root, so neither can disturb the main
project env (`.venv`) or each other:

- `../.venv-bakeoff` — manifoldbt + TA-Lib + pandas + requests (also runs the reference and the comparisons)
- `../.venv-nautilus` — nautilus_trader + TA-Lib + pandas + requests

Both were created from the project's Python 3.13. Recreate with:

```powershell
..\.venv\Scripts\python.exe -m venv ..\.venv-bakeoff
..\.venv-bakeoff\Scripts\python.exe -m pip install manifoldbt TA-Lib pandas pyarrow requests

..\.venv\Scripts\python.exe -m venv ..\.venv-nautilus
..\.venv-nautilus\Scripts\python.exe -m pip install nautilus_trader TA-Lib pyarrow requests
```

## Running it

From this directory. `compare.py` needs the other runners to have written their CSVs first;
`compare_sources.py` needs `run_reference.py` to have run under *both* sources.

```powershell
$env:BAKEOFF_DATA = "twelvedata"
..\.venv-bakeoff\Scripts\python.exe  twelvedata_loader.py     # ~5s, network
..\.venv-bakeoff\Scripts\python.exe  validate_data.py         # source sanity check
..\.venv-bakeoff\Scripts\python.exe  run_reference.py         # ~0.2s
..\.venv-bakeoff\Scripts\python.exe  run_manifoldbt.py        # ~2s
..\.venv-bakeoff\Scripts\python.exe  run_manifoldbt_batch.py  # ~2s
..\.venv-nautilus\Scripts\python.exe run_nautilus.py          # ~100s
..\.venv-bakeoff\Scripts\python.exe  compare.py               # the verdict tables

$env:BAKEOFF_DATA = "yfinance"                                # optional control re-run
# ...same five runners...
..\.venv-bakeoff\Scripts\python.exe  compare_sources.py       # how much the source moved it
```

`results/<source>/comparison.csv` is the joined per-run scoring. `_mbt_store/` is
manifoldbt's regenerated Arrow store — disposable.

## Layout

| file | role |
|---|---|
| `common.py` | universe, source-aware bar loading, the five TA-Lib rules, the reference simulator |
| `twelvedata_loader.py` | Twelve Data fetch + Parquet cache in `data_td/` |
| `validate_data.py` | Twelve Data vs yfinance: calendar, prices, and whether signals actually differ |
| `run_reference.py` | analytic ground truth, both execution conventions |
| `run_manifoldbt.py` | manifoldbt via one `mbt.run()` per rule |
| `run_manifoldbt_batch.py` | manifoldbt via `run_batch_lite` (its intended sweep path) |
| `run_nautilus.py` | Nautilus, in both injected-signal and native-TA-Lib modes |
| `compare.py` | scores every engine against the reference; accuracy, speed, extrapolation |
| `compare_sources.py` | same rules, same engine, different price source — how far apart? |
| `lookahead_probe.py` | pins down manifoldbt's sizing basis on synthetic bars, and tests whether split-sample detection can see it |

## Design notes

The rules are **stateless** — each bar's target position is a pure function of that bar's
indicator value, never of position history. That is deliberate: with a path-dependent rule,
one differently-timed entry cascades through the whole run, and you end up measuring
divergence amplification instead of engine accounting.

Each engine is compared only against the reference run for **its own execution convention**
(`at_close` or `next_open`), never against the other engine, so no engine is charged for a
difference the arithmetic agrees with.

Two workarounds in `run_manifoldbt.py` are load-bearing and documented in that file's
docstring — daily bars must be imported under the `"1h"` label, and a true next-bar fill
needs `signal_delay=1` rather than the `NEXT_BAR_*` execution price. Both are explained in
ANALYSIS.md findings 1 and 2.
