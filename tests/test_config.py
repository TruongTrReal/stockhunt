"""`backtest engine/config.py` — the configuration that is load-bearing beyond config.

Most of this file is data, and data does not need unit tests. Three things here are
*logic* with a recorded failure behind them, and those do:

* `headline_key`, which exists because reading `HEADLINE_SCENARIO` directly became a
  silent trap the moment 1d and 4h collapsed to a single scenario — filtering rows on
  `retail` then matches nothing and the aggregate is the mean of an empty set, which
  prints as a null result rather than as a wrong question;
* `rule_needs_volume`, which decides whether a rule is skipped-and-counted on crypto or
  fed NaN volume and quietly scored as a rule that does nothing;
* the invariants that keep the class table and the directory table from drifting apart.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import config


# --------------------------------------------------------------------- invariants

def test_every_class_has_a_data_directory():
    """Adding a class to CLASSES without adding it here fetches seven stages and then
    dies on a bare KeyError at the eighth, after the bars are already downloaded."""
    assert set(config.CLASS_DIR) == set(config.CLASSES)


def test_every_class_has_a_fee_grid_and_a_headline_scenario():
    for name in config.CLASSES:
        assert config.FEE_SCENARIOS.get(name), name
        assert config.headline_scenario(name)["key"] == config.HEADLINE_SCENARIO[name]


def test_every_fee_scenario_is_itemised():
    """Cost is not a single bps number: commission plus half-spread plus sell-side
    regulatory fees plus short borrow, each charged on something different."""
    for name, grid in config.FEE_SCENARIOS.items():
        keys = {"key", "commission_bps", "half_spread_bps", "sell_fee_bps",
                "borrow_annual"}
        for s in grid:
            assert keys <= set(s), f"{name}/{s.get('key')}"


def test_scenario_keys_are_unique_within_a_class():
    for name, grid in config.FEE_SCENARIOS.items():
        keys = [s["key"] for s in grid]
        assert len(keys) == len(set(keys)), name


def test_the_gross_scenario_charges_nothing():
    for name in config.CLASSES:
        gross = config.scenario(name, "gross")
        assert config.per_side_bps(gross) == 0.0
        assert gross["sell_fee_bps"] == 0.0 and gross["borrow_annual"] == 0.0


def test_crypto_is_not_charged_an_equity_fee_grid():
    """Major-exchange taker fees are ~10bps a side. Charging crypto an equity grid
    manufactures survivors."""
    crypto = {s["key"] for s in config.FEE_SCENARIOS["crypto"]}
    equity = {s["key"] for s in config.FEE_SCENARIOS["us_stocks"]}
    assert crypto != equity
    assert config.per_side_bps(config.headline_scenario("crypto")) > config.per_side_bps(
        config.headline_scenario("us_stocks"))


def test_the_edge_standard_is_six_criteria_and_GATES_is_an_alias():
    assert config.GATES is config.EDGE_STANDARD
    assert len(config.EDGE_STANDARD) == 6
    assert "".join(c["letter"] for c in config.EDGE_STANDARD) == "STRCWH"


def test_the_retired_four_are_kept_separate_and_still_four():
    """Kept only so the `legacy_*` columns already on disk stay interpretable."""
    assert len(config.LEGACY_GATES) == 4
    assert {g["key"] for g in config.LEGACY_GATES} == {"ir", "breadth", "headroom", "t"}
    assert config.LEGACY_GATES is not config.EDGE_STANDARD


def test_every_criterion_carries_a_threshold_and_a_label():
    for c in config.EDGE_STANDARD:
        assert {"key", "letter", "min", "label", "target"} <= set(c)
        assert isinstance(c["min"], (int, float))


def test_the_standing_data_rules_are_where_the_pipeline_expects_them():
    assert config.BACKTEST_START == "2000-01-01"
    assert config.MIN_PRICE_USD == 1.00
    assert config.LOO_MIN_RETENTION == 0.80
    assert config.EDGE_MIN_FOLDS == 20


# ------------------------------------------------------------ end-of-day flattening

def test_flattening_is_reserved_for_the_day_trading_horizons():
    """65-95% of US equity return is earned overnight (95.3% at 4h), so flattening a 4h
    rule removes most of the drift it is being scored on — a no-signal always-long rule
    scores IR -0.59 to -0.84 once flattened, which was the whole of the apparent
    'intraday is worse' effect."""
    assert config.FLATTEN_EOD_TIMEFRAMES == frozenset({"5m", "3m", "2m", "1m"})
    for tf in ("4h", "2h", "1h", "15m", "1d"):
        assert tf not in config.FLATTEN_EOD_TIMEFRAMES


def test_daily_is_not_an_intraday_timeframe():
    assert config.TIMEFRAMES["1d"]["intraday"] is False
    assert config.TIMEFRAMES["5m"]["intraday"] is True


def test_crypto_is_never_flattened():
    """A 24/7 market has no session to flatten into, and forcing a daily flat would
    invent an exposure gap."""
    assert config.CLASSES["crypto"]["flatten_eod"] is False


# ------------------------------------------------------------------ headline_key

def test_headline_key_reports_a_collapsed_sheets_single_scenario(monkeypatch):
    monkeypatch.setattr(config, "SINGLE_SCENARIO_TIMEFRAMES", {"1d": "gross"})
    assert config.headline_key("us_stocks", "1d") == "gross"


def test_headline_key_without_a_timeframe_is_the_class_default():
    assert config.headline_key("us_stocks") == config.HEADLINE_SCENARIO["us_stocks"]
    assert config.headline_key("crypto") == "binance"


def test_headline_key_on_a_full_grid_sheet_is_the_paid_schedule(monkeypatch):
    """FIXED. The collapse must be the REASON, not the mere presence of a timeframe.

    This returned `scenarios_for(cls, tf)[0]["key"]`, which is the collapsed scenario
    only when the timeframe actually collapsed — otherwise it is the first entry of the
    grid, and every grid here starts at `gross`. With SINGLE_SCENARIO_TIMEFRAMES back to
    `{}`, nothing collapses, so all 13 call sites that pass a timeframe were reporting
    the ZERO-COST scenario. Silent, because `gross` rows exist: the failure mode is a
    flattering number, not an empty set.
    """
    monkeypatch.setattr(config, "SINGLE_SCENARIO_TIMEFRAMES", {})
    for asset_class in config.CLASSES:
        for tf in ("1d", "4h", "5m"):
            assert config.headline_key(asset_class, tf) == \
                config.HEADLINE_SCENARIO[asset_class]


def test_the_two_spellings_of_the_headline_agree():
    """A reader cannot be expected to know that passing a timeframe changes which costs
    are charged. Where nothing has collapsed, the two must name the same scenario."""
    for asset_class in config.CLASSES:
        assert (config.headline_key(asset_class, "1d")
                == config.headline_key(asset_class))


def test_the_headline_is_never_the_free_scenario():
    """`gross` charges nothing and exists only to locate the breakeven crossing. If it
    is ever the headline, every reported number is a pre-cost number."""
    for asset_class in config.CLASSES:
        for tf in (None, "1d", "4h", "5m"):
            key = config.headline_key(asset_class, tf)
            assert key != "gross", (asset_class, tf)
            assert config.per_side_bps(config.scenario(asset_class, key)) > 0


def test_a_collapsed_timeframe_still_reports_what_it_ran(monkeypatch):
    """The behaviour the function was written for is preserved: when a sheet really does
    run one scenario, asking for the class headline would match no rows at all."""
    monkeypatch.setattr(config, "SINGLE_SCENARIO_TIMEFRAMES", {"1d": "gross"})
    assert config.headline_key("us_stocks", "1d") == "gross"
    assert config.headline_key("us_stocks", "4h") == "retail"


def test_sweep_headline_falls_back_to_what_the_frame_contains():
    """`sweep._headline_for` carried the same branch. It prefers the class headline and
    falls back only to what the data actually holds."""
    import pandas as pd
    import sweep

    full = pd.DataFrame({"scenario": ["gross", "retail", "wide", "pessimistic"]})
    assert sweep._headline_for("us_stocks", "1d", full) == "retail"

    only_gross = pd.DataFrame({"scenario": ["gross"]})
    assert sweep._headline_for("us_stocks", "1d", only_gross) == "gross"


# ------------------------------------------------------- the sourced rate schedule
#
# Checked against primary sources on 2026-08-11. These tests are not testing arithmetic;
# they pin published rates so that a figure going stale shows up as a failure with the
# source named, rather than as a number nobody re-derives.

SEC_SECTION_31_PER_MILLION = 20.60        # eff. 2026-04-04
FINRA_TAF_PER_SHARE = 0.000195            # eff. 2026-01-01, capped $9.79/trade
UNIVERSE_MEDIAN_CLOSE = 114.02            # 625 priced us_stocks names, 2026-08-11


def test_the_equity_sell_fee_is_section_31_plus_taf_at_the_universes_own_price():
    """The sell fee is two charges levied on different things: Section 31 on notional,
    the FINRA TAF per SHARE. Converting the second needs a price, so it is taken from
    the live universe rather than assumed."""
    section_31_bps = SEC_SECTION_31_PER_MILLION / 1e6 * 1e4
    taf_bps = FINRA_TAF_PER_SHARE / UNIVERSE_MEDIAN_CLOSE * 1e4
    assert section_31_bps == pytest.approx(0.2060)
    assert taf_bps == pytest.approx(0.0171, abs=1e-4)

    for asset_class in ("us_stocks", "us_etfs"):
        for s in config.FEE_SCENARIOS[asset_class]:
            if s["key"] == "gross":
                continue
            assert s["sell_fee_bps"] == pytest.approx(section_31_bps + taf_bps, abs=0.01)


def test_the_price_used_for_the_taf_conversion_barely_matters():
    """A 5x price range moves the total by 0.03bps, which is why a single median is an
    honest simplification rather than a hidden assumption."""
    at = lambda px: SEC_SECTION_31_PER_MILLION / 1e6 * 1e4 + FINRA_TAF_PER_SHARE / px * 1e4
    assert at(47.47) == pytest.approx(0.2471, abs=1e-3)      # universe p25
    assert at(246.21) == pytest.approx(0.2139, abs=1e-3)     # universe p75
    assert at(47.47) - at(246.21) < 0.05


def test_the_sell_fee_does_not_vary_with_execution_quality():
    """Section 31 and the TAF are statutory. A worse broker does not change them."""
    paid = [s["sell_fee_bps"] for s in config.FEE_SCENARIOS["us_stocks"]
            if s["key"] != "gross"]
    assert len(set(paid)) == 1


def test_borrow_stays_inside_the_general_collateral_band_except_for_the_stress_case():
    """General collateral returns the lender up to 0.50%/yr, so a short pays inside that
    band. `pessimistic` is deliberately outside it — that is what makes it a stress case."""
    by_key = {s["key"]: s for s in config.FEE_SCENARIOS["us_stocks"]}
    assert by_key["retail"]["borrow_annual"] <= 0.0050
    assert by_key["wide"]["borrow_annual"] == pytest.approx(0.0050)
    assert by_key["pessimistic"]["borrow_annual"] > 0.0050


def test_retail_equity_commission_is_zero():
    """Schwab and Fidelity both confirmed $0 on online stock and ETF trades in 2026."""
    by_key = {s["key"]: s for s in config.FEE_SCENARIOS["us_stocks"]}
    assert by_key["retail"]["commission_bps"] == 0.0
    assert by_key["wide"]["commission_bps"] == 0.0


def test_the_crypto_venues_carry_their_published_base_tier_taker_fees():
    """Base tier is the honest retail default: these are what a low-volume account pays,
    not what a market maker negotiates."""
    by_key = {s["key"]: s for s in config.FEE_SCENARIOS["crypto"]}
    assert by_key["binance"]["commission_bps"] == 10.0     # VIP 0 spot taker 0.10%
    assert by_key["coinbase"]["commission_bps"] == 60.0    # Advanced <$10K, 0.60%
    assert by_key["kraken"]["commission_bps"] == 80.0      # Kraken Pro Tier 1, 0.80%


def test_kraken_is_no_longer_priced_on_its_retired_flat_tier():
    """It was 26.0bps here, the old flat 0.26%. Kraken restructured into a 12-tier ladder
    whose Tier 1 taker is 0.80%, which makes it the most expensive venue on the grid
    rather than the middle one."""
    by_key = {s["key"]: s for s in config.FEE_SCENARIOS["crypto"]}
    assert by_key["kraken"]["commission_bps"] != 26.0
    assert (by_key["kraken"]["commission_bps"]
            > by_key["coinbase"]["commission_bps"]
            > by_key["binance"]["commission_bps"])


def test_the_crypto_headline_is_the_cheapest_venue_and_that_is_conservative():
    """Binance is the headline. Picking the cheapest real venue means a null result is
    not an artifact of an expensive one — the rule got the best honest execution and
    still lost."""
    headline = config.headline_scenario("crypto")
    paid = [s for s in config.FEE_SCENARIOS["crypto"] if s["key"] != "gross"]
    assert headline["commission_bps"] == min(s["commission_bps"] for s in paid)


def test_no_transaction_tax_is_charged_anywhere():
    """There is no US financial transaction tax or stamp duty as of 2026 — the federal
    stock transfer excise tax was repealed in 1966 and every revival is still a proposal.
    Section 31 and the FINRA TAF are fees. Capital gains tax is real but is not a
    property of a trade, so it cannot enter a bps-of-turnover model; every net figure in
    this repo is pre-tax. A `tax_bps` field appearing here would be an invention."""
    for grid in config.FEE_SCENARIOS.values():
        for s in grid:
            assert "tax_bps" not in s
            assert "transaction_tax" not in s


def test_crypto_charges_no_sell_fee_or_borrow():
    """No SEC fee applies, and retail spot cannot be shorted — charging zero borrow is
    the conservative choice for a short rather than a missing cost."""
    for s in config.FEE_SCENARIOS["crypto"]:
        assert s["sell_fee_bps"] == 0.0
        assert s["borrow_annual"] == 0.0


def test_commodities_are_pure_spread():
    """No commission, no SEC fee, no stock borrow — the whole cost is the dealer spread."""
    for s in config.FEE_SCENARIOS["commodities"]:
        assert s["commission_bps"] == 0.0
        assert s["sell_fee_bps"] == 0.0
        assert s["borrow_annual"] == 0.0


def test_the_commodity_ladder_spans_the_pip_convention_ambiguity():
    """Dealers disagree on whether an XAU/USD pip is $0.01 or $0.10, a 10x difference in
    the implied bps. The ladder deliberately spans it instead of resolving it at the
    flattering end — which is why `retail` is 2.0bps, not the 0.6bps one convention alone
    would give."""
    by_key = {s["key"]: s for s in config.FEE_SCENARIOS["commodities"]}
    assert by_key["tight"]["half_spread_bps"] < 1.0        # the $0.01-pip reading
    assert by_key["retail"]["half_spread_bps"] >= 2.0      # covers the $0.10 reading
    assert by_key["wide"]["half_spread_bps"] >= 5.0


def test_each_grid_is_ordered_cheapest_first():
    """`gross` is first everywhere, and the paid scenarios ascend. Several readers take
    `[0]` meaning 'the free one', and an unordered grid would break them silently."""
    for asset_class, grid in config.FEE_SCENARIOS.items():
        assert grid[0]["key"] == "gross", asset_class
        paid = [config.per_side_bps(s) for s in grid[1:]]
        assert paid == sorted(paid), asset_class


def test_scenarios_for_collapses_to_one_where_configured(monkeypatch):
    monkeypatch.setattr(config, "SINGLE_SCENARIO_TIMEFRAMES", {"4h": "gross"})
    assert [s["key"] for s in config.scenarios_for("us_stocks", "4h")] == ["gross"]
    assert len(config.scenarios_for("us_stocks", "5m")) == len(
        config.FEE_SCENARIOS["us_stocks"])


def test_a_collapsed_sheet_still_names_a_paid_schedule_to_measure_drag_against(monkeypatch):
    monkeypatch.setattr(config, "SINGLE_SCENARIO_TIMEFRAMES", {"1d": "gross"})
    ref = config.reference_scenario("us_stocks", "1d")
    assert ref is not None and config.per_side_bps(ref) > 0
    assert config.reference_scenario("us_stocks", "5m") is None


def test_an_unknown_scenario_raises_rather_than_returning_a_default():
    with pytest.raises(KeyError):
        config.scenario("us_stocks", "no_such_scenario")


# ------------------------------------------------------------- rule_needs_volume

def test_volume_dependent_rules_are_derived_from_talib_not_hardcoded():
    """Measured 2026-08-03 this is exactly 4 of 231: AD, ADOSC, MFI, OBV."""
    funcs = config.volume_dependent_rules()
    assert {"AD", "ADOSC", "MFI", "OBV"} <= funcs
    assert "ADX" not in funcs and "RSI" not in funcs


def test_a_period_variant_inherits_its_parents_volume_dependence():
    funcs = frozenset({"AD", "ADOSC", "MFI", "OBV"})
    assert config.rule_needs_volume("ADOSC_3_10", funcs)
    assert config.rule_needs_volume("MFI_14", funcs)
    assert config.rule_needs_volume("AD", funcs)


def test_a_rule_that_merely_starts_with_a_volume_function_name_is_not_matched():
    """`AD` must not swallow `ADX` or `ADOSC` must not swallow every `ADO*`. The match is
    exact or exact-plus-underscore, and getting it wrong would silently drop working
    rules from the crypto sheet."""
    funcs = frozenset({"AD", "ADOSC", "MFI", "OBV"})
    for rule in ("ADX", "ADXR", "ADD", "ADX_14", "MFIX", "OBVX"):
        assert not config.rule_needs_volume(rule, funcs), rule


def test_no_volume_rule_survives_on_the_real_talib_list():
    funcs = config.volume_dependent_rules()
    assert config.rule_needs_volume("ADOSC_3_10", funcs)
    assert not config.rule_needs_volume("SMA_50", funcs)


# ------------------------------------------------------------------------ paths

def test_safe_symbol_removes_the_path_separator():
    assert config.safe_symbol("BTC/USD") == "BTC_USD"
    assert config.safe_symbol("AAPL") == "AAPL"
    assert config.safe_symbol("XAU/USD") == "XAU_USD"


def test_safe_symbol_agrees_with_the_shared_core_copy():
    """`poscache._safe` mirrors this without importing it, so the two must not drift or
    a cached entry lands under a different name than the one that looks it up."""
    from stockhunt import poscache
    for symbol in ("BTC/USD", "AAPL", "XAU/USD", "BRK.B"):
        assert poscache._safe(symbol) == config.safe_symbol(symbol)


def test_cache_dir_is_organised_by_the_human_readable_class_name():
    assert config.cache_dir("us_stocks", "1d").parts[-2:] == ("stocks", "1d")
    assert config.cache_dir("crypto", "4h").parts[-2:] == ("crypto", "4h")


def test_class_of_finds_a_symbol_and_refuses_an_unknown_one():
    known = config.CLASSES["crypto"]["symbols"][0]
    assert config.class_of(known) == "crypto"
    with pytest.raises(KeyError):
        config.class_of("NOT_A_REAL_SYMBOL")


def test_class_of_resolves_a_benchmark_too():
    for name, spec in config.CLASSES.items():
        if spec.get("benchmark"):
            assert config.class_of(spec["benchmark"]) in config.CLASSES


def test_window_spec_exists_for_every_configured_sheet():
    for (asset_class, timeframe) in config.WINDOWS:
        spec = config.window_spec(asset_class, timeframe)
        assert "start" in spec
        assert asset_class in config.CLASSES
        assert timeframe in config.TIMEFRAMES


def test_the_universe_is_the_point_in_time_top100():
    """The 2026-08-12 change, and the second universe move this study has made.

    Three universes have now been live and a number is only comparable within one:

        until 2026-08-09   MEGA20, 20 names chosen for being large TODAY
        2026-08-09..08-12  SP500_UNIVERSE, 751 point-in-time S&P names
        from 2026-08-12    TOP100_ALL, the point-in-time top 100 by dollar volume

    All three are still named in `config`, so a stale sheet can be regenerated rather than
    merely mistrusted. Nothing may silently select on the old ones.
    """
    live = config.CLASSES["us_stocks"]["symbols"]
    assert live is config.US_STOCKS
    assert config.US_STOCKS is config.TOP100_ALL

    # The union that gets FETCHED is larger than the 100 held at once — that gap IS the
    # point-in-time machinery. Equal would mean the universe had been frozen to one date.
    assert len(config.TOP100_CURRENT) == 100
    assert len(config.TOP100_ALL) > len(config.TOP100_CURRENT)
    assert set(config.TOP100_CURRENT) < set(config.TOP100_ALL)

    # The two superseded universes are still reachable, and still distinct from the live
    # one. `SP500_UNIVERSE` strictly contains it: the top 100 is drawn from S&P members.
    assert len(config.MEGA20) == 20
    assert set(config.MEGA20) != set(live)
    assert len(config.SP500_UNIVERSE) > len(config.US_STOCKS)
    assert set(config.US_STOCKS) <= set(config.SP500_UNIVERSE)


def test_top100_membership_table_matches_the_emitted_universe():
    """`universes_top100.py` is generated from the CSV; a stale one is a silent universe
    change, because `config` reads the module and every stage reads the CSV."""
    import top100_membership

    iv = top100_membership.load()
    assert sorted(top100_membership.universe(iv)) == sorted(config.TOP100_ALL)
    assert sorted(top100_membership.current(iv)) == sorted(config.TOP100_CURRENT)

    # Spells are half-open and non-overlapping per symbol, which is what lets
    # `portfolio_wf.membership_mask` OR them together without double-counting a bar.
    for sym, grp in iv.groupby("symbol"):
        g = grp.sort_values("start")
        ends = g["end"].tolist()[:-1]
        starts = g["start"].tolist()[1:]
        assert all(pd.notna(e) for e in ends), f"{sym} has a closed spell after an open one"
        assert all(e <= s for e, s in zip(ends, starts)), f"{sym} has overlapping spells"


def test_the_universe_carries_no_foreign_namesakes():
    """A bare ticker resolves against every venue Twelve Data carries, and 85 cached
    us_stocks series were a foreign company for their whole length -- `CTRA` was Ciputra
    Development on the Indonesia Stock Exchange, and it ranked 3rd largest US stock in
    2026 before this was caught. Nothing quarantined that way may be in the universe."""
    probe = config.DATA_DIR / "reference" / "us_listing_probe.csv"
    if not probe.exists():
        pytest.skip("no listing probe cached; run check_data.py --probe-listing")
    p = pd.read_csv(probe)
    impostors = set(p.loc[~p["has_us_listing"].astype(bool), "symbol"].astype(str))
    assert impostors, "the probe found nothing, which means it did not run"
    assert not (set(config.US_STOCKS) & impostors)


def test_window_spec_names_the_gap_it_found():
    """A missing WINDOWS row must say it is a TABLE gap, not a vendor limit.

    On 2026-08-21 a 1m fetch for `us_etfs` and `commodities` died on a bare
    `KeyError: ('us_etfs', '1m')` three hours into a batch whose own summary line then
    reported DONE. Nothing was written and nothing said so, and the wrong conclusion —
    "the vendor has no intraday for ETFs" — was the easy one to draw. It had simply
    never been asked for, so no row existed.
    """
    import pytest
    with pytest.raises(KeyError) as e:
        config.window_spec("cme_futures", "1m")
    msg = str(e.value)
    assert "GAP IN THE TABLE" in msg
    assert "'1d'" in msg, "it must list what the class DOES carry"


def test_every_fetchable_timeframe_has_a_window():
    """A class the vendor serves must have a window for every interval it can be asked
    for. `TIMEFRAMES` with `interval: None` are derived by resampling and are never
    fetched, so they are exempt — everything else is a fetch waiting to fail."""
    fetchable = [tf for tf, s in config.TIMEFRAMES.items() if s["interval"] is not None]
    for cls, spec in config.CLASSES.items():
        if spec.get("source", "twelvedata") != "twelvedata":
            continue          # another vendor, another table
        have = {tf for c, tf in config.WINDOWS if c == cls}
        missing = [tf for tf in fetchable if tf not in have]
        # Only assert for the timeframes this repo actually runs on the class; a class
        # nobody fetches at 15m does not need a 15m row invented for it.
        needed = [tf for tf in missing if tf in config.FLATTEN_EOD_TIMEFRAMES]
        assert not needed, (
            f"{cls} can be asked for {needed} but config.WINDOWS has no row for them")
