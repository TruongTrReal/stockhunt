"""The per-class intraday clock: `config.INTRADAY_CLOCK` and everything that reads it.

The defect these are written from is `commodities`. Twelve Data returned that class's
intraday bars stamped in `Australia/Sydney` and declared nothing (`meta.exchange_timezone`
is `null`), while this repo's docs said UTC and every consumer assumed it. **No bar-level
test could have caught it** — the bars are well formed, the instrument is right, the
sequence is complete, and only the joint between the stamps and the world is wrong. It is
the same family as the foreign namesakes and as EEM's truncated first year.

So the tests here are of two kinds and both are needed:

* the CONVERSION is arithmetic and is tested as arithmetic — including the thing that made
  it hard, which is that Sydney's daylight saving and New York's move in opposite
  directions, so the offset is 10 hours for half the year and 11 for the other half;
* the DETECTION is tested against a synthetic market whose session boundary is known,
  because a check that cannot fail on wrong data is not a check.

Synthetic bars only. Nothing here reads `data/`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import check_data
import config
import migrate_cache_clock
from resample_intraday import resample_frame


# --------------------------------------------------------------- the declaration itself

def test_every_class_declares_a_clock():
    """A class with no row has an UNDECLARED clock, which is the whole defect."""
    assert set(config.INTRADAY_CLOCK) == set(config.CLASSES)
    for cls in config.CLASSES:
        assert config.vendor_tz(cls)
        assert config.cache_tz(cls)


def test_only_commodities_needs_converting():
    """Pins the one class whose two clocks differ, so a silent change to the table
    shows up as a failing test rather than as a restamped cache nobody asked for."""
    differ = [c for c in config.CLASSES if config.vendor_tz(c) != config.cache_tz(c)]
    assert differ == ["commodities"]
    assert config.vendor_tz("commodities") == "Australia/Sydney"
    assert config.cache_tz("commodities") == "UTC"


# ------------------------------------------------------------------------ the conversion

def test_identity_where_the_clocks_agree():
    idx = pd.date_range("2025-07-15", periods=48, freq="1h")
    for cls in ("us_stocks", "us_etfs", "crypto", "cme_futures"):
        out = config.to_cache_clock(idx, cls)
        assert list(out) == list(idx)


@pytest.mark.parametrize("stamp, offset_hours", [
    # Southern-hemisphere winter: Sydney is AEST, UTC+10.
    ("2025-07-15 08:00", 10),
    # Southern-hemisphere summer: Sydney is AEDT, UTC+11. A fixed offset would be wrong
    # here by an hour, which is exactly the bug a `tz_localize` prevents and a
    # `- Timedelta(hours=10)` does not.
    ("2025-01-15 08:00", 11),
])
def test_commodity_offset_follows_sydney_daylight_saving(stamp, offset_hours):
    idx = pd.DatetimeIndex([stamp])
    out = config.to_cache_clock(idx, "commodities")
    assert out[0] == pd.Timestamp(stamp) - pd.Timedelta(hours=offset_hours)


def test_the_weekly_reopen_lands_on_18_00_new_york():
    """The measurement that identified the zone, reproduced on synthetic stamps.

    Spot metals reopen 18:00 Sunday New York. Read as Sydney wall clock, the two stamps
    the real cache actually carries at its weekly gaps -- 08:00 in the northern summer and
    10:00 in the northern winter -- are that one instant, twice.
    """
    # Both are a MONDAY in Sydney: 18:00 Sunday New York is already Monday morning there,
    # which is why the real cache's weekly gaps sit at stamped hour 8 and hour 10.
    for stamp in ("2025-07-14 08:00", "2025-01-13 10:00"):
        utc = config.to_cache_clock(pd.DatetimeIndex([stamp]), "commodities")
        ny = utc.tz_localize("UTC").tz_convert("America/New_York")[0]
        assert (ny.dayofweek, ny.hour) == (6, 18)


def test_conversion_is_a_pure_relabel_of_real_instants():
    """Round trip: a UTC instant, written as Sydney wall clock, comes back unchanged."""
    true_utc = pd.date_range("2025-03-01", periods=24 * 400, freq="1h", tz="UTC")
    vendor = true_utc.tz_convert("Australia/Sydney").tz_localize(None)
    back = config.to_cache_clock(vendor, "commodities")
    assert list(back) == list(true_utc.tz_localize(None))


def test_a_whole_hour_shift_keeps_sub_hour_bars_on_the_utc_grid():
    """Why `1m`..`1h` may be relabelled and `4h` may not, as arithmetic.

    The offset is 10 or 11 hours. A bar size that divides an hour therefore covers exactly
    the same window before and after; `4h` does not, because 10 % 4 == 2.
    """
    for tf in ("1m", "5m", "15m", "30m", "1h"):
        assert migrate_cache_clock.relabel_safe(tf)
    assert not migrate_cache_clock.relabel_safe("4h")

    idx = pd.date_range("2025-07-15 00:00", periods=24, freq="1h")
    out = config.to_cache_clock(idx, "commodities")
    assert set(out.minute) == {0}
    # A relabelled 4h bar would sit at 02:00/06:00/... UTC, which no other class's 4h
    # bars cover. Stated as a test so the next person does not "simplify" the rebuild.
    four = pd.date_range("2025-07-15 00:00", periods=6, freq="4h")
    assert set(config.to_cache_clock(four, "commodities").hour) != {0, 4, 8, 12, 16, 20}


# ---------------------------------------------------------------- the information loss

def test_dst_hazard_counts_what_the_vendor_destroyed():
    """Ambiguous and nonexistent stamps are REPORTED, never silently absorbed.

    Sydney falls back on the first Sunday of April (02:00-03:00 happens twice) and springs
    forward on the first Sunday of October (02:00-03:00 never happens). Both are real
    information loss in the vendor's own stamping and the honest answer is a count.
    """
    fell_back = pd.date_range("2026-04-05 02:00", periods=60, freq="1min")
    assert config.dst_hazard(fell_back, "commodities")["ambiguous"] == 60

    sprang_forward = pd.date_range("2026-10-04 02:00", periods=60, freq="1min")
    assert config.dst_hazard(sprang_forward, "commodities")["nonexistent"] == 60

    # A class that needs no conversion has no hazard to report, whatever its stamps.
    assert config.dst_hazard(fell_back, "crypto") == {"ambiguous": 0, "nonexistent": 0}


def test_a_skipped_hour_does_not_raise():
    """`nonexistent="shift_forward"` rather than an exception.

    Nothing in today's cache lands in Sydney's skipped hour -- the transition is Sunday
    breakfast local, which is Saturday afternoon UTC, when spot metals are shut -- but a
    future fetch must not die on a stamp the vendor should never have produced.
    """
    idx = pd.date_range("2026-10-04 02:00", periods=60, freq="1min")
    out = config.to_cache_clock(idx, "commodities")
    assert len(out) == 60
    assert out.is_monotonic_increasing


# --------------------------------------------------------------------- the frame wrapper

def _bars(index: pd.DatetimeIndex, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0, 0.05, len(index)))
    return pd.DataFrame({"Open": close, "High": close + 0.1, "Low": close - 0.1,
                         "Close": close, "Volume": np.arange(len(index), dtype="float64")},
                        index=index)


def test_frame_restamp_moves_labels_and_nothing_else():
    import td_loader

    df = _bars(pd.date_range("2025-07-15 00:00", periods=500, freq="1min"))
    out = td_loader.to_cache_clock_frame(df, "commodities")
    assert len(out) == len(df)
    assert out.index.is_monotonic_increasing
    for col in ("Open", "High", "Low", "Close", "Volume"):
        assert np.array_equal(out[col].to_numpy(), df[col].to_numpy())
    assert (out.index == df.index - pd.Timedelta(hours=10)).all()


def test_frame_restamp_is_identity_for_a_class_that_needs_none():
    import td_loader

    df = _bars(pd.date_range("2025-07-15 00:00", periods=100, freq="1min"))
    out = td_loader.to_cache_clock_frame(df, "crypto")
    assert list(out.index) == list(df.index)


# ------------------------------------------------------------------------ the detection

def _weekly_market(zone: str, weeks: int = 60) -> pd.DatetimeIndex:
    """Hourly bars for a market that reopens 18:00 Sunday New York and shuts Friday 17:00,
    stamped as naive wall clock in `zone`. This is the shape of a spot metal."""
    # The week openings are built as NEW YORK WALL CLOCK and localised afterwards, never
    # by adding seven days to an absolute instant. Adding a `Timedelta` walks the local
    # time an hour off at every New York DST change, which would build a market whose
    # reopen is not fixed in its own zone -- and then the test would be measuring the
    # generator's drift rather than the check's.
    opens = pd.DatetimeIndex(
        [pd.Timestamp("2023-01-01 18:00") + pd.DateOffset(weeks=w) for w in range(weeks)]
    ).tz_localize("America/New_York")
    idx = pd.DatetimeIndex([])
    for open_ in opens:
        idx = idx.append(pd.date_range(open_, periods=119, freq="1h"))
    return pd.DatetimeIndex(idx).tz_convert(zone).tz_localize(None)


def test_the_check_passes_a_correctly_stamped_cache():
    fit, label = check_data.clock_fit(_weekly_market("UTC"), "UTC", "America/New_York")
    assert fit > 0.95
    assert label.startswith("Sun 18:00")


def test_the_check_names_the_zone_a_wrongly_stamped_cache_is_really_in():
    """The bug, reproduced end to end: the same market, stamped in Sydney, read as UTC.

    The declared clock smears the reopen across the year because Sydney's daylight saving
    and New York's move in opposite directions; `Australia/Sydney` puts it back on one
    instant. That comparison is what identified the zone rather than merely suspecting it.
    """
    idx = _weekly_market("Australia/Sydney")
    declared, _ = check_data.clock_fit(idx, "UTC", "America/New_York")
    truth, label = check_data.clock_fit(idx, "Australia/Sydney", "America/New_York")
    assert declared < 0.7
    assert truth > 0.95
    assert label.startswith("Sun 18:00")
    assert truth - declared > check_data.CLOCK_FIT_MARGIN


def test_clock_verdict_fails_the_wrong_clock_and_passes_the_right_one():
    bad = check_data.clock_verdict(_weekly_market("Australia/Sydney"), "commodities")
    assert bad["ok"] is False
    assert bad["best_tz"] == "Australia/Sydney"

    good = check_data.clock_verdict(_weekly_market("UTC"), "commodities")
    assert good["ok"] is True
    assert good["best_tz"] == "UTC"


def test_a_class_with_no_weekly_boundary_is_not_judged():
    """Crypto trades 24/7, so there is no session instant to check against. Saying
    'unjudgeable' is the honest answer; saying 'pass' would be a claim."""
    assert config.session_anchor_tz("crypto") is None
    assert check_data.clock_verdict(_weekly_market("UTC"), "crypto") is None


# ------------------------------------------------------------- rebuilding the 4h grid

def test_four_hour_bars_rebuilt_from_hourly_sit_on_the_utc_grid():
    """The reason `4h` is re-derived instead of relabelled: it has to land on the same
    windows every other class's 4h bars cover."""
    hourly = _bars(pd.date_range("2025-07-14 00:00", periods=24 * 30, freq="1h"))
    out = resample_frame(hourly, 240)
    assert set(out.index.hour) <= {0, 4, 8, 12, 16, 20}
    assert (out.index.minute == 0).all()
    # And it is an aggregation, not a resample of a resample: the first 4h bar's open is
    # the first hour's open and its high is the max of the four.
    first = hourly.iloc[:4]
    assert out["Open"].iloc[0] == first["Open"].iloc[0]
    assert out["High"].iloc[0] == first["High"].max()
