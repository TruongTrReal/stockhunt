# The 751-name point-in-time S&P 500 sheets

Every `us_stocks` result in this folder was measured on `config.SP500_UNIVERSE` — 503
current S&P 500 members plus the 248 departures Twelve Data would price — which was
`config.US_STOCKS` from **2026-08-09 to 2026-08-12**.

They are archived rather than deleted because they are still reproducible: the universe is
still named in `config`, and pointing a stage at `SP500_UNIVERSE` regenerates them.

## Do not compare a number here against a current one

Three things changed at once on 2026-08-12, and any two of them are enough to make a
side-by-side meaningless:

1. **The universe is the point-in-time top 100, not the whole index.** ~100 names live per
   bar instead of ~500, selected by trailing 252-day median dollar volume among S&P
   members, annually, with a 120-rank buffer. See `backtest engine/top100_membership.py`.

2. **Names are cut at both ends of their top-100 career, not just the tail.** Previously a
   name was truncated only at its S&P exit. It is now also truncated at its *entry* to the
   top 100, so NVDA's small-cap decade no longer contributes to a large-cap study. This
   shortens many series substantially.

3. **85 series in this folder are a different company.** A bare ticker resolves against
   every venue Twelve Data carries, and for 85 of the 739 cached `us_stocks` names the
   vendor has no US listing at all, so it served a foreign namesake for the entire length
   of the series — `CTRA` was Ciputra Development Tbk PT on the Indonesia Stock Exchange,
   `STJ` was St. James's Place on the LSE, `K` was Kinross Gold on the TSX. Those bars
   are in every sheet here. They were structurally perfect and passed every check that
   existed at the time; `check_data.wrong_instrument_reason` is the check that now catches
   them, and `td_loader.US_LISTED_CLASSES` stops them at the source.

Point 3 alone means these sheets should be read as **contaminated**, not merely
superseded. The contamination is not uniform — it is concentrated in departed names, and
it flattered whatever a foreign small cap's price grid flatters, which for a
mean-reversion rule is a great deal.
