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


def test_agreement_on_the_overlap_is_accepted():
    cached = bars("2026-01-01", 200)
    arriving = cached[-50:]
    report = cache_warmup.seam_report(cached, arriving)
    assert report["ok"] is True
    assert report["overlap"] == 20, "capped at SEAM_BARS; one revision must not decide it"


def test_a_split_sized_disagreement_is_refused():
    """The case this check exists for: a cache written before a corporate action."""
    cached = bars("2026-01-01", 200, close=100.0)
    arriving = [dict(b, Close=b["Close"] / 4.0) for b in cached[-50:]]
    report = cache_warmup.seam_report(cached, arriving)
    assert report["ok"] is False
    assert "different series" in report["reason"]


def test_a_tiny_revision_is_tolerated():
    """A settled bar can be revised by a basis point; that is not two histories."""
    cached = bars("2026-01-01", 200, close=100.0)
    arriving = [dict(b, Close=b["Close"] * 1.00001) for b in cached[-50:]]
    assert cache_warmup.seam_report(cached, arriving)["ok"] is True


def test_a_disagreement_just_past_the_tolerance_is_refused():
    cached = bars("2026-01-01", 200, close=100.0)
    over = 1.0 + cache_warmup.SEAM_TOLERANCE * 2
    arriving = [dict(b, Close=b["Close"] * over) for b in cached[-50:]]
    assert cache_warmup.seam_report(cached, arriving)["ok"] is False


def test_no_overlap_is_a_failure_and_not_a_pass():
    """A hole between the two is as wrong as a step, and far easier to miss.

    Zero overlapping bars means zero disagreements, which an emptier check would read as
    agreement — and the buffer would then be two series with a gap in the middle.
    """
    cached = bars("2026-01-01", 100)
    arriving = bars("2026-06-01", 100)
    report = cache_warmup.seam_report(cached, arriving)
    assert report["ok"] is False
    assert "hole between them" in report["reason"]


def test_an_empty_side_is_a_failure():
    assert cache_warmup.seam_report([], bars("2026-01-01", 10))["ok"] is False
    assert cache_warmup.seam_report(bars("2026-01-01", 10), [])["ok"] is False


def test_the_reason_tells_the_reader_what_to_do():
    cached = bars("2026-01-01", 200, close=100.0)
    arriving = [dict(b, Close=b["Close"] / 2.0) for b in cached[-50:]]
    assert "Re-fetch" in cache_warmup.seam_report(cached, arriving)["reason"]


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
