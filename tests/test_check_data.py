"""`backtest engine/check_data.py` — the scans that stand between the vendor and a sweep.

Every check here exists because something got through. The tests are written from the
recorded incidents rather than from the code, so each one names the series it is modelled
on and would have caught it:

* BTC printing 2.812 instead of 28,100 for one minute — 126 of them turned buy-and-hold
  into 1e125, and structural validation saw nothing wrong;
* `BMS` falling -99% then recovering +9,900%, where ranking on |log return| picks the
  crash leg and passes the explosive one straight through;
* `LTC/USD` going quiet for 157 days and booking +387% in "one day" on the bar back;
* `FL` — a $0.39 stock trading $7,494/day wearing Foot Locker's ticker, internally
  consistent in every respect, which `ibs` compounded to 6.4e17%.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import check_data
from config import MIN_PRICE_USD

from conftest import make_ohlcv


def frame(close, volume=None, start="2015-01-05", freq="D") -> pd.DataFrame:
    close = np.asarray(close, dtype="float64")
    n = len(close)
    df = pd.DataFrame(
        {"Open": close, "High": close * 1.001, "Low": close * 0.999, "Close": close},
        index=pd.date_range(start, periods=n, freq=freq))
    df["Volume"] = np.full(n, 1e6) if volume is None else np.asarray(volume,
                                                                     dtype="float64")
    return df


# ------------------------------------------------------------------- spike_mask

def test_spike_mask_catches_a_decimal_point_misprint():
    """BTC prints 2.812 instead of 28,100 for one minute, then recovers. Raw returns
    self-cancel so `prod(1+r)` still telescopes to the right answer — nothing looks
    wrong until the -0.999 floor clips the crash leg and not the recovery."""
    close = np.full(50, 28_100.0)
    close[25] = 2.812
    mask = check_data.spike_mask(close)
    assert mask[25]
    assert mask.sum() == 1


def test_spike_mask_catches_an_upward_misprint():
    close = np.full(50, 28.10)
    close[25] = 28_100.0
    assert check_data.spike_mask(close)[25]


def test_spike_mask_ignores_a_real_crash():
    """A backtest that has never seen a crash is worth nothing, so a genuine -40% bar
    must survive."""
    close = np.concatenate([np.full(30, 100.0), np.full(30, 60.0)])
    assert not check_data.spike_mask(close).any()


def test_spike_mask_ignores_an_ordinary_random_walk():
    assert not check_data.spike_mask(
        make_ohlcv(1000, seed=3)["Close"].to_numpy()).any()


def test_spike_mask_uses_a_centred_window():
    """Centred, so a spike is measured against the bars on BOTH sides — a trailing median
    would let the spike drag its own reference."""
    close = np.full(21, 100.0)
    close[10] = 0.01
    mask = check_data.spike_mask(close)
    assert mask[10] and mask.sum() == 1


# ----------------------------------------------------------------------- faults

def test_faults_flags_every_impossible_ohlc_relation():
    df = frame(np.full(6, 100.0))
    df.iloc[1, df.columns.get_loc("High")] = 50.0      # high < low, and < open/close
    df.iloc[2, df.columns.get_loc("Low")] = 150.0      # low > open/close
    df.iloc[3, df.columns.get_loc("Close")] = 0.0      # non-positive
    df.iloc[4, df.columns.get_loc("Open")] = np.nan    # non-finite

    f = check_data.faults(df)
    assert f["high_lt_low"][1] and f["high_lt_close"][1]
    assert f["low_gt_close"][2]
    assert f["nonpositive_close"][3]
    assert f["nonfinite"][4]


def test_a_clean_frame_has_no_faults():
    f = check_data.faults(make_ohlcv(500, seed=4))
    assert not any(v.any() for v in f.values())


# ----------------------------------------------------------------------- repair

def test_repair_drops_a_spike_rather_than_adjusting_it():
    """A bar printing 1/10,000 of the true price is not a mispriced extreme, it is a bar
    that did not happen. Interpolating would invent a trade."""
    close = np.full(50, 100.0)
    close[25] = 0.01
    out, n = check_data.repair(frame(close))
    assert len(out) == 49
    assert n >= 1
    assert (out["Close"] > 0).all()


def test_repair_drops_non_positive_closes():
    """`CBE` arrives with 3,894 of 7,368 closes at or below zero; its raw series produces
    a +4,430% bar and a buy-and-hold equity of 3.03e-07."""
    close = np.full(50, 100.0)
    close[10] = 0.0
    close[20] = -5.0
    out, _ = check_data.repair(frame(close))
    assert len(out) == 48
    assert (out["Close"] > 0).all()


def test_repair_widens_high_and_low_to_contain_open_and_close():
    """Open and Close are prices actually transacted at, so a High below either is a fault
    in the reported extreme. Widening can only shrink an ATR, never invent a move."""
    df = frame(np.full(5, 100.0))
    df.iloc[2, df.columns.get_loc("High")] = 99.0
    df.iloc[3, df.columns.get_loc("Low")] = 101.0
    out, n = check_data.repair(df)
    assert out["High"].iloc[2] >= max(out["Open"].iloc[2], out["Close"].iloc[2])
    assert out["Low"].iloc[3] <= min(out["Open"].iloc[3], out["Close"].iloc[3])
    assert n >= 2


def test_repair_leaves_a_clean_frame_untouched():
    df = make_ohlcv(400, seed=6)
    out, n = check_data.repair(df)
    assert n == 0
    assert len(out) == len(df)
    pd.testing.assert_frame_equal(out, df)


def test_repair_does_not_mutate_its_input():
    df = frame(np.concatenate([np.full(25, 100.0), [0.01], np.full(24, 100.0)]))
    before = df.copy()
    check_data.repair(df)
    pd.testing.assert_frame_equal(df, before)


# ------------------------------------------------------------ unusable_fraction

def test_unusable_fraction_counts_impossible_closes():
    close = np.full(100, 100.0)
    close[:5] = -1.0
    assert check_data.unusable_fraction(frame(close)) == pytest.approx(0.05)


def test_unusable_fraction_of_an_empty_frame_is_total():
    assert check_data.unusable_fraction(frame([])) == 1.0


# --------------------------------------------------------- implausible_return

def test_implausible_return_measures_the_EXPLOSIVE_direction():
    """The `BMS` bug, pinned. A -99% bar and a +9,900% bar are log -4.605 and +4.605 —
    exactly equal in magnitude — so `argmax(|log r|)` picks the crash leg, reports -99%,
    and a threshold on that passes it while the +9,900% leg walks into the sweep.

    The asymmetry is the point: a simple return is floored at -100% but unbounded above,
    and `RETURN_FLOOR` clips the crash leg while leaving the recovery intact.
    """
    close = np.full(200, 100.0)
    close[100] = 1.0                       # -99%
    close[101] = 100.0                     # +9,900%
    _, raw = check_data.implausible_return(frame(close))
    assert raw == pytest.approx(99.0, rel=1e-6)
    assert raw > 0


def test_implausible_return_is_quiet_on_an_ordinary_series():
    _, raw = check_data.implausible_return(make_ohlcv(1000, seed=8))
    assert abs(raw) < check_data.SPIKE_QUARANTINE


def test_implausible_return_needs_thirty_bars():
    assert check_data.implausible_return(frame(np.full(10, 100.0))) == (0.0, 0.0)


def test_implausible_return_uses_a_robust_scale():
    """MAD rather than SD, because the outlier being hunted is itself in the sample and
    would inflate an SD enough to hide behind."""
    rng = np.random.default_rng(9)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 500)))
    close[250:] *= 20.0                    # a single +1,900% jump
    sigma, raw = check_data.implausible_return(frame(close))
    assert raw == pytest.approx(19.0, rel=0.05)
    assert sigma > check_data.SIGMA_REPORT


# ------------------------------------------------------------------- gap_break

def test_gap_break_finds_the_hole_a_series_resumes_after():
    """`LTC/USD` stops on 2020-09-13 at $48.68 and resumes 157 days later at $237.11, so
    the first bar back books +387% in 'one day' for a rally that took five months."""
    early = pd.date_range("2020-01-01", periods=100, freq="D")
    late = pd.date_range("2021-02-17", periods=100, freq="D")
    df = frame(np.full(200, 100.0))
    df.index = early.append(late)
    assert check_data.gap_break(df, 7) == late[0]


def test_gap_break_returns_the_LAST_oversized_break():
    """Everything before it is unreachable anyway, so the usable series starts after the
    final hole."""
    a = pd.date_range("2020-01-01", periods=50, freq="D")
    b = pd.date_range("2020-06-01", periods=50, freq="D")
    c = pd.date_range("2021-06-01", periods=50, freq="D")
    df = frame(np.full(150, 100.0))
    df.index = a.append(b).append(c)
    assert check_data.gap_break(df, 7) == c[0]


def test_gap_break_is_none_on_a_continuous_series():
    assert check_data.gap_break(frame(np.full(200, 100.0)), 7) is None


def test_gap_break_tolerates_a_weekend():
    df = frame(np.full(200, 100.0), freq="B")
    assert check_data.gap_break(df, 7) is None


def test_equities_are_deliberately_absent_from_the_gap_rule():
    """The trap: 'keep the segment after the hole' is right for a vendor outage and
    exactly backwards for a REISSUED ticker, where the later segment is the impostor.
    A full scan proposed truncating BBBY to 2025-08-22 — the wrong half."""
    assert "us_stocks" not in check_data.MAX_GAP_DAYS
    assert "us_etfs" not in check_data.MAX_GAP_DAYS
    assert set(check_data.MAX_GAP_DAYS) == {"crypto", "commodities"}


# ------------------------------------------------------------ peak_dollar_volume

def test_peak_dollar_volume_is_a_median_inside_a_window_and_a_max_across_them():
    """One busy week cannot rescue a series that was never liquid, and one quiet year
    cannot condemn one that was."""
    n = 1500
    vol = np.full(n, 1_000.0)
    vol[500:800] = 1_000_000.0             # one genuinely liquid year
    dv = check_data.peak_dollar_volume(frame(np.full(n, 100.0), vol))
    assert dv is not None
    assert dv == pytest.approx(1e8, rel=0.5)


def test_peak_dollar_volume_is_none_when_the_claim_is_untestable():
    assert check_data.peak_dollar_volume(frame(np.full(50, 100.0))) is None


def test_peak_dollar_volume_is_none_without_a_volume_column():
    df = frame(np.full(500, 100.0)).drop(columns=["Volume"])
    assert check_data.peak_dollar_volume(df) is None


def test_peak_dollar_volume_is_a_per_DAY_figure_not_a_per_bar_one():
    """Summed to calendar days before any median, so the same threshold means the same
    thing on 1d and 5m — a per-bar figure would quarantine an intraday sheet for being
    sliced thinner."""
    daily = frame(np.full(600, 100.0), np.full(600, 10_000.0), freq="D")
    # `freq="h"` covers all 24 hours of a calendar day, so the same dollars per day are
    # spread across 24 bars rather than 8.
    hourly = frame(np.full(600 * 24, 100.0), np.full(600 * 24, 10_000.0 / 24), freq="h")
    a = check_data.peak_dollar_volume(daily)
    b = check_data.peak_dollar_volume(hourly)
    assert a is not None and b is not None
    assert b == pytest.approx(a, rel=0.05)


# -------------------------------------------------------------- quarantine_reason

def test_a_clean_liquid_equity_is_admitted():
    df = make_ohlcv(1500, seed=10)
    df["Volume"] = 5_000_000.0             # ~$500M/day at ~$100
    assert check_data.quarantine_reason(df, "us_stocks") is None


def test_a_ticker_recycling_impostor_is_quarantined():
    """`FL` traded $7,494/day at $0.39 across the window Foot Locker was in the S&P 500.
    Nothing else catches it: the series is internally consistent, passes the OHLC scan,
    the spike scan and the payload validator, and `--fix` cheerfully repairs bars that
    were never the right company's."""
    n = 1500
    # Priced above the $1 floor on purpose, so this exercises the LIQUIDITY branch and
    # not the price one — `ARG`, the fattest impostor, was a $3.4M/day name, and the
    # thinnest real member `NWS` was $32.1M/day. The two populations do not touch.
    df = frame(np.full(n, 5.00), np.full(n, 1_000.0))       # $5k/day
    reason = check_data.quarantine_reason(df, "us_stocks")
    assert reason is not None
    assert "not the member this ticker names" in reason


def test_a_sub_dollar_share_is_quarantined_on_its_LATEST_close():
    """Not the median and not any historical bar: under `adjust=all` every earlier price
    is today's share reflated backwards, so only the last one is a number anybody could
    pay. Applied to the stored history instead, this deletes NVDA and catches nothing."""
    close = np.concatenate([np.full(1400, 500.0), np.full(100, 0.50)])
    df = frame(close, np.full(1500, 5_000_000.0))
    reason = check_data.quarantine_reason(df, "us_stocks")
    assert reason is not None and "floor" in reason


def test_a_high_priced_share_with_a_cheap_history_is_not_quarantined_on_price():
    """The NVDA case: 62% of post-2000 bars under $1 once reflated through its splits."""
    close = np.concatenate([np.full(1400, 0.50), np.full(100, 500.0)])
    df = frame(close, np.full(1500, 5_000_000.0))
    reason = check_data.quarantine_reason(df, "us_stocks")
    assert reason is None or "floor" not in reason


def test_a_surviving_ten_x_bar_is_quarantined():
    close = np.full(500, 100.0)
    close[250:] = 100_000.0                # +99,900% in one bar
    reason = check_data.quarantine_reason(frame(close), "crypto")
    assert reason is not None and "surviving" in reason


def test_mostly_impossible_closes_are_quarantined_not_repaired():
    """What survives a heavy repair is a different instrument, with returns measured
    across the holes."""
    close = np.full(500, 100.0)
    close[:100] = -1.0
    reason = check_data.quarantine_reason(frame(close), "crypto")
    assert reason is not None and "impossible closes" in reason


def test_the_liquidity_test_is_applied_to_equities_only():
    """Thin is a legitimate design for an ETF — `COPX` is really $837k/day — and crypto
    serves no volume at all."""
    df = frame(np.full(1500, 50.0), np.full(1500, 100.0))   # $5k/day
    assert check_data.quarantine_reason(df, "us_stocks") is not None
    for other in ("us_etfs", "crypto", "commodities", None):
        assert check_data.quarantine_reason(df, other) is None


def test_too_little_history_is_unknown_not_fail():
    """The 2026 spin-offs `FDXF`/`HONA` are exempt, not condemned."""
    df = frame(np.full(40, 50.0), np.full(40, 100.0))
    assert check_data.quarantine_reason(df, "us_stocks") is None


def test_the_thresholds_are_the_documented_ones():
    assert check_data.DOLLAR_VOLUME_QUARANTINE == 10_000_000.0
    assert check_data.SPIKE_QUARANTINE == 9.0
    assert check_data.EQUITY_CLASSES == ("us_stocks",)
    assert MIN_PRICE_USD == 1.00


# ------------------------------------------------------------------ fat_tail_note

def test_a_large_but_plausible_extreme_is_reported_and_never_excluded():
    """BTC's -19% 4h bar is 30 robust sigma and every one of those must stay in the
    sample. Sigma is a flag for a human, never a filter."""
    rng = np.random.default_rng(11)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.002, 800)))
    close[400:] *= 2.0                     # +100%: big, but under the 900% line
    df = frame(close)
    assert check_data.fat_tail_note(df) is not None
    assert check_data.quarantine_reason(df, "crypto") is None


def test_no_note_on_an_ordinary_series():
    assert check_data.fat_tail_note(make_ohlcv(1000, seed=12)) is None
