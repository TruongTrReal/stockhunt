"""The strategy layer, shared by every stage of the pipeline.

Two things live here and nothing else does:

* `talib_signals` -- the 231-variant TA-Lib rule table. A **byte-identical copy** of
  `test research/src/talib_signals.py`, which is frozen (see `../LOCKED.md`). It was
  copied rather than moved because 17 modules inside that locked folder still import it;
  the two cannot diverge precisely because the original can no longer be edited.
* `catalog` -- the 26 published strategies as callables plus their parameter grids.

This is a real package -- the folder name has no space in it, unlike the four pipeline
folders -- so it is imported normally, `from strategies.catalog import CATALOG`. That
requires the repo root on `sys.path`, which `backtest engine/config.py` arranges.
"""

from . import catalog, talib_signals   # noqa: F401
