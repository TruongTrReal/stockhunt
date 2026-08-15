"""One way to turn a rule name into a position series, shared by every consumer.

`parity.py`, `sweep.py` and `validate.py` must all produce byte-identical positions for
the same (rule, asset, timeframe) or the parity harness is checking the wrong thing.
That means the benchmark plumbing, the NaN policy and end-of-day flattening live here
and nowhere else.

`rule_positions` adds a caching layer over exactly that, and it lives here for the same
reason everything else does: if two stages could cache positions differently, the parity
harness would be comparing two different things. See `stockhunt.poscache` for why the
cache is keyed on the OHLCV bytes and the signal source rather than on a timestamp.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import (CLASSES, FLATTEN_EOD_TIMEFRAMES, TIMEFRAMES,
                    rule_needs_volume, volume_dependent_rules)
from engines import vector
import td_loader

from stockhunt import paths, poscache
from strategies.talib_signals import BETA_NAME, CORREL_NAME, generate_position

NEEDS_BENCHMARK = {BETA_NAME, CORREL_NAME}

_bench_cache: dict[tuple[str, str], pd.Series | None] = {}


def benchmark_close(asset_class: str, timeframe: str) -> pd.Series | None:
    """Close series of the class benchmark, cached.

    SPY for equities; BTC/USD for crypto. Fetched and cached alongside the universe but
    deliberately *not* part of it — it exists only as the input BETA and CORREL need.
    """
    key = (asset_class, timeframe)
    if key not in _bench_cache:
        symbol = CLASSES[asset_class].get("benchmark")
        loaded = td_loader.load(asset_class, timeframe, [symbol]) if symbol else {}
        frame = loaded.get(symbol)
        _bench_cache[key] = frame["Close"] if frame is not None else None
    return _bench_cache[key]


def usable_rules(rules, asset_class: str, timeframe: str) -> tuple[list[str], list[str]]:
    """Split `rules` into (runnable, skipped) for this class.

    Twelve Data serves no volume for crypto, so AD/ADOSC/MFI/OBV cannot be evaluated
    there at all. They are *skipped and counted*, never run against NaN volume — a
    volume rule fed NaN produces a flat position, which on a leaderboard is
    indistinguishable from a rule that simply does nothing.
    """
    cached = td_loader.load(asset_class, timeframe)
    if not cached:
        return [], list(rules)
    sample = next(iter(cached.values()))
    has_volume = not sample["Volume"].isna().all()
    if has_volume:
        return list(rules), []
    vol_funcs = volume_dependent_rules()
    runnable, skipped = [], []
    for rule in rules:
        (skipped if rule_needs_volume(rule, vol_funcs) else runnable).append(rule)
    return runnable, skipped


# `combine` and `OPERATORS` moved to `strategies.overlays.combo` so the combo label
# grammar and the operator semantics live together, and so `strategies.registry.build`
# can resolve an `A~B|op` label instead of silently returning None for it. Re-exported
# here because the comment that used to sit on `combine` was right: there must be
# exactly one definition, or the searcher and the report renderer drift on what `and`
# means. It moved; it did not multiply.
from strategies.overlays.combo import combine, OPERATORS   # noqa: F401,E402



def position_for_row(row, df: pd.DataFrame, asset_class: str, timeframe: str,
                     baseline_name: str | None = None) -> np.ndarray | None:
    """Position for a leaderboard row, single or combo.

    A combo's name (`SMA_50 and RSI_14`) means nothing to `generate_position`, so a
    combo row is rebuilt from its stored legs and operator instead of parsed back out
    of its label.
    """
    op = row.get("op") if hasattr(row, "get") else None
    if isinstance(op, str) and op in OPERATORS:
        pa = position_for(row["leg_a"], df, asset_class, timeframe, baseline_name)
        pb = position_for(row["leg_b"], df, asset_class, timeframe, baseline_name)
        if pa is None or pb is None:
            return None
        return combine(pa, pb, op)
    return position_for(row["rule"], df, asset_class, timeframe, baseline_name)


def position_for(rule: str, df: pd.DataFrame, asset_class: str, timeframe: str,
                 baseline_name: str | None = None) -> np.ndarray | None:
    """Position series for one rule on one asset. None if the rule cannot be built.

    The baseline is always-long and is **never** flattened end-of-day: flattening the
    benchmark turns it into a different strategy, which is exactly what made the old
    5-minute "beat" an artifact.
    """
    if baseline_name is not None and rule == baseline_name:
        return np.ones(len(df), dtype="float64")

    kwargs = {}
    if rule in NEEDS_BENCHMARK:
        bench = benchmark_close(asset_class, timeframe)
        if bench is None:
            return None
        kwargs["benchmark_close"] = bench.reindex(df.index).ffill()

    try:
        raw = generate_position(rule, df, **kwargs)
    except Exception:
        return None

    pos = np.nan_to_num(np.asarray(raw, dtype="float64"), nan=0.0,
                        posinf=0.0, neginf=0.0)
    if pos.size != len(df):
        return None

    spec = CLASSES[asset_class]
    # Only at the day-trading horizons — see the note on FLATTEN_EOD_TIMEFRAMES. Applying
    # it to every intraday sheet removed the overnight drift, which is 65-95% of US
    # equity return, and made the flattening convention look like signal decay.
    if (spec["flatten_eod"] and TIMEFRAMES[timeframe]["intraday"]
            and timeframe in FLATTEN_EOD_TIMEFRAMES):
        pos = vector.flatten_eod(pos, df.index)
    return pos


# ------------------------------------------------------------------ position cache
#
# Generating positions is 68% of the cost of every stage (0.64 ms/cell against 0.30 ms to
# score one), and seven stages generate the identical series from the identical bars. The
# cache turns the second through seventh into a read.

_CODE_FP: str | None = None


def signal_code_fingerprint() -> str:
    """SHA-256 over every module that can change what a rule means. Computed once."""
    global _CODE_FP
    if _CODE_FP is None:
        _CODE_FP = poscache.code_fingerprint(paths.SIGNAL_SOURCES)
    return _CODE_FP


def sheet_cache(asset_class: str, timeframe: str, data: dict) -> poscache.Sheet:
    """Cache handle for one loaded sheet. Cheap; fingerprints the bars once."""
    return poscache.Sheet(paths.POSITION_CACHE, asset_class, timeframe, data,
                          signal_code_fingerprint())


def rule_positions(rule: str, data: dict, asset_class: str, timeframe: str,
                   cache: poscache.Sheet | None = None,
                   baseline_name: str | None = None) -> dict[str, np.ndarray | None]:
    """Every symbol's position series for ONE rule, served from cache where possible.

    One rule across all symbols, which is how every consumer in this repo loops and
    therefore what the cache is laid out for — a single file open per rule. Peak memory
    is one rule's worth (~28 MB on us_stocks 1d), never the 231-rule tensor that
    `sweep.py`'s memory contract exists to avoid.

    Symbols whose rule cannot be built map to `None`, exactly as `position_for` returns
    it, so callers keep their existing `if pos is None: continue`. A `None` is *not*
    cached: it can mean a genuinely inapplicable rule or a transient failure, and
    persisting the second kind would make it permanent.
    """
    if cache is None or not cache.enabled:
        return {s: position_for(rule, df, asset_class, timeframe, baseline_name)
                for s, df in data.items()}

    out: dict[str, np.ndarray | None] = {}
    with cache.rule(rule) as rc:
        for symbol, df in data.items():
            pos = rc.get(symbol)
            if pos is None:
                pos = position_for(rule, df, asset_class, timeframe, baseline_name)
                rc.put(symbol, pos)
            out[symbol] = pos
    return out
