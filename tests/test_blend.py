"""`stockhunt.blend` — combining N leg curves into one book.

Synthetic curves only, like the rest of this suite: nothing here opens
`walk-forward optimization/results/`, because a test that fails when somebody re-runs
`run_book.sh` is a test nobody will trust. The loader tests build a two-rule sheet in
`tmp_path` instead, which exercises the same code path against a file whose contents the
test wrote.

Every test here is arithmetic that can be worked out on paper, not plumbing. The blend is
a weighted compounding of known series, so a passing test that only asserted "it returned
a dict shaped like this" would go on passing through a sign error in the rebalance. The
two constructions that do most of the work:

* **Mirror legs.** Leg A earns +10%, -10%, +10%, ...; leg B earns exactly the opposite.
  Rebalanced every bar the book earns 0 and finishes at its capital, to the cent. Left
  alone, each leg compounds 1.1 x 0.9 = 0.99 per pair and the book bleeds. The gap between
  the two is the rebalancing bonus, and it has a closed form.
* **Constant-return legs.** Two legs on flat per-bar returns make both schedules exact:
  the rebalanced book compounds the average return, the unrebalanced one is the sum of two
  independently compounded halves.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd
import pytest

from stockhunt import blend

CAPITAL = 100_000.0


# --------------------------------------------------------------------------- helpers

def leg(returns, start: str = "2010-01-31", freq: str = "ME", bench=None,
        cls: str = "", tf: str = "", rule: str = "") -> dict:
    """A leg whose curve compounds `returns`, one bar per entry after inception.

    Month-end dates by default, so every bar begins a new calendar month and the monthly
    reset fires on every one of them. That is what makes the rebalanced arithmetic exact
    rather than schedule-dependent; the tests that care about *when* the reset fires ask
    for daily bars explicitly.
    """
    def curve_of(r):
        return 100.0 * np.concatenate([[1.0], np.cumprod(1.0 + np.asarray(r, "float64"))])

    eq = curve_of(returns)
    idx = pd.date_range(start, periods=eq.size, freq=freq)
    return blend.make_leg(idx, eq, None if bench is None else curve_of(bench),
                          cls=cls, tf=tf, rule=rule)


def alternating(n: int, first: float = 0.10) -> np.ndarray:
    """`+a, -a, +a, ...`, n entries."""
    return np.array([first if i % 2 == 0 else -first for i in range(n)])


def write_sheet(tmp_path, cls: str, tf: str, rules: dict) -> None:
    """A curve file of the shape `portfolio_wf --curves` writes."""
    path = tmp_path / f"book_curves_{cls}_{tf}.json"
    path.write_text(json.dumps(rules), encoding="utf-8")


def sheet_entry(dates, curve, bench=None, **extra) -> dict:
    entry = {"dates": list(dates), "curve": list(curve),
             "bench": None if bench is None else list(bench)}
    entry.update(extra)
    return entry


# ------------------------------------------------------------------ the blend arithmetic

def test_one_leg_is_that_leg_scaled_to_the_capital():
    """The degenerate portfolio. Any wrapper effect would show up here first."""
    r = np.array([0.05, -0.02, 0.03, 0.01, -0.04, 0.02])
    one = leg(r)
    out = blend.blend([one], capital=CAPITAL)

    expected = CAPITAL * np.concatenate([[1.0], np.cumprod(1.0 + r)])
    assert out["curve"] == pytest.approx(list(expected), rel=1e-12)
    assert out["n_legs"] == 1
    assert out["legs"][0]["weight_initial"] == 1.0
    assert out["corr"]["matrix"] == [[pytest.approx(1.0)]]


def test_two_copies_of_one_curve_reproduce_that_curve():
    """Splitting a pot across two identical legs cannot change what the pot does.

    Rebalancing is a no-op between legs that never drift apart, so this holds under both
    schedules — and a rebalance that moved money when the weights were already equal would
    break it.
    """
    r = np.array([0.04, -0.03, 0.06, -0.01, 0.02, 0.05, -0.07])
    expected = CAPITAL * np.concatenate([[1.0], np.cumprod(1.0 + r)])
    for schedule in ("monthly", "none"):
        out = blend.blend([leg(r, rule="a"), leg(r, rule="b")],
                          capital=CAPITAL, rebalance=schedule)
        assert out["curve"] == pytest.approx(list(expected), rel=1e-12)


def test_equal_weight_rebalanced_book_compounds_the_average_return():
    """Two constant-return legs, reset to equal every bar: the book earns the mean.

    +2% and -1% average to +0.5%, so after n bars the pot is exactly `1.005 ** n`. Nothing
    about the legs' own compounding survives a reset every bar, which is the whole point of
    the schedule.
    """
    n = 48
    out = blend.blend([leg(np.full(n, 0.02), rule="up"),
                       leg(np.full(n, -0.01), rule="down")],
                      capital=CAPITAL, rebalance="monthly")

    assert out["axis"]["rebalances"] == n           # every bar after inception
    assert out["metrics"]["final_value"] == pytest.approx(CAPITAL * 1.005 ** n, rel=1e-12)


def test_unrebalanced_book_is_two_halves_compounding_alone():
    """The same legs with the split left to drift: each half compounds on its own."""
    n = 48
    out = blend.blend([leg(np.full(n, 0.02), rule="up"),
                       leg(np.full(n, -0.01), rule="down")],
                      capital=CAPITAL, rebalance="none")

    expected = CAPITAL / 2 * 1.02 ** n + CAPITAL / 2 * 0.99 ** n
    assert out["axis"]["rebalances"] == 0
    assert out["metrics"]["final_value"] == pytest.approx(expected, rel=1e-12)


def test_rebalancing_bonus_on_mirror_legs_has_the_hand_derived_value():
    """The direction *and* the magnitude, from two legs that cancel exactly.

    Leg A earns +10%, -10%, ...; leg B the opposite. Every bar the two returns sum to zero,
    so an equal-weight book reset each bar earns zero and ends at its capital. Left alone,
    each leg compounds 1.1 x 0.9 = 0.99 per PAIR of bars, so after `2k` bars both halves —
    and therefore the whole pot — sit at `0.99 ** k`.

    The gap is the rebalancing bonus and it is `capital * (1 - 0.99 ** k)`, which is
    positive: the reset sells whichever leg just won. That is a real effect of the
    schedule, not a cost model, and it is why the schedule has to be stated with any
    number this module produces.
    """
    k = 24
    n = 2 * k
    legs = [leg(alternating(n, 0.10), rule="a"), leg(-alternating(n, 0.10), rule="b")]

    rebalanced = blend.blend(legs, capital=CAPITAL, rebalance="monthly")
    drifting = blend.blend(legs, capital=CAPITAL, rebalance="none")

    assert rebalanced["metrics"]["final_value"] == pytest.approx(CAPITAL, rel=1e-12)
    assert drifting["metrics"]["final_value"] == pytest.approx(
        CAPITAL * 0.99 ** k, rel=1e-12)

    bonus = (rebalanced["metrics"]["final_value"] - drifting["metrics"]["final_value"])
    assert bonus == pytest.approx(CAPITAL * (1.0 - 0.99 ** k), rel=1e-12)
    assert bonus > 0.0


def test_contributions_are_dollars_that_sum_to_the_books_return():
    """Per-leg attribution has to add up, or it is decoration.

    Rebalancing moves money between legs but creates none, so the legs' P&L must still
    telescope to the pot's. A share-of-profit ratio would not survive the losing book
    below: it would report the profitable leg as a negative contributor.
    """
    n = 36
    out = blend.blend([leg(np.full(n, 0.01), rule="winner"),
                       leg(np.full(n, -0.03), rule="loser")], capital=CAPITAL)

    assert sum(r["pnl"] for r in out["legs"]) == pytest.approx(
        out["metrics"]["final_value"] - CAPITAL, rel=1e-10)
    assert sum(r["contribution"] for r in out["legs"]) == pytest.approx(
        out["metrics"]["total_return"], rel=1e-10)
    assert out["metrics"]["total_return"] < 0            # the book lost
    by_rule = {r["rule"]: r for r in out["legs"]}
    assert by_rule["winner"]["contribution"] > 0         # ...the winner still reads as one


# ------------------------------------------------------------------ the rebalance schedule

def test_monthly_reset_fires_on_the_first_bar_of_each_calendar_month():
    """On a daily axis the schedule is a real subset of the bars, not every one of them."""
    n = 200
    one = leg(np.full(n, 0.001), start="2010-01-04", freq="D", rule="a")
    two = leg(np.full(n, 0.002), start="2010-01-04", freq="D", rule="b")
    out = blend.blend([one, two], capital=CAPITAL, rebalance="monthly")

    axis = pd.to_datetime(out["dates"])
    months = axis.to_period("M")
    # One reset per month boundary crossed. Bar 0 is inception and is never counted: the
    # book is already equal-weight there, and counting it would report a trade nobody made.
    assert out["axis"]["rebalances"] == int((months[1:] != months[:-1]).sum())
    assert 0 < out["axis"]["rebalances"] < axis.size - 1


def test_an_unknown_schedule_is_refused_rather_than_ignored():
    r = np.full(24, 0.01)
    with pytest.raises(blend.BlendError, match="rebalance schedule"):
        blend.blend([leg(r, rule="a"), leg(r, rule="b")], rebalance="quarterly")


# ------------------------------------------------------------------ the correlation matrix

def test_a_leg_correlates_one_with_itself_and_with_its_twin():
    r = np.array([0.03, -0.02, 0.05, -0.01, 0.04, -0.06, 0.02, 0.01])
    out = blend.blend([leg(r, rule="a"), leg(r, rule="b")])
    matrix = out["corr"]["matrix"]

    assert matrix[0][0] == pytest.approx(1.0)
    assert matrix[1][1] == pytest.approx(1.0)
    # The number the whole matrix exists for: two identical picks are one bet, and it says
    # so rather than reporting two names as diversification.
    assert matrix[0][1] == pytest.approx(1.0)


def test_mirror_legs_correlate_minus_one():
    n = 30
    out = blend.blend([leg(alternating(n, 0.08), rule="a"),
                       leg(-alternating(n, 0.08), rule="b")])
    assert out["corr"]["matrix"][0][1] == pytest.approx(-1.0)


def test_legs_off_one_sheet_are_flagged_as_one_universe():
    r = np.array([0.02, -0.01, 0.03, 0.01, -0.02, 0.04])
    out = blend.blend([leg(r, cls="us_stocks", tf="1d", rule="RSI"),
                       leg(-r, cls="us_stocks", tf="1d", rule="MACD")])
    assert any("same sheet (us_stocks/1d)" in w for w in out["warnings"])

    mixed = blend.blend([leg(r, cls="us_stocks", tf="1d", rule="RSI"),
                         leg(-r, cls="crypto", tf="1d", rule="MACD")])
    assert not any("same sheet" in w for w in mixed["warnings"])


# ------------------------------------------------------------------ aligning the axes

def test_the_span_is_the_intersection_and_never_the_union():
    """A short leg and a long one make a short measurement, and the result says so."""
    short = leg(np.full(60, 0.004), start="2010-01-31", rule="short")     # 5 years
    long_ = leg(np.full(300, 0.003), start="2000-01-31", rule="long")     # 25 years
    out = blend.blend([short, long_])

    assert out["axis"]["start"] == "2010-01-31"
    assert out["axis"]["end"] == short["dates"][-1].strftime("%Y-%m-%d")
    assert out["axis"]["years"] == pytest.approx(5.0, abs=0.1)

    by_rule = {r["rule"]: r for r in out["legs"]}
    # The discarded history is reported, not merely dropped: the long leg's own 25 years
    # are visible beside the 5 the portfolio actually measures.
    assert by_rule["long"]["own_start"] == "2000-01-31"
    assert by_rule["long"]["own_years"] > out["axis"]["years"] * 4
    assert by_rule["short"]["own_years"] == pytest.approx(out["axis"]["years"], abs=0.1)


def test_the_axis_is_the_coarse_legs_grid_not_the_fine_legs():
    """A coarse leg must never be resampled up onto a fine one.

    Forward-filling a monthly leg onto a daily axis manufactures flat bars and catch-up
    jumps; interpolating it manufactures movement it never had. Either way the common axis
    has to run at the coarsest leg's resolution, and the bar count proves which happened.
    """
    coarse = leg(np.full(36, 0.01), start="2015-01-31", freq="ME", rule="coarse")
    fine = leg(np.full(1200, 0.0003), start="2014-06-02", freq="D", rule="fine")
    out = blend.blend([coarse, fine])

    assert out["axis"]["grid_from"] == "coarse"
    assert out["axis"]["bars"] <= coarse["dates"].size + 2
    assert out["axis"]["median_bar_days"] > 25.0
    by_rule = {r["rule"]: r for r in out["legs"]}
    assert by_rule["fine"]["interp_ratio"] < 0.1          # a day against a month
    assert by_rule["coarse"]["on_grid_frac"] > 0.9        # its own dates, unprojected


def test_projection_onto_a_shared_date_returns_the_real_observation():
    """Interpolation may fill the gaps; it may not move the points that exist.

    The fine leg's value on a date the coarse grid also has is a genuine observation, and
    a projection that shifted it would put a return in the book that the leg never earned.
    """
    fine = leg(np.array([0.01, -0.02, 0.03, 0.04, -0.01, 0.02, 0.05, -0.03]),
               start="2020-01-01", freq="D")
    axis = pd.DatetimeIndex(["2020-01-01", "2020-01-04", "2020-01-09"])
    got = blend._project(fine["dates"], fine["curve"], axis)

    want = [fine["curve"][list(fine["dates"]).index(pd.Timestamp(d))] for d in axis]
    assert list(got) == pytest.approx(want, rel=1e-12)


def test_bars_per_year_is_measured_off_the_axis_not_assumed():
    """A ~monthly axis annualises at ~12, not at 252.

    The stored curves are stride-decimated, so a "bar" is whatever the file made it. A
    hardcoded 252 here would annualise a four-week bar as if it were a session and inflate
    every Sharpe on the page by about five times.
    """
    n = 120
    out = blend.blend([leg(np.full(n, 0.005), rule="a"),
                       leg(np.full(n, 0.004), rule="b")])
    assert out["axis"]["bars_per_year"] == pytest.approx(12.0, abs=0.3)
    # CAGR from a 0.45%/bar book at ~12 bars a year, which is the compounded monthly rate.
    assert out["metrics"]["cagr"] == pytest.approx(1.0045 ** 12 - 1.0, abs=0.002)


# ------------------------------------------------------------------ the benchmark

def test_the_benchmark_is_blended_on_the_books_schedule_not_its_weights():
    """The benchmark drifts on its OWN returns, equal-weight and reset on the same dates.

    Driving it with the strategy's drifted weights would make the baseline a function of
    the signal, which is the failure `../CLAUDE.md` spends a section on. Left unrebalanced,
    two benches of +1% and +3% a bar are two independently compounding halves — and the
    legs they belong to are +20% and -15% a bar, so a benchmark that had borrowed the
    strategy's weights would land nowhere near this number.
    """
    n = 40
    legs = [leg(np.full(n, 0.20), bench=np.full(n, 0.01), rule="a"),
            leg(np.full(n, -0.15), bench=np.full(n, 0.03), rule="b")]

    drifting = blend.blend(legs, capital=CAPITAL, rebalance="none")
    assert drifting["bench_metrics"]["final_value"] == pytest.approx(
        CAPITAL / 2 * 1.01 ** n + CAPITAL / 2 * 1.03 ** n, rel=1e-12)

    # Reset every bar, the benchmark compounds the average of its own two legs — again
    # with no reference at all to what the strategies did.
    rebalanced = blend.blend(legs, capital=CAPITAL, rebalance="monthly")
    assert rebalanced["bench_metrics"]["final_value"] == pytest.approx(
        CAPITAL * 1.02 ** n, rel=1e-12)


def test_excess_is_the_book_minus_its_own_blended_benchmark():
    n = 40
    legs = [leg(np.full(n, 0.02), bench=np.full(n, 0.01), rule="a"),
            leg(np.full(n, 0.03), bench=np.full(n, 0.015), rule="b")]
    out = blend.blend(legs, capital=CAPITAL)

    assert out["excess"]["cagr"] == pytest.approx(
        out["metrics"]["cagr"] - out["bench_metrics"]["cagr"], rel=1e-12)
    assert out["excess"]["total_return"] > 0


def test_one_leg_without_a_benchmark_suppresses_the_whole_comparison():
    """Not a benchmark over the legs that happen to have one.

    That baseline would cover a different portfolio from the book it sits beside — a
    difference in the universe, not in the signal — and the gap between them would read as
    skill.
    """
    n = 30
    out = blend.blend([leg(np.full(n, 0.02), bench=np.full(n, 0.01), rule="a"),
                       leg(np.full(n, 0.01), bench=None, rule="b")])

    assert out["bench"] is None
    assert out["bench_metrics"] is None
    assert out["excess"] is None
    assert any("no matched benchmark" in w for w in out["warnings"])


# ------------------------------------------------------------------ inception dates

def test_start_clips_the_axis_and_redeploys_the_capital_there():
    """A portfolio may be younger than its legs' histories."""
    n = 120
    legs = [leg(np.full(n, 0.01), start="2010-01-31", rule="a"),
            leg(np.full(n, 0.02), start="2010-01-31", rule="b")]
    out = blend.blend(legs, capital=CAPITAL, start="2015-01-01")

    assert out["dates"][0] >= "2015-01-01"
    assert out["curve"][0] == pytest.approx(CAPITAL)
    assert out["axis"]["years"] < 6.0
    # The legs still report the history they have; only the measurement is clipped.
    assert all(r["own_start"] == "2010-01-31" for r in out["legs"])


def test_start_after_a_legs_history_is_an_error_and_not_an_empty_curve():
    n = 60
    short = leg(np.full(n, 0.01), start="2000-01-31", rule="short")
    long_ = leg(np.full(400, 0.01), start="2000-01-31", rule="long")
    with pytest.raises(blend.BlendError, match="No shared history"):
        blend.blend([short, long_], start="2020-01-01")


def test_legs_that_never_overlap_are_an_error_naming_their_spans():
    """Silence here would price a portfolio nobody could have held."""
    early = leg(np.full(50, 0.01), start="2000-01-31", rule="early")
    late = leg(np.full(50, 0.01), start="2020-01-31", rule="late")
    with pytest.raises(blend.BlendError, match="no overlapping history"):
        blend.blend([early, late])


def test_an_overlap_of_a_single_point_cannot_be_annualised():
    early = leg(np.full(50, 0.01), start="2000-01-31", rule="early")
    end = early["dates"][-1]
    late = leg(np.full(50, 0.01), start=end.strftime("%Y-%m-%d"), rule="late")
    with pytest.raises(blend.BlendError):
        blend.blend([early, late])


# ------------------------------------------------------------------ input validation

def test_an_empty_portfolio_and_a_nonpositive_pot_are_refused():
    with pytest.raises(blend.BlendError, match="at least one leg"):
        blend.blend([])
    with pytest.raises(blend.BlendError, match="positive"):
        blend.blend([leg(np.full(20, 0.01))], capital=0.0)


@pytest.mark.parametrize("dates,curve,message", [
    (["2020-01-01", "2020-02-01"], [100.0], "dates against"),
    (["2020-01-01"], [100.0], "no return in it"),
    (["2020-02-01", "2020-01-01"], [100.0, 101.0], "strictly increasing"),
    (["2020-01-01", "2020-01-01"], [100.0, 101.0], "strictly increasing"),
    (["2020-01-01", "2020-02-01"], [100.0, 0.0], "non-positive"),
    (["2020-01-01", "2020-02-01"], [100.0, float("nan")], "non-finite"),
])
def test_a_malformed_leg_is_rejected_at_construction(dates, curve, message):
    """Each of these would otherwise produce a plausible-looking curve, not an exception.

    A duplicated or out-of-order date in particular survives every downstream step and
    quietly reorders a leg's returns.
    """
    with pytest.raises(blend.BlendError, match=message):
        blend.make_leg(dates, curve)


def test_a_benchmark_of_the_wrong_length_is_rejected():
    with pytest.raises(blend.BlendError, match="benchmark has"):
        blend.make_leg(["2020-01-01", "2020-02-01"], [100.0, 101.0], [100.0])


# ------------------------------------------------------------------ the loader

def test_load_leg_reads_a_sheet_and_names_what_is_missing(tmp_path):
    dates = ["2020-01-31", "2020-02-29", "2020-03-31"]
    write_sheet(tmp_path, "us_stocks", "1d", {
        "RSI": sheet_entry(dates, [100.0, 105.0, 102.0], [100.0, 101.0, 103.0],
                           n_assets=42, side="long", pit=True),
        "MACD": sheet_entry(dates, [100.0, 99.0, 101.0], [100.0, 101.0, 103.0]),
    })

    one = blend.load_leg("us_stocks", "1d", "RSI", results_dir=tmp_path)
    assert list(one["curve"]) == [100.0, 105.0, 102.0]
    assert list(one["bench"]) == [100.0, 101.0, 103.0]
    assert one["n_assets"] == 42 and one["side"] == "long" and one["pit"] is True

    # Both failures name the sheet, because "not found" without it sends the reader to the
    # wrong file. The rule count says the sheet WAS read, so the miss is the rule name.
    with pytest.raises(blend.BlendError, match="2 rules in it"):
        blend.load_leg("us_stocks", "1d", "NOPE", results_dir=tmp_path)
    with pytest.raises(blend.BlendError, match="has not been done with --curves"):
        blend.load_leg("crypto", "4h", "RSI", results_dir=tmp_path)


def test_the_sheet_cache_is_keyed_on_mtime_and_not_only_on_the_path(tmp_path):
    """A re-run of `run_book.sh` must not be served the previous parse.

    The cache is what makes a five-leg portfolio off one sheet cheap; keyed on the path
    alone it would also make that portfolio permanently stale inside a long-lived process,
    with no way to tell from the outside.
    """
    dates = ["2020-01-31", "2020-02-29", "2020-03-31"]
    path = tmp_path / "book_curves_us_stocks_1d.json"
    write_sheet(tmp_path, "us_stocks", "1d",
                {"RSI": sheet_entry(dates, [100.0, 105.0, 102.0])})
    first = blend.load_leg("us_stocks", "1d", "RSI", results_dir=tmp_path)
    assert list(first["curve"]) == [100.0, 105.0, 102.0]

    write_sheet(tmp_path, "us_stocks", "1d",
                {"RSI": sheet_entry(dates, [100.0, 90.0, 80.0])})
    # Set the mtime explicitly: a rewrite inside the filesystem's timestamp resolution can
    # otherwise land on the same value, which would make this test pass for the wrong
    # reason on a fast disk.
    stamp = path.stat().st_mtime + 10.0
    os.utime(path, (stamp, stamp))

    second = blend.load_leg("us_stocks", "1d", "RSI", results_dir=tmp_path)
    assert list(second["curve"]) == [100.0, 90.0, 80.0]


def test_blend_accepts_triples_and_loads_them(tmp_path):
    dates = list(pd.date_range("2015-01-31", periods=40, freq="ME").strftime("%Y-%m-%d"))
    up = list(100.0 * np.cumprod(np.full(40, 1.01)))
    flat = list(100.0 * np.cumprod(np.full(40, 1.005)))
    write_sheet(tmp_path, "us_stocks", "1d", {"RSI": sheet_entry(dates, up, flat),
                                              "MACD": sheet_entry(dates, flat, flat)})

    out = blend.blend([{"cls": "us_stocks", "tf": "1d", "rule": "RSI",
                        "results_dir": tmp_path},
                       {"cls": "us_stocks", "tf": "1d", "rule": "MACD",
                        "results_dir": tmp_path}], capital=CAPITAL)
    assert out["n_legs"] == 2
    assert [r["rule"] for r in out["legs"]] == ["RSI", "MACD"]
    assert out["bench_metrics"] is not None


# ------------------------------------------------------------------ what the result says

def test_the_result_carries_the_caveats_the_numbers_need():
    """The grid resolution and the uncharged rebalance travel with every blend.

    Both change how the figures beside them read, and a caveat that only appears when
    somebody thinks to look is one that will be missing from the screenshot.
    """
    n = 40
    out = blend.blend([leg(np.full(n, 0.01), bench=np.full(n, 0.005), rule="a"),
                       leg(np.full(n, 0.02), bench=np.full(n, 0.005), rule="b")])
    joined = " ".join(out["warnings"])
    assert "Drawdown is a lower bound" in joined
    assert "Rebalancing is charged nothing" in joined


def test_a_short_intersection_reports_nan_sharpe_rather_than_a_number():
    """Below `MIN_SHARPE_OBS` the estimate has an error bar wider than any value it takes."""
    n = blend.MIN_SHARPE_OBS - 5
    out = blend.blend([leg(np.array([0.01, -0.02] * (n // 2)), rule="a"),
                       leg(np.array([-0.01, 0.03] * (n // 2)), rule="b")])
    assert out["axis"]["bars"] < blend.MIN_SHARPE_OBS
    assert np.isnan(out["metrics"]["sharpe"])
    assert any("Sharpe needs" in w for w in out["warnings"])


def test_the_curve_and_the_dates_are_the_same_length():
    """A chart drawn from these two lists is only honest if they line up."""
    n = 50
    out = blend.blend([leg(np.full(n, 0.01), bench=np.full(n, 0.01), rule="a"),
                       leg(np.full(n, 0.02), bench=np.full(n, 0.01), rule="b")])
    assert len(out["dates"]) == len(out["curve"]) == len(out["bench"])
    assert len(out["curve"]) == out["axis"]["bars"] == out["metrics"]["bars"]
