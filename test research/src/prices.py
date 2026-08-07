"""Single place to choose which price source the pipeline reads.

Downstream scripts should import `load_universe` / `update_universe` from here
rather than from `data_loader` or `twelvedata_loader` directly, so the source is
one environment variable instead of an edit in thirty files::

    from prices import load_universe, update_universe, DATA_SOURCE

`STOCKHUNT_DATA` selects it — `twelvedata` (default) or `yfinance`.

The two sources are **not** interchangeable. They disagree on dividend-adjustment
methodology: the rules take a different position on only ~0.05% of position-days,
but final equity differs by a median of 0.9% and up to 18% on dividend payers.
Relative conclusions (which rule beats which) survive the switch; absolute ones
(CAGR, dollar PnL, "beats buy-and-hold by X") do not. So each source has its own
cache directory, and `describe_source()` exists to be printed into any artifact
that outsizes a single session — a CSV of results with no record of which source
produced it is not reproducible.
"""

import os

import data_loader
import twelvedata_loader

DATA_SOURCE = os.environ.get("STOCKHUNT_DATA", "twelvedata")

_MODULES = {
    "twelvedata": twelvedata_loader,
    "yfinance": data_loader,
}

if DATA_SOURCE not in _MODULES:
    raise ValueError(
        f"STOCKHUNT_DATA={DATA_SOURCE!r} is not a known source. "
        f"Choose one of: {', '.join(sorted(_MODULES))}"
    )

_active = _MODULES[DATA_SOURCE]

CACHE_DIR = _active.CACHE_DIR
DEFAULT_START = _active.DEFAULT_START
load_universe = _active.load_universe
update_universe = _active.update_universe


def describe_source() -> str:
    """One-line provenance string to stamp into result artifacts."""
    n_cached = len(list(CACHE_DIR.glob("*.parquet"))) if CACHE_DIR.exists() else 0
    adjustment = "adjust=all" if DATA_SOURCE == "twelvedata" else "auto_adjust=True"
    return (f"source={DATA_SOURCE} ({adjustment}) cache={CACHE_DIR} "
            f"tickers={n_cached}")


if __name__ == "__main__":
    print(describe_source())
