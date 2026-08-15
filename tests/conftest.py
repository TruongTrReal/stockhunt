"""Path bootstrap and shared fixtures for the unit suite.

The four pipeline folders have spaces in their names, so none of them is an importable
package and every module inside one imports its siblings by bare name. A test that wants
`metrics` therefore needs `backtest engine/` on `sys.path` — the same three-hop
arrangement `../CLAUDE.md` describes, done once here instead of in every test file.

Import order is load-bearing exactly as it is in the pipeline: `backtest engine/` goes on
the path first, and importing its `config` is what puts the repo root there, which is the
only reason `strategies.talib_signals` resolves. Nothing here mutates the path *after* a
pipeline module has been imported.

Everything in this suite runs on synthetic bars. No test reads `data/`, hits Twelve Data,
or depends on a result CSV — those are all mutable inputs, and a unit test that fails
because someone refetched a ticker is a test nobody will trust.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

for _p in (REPO_ROOT, REPO_ROOT / "backtest engine"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# --------------------------------------------------------------------- synthetic bars

def make_ohlcv(n: int = 600, freq: str = "1D", seed: int = 7,
               start: str = "2015-01-05", volume: bool = True,
               drift: float = 0.0003, vol: float = 0.012) -> pd.DataFrame:
    """A well-formed OHLCV frame: strictly positive, High/Low bracketing Open/Close.

    Deterministic per `seed`, because a test that reruns a random walk each session is a
    test that fails on a Tuesday for no reason anyone can reconstruct.
    """
    rng = np.random.default_rng(seed)
    ret = rng.normal(drift, vol, n)
    close = 100.0 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[close[0]], close[:-1]])
    span = np.abs(rng.normal(0.0, vol, n)) * close
    high = np.maximum(open_, close) + span
    low = np.minimum(open_, close) - span
    low = np.minimum(low, np.minimum(open_, close))
    low = np.maximum(low, 0.01)

    index = pd.date_range(start, periods=n, freq=freq)
    frame = pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close},
        index=index)
    frame["Volume"] = (rng.uniform(5e5, 5e6, n) if volume
                       else np.full(n, np.nan))
    return frame


@pytest.fixture
def ohlcv() -> pd.DataFrame:
    return make_ohlcv()


@pytest.fixture
def ohlcv_novolume() -> pd.DataFrame:
    """Crypto shape: Twelve Data serves no volume, and the field is absent, not zero."""
    return make_ohlcv(volume=False)


@pytest.fixture
def intraday_5m() -> pd.DataFrame:
    """Three US sessions of 5-minute bars, so end-of-day flattening has boundaries."""
    days = [pd.date_range(f"2024-03-{d} 09:30", f"2024-03-{d} 15:55", freq="5min")
            for d in (11, 12, 13)]
    index = days[0].append(days[1]).append(days[2])
    frame = make_ohlcv(len(index), seed=11)
    frame.index = index
    return frame
