# CLAUDE.md

Guidance for Claude Code working in this directory. Read `../CLAUDE.md` first, and
`../backtest engine/CLAUDE.md` too — the conventions come from there and apply unchanged.

## What this is

Everything that prices **selection**. The engine next door scores a *rule*; this folder
scores *choosing* a rule, which is a different and much harder question.

A single split has the whole test column visible when it picks its winner. Nobody trading
in 2015 had that table. Here, parameters and rule identity are re-selected on each 3-year
in-sample window and applied to the next 12 months — so the leaderboard pays for its own
hindsight.

The result of doing that honestly: on 1d equities the gap between the two is **-0.21 IR**
(-0.447 walk-forward vs -0.237 single-split), and per-fold parameter tuning lost on **all
14** re-optimisable families, mean -0.083. **Quote IS#1 as the headline, not the best fixed
rule.**

## Files

```
wfo_paths.py     sys.path bootstrap + RESULTS_DIR. import it FIRST, before config
walkforward.py   stage 1b: fold generation, champion picking, stitching, IS#1
                 also a library - variants/prereg/combo_wf/strat_wf all import it
variants.py      stage 1c: 8 position transforms over the IS-shortlisted leaders
prereg.py        stage 1d: 5 published rules, no free parameters, no selection at all
strat_wf.py      stage 1e: the ../strategies/ catalog, 117 cells, + exposure controls
combo_wf.py      stage 2b: walk-forward pairs + leg-correlation diagnosis
gate_calibration.py  power check: is a gate below its own noise ceiling?
curves.py        equity curves for the top rules -> results/curves_*.json (dashboard)
wf_vs_split.py   does the ranking actually move once it is walk-forward? (it does)
```

## Commands

```powershell
..\.venv\Scripts\Activate.ps1        # or call ..\.venv\Scripts\python.exe directly
python walkforward.py --class us_stocks --tf 1d
python variants.py --tf 1d 4h --top-k 12
python prereg.py --tf 1d 4h --freeze
python strat_wf.py --tf 1d 4h        # --list prints the catalog and exits
python combo_wf.py --tf 1d 4h --top-k 8
python gate_calibration.py           # no args
python wf_vs_split.py                # no args
python curves.py --tf 1d 4h --top 15
```

Run them **from this directory** — bare-name imports. Long sweeps exceed the 10-minute
harness timeout; launch detached with output to `logs/`.

## `wfo_paths.py` is load-bearing

It is named that and not `config.py` because `../backtest engine/` goes on `sys.path` and
its modules import each other by bare name — a `config.py` here would shadow the engine's
and `signals.py` would silently get the wrong file.

**Import it before `config`.** It puts the engine on the path; the engine's `config` then
puts the repo root on the path, which is the only reason `strategies` resolves.

It also owns `RESULTS_DIR`. Every writer here imports it from `wfo_paths`, not from
`config`, or output lands in the engine's results directory instead.

## Two results directories

The engine's holds what a single split produces: `summary_*`, `per_asset_*`, `combo_*`,
`parity`, `validation`. This folder's holds everything that prices selection: `wf_*`,
`cwf_*`, `var_*`, `prereg_*`, `strat_*`, `curves_*`.

The split is clean in one direction — **every read inside this folder is of a file this
folder wrote**. `wf_vs_split.py` is the single exception, and comparing walk-forward
against the single split is the entire point of it; it reaches across through
`wfo_paths.ENGINE_RESULTS` and nowhere else. Keep it that way.

## Known problems in this folder

Both were flagged during the refactor and deliberately **not** fixed, because fixing them
changes published numbers and that deserves its own task with the diff as the deliverable:

- **`prereg.volmanaged` (`prereg.py:103`) carries a look-ahead leak.** `np.nanmedian` over
  the whole series is future data — one scalar, but still future. Truncating the series
  changed 11 of 93 cells' *past* positions. It is worth 0.084 IR at us_stocks 1d (-0.368
  contaminated vs -0.284 clean). `variants._vol_scale` has the same bug.
  **Stage 1c and 1d were not re-run.** Do not quote their vol-scaled rows.
  `strategies.catalog._causal_median` is the fixed form, using an expanding median.
- **Test causality by truncation, not by reading the code.** Build positions on the full
  series and on the series minus the last N bars, then assert the overlap is identical.
  That is what caught the above; staring at `rolling()` calls did not.

## Reading the output

- **IS#1 is the number.** The `fixed` rows are what the best rule scored; the
  `is1_selection` row is what *following the selection rule* scored, which is the only one
  a person could have traded.
- **Check `gate_calibration.py` before believing a null.** It reports the effective bar,
  `max(IR gate, 2/sqrt(years), noise_ceiling(N, years))`. Only `us_stocks 1d` is coherent —
  41 OOS years, ceiling +0.43 at 327 trials, under the 0.50 gate. Every other sheet carries
  3.4–5.9 years where the ceiling is +0.95 to +1.26, well above the gate.
- **`corr(IR, long_frac) = 0.881` on 1d equities.** The leaderboard is substantially a
  ranking of time-in-market and `or` wins because it is the operator that spends the most.
  `MININDEX~MAXINDEX|or` is long 100% — it is buy-and-hold wearing a rule's name. Crypto
  does not behave this way (-0.12), so it is an equity-uptrend artifact, not a property of
  the metric.
