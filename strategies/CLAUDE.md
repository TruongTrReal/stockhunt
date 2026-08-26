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
published/           ONE FILE PER PUBLISHED STRATEGY (175). Each declares position(),
                     GRID, RULE, SOURCE, FAMILY, ANCHOR, CLASSES, NOTE
                     130 of them are the `mc_*` batch: price FORECASTERS scraped
                     from a public browser, one-cell grids, sharing _forecast.py
_forecast.py         the mc_* batch's adapter: `expose` is the ONE definition of
                     how a predicted price becomes a position (long above the
                     close, flat at or below), plus the window primitives it needs
registry.py          discovers published/ by import; owns the label grammar
_indicators.py       shared primitives: _state_machine, _causal_median, _vol_scale, ...
controls.py          BUYHOLD / ALWAYS_* / RANDOM_* — yardsticks, not strategies
overlays/regime.py   trailing-vol conditioning that wraps any base label
overlays/heikin.py   `ha:` — the base rule's SIGNAL sees Heikin-Ashi candles
overlays/chart.py    `chart:` — day-denominated windows become Pine BAR COUNTS
trials.py            the append-only trial ledger -> ../data/reference/trials.csv
scaffold.py          creates a new published/ file and registers its trial
CONVERSIONS.md       provenance for the rules that came from somebody else's CODE
                     rather than from a paper -- and, per file, WHAT WAS DROPPED to
                     fit the one-exposure-per-bar contract. Also the ones that were
                     rejected, and why
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
`range_filter_macd` does the same to `range_filter`: the pair only prices the MACD gate if
the filter underneath them is bit-for-bit identical.

## Converting somebody else's code

A Pine `strategy()` or a freqtrade `IStrategy` does not fit through `fn(df, close, bpy)`
unchanged, and the gap is where a conversion silently becomes a different rule. Four things
have no expression in a one-exposure-per-bar series — intrabar stops, trade-level state
read at tick resolution, mid-trade position sizing (DCA, pyramiding), and cross-asset
inputs — and a fifth constraint is data: this repo holds daily and 4h bars, so a rule keyed
to 1-minute futures has nothing to run on however sound its logic is.

**Whatever you drop goes in the file's `NOTE` and in `CONVERSIONS.md`, by name.** So does
anything you *reproduced* that looks like a bug: `lorentzian_knn`'s training label is
backwards and `ssl_hybrid`'s short leg tests the wrong Keltner band, and both are kept,
because a fixed version of a published rule is not a replication of it. `CONVERSIONS.md`
also records the sources that were rejected outright — a plausible rewrite of a bot puts a
number on the leaderboard under a name that did not earn it.

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

## The overlays, and the one they exist to keep honest

Three prefixes wrap any base label — `volregime:hi:0.5:`, `ha:` and `chart:` — and they
compose in any order. Each is one substitution and nothing else, which is what makes a
wrapped cell a comparable trial rather than a different experiment:

| label | what changes | what does NOT |
|---|---|---|
| `ha:X` | X's signal is computed on Heikin-Ashi candles | the prices the money settles on |
| `chart:X` | X's day-windows become bar counts (`bpy` forced to 252) | anything already denominated in bars |
| `volregime:hi:q:X` | X is gated off outside the vol regime | X itself |

**`ha:` is the one with a trap under it, and the trap is the fill, not the transform.**
A Heikin-Ashi close is `(O+H+L+C)/4` — an average of four prices, which is not a price
anybody can transact at. TradingView's broker emulator will nonetheless fill at it
unless told otherwise, and that single default is enough to make almost any HA strategy
profitable on a chart: the synthetic close is smoothed toward the middle of the bar, so
buying at it is buying below where you could have. This overlay therefore changes the
*signal only*, and the exposure it returns settles on real closes through the same
`riskmatch_wf.apply_fill` path as every other rule. **A published HA number from a chart
platform is not comparable to one from this repo, and the difference is not small.**

`chart:` exists because this repo and Pine mean different things by the same parameter.
`ema_cross_sniper` here is "an 8-DAY against a 21-DAY EMA" on every sheet, which is the
right convention for comparing a rule across timeframes; on a TradingView 1m chart the
same script is an 8-BAR against a 21-BAR EMA. At 1m the day-form is a 3,900-bar window
and TA-Lib rejects some of them outright, so on the intraday sheets `chart:` is not only
the faithful reading of what the author ran, it is the only buildable one. Both are
legitimate questions; they are different trials and the ledger carries them separately.

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
