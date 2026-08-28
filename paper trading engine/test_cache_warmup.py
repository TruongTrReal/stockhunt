"""The seam between the bars on disk and the bars from the vendor.

Run from THIS directory::

    ..\\.venv\\Scripts\\python -m pytest test_cache_warmup.py -q

Warming a live book from `data/` is what makes a deep buffer possible at a fine timeframe,
and the splice is the whole risk. Two series of one instrument from two routes either
agree or they are two different histories with a step in the middle — and a step in a
rolling buffer is invisible to every indicator computed over it.

Synthetic only. Nothing here reads `data/`, because a test that fails when somebody
re-fetches a ticker is a test nobody trusts.
"""

from __future__ import annotations

import pandas as pd
import pytest

import cache_warmup


def bars(start: str, n: int, close: float = 100.0, step: float = 0.0,
         freq: str = "15min") -> list[dict]:
    stamps = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return [{"Open": close + i * step, "High": close + i * step,
             "Low": close + i * step, "Close": close + i * step,
             "Volume": 1.0, "ts": ts} for i, ts in enumerate(stamps)]


def test_the_futures_class_is_excluded():
    """Its bars are back-adjusted, and the two routes anchor on different bars.

    Splicing them puts a roll-sized step in the buffer — the exact defect back-adjustment
    exists to remove.
    """
    assert cache_warmup.usable("cme_futures") is False
    assert cache_warmup.load("cme_futures", "1h", "ES.v.0", 100) == []


@pytest.mark.parametrize("cls", ["us_stocks", "us_etfs", "crypto", "commodities"])
def test_every_other_class_may_use_the_cache(cls):
    assert cache_warmup.usable(cls) is True


def rescaled(src, factor):
    return [{**b, "Open": b["Open"] * factor, "High": b["High"] * factor,
             "Low": b["Low"] * factor, "Close": b["Close"] * factor} for b in src]


def test_the_same_series_on_a_different_adjustment_basis_is_ACCEPTED():
    """The case that rejected 134 of 141 healthy symbols on the live desk.

    Both sides carry `adjust=all`, applied as of the day each was fetched, so every
    dividend between the two fetches shifts the whole cached series by one constant. That
    is the same history on a different basis, not a different history.
    """
    cached = bars("2026-01-01", 200, close=100.0, step=0.5)
    live = rescaled(cached[-80:], 1.037)
    report = cache_warmup.seam_report(cached, live)
    assert report["ok"] is True
    assert report["scale"] == pytest.approx(1.037, rel=1e-9)
    assert report["worst"] == pytest.approx(0.0, abs=1e-12)


def test_a_ratio_that_is_not_constant_is_refused():
    """A missing split, a truncated history or the wrong instrument looks like this."""
    cached = bars("2026-01-01", 200, close=100.0, step=0.5)
    live = [{**b, "Close": b["Close"] * (1.05 if i % 2 else 1.20)}
            for i, b in enumerate(cached[-80:])]
    report = cache_warmup.seam_report(cached, live)
    assert report["ok"] is False
    assert "not one series rescaled" in report["reason"]


def test_too_few_overlapping_bars_is_refused_rather_than_passed():
    """Any two bars are consistent. The first version decided on ONE and called it a check."""
    cached = bars("2026-01-01", 200)
    live = cached[-3:]
    report = cache_warmup.seam_report(cached, live)
    assert report["ok"] is False
    assert str(cache_warmup.MIN_OVERLAP) in report["reason"]


def test_a_hole_between_the_two_is_named_as_one():
    cached = bars("2026-01-01", 100)
    live = bars("2026-06-01", 100)
    report = cache_warmup.seam_report(cached, live)
    assert report["ok"] is False
    assert "hole between them" in report["reason"]


def test_the_splice_lifts_the_old_bars_onto_the_live_basis():
    cached = bars("2026-01-01", 200, close=100.0, step=1.0)
    live = rescaled(cached[-50:], 2.0)
    merged = cache_warmup.splice(cached, live, 2.0, limit=10_000)
    assert len(merged) == 200, "one bar per timestamp; the live bar wins the overlap"
    assert merged[0]["Close"] == pytest.approx(200.0), "the oldest bar was rescaled"
    assert merged[-1]["Close"] == live[-1]["Close"], "the newest bar is the live one"
    stamps = [b["ts"] for b in merged]
    assert stamps == sorted(stamps), "a spliced buffer must stay in order"
    assert len(set(stamps)) == len(stamps), "and must not repeat a bar"


def test_the_splice_respects_the_window():
    cached = bars("2026-01-01", 500, close=100.0)
    live = rescaled(cached[-50:], 1.0)
    assert len(cache_warmup.splice(cached, live, 1.0, limit=120)) == 120


def test_the_splice_never_rescales_volume():
    """Volume is a count. A price adjustment does not multiply it."""
    cached = bars("2026-01-01", 200, close=100.0)
    live = rescaled(cached[-50:], 3.0)
    merged = cache_warmup.splice(cached, live, 3.0, limit=10_000)
    assert {b["Volume"] for b in merged} == {1.0}


def test_cached_bars_are_stamped_on_the_close_like_a_nautilus_bar(monkeypatch):
    """`ts_event` is the vendor stamp PLUS one interval, and the buffer is keyed on it.

    Handing it the open stamp put the two conventions one interval apart, so out of ~1,490
    genuinely shared daily bars exactly one matched — the defect this test pins.
    """
    idx = pd.date_range("2026-01-01", periods=3, freq="D")
    frame = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0,
                          "Volume": 1.0}, index=idx)

    import sys, types
    stub = types.ModuleType("td_loader")
    stub.load = lambda cls, tf, syms: {syms[0]: frame}
    monkeypatch.setitem(sys.modules, "td_loader", stub)

    out = cache_warmup.load("us_stocks", "1d", "AAPL", 10)
    assert [b["ts"] for b in out] == [pd.Timestamp("2026-01-02", tz="UTC"),
                                      pd.Timestamp("2026-01-03", tz="UTC"),
                                      pd.Timestamp("2026-01-04", tz="UTC")]


@pytest.mark.parametrize("tf,expected", [("1d", "1 days"), ("4h", "4 hours"),
                                         ("1h", "1 hours"), ("15m", "15 minutes"),
                                         ("5m", "5 minutes")])
def test_the_interval_matches_the_timeframe(tf, expected):
    assert cache_warmup._interval(tf) == pd.Timedelta(expected)


def test_an_empty_side_is_a_failure():
    assert cache_warmup.seam_report([], bars("2026-01-01", 10))["ok"] is False
    assert cache_warmup.seam_report(bars("2026-01-01", 10), [])["ok"] is False


def test_a_uniform_halving_is_ACCEPTED_because_it_is_one_constant():
    """A 2:1 split the cache predates is the same history on a different basis.

    The first version refused exactly this and called it "a different series". It is not;
    it is the series the desk already trades, scaled — and `splice` puts it back on the
    live basis.
    """
    cached = bars("2026-01-01", 200, close=100.0)
    live = rescaled(cached[-80:], 0.5)
    report = cache_warmup.seam_report(cached, live)
    assert report["ok"] is True
    assert report["scale"] == pytest.approx(0.5)


def test_the_reason_tells_the_reader_what_to_do():
    """A refusal has to name the fix, or it is just a log line nobody acts on."""
    cached = bars("2026-01-01", 200, close=100.0)
    live = [{**b, "Close": b["Close"] * (1.0 + 0.01 * i)}
            for i, b in enumerate(cached[-80:])]
    report = cache_warmup.seam_report(cached, live)
    assert report["ok"] is False
    assert "Re-fetch" in report["reason"]


def test_a_missing_cache_is_not_an_error():
    """A box carrying no bars must keep trading — this is an improvement, not a dependency."""
    assert cache_warmup.load("us_stocks", "15m", "NOT_A_TICKER", 100) == []


def test_the_window_is_sized_per_timeframe():
    """1,500 bars is six years at 1d and ten weeks at 15m; one constant cannot be both."""
    import paper_config
    assert paper_config.window_bars("1d") == paper_config.DEFAULT_WINDOW_BARS
    assert paper_config.window_bars("1h") > paper_config.DEFAULT_WINDOW_BARS
    assert paper_config.window_bars("15m") > paper_config.window_bars("1h")


def test_the_fine_windows_exceed_one_vendor_request():
    """Deliberate: they are reachable from the disk, never from a single REST call."""
    import paper_config
    import td_live
    assert paper_config.window_bars("15m") > td_live.OUTPUT_SIZE
    assert paper_config.window_bars("1d") <= td_live.OUTPUT_SIZE


def test_one_revised_bar_does_not_veto_a_healthy_history():
    """The verdict is a high percentile, not the maximum, and this is why.

    `BTC/USD` and `XAU/USD` have no single venue behind them — the vendor aggregates, and a
    settled bar can be revised afterwards. Measured against the live feed: 249 of 250 bars
    agree exactly and one differs by tenths of a percent. Judging on the maximum would let
    that single print veto the whole series, which is the same "one bar decided it" failure
    the overlap floor exists to remove, wearing the opposite sign.
    """
    cached = bars("2026-01-01", 300, close=100.0)
    live = rescaled(cached[-200:], 1.0)
    live[7] = {**live[7], "Close": live[7]["Close"] * 1.02}
    report = cache_warmup.seam_report(cached, live)
    assert report["ok"] is True
    assert report["worst_bar"] > report["worst"], "the outlier is still reported"


def test_a_systematic_difference_still_fails_under_the_percentile():
    """Most of the distribution has to move for this to be a different series — and does."""
    cached = bars("2026-01-01", 300, close=100.0)
    live = [{**b, "Close": b["Close"] * (1.0 if i % 2 else 1.4)}
            for i, b in enumerate(cached[-200:])]
    assert cache_warmup.seam_report(cached, live)["ok"] is False


def test_the_splice_is_skipped_when_the_vendor_window_already_fills_the_buffer():
    """At 1d and 4h the request cap is decades, so there is nothing in front to add.

    Reading a parquet per symbol PER BOOK on a hundred-name universe is tens of seconds of
    start-up bought for zero bars, so the buffer's own length is the guard.
    """
    window = 100
    live = bars("2026-01-01", window)
    assert len(live) >= window, "the precondition the guard tests for"
    # `splice` itself would still be correct here; the point is that it is not called.
    merged = cache_warmup.splice(bars("2020-01-01", 500), live, 1.0, limit=window)
    assert merged == live, "and if it were called, it would change nothing"
