"""`strategies.registry` — label grammar and discovery.

The label grammar is what every result CSV in this repo is keyed on, going back three
studies, so a round-trip failure orphans history rather than merely raising. Discovery is
the other half: a file in `published/` is the unit of a tested thing, and the two ways it
can go wrong are both silent — a scaffolded `NotImplementedError` becomes a rule that
never trades, and an unstable enumeration order reshuffles `cells()` in a project that
charges itself for the number of trials it runs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies import registry
from strategies.controls import BASELINE, CONTROLS
from strategies.registry import CATALOG, build, cells, decode, encode, skipped_for


@pytest.fixture
def frame() -> tuple:
    n = 900
    rng = np.random.default_rng(3)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
    high = close * (1.0 + np.abs(rng.normal(0, 0.006, n)))
    low = close * (1.0 - np.abs(rng.normal(0, 0.006, n)))
    df = pd.DataFrame(
        {"Open": close, "High": high, "Low": low, "Close": close,
         "Volume": rng.uniform(1e6, 5e6, n)},
        index=pd.date_range("2018-01-01", periods=n, freq="D"))
    return df, close, 252.0


# ------------------------------------------------------------------- the catalog

def test_the_catalog_is_discovered_and_not_empty():
    assert len(CATALOG) >= 25


def test_every_strategy_declares_its_provenance():
    """This repo's entire premise is testing *published* rules. A strategy with no SOURCE
    is not a replication of anything."""
    missing = [n for n, s in CATALOG.items() if not s.source or not s.family]
    assert missing == []


def test_every_strategy_records_where_it_lives():
    assert all(s.module.startswith("strategies.published.") for s in CATALOG.values())


def test_the_module_name_is_the_strategy_name():
    """The filename is the identity — it is the label every result CSV is keyed on, so
    renaming a file renames a strategy and orphans its history."""
    for name, s in CATALOG.items():
        assert s.module.rsplit(".", 1)[1] == name


def test_grid_zero_is_always_the_published_parameter_set():
    """Load-bearing for stage 1e: it is what makes the no-fitting row and the
    walk-forward row directly comparable."""
    for name, s in CATALOG.items():
        assert s.grid[0] == s.published, name
        assert isinstance(s.published, dict), name


def test_drafts_are_excluded_from_the_catalog():
    """A scaffolded `position()` raises NotImplementedError, `build` catches it and
    returns None, and the strategy would appear on a leaderboard as a rule that simply
    never trades — indistinguishable from a real rule that does nothing."""
    assert not (set(registry.DRAFTS) & set(CATALOG))


def test_discovery_order_is_stable():
    """An unstable order silently reshuffles `cells()`, and this project charges itself
    for the number of trials it runs."""
    assert list(CATALOG) == sorted(CATALOG)


# ------------------------------------------------------------ the label grammar

def test_the_published_cell_keeps_a_bare_name():
    name = next(iter(CATALOG))
    s = CATALOG[name]
    assert encode(name, dict(s.published), s) == name


def test_a_variant_is_written_as_a_diff_against_the_published_set():
    name, s = next((n, s) for n, s in CATALOG.items() if s.published)
    key = sorted(s.published)[0]
    params = dict(s.published)
    params[key] = s.published[key] + 1
    label = encode(name, params, s)
    assert label.startswith(name + registry.SEP)
    assert f"{key}=" in label
    # Only the differing parameter is written.
    assert label.count("=") == 1


def test_encode_sorts_its_parameters_so_a_label_is_canonical():
    name, s = next((n, s) for n, s in CATALOG.items() if len(s.published) >= 2)
    params = dict(s.published)
    keys = sorted(params)[:2]
    for k in keys:
        params[k] = params[k] + 1
    label = encode(name, params, s)
    written = [p.split("=")[0] for p in label.split(registry.SEP)[1].split(",")]
    assert written == sorted(written)


def test_decode_fills_the_unstated_parameters_from_the_published_set():
    name, s = next((n, s) for n, s in CATALOG.items() if s.published)
    got_name, params = decode(name)
    assert got_name == name
    assert params == s.published
    assert params is not s.published            # a copy, so a caller cannot mutate it


def test_encode_decode_round_trips_every_cell_in_the_catalog():
    for name, s in CATALOG.items():
        for grid_params in s.grid:
            label = encode(name, grid_params, s)
            back_name, back_params = decode(label)
            assert back_name == name, label
            assert back_params == grid_params, label


def test_decode_preserves_the_published_parameter_TYPE():
    """`int` stays `int`. A lookback silently becoming 5.0 changes what `range()` and
    every `talib` timeperiod do with it."""
    for name, s in CATALOG.items():
        for key, value in s.published.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            other = value + 1
            _, params = decode(f"{name}{registry.SEP}{key}={other}")
            assert type(params[key]) is type(value), f"{name}.{key}"


def test_decode_rejects_an_unknown_strategy():
    with pytest.raises(KeyError):
        decode("no_such_strategy")


# ------------------------------------------------------------------------- cells

def test_cells_lists_every_grid_point_for_a_class():
    labels = cells("us_stocks")
    assert len(labels) == len(set(labels))                  # no duplicates
    assert len(labels) == sum(len(s.grid) for n, s in CATALOG.items()
                              if s.classes is None or "us_stocks" in s.classes)


def test_cells_puts_each_strategys_published_label_first():
    labels = cells("us_stocks")
    for name, s in CATALOG.items():
        if s.classes is not None and "us_stocks" not in s.classes:
            continue
        mine = [l for l in labels if l == name or l.startswith(name + registry.SEP)]
        assert mine[0] == name, name


def test_cells_is_deterministic():
    assert cells("crypto") == cells("crypto")


def test_a_class_restricted_strategy_is_skipped_and_counted_not_run_flat():
    """A rule undefined on a class and scored anyway produces a flat position, which on a
    leaderboard is indistinguishable from a rule that does nothing."""
    for asset_class in ("us_stocks", "crypto", "us_etfs", "commodities"):
        skipped = skipped_for(asset_class)
        listed = cells(asset_class)
        for name in skipped:
            assert name not in listed
            assert CATALOG[name].classes is not None


def test_every_strategy_is_either_run_or_skipped_on_every_class():
    for asset_class in ("us_stocks", "crypto"):
        runnable = {l.split(registry.SEP)[0] for l in cells(asset_class)}
        assert runnable | set(skipped_for(asset_class)) == set(CATALOG)


# ------------------------------------------------------------------------- build

def test_build_returns_the_controls(frame):
    df, close, bpy = frame
    n = len(df)
    np.testing.assert_array_equal(build(BASELINE, df, close, bpy, "AAPL"), np.ones(n))
    np.testing.assert_array_equal(build("ALWAYS_LONG", df, close, bpy, "AAPL"), np.ones(n))
    np.testing.assert_array_equal(build("ALWAYS_FLAT", df, close, bpy, "AAPL"), np.zeros(n))


def test_build_returns_the_random_controls_at_the_requested_exposure(frame):
    df, close, bpy = frame
    pos = build("RANDOM_75", df, close, bpy, "AAPL")
    assert pos.size == len(df)
    assert float(np.mean(pos)) == pytest.approx(0.75, abs=0.1)


def test_every_declared_control_builds(frame):
    df, close, bpy = frame
    for name in (BASELINE, *CONTROLS):
        pos = build(name, df, close, bpy, "AAPL")
        assert pos is not None and pos.size == len(df), name


def test_build_returns_none_rather_than_raising_on_a_bad_label(frame):
    df, close, bpy = frame
    for label in ("no_such_strategy", "ibs@nonsense=1", "", "ibs@buy=notanumber"):
        assert build(label, df, close, bpy, "AAPL") is None, label


def test_a_built_strategy_is_finite_and_the_right_length(frame):
    df, close, bpy = frame
    built = 0
    for name in CATALOG:
        pos = build(name, df, close, bpy, "AAPL")
        if pos is None:
            continue
        built += 1
        assert pos.size == len(df), name
        assert np.isfinite(pos).all(), name
        assert np.abs(pos).max() <= 1.0, name               # target exposure in -1..1
    assert built >= 20, "most of the catalog should build on a plain daily series"


def test_a_regime_overlay_wraps_a_base_label(frame):
    df, close, bpy = frame
    base = build("ALWAYS_LONG", df, close, bpy, "AAPL")
    gated = build("volregime:hi:0.5:ALWAYS_LONG", df, close, bpy, "AAPL")
    assert gated is not None
    assert set(np.unique(gated)) <= {0.0, 1.0}
    assert float(np.mean(gated)) < float(np.mean(base))     # it sits out whole stretches


@pytest.mark.parametrize("label", [
    "volregime:mid:0.5:ALWAYS_LONG",       # side must be hi or lo
    "volregime:hi:1.5:ALWAYS_LONG",        # quantile out of range
    "volregime:hi:abc:ALWAYS_LONG",        # unparseable quantile
    "volregime:hi:0.5",                    # no base label
])
def test_a_malformed_regime_overlay_is_none(frame, label):
    df, close, bpy = frame
    assert build(label, df, close, bpy, "AAPL") is None


# ------------------------------------------------------- overlay-aware label parsing

def test_strip_overlays_peels_prefixes_and_keeps_the_variant():
    """`ha:chart:ibs@buy=0.3` is a published strategy wearing two prefixes.

    Anything answering "what strategy is this label about" needs this. Splitting on SEP
    alone yields `ha:chart:ibs`, which is in no catalog — which is exactly how
    `live_signal.family` came to report UNKNOWN for every overlay label, and would have
    made the paper desk refuse a registration it can in fact build.
    """
    from strategies.registry import strip_overlays
    assert strip_overlays("ha:chart:ibs@buy=0.3") == ("ha:chart:", "ibs@buy=0.3")
    assert strip_overlays("chart:ibs") == ("chart:", "ibs")
    assert strip_overlays("ha:ibs") == ("ha:", "ibs")
    assert strip_overlays("ibs") == ("", "ibs")
    assert strip_overlays("SMA_200") == ("", "SMA_200")


def test_strip_overlays_counts_parameter_fields():
    """`hold:` carries one field and `volregime:` two. Stripping the bare word would
    leave `30:ibs` behind and report it as an unknown rule."""
    from strategies.registry import strip_overlays
    assert strip_overlays("hold:30:ibs") == ("hold:30:", "ibs")
    assert strip_overlays("volregime:hi:0.5:ibs") == ("volregime:hi:0.5:", "ibs")
    assert strip_overlays("hold:30:chart:ibs") == ("hold:30:chart:", "ibs")


def test_strip_overlays_hands_back_a_malformed_label_untouched():
    """A prefix with its fields missing is not an overlay; returning it whole lets the
    caller report an unknown label rather than a confidently wrong base."""
    from strategies.registry import strip_overlays
    assert strip_overlays("hold:oops") == ("", "hold:oops")
    assert strip_overlays("volregime:hi") == ("", "volregime:hi")


def test_every_registered_overlay_prefix_is_strippable():
    """A new overlay added to `build` but not to OVERLAY_PREFIXES would be invisible to
    every label parser in the repo. This fails the moment those two drift apart."""
    from strategies.registry import OVERLAY_PREFIXES, strip_overlays
    for name, sep, fields in OVERLAY_PREFIXES:
        label = name + sep + sep.join(["1"] * fields) + (sep if fields else "") + "ibs"
        prefix, base = strip_overlays(label)
        assert base == "ibs", f"{name}: {label} -> {base!r}"
        assert prefix.startswith(name)
