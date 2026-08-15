"""Backwards-compatible surface for what used to be one 976-line module.

The published strategies now live one-per-file in `strategies/published/`, the shared
primitives in `strategies/_indicators.py`, the signal-free controls in
`strategies/controls.py`, and label resolution in `strategies/registry.py`.

This module re-exports the names those files used to provide, because five call sites
across the walk-forward stage, the paper desk and the dashboard import from
`strategies.catalog` and the label grammar they key on has not changed. New code should
import from `strategies.registry` directly.
"""

from strategies._indicators import (D, M, TREND_MAS, _bars, _causal_median,  # noqa: F401
                                    _day_ordinals, _pct_rank, _rolling_max, _rolling_min,
                                    _state_machine, _streak, _supertrend_trend, _vol_scale)
from strategies.controls import (BASELINE, CONTROLS, RANDOM_BLOCK,  # noqa: F401
                                 RANDOM_DRAWS, RANDOM_SEED, random_control)
from strategies.overlays.regime import (REGIME_PREFIX, REGIME_SEP,  # noqa: F401
                                        REGIME_WARMUP_YEARS, regime_gate)
from strategies.registry import (CATALOG, SEP, Strategy, build, cells,  # noqa: F401
                                 decode, encode, skipped_for)

__all__ = ["BASELINE", "CATALOG", "CONTROLS", "RANDOM_DRAWS", "SEP", "Strategy",
           "build", "cells", "decode", "encode", "regime_gate", "random_control",
           "skipped_for"]
