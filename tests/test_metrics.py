"""`backtest engine/metrics.py` — the IR, the deflation, and the edge standard.

Two things here have already produced a wrong published number and both get a test that
names the incident:

* `information_ratio`'s **relative** zero-variance guard, without which a rule holding
  exactly the benchmark position returns an IR of order 1e15 (it fired 1,600 times in
  49,950 cells and poisoned `mean_is_ir` to -2.1e13);
* `apply_edge_standard`'s **Bonferroni correction on t**, without which one row in 1,974
  came back PASS at t = 3.014 when the corrected bar for that many trials is 4.21.

The rest is arithmetic that decides whether a row is evidence, so it is pinned against
values computed by hand or from the closed forms rather than against the code's own output.
"""

from __future__ import annotations

import numpy as np
import pytest

import metrics
from config import EDGE_MIN_FOLDS, EDGE_STANDARD, LEGACY_GATES

DAILY = 252.0


# ------------------------------------------------------------- information_ratio

def test_information_ratio_is_computed_on_the_difference_series():
    rng = np.random.default_rng(0)
    bench = rng.normal(0.0004, 0.01, 2000)
    strat = bench + rng.normal(0.0001, 0.002, 2000)
    diff = strat - bench
    assert metrics.information_ratio(strat, bench, DAILY) == pytest.approx(
        float(np.mean(diff) / np.std(diff, ddof=1) * np.sqrt(DAILY)))


def test_information_ratio_of_a_rule_against_itself_is_undefined():
    r = np.random.default_rng(1).normal(0.0004, 0.01, 500)
    assert np.isnan(metrics.information_ratio(r, r, DAILY))


def test_information_ratio_guards_a_CONSTANT_difference_relatively():
    """The 1e15 incident, pinned.

    A rule holding exactly the benchmark position has an excess series that is the cost
    charged each bar — a CONSTANT. In exact arithmetic sd is 0 and an `sd <= 0` guard
    fires; in float64 the cost terms accumulate ~1e-16 of noise, so sd is a tiny positive
    number, an absolute guard passes, and `mean / sd` returns order 1e15.
    """
    bench = np.random.default_rng(2).normal(0.0004, 0.01, 5000)
    strat = bench - 0.0001                      # always-long, charged a flat 1bp a bar
    assert np.isnan(metrics.information_ratio(strat, bench, DAILY))

    # And the same series built the way the engine builds it, so the float noise is real
    # rather than synthesised by the subtraction above.
    noisy = np.array([b - 0.0001 for b in bench])
    assert np.isnan(metrics.information_ratio(noisy, bench, DAILY))


def test_information_ratio_still_scores_a_genuinely_small_edge():
    """The guard must not swallow a real signal — it is relative to the differences'
    own scale, not an absolute floor on tracking error."""
    rng = np.random.default_rng(3)
    bench = rng.normal(0.0004, 0.01, 5000)
    strat = bench + rng.normal(1e-6, 1e-5, 5000)
    ir = metrics.information_ratio(strat, bench, DAILY)
    assert np.isfinite(ir)


def test_information_ratio_needs_three_observations():
    assert np.isnan(metrics.information_ratio(np.ones(2), np.zeros(2), DAILY))


def test_information_ratio_drops_non_finite_bars():
    strat = np.array([0.01, np.nan, 0.02, 0.03, np.inf, 0.01])
    bench = np.zeros(6)
    clean = metrics.information_ratio(np.array([0.01, 0.02, 0.03, 0.01]),
                                      np.zeros(4), DAILY)
    assert metrics.information_ratio(strat, bench, DAILY) == pytest.approx(clean)


# ------------------------------------------------------------------ cost_headroom

def test_cost_headroom_locates_the_crossing_from_two_points():
    """1.0 means the edge dies exactly at real cost; 3.0 means it survives 3x."""
    assert metrics.cost_headroom(0.6, 0.0) == pytest.approx(1.0)
    assert metrics.cost_headroom(0.6, 0.4) == pytest.approx(3.0)
    assert metrics.cost_headroom(1.0, 0.5) == pytest.approx(2.0)


def test_cost_headroom_is_zero_when_the_rule_loses_before_any_fees():
    """There is no cost it could have survived."""
    assert metrics.cost_headroom(-0.2, -0.5) == 0.0
    assert metrics.cost_headroom(0.0, -0.1) == 0.0


def test_cost_headroom_is_undefined_when_only_one_scenario_ran():
    """The documented trap: naive arithmetic returns `inf` on 17% of rows once 1d and 4h
    collapsed to `gross`, and `inf` reads as 'survives unlimited cost' when the truth is
    'cost was never charged'. A gate on that would pass trivially."""
    assert np.isnan(metrics.cost_headroom(0.6, 0.6))
    assert np.isnan(metrics.cost_headroom(-0.3, -0.3))


def test_cost_headroom_is_infinite_when_fees_helped():
    assert metrics.cost_headroom(0.4, 0.5) == float("inf")      # inspect the rule


def test_cost_headroom_is_nan_on_non_finite_input():
    assert np.isnan(metrics.cost_headroom(float("nan"), 0.5))
    assert np.isnan(metrics.cost_headroom(0.5, float("nan")))


# ------------------------------------------------------------------ leave_one_out

def test_leave_one_out_measures_what_dropping_the_best_asset_costs():
    """The top-20 study measured exactly this: dropping NVDA cost 34% of the IR."""
    ret, dropped = metrics.leave_one_out({"A": 1.0, "B": 0.1, "C": 0.1})
    assert dropped == "A"
    assert ret == pytest.approx(0.1 / 0.4)      # a strong average carried by one name


def test_leave_one_out_barely_moves_on_a_broad_result():
    ret, _ = metrics.leave_one_out({k: 0.5 for k in "ABCDEFGHIJ"})
    assert ret == pytest.approx(1.0)


def test_leave_one_out_needs_three_finite_assets():
    assert metrics.leave_one_out({"A": 1.0, "B": 0.5})[1] is None
    assert np.isnan(metrics.leave_one_out({"A": 1.0, "B": 0.5})[0])
    assert np.isnan(metrics.leave_one_out({"A": 1.0, "B": np.nan, "C": np.nan})[0])


def test_leave_one_out_is_undefined_when_the_mean_is_not_positive():
    """A retention ratio over a negative base is not interpretable."""
    ret, dropped = metrics.leave_one_out({"A": -0.1, "B": -0.5, "C": -0.6})
    assert np.isnan(ret)
    assert dropped == "A"


# --------------------------------------------------------------- power and ceilings

def test_se_ir_depends_on_years_not_on_how_finely_they_are_sliced():
    """390x the bars, identical sqrt(years) — why 1-minute data buys no significance."""
    assert metrics.se_ir(25.0) == pytest.approx(1.0 / 5.0)
    assert np.isnan(metrics.se_ir(0.0))
    assert np.isnan(metrics.se_ir(-1.0))


def test_noise_ceiling_rises_with_trials_and_falls_with_history():
    """History is the only lever: a crypto rule on 2.6 years needs to look twice as good
    as an equity rule on 10.6 to mean the same thing."""
    assert metrics.noise_ceiling(231, 10.6) < metrics.noise_ceiling(231, 2.6)
    assert metrics.noise_ceiling(500, 10.0) > metrics.noise_ceiling(50, 10.0)
    assert metrics.noise_ceiling(231, 10.6) == pytest.approx(0.9, abs=0.15)


def test_noise_ceiling_is_undefined_below_two_candidates():
    assert np.isnan(metrics.noise_ceiling(1, 10.0))
    assert np.isnan(metrics.noise_ceiling(231, 0.0))


def test_bonferroni_t_grows_with_the_number_of_simultaneous_tests():
    assert metrics.bonferroni_t(1) == pytest.approx(1.959964, abs=1e-5)
    assert metrics.bonferroni_t(1974) == pytest.approx(4.21, abs=0.02)
    assert metrics.bonferroni_t(100) > metrics.bonferroni_t(10)
    assert np.isnan(metrics.bonferroni_t(0))


# --------------------------------------------------------------- deflated Sharpe

def test_sharpe_se_collapses_to_the_normal_case():
    """Under g3=0, g4=3 the Mertens SE becomes Lo (2002)'s normal-IID form,
    sqrt((1 + SR^2/2)/(T-1)) — not the plain sqrt(1/(T-1)), which it only reaches as
    SR -> 0. The module docstring rounds that off; the arithmetic does not."""
    assert metrics.sharpe_se(0.05, 0.0, 3.0, 1000) == pytest.approx(
        np.sqrt((1.0 + 0.05 ** 2 / 2.0) / 999), rel=1e-12)
    assert metrics.sharpe_se(0.0, 0.0, 3.0, 1000) == pytest.approx(
        np.sqrt(1.0 / 999), rel=1e-12)


def test_negative_skew_widens_the_error_bar():
    """The correct penalty for selling insurance — and the de-risking rules that keep
    topping this leaderboard are short volatility by construction."""
    normal = metrics.sharpe_se(0.05, 0.0, 3.0, 1000)
    skewed = metrics.sharpe_se(0.05, -1.5, 3.0, 1000)
    assert skewed > normal


def test_fat_tails_widen_the_error_bar():
    assert metrics.sharpe_se(0.05, 0.0, 30.0, 1000) > metrics.sharpe_se(0.05, 0.0, 3.0, 1000)


def test_sharpe_se_needs_three_observations():
    assert np.isnan(metrics.sharpe_se(0.05, 0.0, 3.0, 2))
    assert np.isnan(metrics.sharpe_se(float("nan"), 0.0, 3.0, 500))


def test_probabilistic_sharpe_is_a_probability_and_moves_the_right_way():
    p = metrics.probabilistic_sharpe(0.05, 0.0, 3.0, 1000)
    assert 0.0 <= p <= 1.0
    assert p > 0.5                                          # positive observed Sharpe
    # Raising the benchmark can only lower the probability of clearing it.
    assert metrics.probabilistic_sharpe(0.05, 0.0, 3.0, 1000, benchmark=0.10) < p


def test_expected_max_sharpe_scales_with_the_dispersion_of_the_trials():
    """`sharpe_std` is the cross-sectional spread of the trials actually run, not an
    assumed 1.0 — 416 near-identical always-long variants have a tiny spread and a
    correspondingly low bar."""
    wide = metrics.expected_max_sharpe(400, 0.30)
    narrow = metrics.expected_max_sharpe(400, 0.05)
    assert wide > narrow
    assert wide == pytest.approx(narrow * 6.0, rel=1e-9)    # linear in sharpe_std


def test_expected_max_sharpe_grows_with_the_trial_count():
    assert metrics.expected_max_sharpe(4000, 0.2) > metrics.expected_max_sharpe(40, 0.2)


def test_expected_max_sharpe_is_undefined_on_degenerate_input():
    assert np.isnan(metrics.expected_max_sharpe(1, 0.2))
    assert np.isnan(metrics.expected_max_sharpe(400, 0.0))
    assert np.isnan(metrics.expected_max_sharpe(400, float("nan")))


def test_deflated_sharpe_refuses_a_short_series():
    assert np.isnan(metrics.deflated_sharpe(np.random.default_rng(0).normal(size=29),
                                            100, 0.2)["dsr"])


def test_deflated_sharpe_reports_raw_kurtosis_not_excess():
    """3.0 is normal. Feeding an excess kurtosis in would understate the error bar."""
    r = np.random.default_rng(4).normal(0.0005, 0.01, 20_000)
    out = metrics.deflated_sharpe(r, 100, 0.2, bpy=DAILY)
    assert out["kurtosis"] == pytest.approx(3.0, abs=0.15)
    assert out["skew"] == pytest.approx(0.0, abs=0.05)


def test_deflated_sharpe_is_harder_to_clear_than_the_undeflated_one():
    """`sharpe_std` is PER-OBSERVATION, like the series' own Sharpe. Handing it an
    annualised dispersion inflates the bar by sqrt(bpy) and pins every DSR at 0 —
    `dsr_from_leaderboard` divides by sqrt(bpy) for exactly this reason."""
    r = np.random.default_rng(5).normal(0.0008, 0.01, 5000)
    out = metrics.deflated_sharpe(r, 800, 0.29 / np.sqrt(DAILY), bpy=DAILY)
    assert out["dsr"] < out["psr"]                          # the whole point
    assert out["sr_star_bar"] > 0
    assert out["dsr_pass"] is (out["dsr"] >= 0.95)


def test_deflated_sharpe_annualises_only_when_given_bpy():
    r = np.random.default_rng(6).normal(0.0005, 0.01, 2000)
    out = metrics.deflated_sharpe(r, 100, 0.2, bpy=DAILY)
    assert out["sharpe_ann"] == pytest.approx(out["sharpe_bar"] * np.sqrt(DAILY))
    assert np.isnan(metrics.deflated_sharpe(r, 100, 0.2)["sharpe_ann"])


def test_more_trials_lower_the_deflated_probability():
    r = np.random.default_rng(7).normal(0.0008, 0.01, 5000)
    std = 0.29 / np.sqrt(DAILY)
    few = metrics.deflated_sharpe(r, 10, std, bpy=DAILY)["dsr"]
    many = metrics.deflated_sharpe(r, 5000, std, bpy=DAILY)["dsr"]
    assert few > 0.95 > many                    # the same series, priced for its search


def test_dsr_from_leaderboard_reads_the_trial_count_off_the_whole_column():
    """Filtering `trial_sharpes` to the survivors understates the dispersion and so
    understates the bar — the precise mistake deflation exists to prevent."""
    rng = np.random.default_rng(8)
    r = rng.normal(0.0008, 0.01, 4000)
    column = rng.normal(0.0, 0.3, 500)
    out = metrics.dsr_from_leaderboard(r, column, bpy=DAILY)
    assert out["n_trials"] == 500
    survivors = column[column > 0.4]
    filtered = metrics.dsr_from_leaderboard(r, survivors, bpy=DAILY)
    assert filtered["dsr"] > out["dsr"]                     # flattering, and wrong


def test_dsr_from_leaderboard_needs_two_trials_and_a_bpy():
    r = np.random.default_rng(9).normal(0.0005, 0.01, 2000)
    assert np.isnan(metrics.dsr_from_leaderboard(r, [0.3], bpy=DAILY)["dsr"])
    assert np.isnan(metrics.dsr_from_leaderboard(r, [0.3, 0.4], bpy=None)["dsr"])


def test_dsr_from_leaderboard_ignores_non_finite_trials():
    r = np.random.default_rng(10).normal(0.0005, 0.01, 2000)
    out = metrics.dsr_from_leaderboard(r, [0.1, np.nan, 0.2, np.inf, 0.3], bpy=DAILY)
    assert out["n_trials"] == 3


# ------------------------------------------------------------------- the standard

def base_row(**kw) -> dict:
    row = {"edge_dsharpe": 0.20, "edge_t": 5.0, "edge_vs_random": 0.05,
           "edge_vs_constant": 0.05, "edge_wealth": 0.10, "edge_headroom": 4.0,
           "n_folds_scored": 30, "exposure": 0.6, "fold_coverage": 0.9}
    row.update(kw)
    return row


def test_a_row_clearing_all_six_is_a_PASS():
    out = metrics.apply_edge_standard(base_row(n_trials=1))
    assert out["edge_passed"] == len(EDGE_STANDARD) == 6
    assert out["edge_verdict"] == "PASS"


def test_a_row_missing_one_criterion_fails():
    out = metrics.apply_edge_standard(base_row(edge_wealth=-0.01, n_trials=1))
    assert out["edge_gate_wealth"] is False
    assert out["edge_verdict"] == "fail"


def test_too_few_folds_is_underpowered_not_a_fail():
    """Reporting '0 passed' on a sheet that could not have detected the effect is how a
    null gets manufactured. Five of six sheets qualify."""
    out = metrics.apply_edge_standard(base_row(n_folds_scored=EDGE_MIN_FOLDS - 1,
                                               n_trials=1))
    assert out["edge_powered"] is False
    assert out["edge_verdict"] == "underpowered"
    assert out["edge_passed"] == 6              # the criteria are still scored


def test_a_rule_that_barely_trades_is_unrankable_rather_than_scored():
    """`CDLKICKING` never fires — 0% exposure, pure cash — and topped an 804-row sheet at
    delta-Sharpe +0.949, because a cash series has almost no variance."""
    out = metrics.apply_edge_standard(base_row(exposure=0.0, n_trials=1))
    assert out["edge_rankable"] is False
    assert out["edge_verdict"] == "unrankable"


def test_thin_fold_coverage_is_also_unrankable():
    out = metrics.apply_edge_standard(base_row(fold_coverage=0.2, n_trials=1))
    assert out["edge_verdict"] == "unrankable"


def test_unrankable_outranks_underpowered_in_the_verdict():
    out = metrics.apply_edge_standard(base_row(exposure=0.0, n_folds_scored=3,
                                               n_trials=1))
    assert out["edge_verdict"] == "unrankable"


def test_the_t_criterion_is_bonferroni_corrected_for_the_trial_count():
    """The 2026-08-10 incident, pinned. One row of 1,974 rankable trials satisfied the
    single-test 2.0 at t = 3.014 and came back PASS — the first in this repo's history —
    when the corrected bar for that many trials is 4.21."""
    out = metrics.apply_edge_standard(base_row(edge_t=3.014, n_trials=1974))
    assert out["edge_gate_t"] is False
    assert out["edge_t_bar_corrected"] == pytest.approx(4.21, abs=0.02)
    assert out["edge_t_uncorrected_pass"] is True           # flagged, not hidden
    assert out["edge_verdict"] == "fail"


def test_a_t_that_clears_the_corrected_bar_still_passes():
    out = metrics.apply_edge_standard(base_row(edge_t=5.0, n_trials=1974))
    assert out["edge_gate_t"] is True
    assert out["edge_verdict"] == "PASS"


def test_without_a_trial_count_the_bar_stays_uncorrected_and_says_so():
    """So nobody mistakes an uncorrected pass for a corrected one."""
    out = metrics.apply_edge_standard(base_row(edge_t=3.014))
    assert out["edge_gate_t"] is True
    assert np.isnan(out["edge_t_bar_corrected"])
    assert out["edge_t_uncorrected_pass"] is True


def test_a_failing_t_is_not_flagged_as_an_uncorrected_pass():
    out = metrics.apply_edge_standard(base_row(edge_t=1.0, n_trials=1974))
    assert out["edge_gate_t"] is False
    assert "edge_t_uncorrected_pass" not in out


def test_missing_and_non_finite_criteria_are_failures_not_crashes():
    out = metrics.apply_edge_standard({"n_folds_scored": 30, "exposure": 0.5})
    assert out["edge_passed"] == 0
    assert out["edge_verdict"] == "fail"
    assert metrics.apply_edge_standard(
        base_row(edge_dsharpe=float("nan"), n_trials=1))["edge_gate_dsharpe"] is False


def test_criteria_are_read_from_the_unprefixed_key_as_a_fallback():
    row = base_row(n_trials=1)
    row["dsharpe"] = row.pop("edge_dsharpe")
    assert metrics.apply_edge_standard(row)["edge_gate_dsharpe"] is True


# --------------------------------------------------------------------- aggregate

def test_aggregate_emits_diagnostics_and_never_a_verdict():
    """`aggregate` used to call `apply_gates`, which is how every IR-based sweep came to
    stamp a pass/fail on rows whose inputs cannot support one. The only place a verdict
    exists is `results/edge_standard.csv`."""
    per_asset = {"A": {"ir": 0.6}, "B": {"ir": 0.4}, "C": {"ir": -0.1}}
    row = metrics.aggregate(per_asset, years=10.0, ir_gross=0.5, ir_headline=0.3)

    assert row["ir_net"] == pytest.approx(0.3)
    assert row["ir_hit_rate"] == pytest.approx(2 / 3)
    assert row["t_stat"] == pytest.approx(0.3 * np.sqrt(10.0))
    assert row["n_assets"] == 3 and row["n_ir"] == 3

    assert "edge_verdict" not in row
    assert "gates_passed" not in row
    # Everything the retired four contribute is prefixed, so a stale `gates_passed`
    # column in an old CSV is visibly a different thing.
    assert row["legacy_passed"] == sum(
        row[f"legacy_gate_{g['key']}"] for g in LEGACY_GATES)


def test_aggregate_counts_only_finite_irs_toward_the_mean():
    per_asset = {"A": {"ir": 0.6}, "B": {"ir": np.nan}, "C": {"ir": 0.4}}
    row = metrics.aggregate(per_asset, 10.0, 0.5, 0.3)
    assert row["ir_net"] == pytest.approx(0.5)
    assert row["n_assets"] == 3
    assert row["n_ir"] == 2                     # the gap is visible, not averaged away


def test_aggregate_survives_a_rule_that_scored_nowhere():
    row = metrics.aggregate({"A": {"ir": np.nan}}, 10.0, np.nan, np.nan)
    assert np.isnan(row["ir_net"]) and np.isnan(row["t_stat"])
    assert row["legacy_passed"] == 0


def test_legacy_gates_are_the_retired_four():
    row = metrics.apply_legacy_gates(
        {"ir_net": 0.6, "ir_hit_rate": 0.75, "headroom": 4.0, "t_stat": 2.5,
         "loo_retention": 0.9})
    assert row["legacy_passed"] == 4
    assert row["legacy_gate_loo"] is True
    assert all(k.startswith("legacy_") for k in row)


# ---------------------------------------------------------------------- rankable

def test_rankable_requires_enough_assets_to_have_produced_an_ir():
    """Without this, rules that sit flat on most names win on a couple of assets' worth
    of noise — and a ratio objective rewards doing nothing."""
    assert metrics.rankable({"n_assets": 100, "n_ir": 80}, 0.75) is True
    assert metrics.rankable({"n_assets": 100, "n_ir": 20}, 0.75) is False
    assert metrics.rankable({"n_assets": 0, "n_ir": 0}, 0.75) is False


def test_rankable_is_inclusive_at_the_boundary():
    assert metrics.rankable({"n_assets": 100, "n_ir": 75}, 0.75) is True
