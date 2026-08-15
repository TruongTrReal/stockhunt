"""`backtest engine/signals.py` — the ONE way a rule name becomes a position series.

`parity.py`, `sweep.py`, `combo_sweep.py` and `validate.py` must all produce
byte-identical positions for the same cell, or the parity harness is checking the wrong
thing. That makes three policies here load-bearing, and each gets a test:

* the **baseline is never flattened** — flattening the benchmark turns it into a different
  strategy, which is precisely what made the old 5-minute "beat" an artifact;
* the **NaN policy** is applied once, here, not per caller;
* **volume rules are skipped and counted** on a class with no volume, never fed NaN — a
  volume rule on NaN produces a flat position, indistinguishable on a leaderboard from a
  rule that does nothing.

`rule_positions` is the cached entry point and must be indistinguishable from the uncached
`position_for` that parity gates on. That equivalence is tested directly, both ways.
"""

from __future__ import annotations

import numpy as np
import pytest

import signals
from config import FLATTEN_EOD_TIMEFRAMES

from conftest import make_ohlcv

BASELINE = "BUYHOLD"


@pytest.fixture
def daily():
    return make_ohlcv(900, seed=21)


# ---------------------------------------------------------------- the baseline

def test_the_baseline_is_always_long(daily):
    pos = signals.position_for(BASELINE, daily, "us_stocks", "1d", BASELINE)
    np.testing.assert_array_equal(pos, np.ones(len(daily)))


def test_the_baseline_is_never_flattened_even_at_a_flattening_timeframe(intraday_5m):
    """The 5-minute artifact, pinned. Flattening the benchmark turns it into a different
    strategy and is how the old 'beat' was manufactured."""
    assert "5m" in FLATTEN_EOD_TIMEFRAMES
    pos = signals.position_for(BASELINE, intraday_5m, "us_stocks", "5m", BASELINE)
    np.testing.assert_array_equal(pos, np.ones(len(intraday_5m)))
    assert pos[-1] == 1.0                       # not closed at the final session end


def test_without_a_baseline_name_nothing_is_special_cased(daily):
    """`BUYHOLD` is not a TA-Lib rule, so it cannot be built as one."""
    assert signals.position_for(BASELINE, daily, "us_stocks", "1d") is None


# ------------------------------------------------------------- flatten_eod policy

def test_an_intraday_rule_IS_flattened_at_a_flattening_timeframe(intraday_5m):
    pos = signals.position_for("SMA_50", intraday_5m, "us_stocks", "5m", BASELINE)
    assert pos is not None
    days = intraday_5m.index.normalize()
    last = np.flatnonzero(np.append(days[1:] != days[:-1], True))
    assert np.all(pos[last] == 0.0)


def test_a_4h_rule_is_NOT_flattened(daily):
    """65-95% of US equity return is overnight; flattening at 4h removed most of the
    drift the rule is scored on and made the convention look like signal decay."""
    assert "4h" not in FLATTEN_EOD_TIMEFRAMES
    frame = make_ohlcv(400, freq="4h", seed=22)
    pos = signals.position_for("SMA_50", frame, "us_stocks", "4h", BASELINE)
    assert pos is not None
    days = frame.index.normalize()
    last = np.flatnonzero(np.append(days[1:] != days[:-1], True))
    assert not np.all(pos[last] == 0.0)


def test_crypto_is_never_flattened(intraday_5m):
    """A 24/7 market has no session to flatten into; forcing a daily flat would invent an
    exposure gap."""
    pos = signals.position_for("SMA_50", intraday_5m, "crypto", "5m", BASELINE)
    assert pos is not None
    days = intraday_5m.index.normalize()
    last = np.flatnonzero(np.append(days[1:] != days[:-1], True))
    assert not np.all(pos[last] == 0.0)


# ------------------------------------------------------------------- NaN policy

def test_every_one_of_the_231_rules_builds_a_finite_series(daily):
    """The NaN policy is applied once, here, rather than per caller — so it has to hold
    for the whole rule table, not a sample of it. Period variants exist only for the MA
    family (`SMA_50`, `EMA_200`); `RSI` and `ADX` carry no `_14` suffix."""
    from strategies.talib_signals import get_all_indicator_names

    rules = get_all_indicator_names()
    assert len(rules) == 231

    unbuildable = []
    for rule in rules:
        pos = signals.position_for(rule, daily, "us_stocks", "1d", BASELINE)
        if pos is None:
            unbuildable.append(rule)
            continue
        assert pos.size == len(daily), rule
        assert np.isfinite(pos).all(), rule     # NaN/inf mapped to flat, once, here
        assert np.abs(pos).max() <= 1.0, rule

    assert unbuildable == []


def test_an_unbuildable_rule_is_none_rather_than_an_exception(daily):
    for rule in ("NOT_A_RULE", "", "SMA_notanumber"):
        assert signals.position_for(rule, daily, "us_stocks", "1d", BASELINE) is None


def test_a_rule_needing_a_benchmark_is_none_when_none_is_available(daily, monkeypatch):
    """BETA and CORREL measure a relationship with the index, so without the benchmark
    series there is no position to build — and flat would be a lie."""
    monkeypatch.setattr(signals, "benchmark_close", lambda *a, **k: None)
    for rule in signals.NEEDS_BENCHMARK:
        assert signals.position_for(rule, daily, "us_stocks", "1d", BASELINE) is None


# ------------------------------------------------------------------ usable_rules

def test_volume_rules_are_skipped_and_counted_on_a_class_without_volume(
        monkeypatch, ohlcv_novolume):
    """Never fed NaN: a volume rule on NaN produces a flat position, indistinguishable on
    a leaderboard from a rule that simply does nothing."""
    monkeypatch.setattr(signals.td_loader, "load",
                        lambda *a, **k: {"BTC/USD": ohlcv_novolume})
    rules = ["SMA_50", "AD", "ADOSC", "MFI", "OBV", "RSI", "ADX"]
    runnable, skipped = signals.usable_rules(rules, "crypto", "1d")
    assert set(skipped) == {"AD", "ADOSC", "MFI", "OBV"}
    assert set(runnable) == {"SMA_50", "RSI", "ADX"}
    assert len(runnable) + len(skipped) == len(rules)        # counted, not dropped


def test_every_rule_is_runnable_on_a_class_that_has_volume(monkeypatch, ohlcv):
    monkeypatch.setattr(signals.td_loader, "load", lambda *a, **k: {"AAPL": ohlcv})
    rules = ["SMA_50", "AD", "MFI"]
    runnable, skipped = signals.usable_rules(rules, "us_stocks", "1d")
    assert runnable == rules and skipped == []


def test_an_unfetched_sheet_skips_everything_rather_than_claiming_it_ran(monkeypatch):
    monkeypatch.setattr(signals.td_loader, "load", lambda *a, **k: {})
    runnable, skipped = signals.usable_rules(["SMA_50", "AD"], "crypto", "1d")
    assert runnable == []
    assert skipped == ["SMA_50", "AD"]


# --------------------------------------------------------------- position_for_row

def test_a_combo_row_is_rebuilt_from_its_legs_not_parsed_from_its_label(daily):
    """A combo's name means nothing to `generate_position`, so the row carries its legs."""
    row = {"rule": "SMA_50 and RSI", "leg_a": "SMA_50", "leg_b": "RSI", "op": "and"}
    a = signals.position_for("SMA_50", daily, "us_stocks", "1d", BASELINE)
    b = signals.position_for("RSI", daily, "us_stocks", "1d", BASELINE)
    assert a is not None and b is not None
    got = signals.position_for_row(row, daily, "us_stocks", "1d", BASELINE)
    np.testing.assert_array_equal(got, signals.combine(a, b, "and"))


def test_a_single_row_goes_through_position_for(daily):
    row = {"rule": "SMA_50", "op": None}
    np.testing.assert_array_equal(
        signals.position_for_row(row, daily, "us_stocks", "1d", BASELINE),
        signals.position_for("SMA_50", daily, "us_stocks", "1d", BASELINE))


def test_a_combo_row_with_an_unbuildable_leg_is_none(daily):
    row = {"rule": "x", "leg_a": "SMA_50", "leg_b": "NOT_A_RULE", "op": "or"}
    assert signals.position_for_row(row, daily, "us_stocks", "1d", BASELINE) is None


# -------------------------------------------------------------- the cached path

def test_rule_positions_matches_the_uncached_definition(tmp_path, monkeypatch):
    """`position_for` is the single uncached definition and remains the thing parity
    gates on. If the cached entry point can disagree with it, parity is checking the
    wrong thing."""
    from stockhunt import paths, poscache

    monkeypatch.setattr(paths, "POSITION_CACHE", tmp_path)
    monkeypatch.setattr(signals.paths, "POSITION_CACHE", tmp_path)
    monkeypatch.delenv("STOCKHUNT_NO_POSCACHE", raising=False)

    data = {"AAPL": make_ohlcv(600, seed=23), "MSFT": make_ohlcv(600, seed=24)}
    expected = {s: signals.position_for("SMA_50", df, "us_stocks", "1d", BASELINE)
                for s, df in data.items()}

    cache = poscache.Sheet(tmp_path, "us_stocks", "1d", data,
                           signals.signal_code_fingerprint())
    assert cache.enabled

    cold = signals.rule_positions("SMA_50", data, "us_stocks", "1d", cache, BASELINE)
    warm = signals.rule_positions("SMA_50", data, "us_stocks", "1d", cache, BASELINE)

    for symbol, want in expected.items():
        if want is None:
            assert cold[symbol] is None and warm[symbol] is None
        else:
            np.testing.assert_array_equal(cold[symbol], want)
            np.testing.assert_array_equal(warm[symbol], want)


def test_rule_positions_without_a_cache_is_the_uncached_path():
    data = {"AAPL": make_ohlcv(400, seed=25)}
    got = signals.rule_positions("SMA_50", data, "us_stocks", "1d", None, BASELINE)
    want = signals.position_for("SMA_50", data["AAPL"], "us_stocks", "1d", BASELINE)
    if want is None:
        assert got["AAPL"] is None
    else:
        np.testing.assert_array_equal(got["AAPL"], want)


def test_rule_positions_covers_every_symbol_including_the_failures():
    """Symbols whose rule cannot be built map to None, exactly as `position_for` returns
    it, so callers keep their existing `if pos is None: continue`."""
    data = {"AAPL": make_ohlcv(400, seed=26), "MSFT": make_ohlcv(400, seed=27)}
    got = signals.rule_positions("NOT_A_RULE", data, "us_stocks", "1d", None, BASELINE)
    assert set(got) == set(data)
    assert all(v is None for v in got.values())


def test_the_code_fingerprint_is_computed_once_and_is_stable():
    first = signals.signal_code_fingerprint()
    assert first == signals.signal_code_fingerprint()
    assert len(first) == 24


def test_the_code_fingerprint_covers_the_signal_sources():
    """Edit a rule and every entry it could reach is invalidated automatically."""
    from stockhunt import paths, poscache
    assert signals.signal_code_fingerprint() == poscache.code_fingerprint(
        paths.SIGNAL_SOURCES)
    names = {p.name for p in paths.SIGNAL_SOURCES}
    assert {"strategies", "signals.py", "vector.py"} <= names
