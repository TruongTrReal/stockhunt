"""Signal-free controls. Not strategies — the yardstick strategies are read against.

`ir_vs_random` exists because IR against a rising benchmark is close to a linear
function of time-in-market, so a rule must be scored against a *no-signal* rule at
its own exposure before any ordering means anything.
"""

from __future__ import annotations

import numpy as np


BASELINE = "BUYHOLD"


CONTROLS = ("ALWAYS_LONG", "ALWAYS_FLAT",
            "RANDOM_25", "RANDOM_50", "RANDOM_75", "RANDOM_90")


RANDOM_BLOCK = 20


RANDOM_SEED = 20260807


RANDOM_DRAWS = 12


def random_control(n: int, p_long: float, symbol: str, draw: int = 0) -> np.ndarray:
    """Signal-free long/flat at `p_long` exposure, in blocks, deterministic per symbol.

    Blocks rather than per-bar draws because a coin flipped every bar would turn over
    ~130 times a year and be dominated by fees, which is not the quantity being
    measured — the question is what a rule with *no information* and this much market
    exposure scores, not what one with absurd turnover scores.

    Seeded from the symbol via CRC32, not `hash()`, which is randomised per process by
    PYTHONHASHSEED and would make the control a different series on every run. Per-symbol
    seeds also matter: one shared draw would correlate every asset's IR and hand the
    breadth statistic an agreement it did not earn.
    """
    from zlib import crc32
    rng = np.random.default_rng(RANDOM_SEED + crc32(symbol.encode("utf-8"))
                                + 7919 * draw)
    n_blocks = int(np.ceil(n / RANDOM_BLOCK))
    blocks = (rng.random(n_blocks) < p_long).astype("float64")
    return np.repeat(blocks, RANDOM_BLOCK)[:n]
