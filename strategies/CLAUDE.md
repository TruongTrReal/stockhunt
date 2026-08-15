# CLAUDE.md

Guidance for Claude Code working in this directory. Read `../CLAUDE.md` first.
**No results here** — the `results/` CSVs and the dashboard own those.

## What this is

The strategy layer, shared by every stage of the pipeline. Deliberately free of any
dependency on the rest of the repo — numpy, pandas and talib only.

```
talib_signals.py     the 231-variant TA-Lib rule table. name -> position series
                     ONE dispatcher, not 231 files: the "rules" are TA-Lib's own
                     function list enumerated at runtime, so there is no per-rule code
published/           ONE FILE PER PUBLISHED STRATEGY (31). Each declares position(),
                     GRID, RULE, SOURCE, FAMILY, ANCHOR, CLASSES, NOTE
registry.py          discovers published/ by import; owns the label grammar
_indicators.py       shared primitives: _state_machine, _causal_median, _vol_scale, ...
controls.py          BUYHOLD / ALWAYS_* / RANDOM_* — yardsticks, not strategies
overlays/regime.py   trailing-vol conditioning that wraps any base label
trials.py            the append-only trial ledger -> ../data/reference/trials.csv
scaffold.py          creates a new published/ file and registers its trial
tests/               gates. `python strategies/tests/test_causality.py` exits nonzero
catalog.py           back-compat shim re-exporting the old names. New code should
                     import from `strategies.registry`
```

This is a **real package** — the folder name has no space in it, unlike the four pipeline
folders — so it is imported normally: `from strategies.registry import CATALOG`. That needs
the repo root on `sys.path`, which `backtest engine/config.py` arranges as a side effect of
being imported.

## Adding a strategy, or fifty

```powershell
# one
python strategies\scaffold.py new rsi_divergence --family reversion `
    --source "Author (2019), 'Title'" --rule "Long on a bullish RSI divergence." `
    --scope us_stocks/1d --why "never tested here" --hypothesis "weak; costs eat it"

# a batch — CSV with columns name,family,source,rule
python strategies\scaffold.py batch ideas.csv --scope us_stocks/1d --why "..."

# then, always, in this order
python strategies\tests\test_causality.py --rules rsi_divergence
cd "walk-forward optimization"; python portfolio_wf.py --rules rsi_divergence --pit
python strat_wf.py --tf 1d --rules rsi_divergence     # its walk-forward sheet, scoped
```

`strat_wf --rules` is the cheap loop: it scores the named strategy's cells and the
controls and nothing else, so it is seconds rather than the whole catalog. What it
cannot do is finish the job. `IS#1`, the two noise ceilings and ranking stability are
defined over the catalog that was scored, so a scoped run's copies of them are different
quantities wearing the same column names — which is why it writes `*.partial.csv` and
leaves the sheet of record alone.

**"The ranking is relative" is true of less than it sounds.** No row's backtest depends on
which other rules exist — a book is built from its own positions and the price bars. What
is relative is a handful of panel columns, and on the book sheets those are re-derivable
from what is already stored:

```powershell
cd "walk-forward optimization"
python merge_book.py --class us_stocks --tf 1d --rules my_rule    # ~seconds, not ~30 min
```

It scores the one rule, appends it, and re-runs the panel passes over the merged sheet —
including criterion R, which a `--rules` run cannot compute for want of the `RANDOM_*`
controls. It refuses to write to a sheet it cannot first reproduce. See the section in
that folder's `CLAUDE.md`.

Two things it does not reach, and they are why the full stages still exist.
`edge_standard.csv` is written by `riskmatch_wf.py`, and `make_book_rules.py` reads the
labels from there. `IS#1` is a real backtest of the act of choosing — per fold, the best
cell in the whole catalog — so a new candidate can win a fold and no stored column can
re-derive that.

Three properties of this loop are deliberate and should not be worked around.

**Scaffolding pre-registers the trial.** `trials.py` is an append-only ledger in
`../data/reference/trials.csv` recording what was tested, on what scope, and why — written
*before* any result exists. `metrics.deflated_sharpe` needs an honest `n_trials`, and
counting from whatever CSVs survive gets it wrong in the flattering direction every time:
abandoned rules leave no file, narrowed grids leave only the narrowed version, and both
still consumed a look at the data.

**New files are `DRAFT = True` and are excluded from `CATALOG`.** A scaffolded `position()`
raises `NotImplementedError`, `registry.build` catches it and returns `None`, and the
strategy would appear on a leaderboard as a rule that simply never trades —
indistinguishable from a real rule that does nothing. Flip `DRAFT` to `False` when it is
implemented; `registry.DRAFTS` lists what is still pending.

**The causality gate runs before the scorer.** Truncation, not review: build on the full
series and on the series minus N bars and require the overlap to be identical.

**A file in `published/` is the unit of a tested thing, and its filename is its identity.**
The module name is the strategy name, which is the label every result CSV in this repo is
keyed on, going back three studies — renaming a file renames a strategy and orphans its
history. There is no registration step: `registry._discover()` imports everything in the
folder and picks up whatever defines `position` and a non-empty `GRID`.

Two strategies share an implementation with a sibling on purpose — `donchian_s2`,
`rsi2_raw` and `supertrend_lf` import `position` from their primary rather than copying it,
because they are the same mechanism published with different parameters.

## `talib_signals.py` is a copy, and must never diverge

It is **byte-identical** to `../test research/src/talib_signals.py`, which is frozen (see
`../LOCKED.md`).

It was copied rather than moved because 17 modules inside that locked folder still import
the original. Copying is normally how two implementations drift apart — here it is safe
precisely because the original can no longer be edited, and the deny rules in
`.claude/settings.json` enforce that.

Before the refactor, `backtest engine` put `../test research/src` on `sys.path` so it could
`import talib_signals`, which meant live code loaded its signal definitions out of a
finished study. Cutting that was the point.

Equivalence was verified both ways at the copy. **If you ever change this file, that claim
is void** — re-check it or delete it.

## The strategy signature

Every strategy is one function with the same signature:

```python
fn(df, close, bpy, **params) -> np.ndarray      # target exposure per bar, -1..1
```

`bpy` is **measured** bars per year, never a constant: a US equity 4h "day" is one 4h bar
plus a 2.5h stub, so "a 50-day moving average" is a different bar count on every sheet.
`_bars()` is how a calendar span becomes a window length. Do not hardcode 252.

`CATALOG` records each one with its source, its published parameters and a grid.
**`grid[0]` is always the published setting** — that is what makes the no-fitting row and
the walk-forward row directly comparable, and it is load-bearing for stage 1e.

```python
@dataclass(frozen=True)
class Strategy:
    fn, rule, source, family, grid
    anchor: bool = False        # already run by prereg.py at its published settings
    classes: tuple | None = None
    note: str = ""
```

Labels serialise as `name@param=value` (`SEP = "@"`). Two other separator conventions exist
elsewhere and are not interchangeable: `variants.py` uses `|`, `combo_wf.py` uses `~` for
legs and `|` for the operator.

## The run harness is not here

`strat_wf.py` in `../walk-forward optimization/` is the half that turns this into a
leaderboard — it needs the engine, the fee model, the fold machinery and `walkforward`'s
private helpers. The split is at what used to be `strategies.py:957`, and the dependency
runs one way only: the harness imports the catalog, never the reverse. Keep it that way, or
this stops being importable by the paper desk and the dashboard.

## `_causal_median` is the fixed form

`_causal_median` uses an **expanding** median. `np.nanmedian` over a whole series is
look-ahead — one scalar, but still future data. The unfixed form still exists in
`../walk-forward optimization/` (`prereg.volmanaged`, `variants._vol_scale`) and those
stages were not re-run.

Any new volatility-scaled strategy uses `_causal_median`. Test it by truncation: build
positions on the full series and on the series minus the last N bars, then assert the
overlap is identical.
