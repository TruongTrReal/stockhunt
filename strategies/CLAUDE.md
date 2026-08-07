# CLAUDE.md

Guidance for Claude Code working in this directory. Read `../CLAUDE.md` first.

## What this is

The strategy layer, shared by every stage of the pipeline. Two modules, both deliberately
free of any dependency on the rest of the repo — numpy, pandas and talib only.

```
talib_signals.py   the 231-variant TA-Lib rule table. name -> position series
catalog.py         26 published strategies as callables + their parameter grids
```

This is a **real package** — the folder name has no space in it, unlike the four pipeline
folders — so it is imported normally: `from strategies.catalog import CATALOG`. That needs
the repo root on `sys.path`, which `backtest engine/config.py` arranges as a side effect
of being imported.

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

Equivalence was verified both ways at the copy: same SHA-256, and all 231 rules producing
identical positions on AAPL 1d when loaded from each path side by side. **If you ever
change this file, that claim is void** — re-check it or delete it.

## `catalog.py`

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
runs one way only: the harness imports the catalog, never the reverse. Keep it that way,
or this stops being importable by the paper desk and the dashboard.

## `_causal_median` is the fixed form

`_causal_median` uses an **expanding** median. `np.nanmedian` over a whole series is
look-ahead — one scalar, but still future data — and truncating the series changed 11 of 93
cells' *past* positions. The unfixed form still exists in `../walk-forward optimization/`
(`prereg.volmanaged`, `variants._vol_scale`) and those stages were not re-run.

Any new volatility-scaled strategy uses `_causal_median`. Test it by truncation: build
positions on the full series and on the series minus the last N bars, then assert the
overlap is identical.
