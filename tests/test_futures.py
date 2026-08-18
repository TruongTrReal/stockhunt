"""The three things about a futures series that a bar-level check cannot catch.

Everything here runs on synthetic bars with a roll planted in a known place, because the
failures being guarded against are *silent*: an unadjusted roll gap is a well-formed bar
that passes every OHLC integrity test in `check_data.py` and simply hands a rule a return
nobody earned. So is a Sunday stub: two hours of trade wearing a real timestamp, which
reads as a very quiet day rather than as a bar that should not exist.

No network, no `data/`, no vendor. `db_loader` is imported for its pure functions only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import db_loader
import futures_specs


# ------------------------------------------------------------------ fixtures

def _daily(n_days: int = 6, start: str = "2020-03-02", roll_day: int | None = 3,
           level: float = 100.0, jump: float = 1.25) -> pd.DataFrame:
    """Daily bars over consecutive weekdays, with an optional contract change.

    The planted roll multiplies the level by `jump`, which is the gap a real roll leaves
    behind and the thing `back_adjust` exists to remove.
    """
    rows, idx = [], []
    price = level
    day = pd.Timestamp(start)
    for d in range(n_days):
        while day.weekday() >= 5:
            day += pd.Timedelta(days=1)
        contract = 1 if (roll_day is None or d < roll_day) else 2
        if roll_day is not None and d == roll_day:
            price *= jump
        price *= 1.004
        rows.append({"Open": price * 0.999, "High": price * 1.002, "Low": price * 0.997,
                     "Close": price, "Volume": 1_000_000.0, "instrument_id": contract})
        idx.append(day)
        day += pd.Timedelta(days=1)
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="Date"))


# ------------------------------------------------------- the Sunday stub

def test_sunday_stub_is_merged_into_the_session_it_opens():
    """CME reopens 17:00 CT Sunday, still Sunday in UTC. That is not a trading day."""
    idx = pd.DatetimeIndex(["2015-06-05", "2015-06-07", "2015-06-08", "2015-06-09"],
                           name="Date")               # Fri, SUN stub, Mon, Tue
    df = pd.DataFrame({
        "Open":  [2098.75, 2092.00, 2091.75, 2080.25],
        "High":  [2102.75, 2093.00, 2093.25, 2084.75],
        "Low":   [2083.50, 2090.00, 2076.25, 2068.75],
        "Close": [2091.50, 2091.50, 2080.50, 2081.75],
        "Volume": [1448855.0, 5600.0, 1247252.0, 1349538.0],
        "instrument_id": [1, 1, 1, 1],
    }, index=idx)

    out = db_loader.merge_session_stubs(df)

    assert list(out.index) == [pd.Timestamp("2015-06-05"), pd.Timestamp("2015-06-08"),
                               pd.Timestamp("2015-06-09")]
    monday = out.loc[pd.Timestamp("2015-06-08")]
    # The sliver IS Monday's first two hours, so it sets Monday's open and widens its
    # range — merging, not dropping, is what keeps the session's true open.
    assert monday["Open"] == pytest.approx(2092.00)
    assert monday["High"] == pytest.approx(2093.25)
    assert monday["Close"] == pytest.approx(2080.50)
    assert monday["Volume"] == pytest.approx(5600.0 + 1247252.0)


def test_weekday_bars_pass_through_the_merge_untouched():
    df = _daily(roll_day=None)
    out = db_loader.merge_session_stubs(df)
    assert len(out) == len(df)
    assert np.allclose(out["Close"].to_numpy(), df["Close"].to_numpy())


def test_a_stub_from_the_outgoing_contract_is_dropped_not_merged():
    """The roll lands on a Monday 41% of the time, and then the sliver is the OLD contract.

    Merging it would give the bar an Open from one instrument and a Close from another,
    displaced by the whole roll gap. `IBS` is `(C-L)/(H-L)` — it reads exactly that.
    """
    idx = pd.DatetimeIndex(["2015-06-05", "2015-06-07", "2015-06-08"], name="Date")
    df = pd.DataFrame({
        "Open":  [100.0, 100.5, 130.0],
        "High":  [101.0, 101.0, 132.0],
        "Low":   [ 99.0,  99.5, 129.0],
        "Close": [100.5, 100.5, 131.0],
        "Volume": [1_000_000.0, 5_000.0, 1_200_000.0],
        "instrument_id": [1, 1, 2],       # the weekend carried the roll
    }, index=idx)

    out = db_loader.merge_session_stubs(df)

    monday = out.loc[pd.Timestamp("2015-06-08")]
    assert monday["Open"] == pytest.approx(130.0)      # the NEW contract's own open
    assert monday["Low"] == pytest.approx(129.0)       # not dragged to the old level
    assert monday["Volume"] == pytest.approx(1_200_000.0)


def test_a_stub_from_the_same_contract_is_still_merged():
    """The drop is narrow on purpose: it is about a contract change, not about Sundays."""
    idx = pd.DatetimeIndex(["2015-06-07", "2015-06-08"], name="Date")
    df = pd.DataFrame({
        "Open":  [100.5, 100.7], "High": [101.0, 102.0], "Low": [99.5, 100.0],
        "Close": [100.6, 101.5], "Volume": [5_000.0, 1_200_000.0],
        "instrument_id": [1, 1],
    }, index=idx)
    out = db_loader.merge_session_stubs(df)
    assert len(out) == 1
    assert out.iloc[0]["Open"] == pytest.approx(100.5)
    assert out.iloc[0]["Volume"] == pytest.approx(1_205_000.0)


def test_a_class_with_no_sunday_bar_is_unchanged():
    """Grains open 19:00 CT, which is already the next UTC day — nothing to merge."""
    idx = pd.DatetimeIndex(["2015-06-05", "2015-06-08", "2015-06-09"], name="Date")
    df = pd.DataFrame({"Open": [1.0, 2.0, 3.0], "High": [1.0, 2.0, 3.0],
                       "Low": [1.0, 2.0, 3.0], "Close": [1.0, 2.0, 3.0],
                       "Volume": [1.0, 1.0, 1.0], "instrument_id": [1, 1, 1]}, index=idx)
    assert db_loader.merge_session_stubs(df).equals(df)


# ------------------------------------------------------------ back-adjustment

def test_back_adjust_removes_the_roll_gap():
    """The return across a roll must be the new contract's return, not the level jump."""
    daily = _daily(roll_day=3, jump=1.25)
    raw_returns = daily["Close"].pct_change()
    assert raw_returns.max() > 0.2          # the fiction is there before adjustment

    adjusted, ledger = db_loader.back_adjust(daily, pd.DataFrame(), "TEST.v.0")
    assert len(ledger) == 1
    assert adjusted["Close"].pct_change().max() < 0.01


def test_back_adjust_preserves_returns_inside_a_contract():
    """Scaling a whole segment by a constant cannot change any return within it."""
    daily = _daily(roll_day=3)
    adjusted, _ = db_loader.back_adjust(daily, pd.DataFrame(), "TEST.v.0")
    before = daily["Close"].pct_change().iloc[1:3]
    after = adjusted["Close"].pct_change().iloc[1:3]
    assert np.allclose(before.to_numpy(), after.to_numpy())


def test_back_adjust_anchors_on_the_newest_contract():
    """The newest bars stay real quotes; history is what moves, by exactly the ratio.

    The direction of the move follows the roll, not the calendar — a contango roll
    scales history up and a backwardation roll scales it down — so the assertion is on
    the factor, not on a sign.
    """
    daily = _daily(roll_day=3)
    adjusted, ledger = db_loader.back_adjust(daily, pd.DataFrame(), "TEST.v.0")
    ratio = float(ledger.loc[0, "ratio"])
    assert adjusted["Close"].iloc[-1] == pytest.approx(daily["Close"].iloc[-1])
    assert adjusted["Close"].iloc[0] == pytest.approx(daily["Close"].iloc[0] * ratio)


def test_same_bar_ratio_is_used_when_rank_one_is_the_contract_rolled_into():
    """The exact adjustment: both contracts priced on the same bar, and it says so."""
    front = _daily(roll_day=3, jump=1.25)
    roll_at = int(np.flatnonzero(np.diff(front["instrument_id"].to_numpy()) != 0)[0]) + 1

    behind = front.copy()
    behind["instrument_id"] = 2                       # rank 1 IS the incoming contract
    behind["Close"] = front["Close"] * 1.3            # ...at a different level

    _, ledger = db_loader.back_adjust(front, behind, "TEST.v.0")
    assert ledger.loc[0, "method"] == "same-bar rank 1"
    assert ledger.loc[0, "ratio"] == pytest.approx(1.3)
    assert roll_at > 0


def test_splice_is_labelled_when_rank_one_is_a_different_contract():
    """Ranks can skip a month. When they do, the adjustment is inexact and is marked."""
    front = _daily(roll_day=3)
    behind = front.copy()
    behind["instrument_id"] = 99                      # not the contract rank 0 became
    _, ledger = db_loader.back_adjust(front, behind, "TEST.v.0")
    assert ledger.loc[0, "method"] == "close-to-close splice"


def test_no_rolls_means_no_adjustment():
    daily = _daily(roll_day=None)
    adjusted, ledger = db_loader.back_adjust(daily, pd.DataFrame(), "TEST.v.0")
    assert ledger.empty
    assert np.allclose(adjusted["Close"].to_numpy(), daily["Close"].to_numpy())


def test_adjusted_frame_drops_the_contract_id():
    """`instrument_id` is scaffolding for the adjustment, not a column any stage reads."""
    adjusted, _ = db_loader.back_adjust(_daily(), pd.DataFrame(), "TEST.v.0")
    assert list(adjusted.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_a_price_through_zero_refuses_rather_than_adjusts():
    """WTI settled at -$37 in April 2020. A ratio adjustment cannot represent that."""
    daily = _daily(roll_day=None)
    daily.loc[daily.index[2], "Low"] = -1.0
    with pytest.raises(ValueError, match="non-positive"):
        db_loader.back_adjust(daily, pd.DataFrame(), "TEST.v.0")


# --------------------------------------------------------------- contract specs

@pytest.mark.parametrize("root, price, expected", [
    ("ES", 6411.25, 320_562.50),      # 50 index points
    ("ZN", 111.234375, 111_234.375),  # percent of $100,000 par
    ("ZC", 438.25, 21_912.50),        # cents per bushel, 5,000 bushels
    ("GC", 3414.85, 341_485.0),       # $100 per troy ounce
    ("6E", 1.15285, 144_106.25),      # 125,000 euros
    ("LE", 220.725, 88_290.0),        # cents per pound, 40,000 pounds
])
def test_notional_matches_the_exchange_contract_size(root, price, expected):
    assert futures_specs.notional_usd(root, price) == pytest.approx(expected)


def test_a_price_scale_wrong_by_a_hundred_is_caught():
    """The guard that stands in for the vendor, which does not publish the quote unit."""
    with pytest.raises(ValueError, match="cents"):
        futures_specs.check_notional("ZC", 43825.0)


def test_every_pooled_symbol_has_a_contract_spec():
    import config
    for symbol in config.CME_POOL:
        futures_specs.spec(symbol.split(".")[0])


def test_no_micro_contract_is_in_the_pool():
    """A micro is its parent at a fifth the size — two symbols, one asset, one return."""
    import config
    pooled = {s.split(".")[0] for s in config.CME_POOL}
    assert not (pooled & set(futures_specs.MICRO_OF))
