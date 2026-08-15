"""Trailing-volatility conditioning over any base strategy."""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies._indicators import D, _bars


REGIME_PREFIX = "volregime"


REGIME_SEP = ":"


REGIME_WARMUP_YEARS = 3.0


def _causal_quantile(x: np.ndarray, q: float, min_periods: int) -> np.ndarray:
    """Expanding quantile — the threshold a trader could have computed by bar t.

    The sibling of `_causal_median` and it exists for the same reason: the natural way to
    write "trade only when volatility is in its upper half" is `np.nanquantile(vol, 0.5)`
    over the whole series, and that number is not knowable until the series has finished.
    A regime filter is *especially* exposed to this, because the whole claim is that the
    rule knew which regime it was in at the time.
    """
    return pd.Series(x).expanding(min_periods=min_periods).quantile(q).to_numpy()


def _trailing_vol(close: np.ndarray, window: int) -> np.ndarray:
    ret = np.empty_like(close)
    ret[0] = 0.0
    ret[1:] = close[1:] / close[:-1] - 1.0
    return pd.Series(ret).rolling(window).std(ddof=1).to_numpy()


def regime_gate(close: np.ndarray, bpy: float, side: str, q: float,
                window_days: float = 60.0) -> np.ndarray:
    """1.0 on bars whose trailing volatility is in the requested regime, else 0.0.

    Measured on `ibs` across the 20 mega-caps at 1d, bucketing by trailing 60-bar vol,
    the excess Sharpe over buy-and-hold runs monotonically from **-0.22** in the calmest
    quintile to **+0.43** in the most stressed — a 0.66 swing that every unconditional
    leaderboard in this repo averages into a single number near zero. That is the
    hypothesis this gate exists to test, and it is worth being precise about what it
    does and does not license:

    * It is **one new free parameter** (two, counting the window), and it was chosen
      after looking at that table. So it must be pre-registered and it must be paid for
      in the trial count — `metrics.deflated_sharpe` takes `n_trials` for this reason.
      Running it as an afterthought on the same sheet that suggested it is how this
      project has twice had to retract a finding.
    * `side="lo"` is not optional decoration. Reporting only the half that worked, after
      seeing both, is selection on the test set wearing a regime filter's clothes. Both
      halves get scored and both get published.

    A regime-gated rule is also mechanically less invested than its base — it sits out
    whole years — so its IR against a fully-invested benchmark falls for reasons that
    have nothing to do with skill. Score these on risk-matched wealth or `ir_vs_random`,
    never on raw IR.
    """
    n = _bars(bpy, window_days * D)
    vol = _trailing_vol(close, n)
    thr = _causal_quantile(vol, q, _bars(bpy, REGIME_WARMUP_YEARS))
    ok = np.isfinite(vol) & np.isfinite(thr)
    hot = np.where(ok, vol > thr, False)
    # Before the warmup completes there is no threshold, and the honest default is to
    # stand aside rather than to guess: a gate that trades its warmup is a gate whose
    # earliest years are unconditional, which is exactly what it claims not to be.
    return (hot if side == "hi" else (~hot & ok)).astype("float64")


def apply(label, df, close, bpy, symbol, build):
    """Decode `volregime:<hi|lo>:<q>:<base label>` and gate the base rule with it.

    `build` arrives as an argument rather than as an import: the registry owns label
    resolution and would be a circular import from here.
    """
    parts = label.split(REGIME_SEP, 3)
    if len(parts) != 4 or parts[0] != REGIME_PREFIX or parts[1] not in ("hi", "lo"):
        return None
    _, side, q_raw, base_label = parts
    try:
        q = float(q_raw)
    except ValueError:
        return None
    if not 0.0 < q < 1.0:
        return None
    base = build(base_label, df, close, bpy, symbol)
    if base is None:
        return None
    return base * regime_gate(close, bpy, side, q)
