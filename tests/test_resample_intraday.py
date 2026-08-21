"""`resample_intraday.resample_frame` — the aggregation the 2m/3m sheets stand on.

What can go wrong in a resample is always one of three things: the window grabs a bar
it should not (session straddle), the label lies about which side of the window it
names (a hidden one-bar look-ahead), or an aggregate launders missingness into a value
(NaN volume becoming zero). One test per failure, on synthetic bars.
"""

import numpy as np
import pandas as pd

from resample_intraday import resample_frame


def _minute_frame(index: pd.DatetimeIndex, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(index)
    close = 100.0 + np.cumsum(rng.normal(0, 0.05, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    span = np.abs(rng.normal(0, 0.05, n))
    df = pd.DataFrame({
        "Open": open_,
        "High": np.maximum(open_, close) + span,
        "Low": np.minimum(open_, close) - span,
        "Close": close,
        "Volume": rng.uniform(1e3, 1e5, n),
    }, index=index)
    df.index.name = "Date"
    return df


def _session_index(days: int = 3) -> pd.DatetimeIndex:
    parts = [pd.date_range(f"2024-01-0{d} 09:30", f"2024-01-0{d} 15:59", freq="1min")
             for d in range(2, 2 + days)]
    return parts[0].append(parts[1:])


def test_ohlc_aggregation_is_first_max_min_last():
    df = _minute_frame(pd.date_range("2024-01-02 00:00", periods=9, freq="1min"))
    out = resample_frame(df, 3)
    assert len(out) == 3
    for i in range(3):
        chunk = df.iloc[3 * i: 3 * i + 3]
        assert out["Open"].iloc[i] == chunk["Open"].iloc[0]
        assert out["Close"].iloc[i] == chunk["Close"].iloc[-1]
        assert out["High"].iloc[i] == chunk["High"].max()
        assert out["Low"].iloc[i] == chunk["Low"].min()
        assert np.isclose(out["Volume"].iloc[i], chunk["Volume"].sum(),
                          rtol=1e-12, atol=0.0)


def test_labels_are_window_opens_and_sessions_align():
    """09:30 must start a fresh window at 2m and 3m, and no bar may straddle a night."""
    df = _minute_frame(_session_index())
    for minutes in (2, 3):
        out = resample_frame(df, minutes)
        assert (out.index.minute % minutes == 0).all()
        first_of_day = out.groupby(out.index.date).apply(lambda g: g.index.min())
        assert all(ts.time() == pd.Timestamp("09:30").time() for ts in first_of_day)
        # Every output bar's minutes must come from ONE session: the window that would
        # bridge 16:00 -> 09:30 must not exist.
        assert (out.index.time >= pd.Timestamp("09:30").time()).all()
        assert (out.index.time <= pd.Timestamp("15:59").time()).all()


def test_empty_windows_are_dropped_not_filled():
    idx = pd.date_range("2024-01-02 00:00", periods=10, freq="1min")
    df = _minute_frame(idx).drop(idx[4:8])          # a 4-minute hole
    out = resample_frame(df, 2)
    assert not out["Close"].isna().any()
    assert pd.Timestamp("2024-01-02 00:04") not in out.index
    assert pd.Timestamp("2024-01-02 00:06") not in out.index


def test_all_nan_volume_stays_nan():
    """Crypto's volumeless cache must not resample into 'zero turnover'."""
    df = _minute_frame(pd.date_range("2024-01-02 00:00", periods=12, freq="1min"))
    df["Volume"] = np.nan
    out = resample_frame(df, 3)
    assert out["Volume"].isna().all()


def test_resample_is_tail_truncation_invariant():
    """Cutting future minutes must not change any completed earlier bar."""
    df = _minute_frame(pd.date_range("2024-01-02 00:00", periods=300, freq="1min"))
    full = resample_frame(df, 3)
    cut = resample_frame(df.iloc[:150], 3)          # 150 divides by 3: all bars complete
    pd.testing.assert_frame_equal(full.iloc[:50], cut)
