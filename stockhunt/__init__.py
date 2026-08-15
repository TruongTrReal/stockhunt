"""Shared library for the stockhunt pipeline.

The four pipeline folders (`backtest engine/`, `walk-forward optimization/`,
`paper trading engine/`, `Stockhunt Dashboard/`) have spaces in their names, so none of
them can be a Python package, so they import each other by bare name off `sys.path`.
That constraint produced four separately-named bootstraps, a rule that module basenames
must be globally unique across the repo, and an import chain whose *order* is
load-bearing. It also produced the thing this package exists to stop: when a helper
cannot be imported from a shared location, it gets copied, and copies drift.

They had drifted. Before this package existed there were three `_cagr`, two `_max_dd`,
two `_sharpe` and two `trade_stats`, and they were not the same function:

* `_sharpe` demanded 3 observations in `riskmatch_wf` and 30 in `portfolio_wf`;
* `_max_dd` fed NaNs straight into `cumprod` (poisoning the whole result to NaN) while
  `focus_wf.drawdown`, same concept and same name-shape, filtered them out first.

`stockhunt.stats` is now the one definition of each, and the divergences survive as
**explicit arguments** rather than as hidden differences between two files. Every call
site passes the value that reproduces what it did before, so adopting this package
changed no published number — see `tools/golden.py`, which proves it.

This is a real package (no space in the name), so it imports normally. It currently
resolves because `backtest engine/config.py` puts the repo root on `sys.path`; once
`pip install -e .` is run it resolves regardless, which is what will eventually let the
four bootstraps be deleted.

Dependencies are numpy and pandas only. Nothing here may import `config`, `td_loader`
or anything else from a pipeline folder — the dependency runs one way, or the paper desk
and the dashboard stop being able to import it.
"""

__all__ = ["stats", "poscache", "parallel", "paths"]
