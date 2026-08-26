# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Guidance for Claude Code working in this directory. Read `../CLAUDE.md` first — the
universe, the vendor traps, the benchmark contract and the fill rules come from there and
apply here unchanged. `../backtest engine/CLAUDE.md` and
`../walk-forward optimization/CLAUDE.md` are the two neighbours this folder leans on.
**No results here** — the `results/` sheets and the dashboard own those.

## What this is

Where every machine-learning study in this project lives. New as of 2026-08-27, and empty
of studies by design: the folder exists so that a fitted model is held to the same
standard as a hand-written rule, rather than getting its own private scorer.

The one sentence that shapes everything below: **this repo has never been short of
candidates, only of evidence.** Every stage upstream — 231 indicators, 175 published
strategies, 101 formulaic alphas, 8 variants, walk-forward pairs — exists to price
*selection*, because selection is what a search spends and a leaderboard hides. A learner
is a search with a much larger budget, so it does not get a lighter burden of proof; it
gets the same one, applied to a bigger N.

## The seam: how anything built here re-enters the pipeline

There is no ML scorer, and there must never be one. A model's output becomes a **position
series per symbol per bar** and is then scored by the stages that already exist:

```
data/*.parquet --td_loader.load--> features (panel, cached)
                                       |
                                  fit per FOLD  (walkforward.generate_folds)
                                       |
                              predictions --> a POSITION panel in .cache/ml/books/
                                       |
              riskmatch_wf.resolve_position reads one symbol's column back
                                       |
        riskmatch_wf.py --> edge_standard row | portfolio_wf.py --> the BOOK
```

`alpha101.py` in `../walk-forward optimization/` is the worked example and the template to
copy, not to reinvent. It is the fourth population on that leaderboard and the only prior
one whose position cannot be computed from a single symbol's own frame — exactly the shape
an ML model has. Four things it does that any model here must also do:

* **Build the panel once, on disk, keyed by a fingerprint** over the bars *and* over the
  source that produced it (`alpha101.fingerprint`). `riskmatch_wf` fans rules across
  processes and hands a worker only a *name*; the panel cannot cross that boundary, and
  recomputing it per worker would dominate the run.
* **Return `None` when the cache is cold**, so the rule is dropped from the sheet rather
  than scored as a rule that never trades. A silent zero column is a fabricated result.
* **Reindex onto the caller's bar index and fill flat, never forward-fill.** Carrying a
  holding across a gap invents a trade nobody made.
* **Give the model a label that survives in a CSV**, and never rename it. Every result
  sheet in this repo is keyed on the label, and renaming orphans the history.

`stockhunt/poscache.py` does **not** cover this folder: its code fingerprint is
`strategies/`, `signals.py` and `engines/vector.py` only. An ML book therefore carries its
own fingerprint or it is stale the first time a feature changes.

## Setup and commands

**`../.venv-ml` is this folder's venv, and it already exists** (built 2026-08-27, Python
3.13). The pipeline venv is deliberately not used: `../.venv` runs the live paper desk,
which also runs on the VPS, and a solver stack in it means a numpy or pandas resolution
can move under a process that is trading. Rebuild it from `requirements.txt` if it is ever
lost:

```powershell
py -3.13 -m venv ..\.venv-ml
..\.venv-ml\Scripts\python -m pip install -e .. -r requirements.txt
```

It carries scipy, scikit-learn and lightgbm **plus** what the repo's own modules need to
import at all — TA-Lib, requests, tqdm, pyarrow — so every research stage imports and runs
here. `nautilus_trader` is the one real absence, and no ML study needs it. What it can do,
verified:

```powershell
..\.venv-ml\Scripts\python -c "import ml_paths, config; print(config.US_STOCKS[:3])"
..\.venv-ml\Scripts\python -c "import ml_paths, td_loader; print(len(td_loader.load('us_stocks','1d',symbols=['AAPL'])['AAPL']))"
..\.venv-ml\Scripts\python -m pytest tests -q          # this folder's unit suite
..\.venv-ml\Scripts\python -m pytest tests\test_x.py -k leak -q
```

Run everything **from this directory** — `ml_paths` is the bootstrap and must be imported
before `config`, same three-hop rule as every other folder. `ml_paths` also appends
`../walk-forward optimization/` to the path, so `walkforward`, `riskmatch_wf`,
`portfolio_wf` and `alpha101` import by bare name from here.

Scoring can therefore run on either venv, and the choice is not arbitrary. **A sheet of
record is produced by the pipeline venv**, because that is the environment every other
published sheet came out of; `.venv-ml` is for the iteration loop:

```powershell
cd "..\walk-forward optimization"
..\.venv\Scripts\python riskmatch_wf.py --class us_stocks --tf 1d --rules <label>
..\.venv\Scripts\python portfolio_wf.py --class us_stocks --tf 1d --pit --rules <label>
```

The handoff between the two is a **file** — the position panel in `.cache/ml/books/` —
never a function call, which is what lets the two environments drift on a patch version
without either one noticing.

**This folder is not in the root `testpaths`, and must not be added.** The root unit suite
depends on numpy and pandas only; collecting `ML/tests` from the root would make
scikit-learn a prerequisite for running `pytest -q`. `paper api/` and
`paper trading engine/` keep their suites out for the same reason.

**Long jobs get killed at 10 minutes** by the harness timeout, and a fit is a long job.
Launch detached from **bash**, never PowerShell — `nohup ./run_x.sh > logs/x.log 2>&1 &`
with `python -u`. The reason is in `../CLAUDE.md`: PowerShell 5.1 turns a numpy
`RuntimeWarning` into a terminating error and orphans the workers, so the job is dead,
silent, and indistinguishable from a slow one.

## What binds harder here than anywhere else in the repo

### 1. The fit is inside the fold, or the study is worthless

`walkforward.generate_folds` is the one definition: 3-year in-sample, 1-year out-of-sample,
stepped a year at a time, on a calendar shared by the whole universe so that "fold 7" names
the same dates for every symbol. A model **retrains on each fold's in-sample window and
predicts only its out-of-sample window.** One fit over the whole series and a random
train/test split are both look-ahead, and neither is detectable in the output.

This is not advice; `../CLAUDE.md` records it as a standing rule for every test in the repo.

### 2. Causality is proved by truncation, and ML has its own leaks

The gate style is `strategies/tests/test_causality.py`: build on the full series, build on
the series minus N bars, require the overlap to be **identical**. Not code review —
truncation. Any feature builder here needs an equivalent, and it will catch the leaks that
reading the code does not:

| leak | why it passes review |
|---|---|
| a scaler fit on the whole series | `StandardScaler().fit(X)` before splitting looks like preprocessing, not like a peek at the future's mean and variance. The repo has already been bitten by the non-ML version of this — see the `nanmedian` whole-series vol target |
| cross-sectional normalisation over the wrong axis | rank-within-date is causal; rank-within-symbol-over-all-time is not. `test_alpha101.py` gates exactly this confusion, because the two axes are one transpose apart |
| the label shifted the wrong way | a target built with `shift(-1)` on a frame that was already lagged is a return you knew before you traded it |
| group leakage across symbols | 100 co-moving mega-caps in one month are close to one observation. A random-row split puts the same day's AAPL in train and MSFT in test |
| imputation, resampling or target encoding done before the split | all three read the test rows |

### 3. Every fit is a trial, and the ledger is what makes deflation honest

`strategies/trials.py` appends to `data/reference/trials.csv` **before** any result exists.
Register hyperparameter grids, feature sets and architectures the same way — each
configuration evaluated is a trial, whether or not it produced a file.
`metrics.deflated_sharpe` and the noise ceiling both need an honest N, and counting from
surviving CSVs gets it wrong in the flattering direction every time: abandoned runs leave
no file and still consumed a look at the data.

The arithmetic is the reason ML is hard here and is worth reading before designing a
search: `metrics.noise_ceiling` is `se(per-fold delta-Sharpe) * Phi^-1(1 - 1/(N+1))`, so
**the bar rises with the size of the search**. The table for concrete N is in
`alpha101.py`'s docstring. A search that evaluates candidates by the thousand raises its
own hurdle faster than it raises its best draw, which is why the alpha101 stage is a fixed
pre-registered set rather than a generator. A learner is subject to the same arithmetic,
and the two ways out are the only two there have ever been: **fix the hypothesis in
advance, or shrink the search.** Neither is "try more models".

Deflation needs two facts and a shortlist can supply neither — the count and the
dispersion of the candidates' Sharpes. `--n-trials` and `--trial-dispersion` are the
overrides on `portfolio_wf`; pass both or neither.

### 4. Rank-IC at a multi-bar horizon is the powered test; Sharpe is not

`metrics.se_ir` falls as `1/sqrt(years)`, not with breadth — so on this history the
per-fold t on delta-Sharpe can only resolve large effects, and `EDGE_MIN_FOLDS = 20` exists
because a sheet with fewer folds returns `underpowered` rather than a verdict. Report
predictive accuracy where the statistics are strongest — rank-IC across the cross-section,
per date, with a Newey-West t on the IC series — and treat it as a **search signal**, not
as a result. A model that ranks well and then fails the gauntlet has been measured
correctly, twice.

The six criteria a result must clear are `config.EDGE_STANDARD` (S, T, R, C, W, H) and they
are not negotiable per stage. Two of them exist specifically to kill things a learner
produces easily: **R** (beat an exposure-matched random control) kills "it is just less
beta", and **C** (beat a constant weight at the same average exposure) kills "drawdown
reduction anyone can buy".

### 5. Everything the root file says about comparison still applies

A benchmark is valid only if it differs from the strategy in exactly one thing: the signal.
Read the `../CLAUDE.md` section on it before quoting anything — universe and membership
dates, weighting and rebalance schedule, fee schedule, fill timing and cash treatment all
have to match, and survivorship cannot be matched away (`--stress-delisted`). Report three
numbers, never one.

Fill timing deserves a second mention because ML makes it worse: a model whose features
include the current bar's close and whose position fills at that same close carries a real
look-ahead, and a learner will find and exploit it far more thoroughly than an indicator
does. `--fill open` is the pessimistic bound; **a result is only safe if it survives
there.**

### 6. The data traps are upstream and they are not hypothetical

Point-in-time membership (`top100_membership`, `td_loader.membership_span`), foreign
namesakes resolving on a bare ticker, a series that is the right instrument with a wrong
first year, futures back-adjustment, intraday timestamps that are exchange-local for
equities and UTC for everything else. All are in `../CLAUDE.md`. A feature panel is built
by joining across symbols and dates, which is precisely where a wrong-instrument or
wrong-clock series stops being one bad column and becomes a bad panel.

## Before proposing a study, read what has already been asked

`data/reference/trials.csv` is append-only and has thousands of rows. It is the record of
what this project has already spent, and the first thing to grep. In particular a
plan-space alpha miner (`schema_*` in that ledger) was **built and removed** in August 2026
after two complete studies; its design notes and both verdicts are recorded outside the
repo, so re-proposing a search over the same space without reading them spends the trial
budget twice for one answer.

The frozen studies `../test research/` and `../top 20 stocks/` are denied to writes by
`.claude/settings.json`; read `../LOCKED.md` before touching either.

## Conventions in this folder

```
ml_paths.py      the sys.path bootstrap + RESULTS_DIR / CACHE. Import it FIRST
requirements.txt what ../.venv-ml gets, and what it deliberately does not
results/         SHEETS ONLY: small, tracked, one row per (model, cell), same shape as
                 every other stage's results/. A scoped run writes *.partial.csv and
                 leaves the sheet of record alone -- panel columns computed over a
                 subset are different quantities wearing the same column names
logs/            gitignored by `**/logs/`. Detached job output lands here
tests/           this folder's pytest suite. Synthetic panels only -- no data/, no
                 vendor, no result sheet. Plus __main__ gates that exit nonzero, run
                 directly, for anything proved by truncation
```

**Large or regenerable artifacts go to `../.cache/ml/`, never into this folder.** Feature
panels, fitted estimators and position books all live there — `ml_paths.FEATURES`,
`.MODELS`, `.BOOKS` — under a fingerprint directory, and `.cache/` is gitignored whole. The
split is enforced by where you write, not by a `.gitignore` line somebody has to remember.

**Module basenames must be globally unique** across every folder that lands on `sys.path`
together. `features.py` is fine; `config.py`, `metrics.py`, `signals.py`, `catalog.py` and
`registry.py` are already taken and would shadow the real ones silently.

**The dependency runs one way.** Nothing in `../stockhunt/`, `../strategies/`,
`../backtest engine/` or `../walk-forward optimization/` may import from this folder — it
has a space-free name but it is not on the path for them, and making it so would put
scikit-learn behind a Sharpe ratio. This folder imports *them*.

For parallel sessions, `../PARALLEL.md` §7 applies: the partition is the file set, and two
runs writing the same sheet or the same fingerprint directory is last-writer-wins,
silently.
