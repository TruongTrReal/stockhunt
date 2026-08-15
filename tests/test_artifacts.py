"""`stockhunt.artifacts` — how a long diagnostic table gets written.

The rule is that a table nothing reads by literal name is written as Parquet, and **the
superseded CSV is removed rather than left beside it**. A stale twin of a generated file
is a failure this repo already documents for `report/index.html` and `web/data.js`: a
reader picking the wrong one gets last month's numbers and nothing anywhere says so.

So the two tests that matter are that the CSV twin goes, and that it only goes *after*
the Parquet is on disk.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stockhunt import artifacts

pytest.importorskip("pyarrow", reason="Parquet needs pyarrow")


@pytest.fixture
def table() -> pd.DataFrame:
    """The real shape: rule x symbol x scenario x fold, three low-cardinality strings
    repeating beside two high-entropy floats."""
    rng = np.random.default_rng(0)
    n = 2000
    return pd.DataFrame({
        "rule": rng.choice(["SMA_50", "RSI_14", "ibs"], n),
        "symbol": rng.choice(["AAPL", "MSFT", "JNJ"], n),
        "scenario": rng.choice(["gross", "retail"], n),
        "fold": rng.integers(0, 24, n),
        "ir_is": rng.normal(0, 0.5, n),
        "ir_oos": rng.normal(0, 0.5, n),
    })


def test_write_bulk_produces_a_parquet(tmp_path, table):
    path = tmp_path / "per_asset_us_stocks_1d.parquet"
    artifacts.write_bulk(table, path)
    assert path.exists()
    pd.testing.assert_frame_equal(pd.read_parquet(path), table)


def test_the_round_trip_is_bit_exact_including_the_float64_mantissas(tmp_path, table):
    """The decimal spelling goes; the precision does not."""
    path = tmp_path / "t.parquet"
    artifacts.write_bulk(table, path)
    back = artifacts.read_bulk(path)
    for col in ("ir_is", "ir_oos"):
        assert back[col].to_numpy().tobytes() == table[col].to_numpy().tobytes()


def test_write_bulk_removes_the_superseded_csv_twin(tmp_path, table):
    """`sweep.py` went on writing a 166 MB `per_asset_us_stocks_4h.csv` that no code in
    the repo reads. A stale twin is worse than a large one."""
    path = tmp_path / "per_asset.parquet"
    csv = tmp_path / "per_asset.csv"
    csv.write_text("rule,ir\nSTALE,999\n")
    artifacts.write_bulk(table, path)
    assert path.exists()
    assert not csv.exists()


def test_write_bulk_does_not_drop_the_csv_when_the_parquet_write_fails(tmp_path, table):
    """The removal only happens after the Parquet is on disk — otherwise a failed write
    loses the data outright."""
    csv = tmp_path / "per_asset.csv"
    csv.write_text("rule,ir\nREAL,1.0\n")
    bad = tmp_path / "per_asset.parquet"

    class Unwritable:
        def to_parquet(self, *a, **kw):
            raise OSError("disk full")

    with pytest.raises(OSError):
        artifacts.write_bulk(Unwritable(), bad)
    assert csv.exists()


def test_write_bulk_creates_missing_parent_directories(tmp_path, table):
    path = tmp_path / "results" / "nested" / "t.parquet"
    artifacts.write_bulk(table, path)
    assert path.exists()


def test_write_bulk_does_not_write_the_index(tmp_path, table):
    path = tmp_path / "t.parquet"
    artifacts.write_bulk(table.set_index("rule"), path)
    assert "rule" not in pd.read_parquet(path).columns


def test_read_bulk_prefers_the_parquet_over_a_csv_of_the_same_stem(tmp_path, table):
    stem = tmp_path / "t"
    artifacts.write_bulk(table, stem.with_suffix(".parquet"))
    stem.with_suffix(".csv").write_text("rule,ir\nSTALE,999\n")
    got = artifacts.read_bulk(stem.with_suffix(".csv"))
    assert len(got) == len(table)
    assert "STALE" not in set(got["rule"])


def test_read_bulk_falls_back_to_a_csv_written_before_the_migration(tmp_path):
    """A reader must keep working against sheets written before the change."""
    csv = tmp_path / "t.csv"
    csv.write_text("rule,ir\nSMA_50,0.5\n")
    got = artifacts.read_bulk(csv)
    assert list(got["rule"]) == ["SMA_50"]


def test_read_bulk_accepts_either_spelling_of_the_path(tmp_path, table):
    stem = tmp_path / "t"
    artifacts.write_bulk(table, stem.with_suffix(".parquet"))
    for suffix in (".parquet", ".csv", ""):
        assert artifacts.read_bulk(stem.with_suffix(suffix)) is not None


def test_read_bulk_returns_none_when_the_sheet_was_never_produced(tmp_path):
    """Callers already treat this as 'this sheet does not exist'."""
    assert artifacts.read_bulk(tmp_path / "never_written.parquet") is None


def test_wfo_paths_delegates_rather_than_reimplementing(tmp_path, table):
    """`wfo_paths.write_bulk` still exists so `walk-forward optimization/` keeps working.
    It must DELEGATE — this helper moved out of that module precisely so both sides of
    the pipeline could reach one implementation, and a second copy would drift.

    The WFO folder goes on the path here rather than in `conftest`, so importing it
    cannot reorder anything for the rest of the suite.
    """
    import sys

    from stockhunt import paths
    sys.path.insert(0, str(paths.WFO))
    try:
        import wfo_paths
    finally:
        sys.path.remove(str(paths.WFO))

    wfo_paths.write_bulk(table, tmp_path / "t.parquet")
    assert (tmp_path / "t.parquet").exists()

    # Same behaviour, reached through the delegate: the CSV twin still goes.
    csv = tmp_path / "u.csv"
    csv.write_text("rule,ir\nSTALE,999\n")
    wfo_paths.write_bulk(table, tmp_path / "u.parquet")
    assert not csv.exists()
