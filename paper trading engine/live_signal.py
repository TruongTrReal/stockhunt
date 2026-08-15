"""One label -> one position series, for whichever family the label belongs to.

This repo has **two** signal dispatchers, and until now the live desk only knew about one:

    signals.position_for      the 231 TA-Lib rules, and combos of them. Parity-gated.
    registry.build            the 31 published strategies in `strategies/published/`,
                              their `@param=value` variants, and the overlays

`SMA_200` goes through the first and returns None from the second; `ibs` goes through the
second and RAISES from the first. Neither one alone can price the dashboard's leaderboard,
because that board merges both families into a single ranked list.

The consequence was silent and specific: `ibs` and `volmanaged` sit at the top of the
`us_stocks 1d` board and the desk could not trade either of them. `TalibRuleStrategy`
called `generate_position` directly, which answers `No signal rule registered for 'ibs'`.
The rules the research likes best were exactly the ones the paper desk could not run.

**Order matters and is not arbitrary.** TA-Lib first, because `signals.position_for` is
what `parity.py` gates on and what the position cache is fingerprinted against — a label
those two can build must keep being built by them, or the live desk and the research stop
agreeing about what a rule means. `registry.build` is only asked about labels the first
one declines.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import paper_config                                   # noqa: F401  (wires sys.path)

from engines import vector                            # ../backtest engine
from strategies.registry import build as _build_published
from strategies.talib_signals import generate_position

# What a label belongs to, for anything that needs to decide before computing.
TALIB = "talib"
PUBLISHED = "published"
COMBO = "combo"
UNKNOWN = "unknown"


def family(label: str) -> str:
    """Which dispatcher owns this label, without building anything.

    Used by `catalog.py` to decide what may be offered for promotion, and by the API to
    refuse a registration the desk could not honour — a menu entry that cannot trade is
    worse than an absent one, because it fails after somebody has chosen it.
    """
    from strategies import registry
    # `CATALOG` is the discovered set, keyed on the published name. The label may carry a
    # variant suffix (`ibs@buy=0.3`), and `SEP` is what separates the two.
    name = label.split(registry.SEP, 1)[0]
    if name in registry.CATALOG:
        return PUBLISHED
    # A TA-Lib rule is `<INDICATOR>` or `<INDICATOR>_<period>`, so the indicator is the
    # part before the last underscore-and-digits. Matching on the indicator list rather
    # than on a full rule list is what makes `SMA_50` and `SMA_200` both resolve without
    # enumerating every period.
    try:
        from strategies.talib_signals import get_all_indicator_names
        indicators = set(get_all_indicator_names())
        head = label.rsplit("_", 1)[0] if label.rsplit("_", 1)[-1].isdigit() else label
        if label in indicators or head in indicators:
            return TALIB
    except Exception:
        pass
    # A combo — `HT_TRENDMODE~MAXINDEX|or`. Tradable, provided BOTH legs are: the label
    # carries everything needed to rebuild it, so nothing has to be looked up.
    #
    # This was refused for a while on the grounds that a combo is reconstructed from a
    # leaderboard row by `signals.position_for_row`, which a live strategy does not have.
    # That was true of the row-based path and false of the label: `combo.parse` reads the
    # legs and the operator straight out of the name. The cost of the mistake was the
    # whole top of the crypto 1d board being marked unavailable — five of its first five.
    from strategies.overlays import combo
    if combo.is_combo(label):
        a, b, op = combo.parse(label)
        if op in combo.OPERATORS and family(a) != UNKNOWN and family(b) != UNKNOWN:
            return COMBO
    return UNKNOWN


def position_for(label: str, df: pd.DataFrame, symbol: str = "") -> np.ndarray | None:
    """The position series, or None when no dispatcher can build this label.

    `df` must carry Open/High/Low/Close/Volume indexed by timestamp, which is what the
    live strategy's rolling buffer produces and what both dispatchers expect.

    **`generate_position`, not `signals.position_for`.** The second one looks like the
    better door — it is the parity-gated definition — but it takes an asset class and a
    timeframe and uses them to pick a benchmark and to decide end-of-day flattening. The
    live strategy has carried `asset_class="equity"` by default since it was written,
    which is not one of the four real classes, so routing through it returned None for
    every rule and the desk stopped trading entirely while looking healthy. It would also
    have quietly added intraday flattening the live path never had.

    `generate_position` is what this strategy has always called and what its warm-up
    window was measured against. It stays the TA-Lib path; the registry is only consulted
    for labels it does not know.
    """
    # A combo first, because neither single-rule dispatcher recognises `A~B|op` — the TA-Lib
    # one raises on it and the registry declines it, so it would fall through to None.
    from strategies.overlays import combo
    if combo.is_combo(label):
        a, b, op = combo.parse(label)
        pa = position_for(a, df, symbol)
        pb = position_for(b, df, symbol)
        if pa is None or pb is None or op not in combo.OPERATORS:
            return None
        return np.asarray(combo.combine(pa, pb, op), dtype="float64")

    try:
        pos = generate_position(label, df)
    except Exception:
        # It RAISES for an unregistered name rather than returning None, so a published
        # label arrives here as an exception. Not an error — the first dispatcher
        # declining.
        pos = None
    if pos is not None:
        return np.asarray(pos, dtype="float64")

    close = df["Close"].to_numpy(dtype="float64")
    # Derived from the index, exactly as `strat_wf.py` does it, rather than assumed from
    # the timeframe: a published strategy that annualises anything (`volmanaged` targets a
    # volatility) would otherwise be scaled differently live than it was in the research.
    try:
        bpy = float(vector.bars_per_year(df.index))
    except Exception:
        bpy = 252.0
    pos = _build_published(label, df, close, bpy, symbol)
    return None if pos is None else np.asarray(pos, dtype="float64")
