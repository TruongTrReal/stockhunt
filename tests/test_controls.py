"""`strategies.controls` — the yardstick every leaderboard is read against.

IR against a rising benchmark is close to a linear function of time-in-market
(`corr(IR, long_frac) = 0.881` on 1d equities), so a rule means nothing until it is scored
against a no-signal rule at its own exposure. That makes the control's *reproducibility*
load-bearing: a control that differs between two runs is not a yardstick.

The seeding detail is the one that has teeth. `hash()` is randomised per process by
PYTHONHASHSEED, so a control seeded from it would be a different series on every run —
this uses CRC32 instead, and that is verified here in a subprocess with the hash seed
deliberately changed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from strategies.controls import (BASELINE, CONTROLS, RANDOM_BLOCK, RANDOM_DRAWS,
                                 RANDOM_SEED, random_control)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_control_series_has_the_requested_length():
    for n in (1, 19, 20, 21, 1000):
        assert random_control(n, 0.5, "AAPL").size == n


def test_exposure_tracks_the_requested_probability():
    for p in (0.25, 0.5, 0.75, 0.9):
        got = float(np.mean(random_control(50_000, p, "AAPL")))
        assert got == pytest.approx(p, abs=0.02)


def test_the_series_is_long_flat_only():
    assert set(np.unique(random_control(5000, 0.5, "AAPL"))) <= {0.0, 1.0}


def test_positions_come_in_blocks_rather_than_per_bar_coin_flips():
    """A coin flipped every bar turns over ~130 times a year and would be dominated by
    fees, which is not the quantity being measured."""
    pos = random_control(2000, 0.5, "AAPL")
    for start in range(0, 2000 - RANDOM_BLOCK, RANDOM_BLOCK):
        block = pos[start:start + RANDOM_BLOCK]
        assert len(set(block)) == 1, f"block at {start} is not constant"


def test_turnover_is_bounded_by_the_block_length():
    pos = random_control(10_000, 0.5, "AAPL")
    flips = int((np.diff(pos) != 0).sum())
    assert flips <= 10_000 / RANDOM_BLOCK


def test_the_same_symbol_gives_the_same_series_every_time():
    np.testing.assert_array_equal(random_control(500, 0.5, "AAPL"),
                                  random_control(500, 0.5, "AAPL"))


def test_different_symbols_get_different_draws():
    """One shared draw would correlate every asset's IR and hand the breadth statistic an
    agreement it did not earn."""
    a = random_control(2000, 0.5, "AAPL")
    b = random_control(2000, 0.5, "MSFT")
    assert not np.array_equal(a, b)
    assert abs(float(np.corrcoef(a, b)[0, 1])) < 0.15


def test_different_draws_of_the_same_symbol_differ():
    a = random_control(2000, 0.5, "AAPL", draw=0)
    b = random_control(2000, 0.5, "AAPL", draw=1)
    assert not np.array_equal(a, b)


def test_all_twelve_draws_are_distinct():
    """`RANDOM_DRAWS` exists because one draw carries as much sampling noise as the thing
    it is meant to calibrate."""
    seen = {random_control(1000, 0.5, "AAPL", d).tobytes() for d in range(RANDOM_DRAWS)}
    assert len(seen) == RANDOM_DRAWS


def test_a_shorter_series_is_a_prefix_of_a_longer_one():
    """Blocks are drawn then truncated, so the control on a fold is the control on the
    sheet restricted to that fold — not a fresh draw."""
    long_ = random_control(1000, 0.5, "AAPL")
    short = random_control(300, 0.5, "AAPL")
    np.testing.assert_array_equal(short, long_[:300])


def test_the_seed_survives_a_changed_python_hash_seed():
    """The whole reason CRC32 is used instead of `hash()`. Run in a subprocess because
    PYTHONHASHSEED is read at interpreter start and cannot be changed in-process."""
    script = (
        "import sys, numpy as np;"
        f"sys.path.insert(0, {str(REPO_ROOT)!r});"
        "from strategies.controls import random_control;"
        "print(random_control(200, 0.5, 'AAPL').sum())"
    )
    outs = []
    for seed in ("0", "1", "12345"):
        env = {**dict(__import__("os").environ), "PYTHONHASHSEED": seed}
        outs.append(subprocess.run([sys.executable, "-c", script], env=env,
                                   capture_output=True, text=True,
                                   check=True).stdout.strip())
    assert len(set(outs)) == 1, f"control is not reproducible across hash seeds: {outs}"
    assert float(outs[0]) == pytest.approx(float(random_control(200, 0.5, "AAPL").sum()))


def test_the_control_names_are_the_ones_the_sheets_are_keyed_on():
    assert BASELINE == "BUYHOLD"
    assert CONTROLS == ("ALWAYS_LONG", "ALWAYS_FLAT",
                        "RANDOM_25", "RANDOM_50", "RANDOM_75", "RANDOM_90")


def test_the_seed_is_frozen():
    """Changing it silently redraws every control curve already published."""
    assert RANDOM_SEED == 20260807
    assert RANDOM_BLOCK == 20


@pytest.mark.parametrize("p,expected", [(0.0, 0.0), (1.0, 1.0)])
def test_the_degenerate_probabilities_are_exact(p, expected):
    assert float(np.mean(random_control(1000, p, "AAPL"))) == expected
