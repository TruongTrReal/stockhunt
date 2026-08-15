"""`stockhunt.poscache` — the fingerprints are the whole safety argument.

A stale position cache would serve last week's signal definitions under this week's rule
name, and every gate, ceiling and verdict downstream would be computed on it without
complaint. The module's defence is that nothing is trusted: an entry is served only if the
OHLCV bytes AND the signal source both hash to what they hashed when it was written.

So these tests are mostly about *misses* — the cases that must NOT be served — plus the
one storage detail that could corrupt a hit silently: int8 packing rounding a fractional
exposure to zero and turning a scaled rule into a flat one.
"""

from __future__ import annotations

import numpy as np
import pytest

from stockhunt import poscache

from conftest import make_ohlcv


@pytest.fixture
def data() -> dict:
    return {"AAPL": make_ohlcv(300, seed=1), "BTC/USD": make_ohlcv(300, seed=2)}


@pytest.fixture
def sheet(tmp_path, data) -> poscache.Sheet:
    return poscache.Sheet(tmp_path, "us_stocks", "1d", data, "codefp")


# ------------------------------------------------------------------- fingerprints

def test_data_fingerprint_is_stable_for_identical_bars():
    a, b = make_ohlcv(200, seed=5), make_ohlcv(200, seed=5)
    assert poscache.data_fingerprint(a) == poscache.data_fingerprint(b)


def test_data_fingerprint_catches_a_single_repaired_bar_in_the_middle():
    """The exact case `check_data.py --fix` creates, and the reason this is not a
    length-and-endpoints check: one bar changes deep in an otherwise identical series."""
    a = make_ohlcv(200, seed=5)
    b = a.copy()
    b.iloc[100, b.columns.get_loc("Close")] *= 1.000001
    assert poscache.data_fingerprint(a) != poscache.data_fingerprint(b)


def test_data_fingerprint_covers_every_ohlcv_column_and_the_index():
    base = make_ohlcv(200, seed=5)
    for col in ("Open", "High", "Low", "Close", "Volume"):
        other = base.copy()
        other.iloc[50, other.columns.get_loc(col)] += 0.5
        assert poscache.data_fingerprint(other) != poscache.data_fingerprint(base), col

    shifted = base.copy()
    shifted.index = shifted.index + np.timedelta64(1, "D")
    assert poscache.data_fingerprint(shifted) != poscache.data_fingerprint(base)


def test_data_fingerprint_distinguishes_absent_volume_from_present():
    """Crypto has no Volume column at all; that must not collide with one that does."""
    with_vol = make_ohlcv(200, seed=5)
    without = with_vol.drop(columns=["Volume"])
    assert poscache.data_fingerprint(without) != poscache.data_fingerprint(with_vol)


def test_code_fingerprint_changes_when_a_signal_module_changes(tmp_path):
    src = tmp_path / "rules.py"
    src.write_text("def rule(): return 1\n")
    before = poscache.code_fingerprint([src])
    src.write_text("def rule(): return -1\n")
    assert poscache.code_fingerprint([src]) != before


def test_code_fingerprint_walks_directories_in_sorted_order(tmp_path):
    pkg = tmp_path / "strategies"
    (pkg / "published").mkdir(parents=True)
    (pkg / "a.py").write_text("A\n")
    (pkg / "published" / "b.py").write_text("B\n")
    first = poscache.code_fingerprint([pkg])
    assert first == poscache.code_fingerprint([pkg])       # stable across calls
    (pkg / "published" / "b.py").write_text("B!\n")
    assert poscache.code_fingerprint([pkg]) != first


def test_code_fingerprint_ignores_non_python_files(tmp_path):
    pkg = tmp_path / "strategies"
    pkg.mkdir()
    (pkg / "a.py").write_text("A\n")
    before = poscache.code_fingerprint([pkg])
    (pkg / "notes.md").write_text("does not change what a rule means\n")
    assert poscache.code_fingerprint([pkg]) == before


def test_code_fingerprint_treats_a_deleted_module_as_a_change(tmp_path):
    """A missing path contributes its name only — deleting a module is itself a change."""
    present = tmp_path / "signals.py"
    present.write_text("x\n")
    with_file = poscache.code_fingerprint([present])
    present.unlink()
    assert poscache.code_fingerprint([present]) != with_file


def test_safe_symbol_mirrors_config_without_importing_it():
    assert poscache._safe("BTC/USD") == "BTC_USD"
    assert poscache._safe("AAPL") == "AAPL"


# ------------------------------------------------------------------ round-tripping

def test_round_trip_serves_the_identical_series(sheet):
    pos = np.array([1.0, 0.0, -1.0, 1.0, 0.0] * 60)
    with sheet.rule("SMA_50") as rc:
        rc.put("AAPL", pos)
    with sheet.rule("SMA_50") as rc:
        np.testing.assert_array_equal(rc.get("AAPL"), pos)


def test_a_cached_series_comes_back_as_float64(sheet):
    """Callers do arithmetic on this; an int8 leaking out would silently truncate."""
    with sheet.rule("SMA_50") as rc:
        rc.put("AAPL", np.array([1.0, 0.0, -1.0] * 100))
    with sheet.rule("SMA_50") as rc:
        assert rc.get("AAPL").dtype == np.float64


def test_fractional_exposure_is_not_rounded_to_flat(sheet):
    """The documented hazard: storing 0.35 as int8 rounds it to 0 and turns a scaled
    published strategy into one that never trades — indistinguishable on a leaderboard."""
    pos = np.array([0.35, -0.7, 0.0, 1.0, 0.125] * 40)
    with sheet.rule("volmanaged") as rc:
        rc.put("AAPL", pos)
    with sheet.rule("volmanaged") as rc:
        got = rc.get("AAPL")
    assert got is not None
    assert not np.array_equal(got, np.zeros_like(pos))
    np.testing.assert_allclose(got, pos, rtol=1e-6)


def test_int8_packing_is_used_only_where_it_is_lossless(sheet, tmp_path):
    with sheet.rule("SMA_50") as rc:
        rc.put("AAPL", np.array([1.0, 0.0, -1.0] * 100))
    with sheet.rule("volmanaged") as rc:
        rc.put("AAPL", np.array([0.35, 0.5, -0.25] * 100))

    def stored_dtype(rule):
        path = tmp_path / "codefp" / "us_stocks" / "1d" / f"{rule}.npz"
        with np.load(path) as z:
            return z["p:AAPL"].dtype

    assert stored_dtype("SMA_50") == np.int8
    assert stored_dtype("volmanaged") == np.float32


def test_symbols_are_isolated_within_one_rule_file(sheet):
    with sheet.rule("RSI_14") as rc:
        rc.put("AAPL", np.ones(300))
        rc.put("BTC/USD", np.zeros(300))
    with sheet.rule("RSI_14") as rc:
        np.testing.assert_array_equal(rc.get("AAPL"), np.ones(300))
        np.testing.assert_array_equal(rc.get("BTC/USD"), np.zeros(300))


def test_a_slashed_symbol_does_not_escape_the_cache_directory(sheet, tmp_path):
    with sheet.rule("A/B") as rc:
        rc.put("BTC/USD", np.ones(10))
    files = list((tmp_path / "codefp" / "us_stocks" / "1d").glob("*.npz"))
    assert [f.name for f in files] == ["A_B.npz"]


def test_an_unwritten_rule_is_a_miss_not_a_crash(sheet):
    with sheet.rule("NEVER_WRITTEN") as rc:
        assert rc.get("AAPL") is None


def test_an_unknown_symbol_is_a_miss(sheet):
    with sheet.rule("SMA_50") as rc:
        rc.put("AAPL", np.ones(300))
    with sheet.rule("SMA_50") as rc:
        assert rc.get("TSLA") is None


# ------------------------------------------------------------------- invalidation

def test_changed_bars_invalidate_that_symbol_and_only_that_symbol(tmp_path, data):
    sheet = poscache.Sheet(tmp_path, "us_stocks", "1d", data, "codefp")
    with sheet.rule("SMA_50") as rc:
        rc.put("AAPL", np.ones(300))
        rc.put("BTC/USD", np.zeros(300))

    refetched = dict(data)
    changed = data["AAPL"].copy()
    changed.iloc[10, changed.columns.get_loc("Close")] *= 1.01
    refetched["AAPL"] = changed

    later = poscache.Sheet(tmp_path, "us_stocks", "1d", refetched, "codefp")
    with later.rule("SMA_50") as rc:
        assert rc.get("AAPL") is None                       # refetched ticker: miss
        np.testing.assert_array_equal(rc.get("BTC/USD"), np.zeros(300))   # untouched: hit


def test_a_new_code_fingerprint_cannot_reach_the_old_entries(tmp_path, data):
    old = poscache.Sheet(tmp_path, "us_stocks", "1d", data, "codefp_v1")
    with old.rule("SMA_50") as rc:
        rc.put("AAPL", np.ones(300))
    new = poscache.Sheet(tmp_path, "us_stocks", "1d", data, "codefp_v2")
    with new.rule("SMA_50") as rc:
        assert rc.get("AAPL") is None


def test_sheets_do_not_collide_across_class_or_timeframe(tmp_path, data):
    a = poscache.Sheet(tmp_path, "us_stocks", "1d", data, "fp")
    b = poscache.Sheet(tmp_path, "us_stocks", "4h", data, "fp")
    c = poscache.Sheet(tmp_path, "crypto", "1d", data, "fp")
    with a.rule("SMA_50") as rc:
        rc.put("AAPL", np.ones(300))
    for other in (b, c):
        with other.rule("SMA_50") as rc:
            assert rc.get("AAPL") is None


def test_a_none_position_is_never_cached(sheet):
    """None can mean a transient failure, and persisting that would make it permanent."""
    with sheet.rule("MFI") as rc:
        rc.put("AAPL", None)
    with sheet.rule("MFI") as rc:
        assert rc.get("AAPL") is None
    assert not list(sheet._dir.glob("*.npz"))               # nothing written at all


def test_a_truncated_npz_is_a_miss_not_a_dead_run(sheet, tmp_path):
    with sheet.rule("SMA_50") as rc:
        rc.put("AAPL", np.ones(300))
    path = tmp_path / "codefp" / "us_stocks" / "1d" / "SMA_50.npz"
    path.write_bytes(path.read_bytes()[:40])                # a killed run's half-write
    with sheet.rule("SMA_50") as rc:
        assert rc.get("AAPL") is None


def test_close_leaves_no_temp_file_behind(sheet, tmp_path):
    with sheet.rule("SMA_50") as rc:
        rc.put("AAPL", np.ones(300))
    written = tmp_path / "codefp" / "us_stocks" / "1d"
    assert not list(written.glob("*.tmp.npz"))
    assert (written / "SMA_50.npz").exists()


def test_close_is_a_no_op_when_nothing_was_written(sheet, tmp_path):
    with sheet.rule("SMA_50"):
        pass
    assert not (tmp_path / "codefp").exists()


# ------------------------------------------------------------------------ the switch

def test_the_env_switch_disables_the_cache_entirely(tmp_path, data, monkeypatch):
    """`STOCKHUNT_NO_POSCACHE=1` — the honest first move when a result looks surprising."""
    monkeypatch.setenv("STOCKHUNT_NO_POSCACHE", "1")
    assert poscache.disabled()
    sheet = poscache.Sheet(tmp_path, "us_stocks", "1d", data, "codefp")
    assert not sheet.enabled
    with sheet.rule("SMA_50") as rc:
        rc.put("AAPL", np.ones(300))
        assert rc.get("AAPL") is None
    assert not list(tmp_path.rglob("*.npz"))


@pytest.mark.parametrize("value,off", [("", False), ("0", False), ("1", True),
                                       ("yes", True)])
def test_the_env_switch_reads_unset_and_zero_as_enabled(monkeypatch, value, off):
    monkeypatch.setenv("STOCKHUNT_NO_POSCACHE", value)
    assert poscache.disabled() is off


def test_disabled_sheet_skips_fingerprinting_the_bars(tmp_path, data):
    """Fingerprinting is not free; a disabled sheet must not pay for it."""
    sheet = poscache.Sheet(tmp_path, "us_stocks", "1d", data, "fp", enabled=False)
    assert not sheet.enabled
    assert sheet._fp == {}
