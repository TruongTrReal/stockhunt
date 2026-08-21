"""`portfolio_wf.apply_flatten` — the end-of-day bound, and its two exemptions.

The failure this guards is a MIXED sheet. `signals.position_for` flattens intraday
TA-Lib rules; `registry.build`, which every published strategy and every control goes
through, does not. Score both on one 1m sheet without a single owner for the decision
and half the rows carry a handicap the other half does not — and the handicap is worth
IR 0.6-0.84 on its own, so the leaderboard is decided by which code path a rule happened
to take.

`apply_flatten` is that single owner. What it must do, and does not get to do quietly:
"""

import numpy as np
import pandas as pd
import pytest

from conftest import make_ohlcv

import portfolio_wf as pwf
from strategies.catalog import BASELINE


def _intraday(days: int = 4) -> pd.DataFrame:
    parts = [pd.date_range(f"2024-01-0{d} 09:30", f"2024-01-0{d} 15:57", freq="3min")
             for d in range(2, 2 + days)]
    index = parts[0].append(parts[1:])
    return make_ohlcv(len(index), seed=41).set_axis(index)


def test_last_bar_of_every_session_goes_flat():
    df = _intraday()
    pos = np.ones(len(df))
    out = pwf.apply_flatten(pos, df, "chart:bar_updn", "us_stocks")
    last = pd.Series(out, index=df.index).groupby(df.index.date).last()
    assert (last == 0.0).all()
    # ...and nothing else was touched.
    days = np.asarray(df.index.normalize())
    is_last = np.empty(len(days), bool)
    is_last[:-1] = days[1:] != days[:-1]
    is_last[-1] = True
    assert (out[~is_last] == 1.0).all()


def test_the_baseline_is_never_flattened():
    """Flattening the benchmark makes it a different strategy — the 5m 'beat' artifact."""
    df = _intraday()
    pos = np.ones(len(df))
    out = pwf.apply_flatten(pos, df, BASELINE, "us_stocks")
    np.testing.assert_array_equal(out, pos)


def test_crypto_is_never_flattened():
    """A 24/7 market has no session, so a daily flat would invent an exposure gap."""
    df = _intraday()
    pos = np.ones(len(df))
    out = pwf.apply_flatten(pos, df, "chart:bar_updn", "crypto")
    np.testing.assert_array_equal(out, pos)


@pytest.mark.parametrize("control", ["RANDOM_50", "ALWAYS_LONG"])
def test_the_exposure_controls_are_flattened_too(control):
    """They exist to PRICE this handicap. Exempting them would charge every real rule a
    cost its own control never pays, and the gap would read as failed signal."""
    df = _intraday()
    out = pwf.apply_flatten(np.ones(len(df)), df, control, "us_stocks")
    assert (pd.Series(out, index=df.index).groupby(df.index.date).last() == 0.0).all()


def test_flattening_is_idempotent():
    """Safe on a position that already came through `signals.position_for`."""
    df = _intraday()
    once = pwf.apply_flatten(np.ones(len(df)), df, "chart:bar_updn", "us_stocks")
    twice = pwf.apply_flatten(once, df, "chart:bar_updn", "us_stocks")
    np.testing.assert_array_equal(once, twice)


def test_build_book_does_not_flatten_by_default():
    """Off is the faithful Pine reading, and it is what the published sheets used —
    so the default must stay off or every existing intraday number silently moves."""
    import inspect
    sig = inspect.signature(pwf.build_book)
    assert sig.parameters["flatten_eod"].default is False
