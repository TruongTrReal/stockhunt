"""One way to turn a rule name into a position series, shared by every consumer.

`parity.py`, `sweep.py` and `validate.py` must all produce byte-identical positions for
the same (rule, asset, timeframe) or the parity harness is checking the wrong thing.
That means the benchmark plumbing, the NaN policy and end-of-day flattening live here
and nowhere else.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import CLASSES, rule_needs_volume, volume_dependent_rules
from engines import vector
import td_loader

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


def combine(a: np.ndarray, b: np.ndarray, op: str) -> np.ndarray:
    """Two 1/0/-1 position series -> one. Lives here so `combo_sweep.py` (which
    searches) and `build_payload.py` (which has to redraw the winner's curve) cannot
    drift apart on what an operator means."""
    if op == "vote":
        return np.sign(a + b)
    if op == "and":
        return np.where(np.sign(a) == np.sign(b), a, 0.0)
    if op == "or":
        return np.where(a != 0, a, b)
    if op == "gate":
        return np.where(b != 0, a, 0.0)
    raise ValueError(f"unknown operator {op!r}")


OPERATORS = ("vote", "and", "or", "gate")


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
    from config import FLATTEN_EOD_TIMEFRAMES, TIMEFRAMES
    # Only at the day-trading horizons — see the note on FLATTEN_EOD_TIMEFRAMES. Applying
    # it to every intraday sheet removed the overnight drift, which is 65-95% of US
    # equity return, and made the flattening convention look like signal decay.
    if (spec["flatten_eod"] and TIMEFRAMES[timeframe]["intraday"]
            and timeframe in FLATTEN_EOD_TIMEFRAMES):
        pos = vector.flatten_eod(pos, df.index)
    return pos
