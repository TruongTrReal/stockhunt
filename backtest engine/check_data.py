"""Scan the cache for OHLC bars that cannot be real, and other integrity faults.

Found because NautilusTrader refused to accept some Twelve Data 1-hour equity bars
("high was < close", "low was > open"). The vectorised sweep reads Close only, so it
would have consumed those bars in silence forever — which is precisely the class of
problem a second engine exists to catch.

An inconsistent bar is not merely cosmetic. Any rule reading High or Low — the
mean-reversion bands, ATR-scaled rules, the whole candlestick family, BETA/CORREL — is
computing on a bar that never existed.

Run::

    python check_data.py                # scan everything cached
    python check_data.py --fix          # additionally write repaired parquet
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from config import (CANDIDATE_TZS, CLASSES, DATA_DIR, MIN_PRICE_USD, RESULTS_DIR,
                    TIMEFRAMES, cache_dir, cache_tz, safe_symbol, session_anchor_tz)
import td_loader


# A bar whose close sits this far from the local median is a vendor misprint, not a
# move. Observed cases are decimal-point errors of 1000-10000x (BTC 28,100 -> 2.812 ->
# 28,118 in consecutive minutes), so the threshold is set far outside anything a real
# crypto minute does.
SPIKE_LO, SPIKE_HI = 0.2, 5.0
SPIKE_WINDOW = 5


def spike_mask(close: np.ndarray) -> np.ndarray:
    """Bars whose close is a wild outlier against a centred rolling median.

    Single-bar price spikes are invisible to the OHLC consistency checks — each bad bar
    is internally consistent (its own high/low bracket its own close). They only show up
    bar-to-bar, and they are devastating in combination with the -0.999 return floor:
    the crash leg gets clipped, the recovery leg does not, so each spike pair multiplies
    equity by ~10x. 125 of them on BTC 1-minute turned buy-and-hold into 1e125.
    """
    s = pd.Series(close)
    med = s.rolling(SPIKE_WINDOW, center=True, min_periods=2).median().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = np.where(med > 0, close / med, 1.0)
    return (ratio < SPIKE_LO) | (ratio > SPIKE_HI)


def faults(df: pd.DataFrame) -> dict[str, np.ndarray]:
    o, h, l, c = (df[k].to_numpy(dtype="float64") for k in ("Open", "High", "Low", "Close"))
    return {
        "high_lt_low": h < l,
        "high_lt_open": h < o,
        "high_lt_close": h < c,
        "low_gt_open": l > o,
        "low_gt_close": l > c,
        "nonpositive_close": ~(c > 0),
        "nonfinite": ~(np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c)),
        "price_spike": spike_mask(c),
    }


def repair(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop impossible bars, then widen High/Low to contain Open and Close.

    Three different faults, repaired differently because they mean different things:

    * **Price spikes are removed, not adjusted.** A bar printing 1/10,000 of the true
      price is not a mispriced extreme, it is a bar that did not happen; there is no
      honest value to substitute, so the row is dropped and the series closes over the
      gap. Interpolating would invent a trade.
    * **Non-positive and non-finite closes are removed for the same reason**, and this
      is not hypothetical: `CBE` arrives with 3,894 of 7,368 closes at or below zero and
      `MRO` with 1,121 of 5,762. A close of zero or less makes `close[t]/close[t-1]`
      infinite or sign-flipped — CBE's raw series produces a +4,430% bar, a -270% bar,
      and a buy-and-hold equity of **3.03e-07**. This is the same failure mode as the
      BTC decimal-point spikes that once printed a $2.9e129 PnL, and it was likewise
      invisible to the OHLC consistency check, because a bar whose High and Low bracket
      its own impossible Close is internally consistent.
    * **High/Low are widened** to contain Open and Close. Those two are prices actually
      transacted at, so a High below either is a fault in the reported *extreme*. Widening
      to the minimum consistent range can only shrink an ATR or a band, never invent a
      move.

    Dropping is necessary but it is not sufficient: a series that loses half its bars in
    scattered pieces has surviving returns measured across multi-day gaps, which is a
    different instrument. `unusable_fraction` is what decides whether the symbol should
    be in the universe at all.
    """
    out = df.copy()
    c = out["Close"].to_numpy(dtype="float64")
    impossible = ~(c > 0) | ~np.isfinite(c)
    n_impossible = int(impossible.sum())
    if n_impossible:
        out = out.loc[~impossible]

    spikes = spike_mask(out["Close"].to_numpy(dtype="float64"))
    n_spikes = int(spikes.sum()) + n_impossible
    if int(spikes.sum()):
        out = out.loc[~spikes]

    o, c = out["Open"].to_numpy(), out["Close"].to_numpy()
    hi = np.maximum(out["High"].to_numpy(), np.maximum(o, c))
    lo = np.minimum(out["Low"].to_numpy(), np.minimum(o, c))
    n_hl = int((hi != out["High"].to_numpy()).sum() + (lo != out["Low"].to_numpy()).sum())
    out["High"], out["Low"] = hi, lo
    return out, n_spikes + n_hl


# Above this share of impossible closes, repairing the symbol is not the right move —
# what survives is a different instrument with returns measured across the holes.
UNUSABLE_QUARANTINE = 0.02

# Quarantine on the RAW size of the surviving move, not on how many sigmas it is.
#
# A robust-sigma rule was tried first and it does not work, for a reason worth recording:
# MAD-scaled sigma measures the *typical* bar, and these series are fat-tailed enough
# that real events sit tens of sigmas out. At 25 sigma the rule flagged BTC's -19% 4h
# bar (30 sigma), silver's -14% (29) and WTI's +18% (26) — all genuine market moves,
# every one of which must stay in the sample, because a backtest that has never seen a
# crash is worth nothing.
#
# What actually needs excluding is the thing that makes an equity curve explode, and that
# is an absolute magnitude no market produces: `IMX/USD` +657,666% in one bar, `OP/USD`
# +1,109,948%, `MRO` +4,600%. 10x in a single bar is the line — above it the print is a
# vendor artifact, below it the tail is real and belongs in the sample. Sigma is still
# computed and still reported, as a flag for a human, never as a filter.
SPIKE_QUARANTINE = 9.0          # +900% in one bar
SIGMA_REPORT = 25.0             # reported for attention; does NOT exclude
QUARANTINE_CSV = "quarantine.csv"

# A ticker is not an identity, and Twelve Data resolves a bare one against every venue it
# carries. `sp500_membership.py` asks `/earliest_timestamp` whether `FL` prices; something
# answers yes, and `td_loader` then fetches whatever instrument currently wears those two
# letters. For the departed S&P names that is routinely an unrelated OTC listing: "FL"
# returns a $0.39 stock trading **$7,494 a day** for the window Foot Locker was in the
# index, "GR" $2,171/day, "COV" $1,223/day, "RX" exactly $0.
#
# Nothing above catches this. The series is internally consistent — no impossible closes,
# no 10x bar, its own highs correctly bracketing its own lows — so it passes every
# structural check, and `check_data --fix` cheerfully repairs bars that were never the
# right company's to begin with.
#
# What it does to a result is specific and severe. At $0.28 a share a one-cent tick is a
# **3.6% move**, so the bid-ask bounce on the vendor's own rounding grid dwarfs the 15-19bp
# retail cost grid, which was calibrated on mega-caps. Any mean-reversion rule harvests
# that grid: `ibs` buys the low and sells the high 58 times a year and compounds to
# **6.4e17%** on FL over 25 years, ranking it first of 704 assets with IR +1.06. It is not
# an edge and it is not even a market — it is quantization noise, levered by turnover.
#
# The test is liquidity, because that is what index membership actually asserts. It is
# measured on the PEAK — the best rolling one-year stretch the series ever had — and the
# distinction is not a detail. A whole-series median averages a 2026 mega-cap against its
# own 1982 small-cap history and flags `TYL` ($864k/day lifetime, ~$200M today), `CRH`,
# `WST` and `FRT`, all of which are genuinely in the index. What membership claims is that
# the name was, at some point, one of the largest listings in the country — a statement
# about the maximum, never about the mean.
#
# Measured that way the two populations do not touch. The thinnest of the 503 current
# members is `NWS` at **$32.1M/day** and the 1st percentile is $83M; the fattest impostor
# is `ARG` at **$3.4M**, and `FL`, `GR` and `LEH` come in at $728k, $15k and $182. The
# threshold sits at $10M — 3x below the thinnest real member and 3x above the fattest
# impostor, which is as close to the middle of an empty order of magnitude as it gets.
#
# Too little history is UNKNOWN, not FAIL: `FDXF` and `HONA` are 2026 spin-offs with under
# 60 priced days, and a liquidity claim cannot be evaluated on them yet.
#
# Volume-gated on purpose: Twelve Data serves no crypto volume at all (the field is absent,
# not zero), so this must never fire on a class where the input does not exist.
# `us_stocks` ONLY, and the restriction is the argument rather than an oversight. The test
# is not "thin things are bad", it is "this series contradicts the one thing membership
# asserts about it". No such claim exists for `us_etfs`, where thin is a legitimate design:
# COPX turns over $837k/day and is exactly the copper-miner fund it says it is. Applying
# the floor there would delete a real ETF to catch an impostor that is not in that class.
DOLLAR_VOLUME_QUARANTINE = 10_000_000.0     # peak 1y median $/day
LIQUIDITY_WINDOW = 252                      # trading days in the peak window
LIQUIDITY_MIN_DAYS = 60                     # below this the claim is untestable
MIN_PRICED_BARS = 100
EQUITY_CLASSES = ("us_stocks",)

# How long a hole has to be before the bar after it stops being a bar. Per class, because
# the classes do not keep the same hours: crypto trades continuously so any multi-day hole
# is an outage, while a commodity pair still stops for a weekend.
#
# EQUITIES ARE DELIBERATELY ABSENT, and the reason is a trap worth recording. "Keep the
# segment after the hole" is right when the hole is a vendor outage — `LTC/USD` goes quiet
# for 157 days and comes back as the same Litecoin. It is exactly backwards when the hole
# is a ticker being REISSUED, because then the later segment is the impostor and the
# earlier one is the real company. A full scan proposed truncating `BBBY` to 2025-08-22
# (discarding 7,826 real bars to keep whatever holds those letters now), `APC` to
# 2026-02-10 and `CBE` to 2021-10-25 — precisely the wrong half in every case.
#
# Equities do not need this rule anyway: `td_loader.membership_exits` already truncates
# every departed name at its index exit, which cuts the reissued segment off at the front
# of the problem rather than guessing from the shape of a gap. Crypto pairs and spot
# commodities are not reissued to other issuers, so there the later segment is always the
# same instrument and the rule is safe.
MAX_GAP_DAYS = {"crypto": 7, "commodities": 10}


def unusable_fraction(df: pd.DataFrame) -> float:
    c = df["Close"].to_numpy(dtype="float64")
    return float((~(c > 0) | ~np.isfinite(c)).mean()) if len(c) else 1.0


def implausible_return(df: pd.DataFrame) -> tuple[float, float]:
    """Largest surviving log return, in robust sigmas, and its raw size.

    Repairing the bars is not the same as repairing the series. Dropping `MRO`'s 1,121
    impossible closes leaves a hole the price jumps across, and what comes out the far
    side is a +4,600% bar and a buy-and-hold equity of **3.2e17** — a series that passes
    every structural check and would contaminate every aggregate it touches, exactly as
    the BTC decimal spikes did before they were caught by looking at a rendered number
    rather than at a schema.

    Scale is measured with the median absolute deviation rather than the standard
    deviation, because the outlier being hunted is itself in the sample and would inflate
    an SD enough to hide behind.
    """
    c = df["Close"].to_numpy(dtype="float64")
    c = c[np.isfinite(c) & (c > 0)]
    if c.size < 30:
        return 0.0, 0.0
    lr = np.diff(np.log(c))
    simple = np.expm1(lr)

    # Rank on the SIMPLE return, and specifically its upside, not on |log return|.
    #
    # A log ranking has a blind spot that is not theoretical: `BMS` falls -99.0% and
    # recovers +9,900% on consecutive bars — a decimal-point spike pair — and those are
    # log -4.605 and +4.605, *exactly* equal in magnitude. `argmax(|log r|)` therefore
    # picks the crash leg, reports -99%, and a threshold on that passes it as plausible
    # while the +9,900% leg walks straight into the sweep.
    #
    # The asymmetry is the whole point and it is the same one the repo has been bitten by
    # before: a simple return is floored at -100% but unbounded above, and the engine's
    # `RETURN_FLOOR = -0.999` clips the crash leg while leaving the recovery intact. The
    # pair does not cancel; it multiplies equity by ~100x. So the explosive direction is
    # the one that must be measured.
    worst = int(np.argmax(simple))
    mad = float(np.median(np.abs(lr - np.median(lr))))
    sigma = mad * 1.4826
    if not np.isfinite(sigma) or sigma <= 0:
        return 0.0, float(simple[worst])
    return float(abs(lr[worst]) / sigma), float(simple[worst])


def gap_break(df: pd.DataFrame, max_gap_days: int) -> pd.Timestamp | None:
    """Start of the usable segment when the series has a hole too large to price across.

    A hole is not a missing bar; it is a missing *period*, and the next bar's return
    silently contains all of it. `LTC/USD` stops on 2020-09-13 at $48.68 and resumes on
    2021-02-17 at $237.11 — the vendor has no data for 157 days, so the first bar back
    books **+387% in one day** for a rally that actually took five months. Any rule holding
    across it banks the whole move as a single bar, which is the decimal-spike failure in a
    different costume: one fabricated bar return, compounding into every aggregate.

    Nothing above catches it. Both bars are internally consistent, the move is under
    `SPIKE_QUARANTINE`'s 900% line, and the OHLC scan sees two perfectly ordinary rows.

    Quarantining the symbol would be the heavier answer and the wrong one — 5.5 years of
    LTC after the hole is real data, and a 32-name crypto universe cannot spare a name to
    tidy up a vendor outage. So the series is truncated to the segment AFTER its worst
    break, keeping whichever side is longer only if that side is the later one; the earlier
    segment is discarded because a strategy cannot trade a window the data resumes after.

    Returns None when the series is continuous enough to use whole.
    """
    idx = df.index
    if len(idx) < 3:
        return None
    gaps = pd.Series(idx).diff().dt.days
    if gaps.max() is None or not (gaps.max() > max_gap_days):
        return None
    # The LAST oversized break, not the largest: everything before it is unreachable
    # anyway, so the usable series starts after the final hole.
    return idx[int(gaps[gaps > max_gap_days].index[-1])]


# How long a hole has to be to count as a weekly session boundary rather than a lunch
# break or a holiday. 12 hours clears the ~1h CME settlement pause on spot metals and the
# overnight gap on anything that closes, and is far under the ~48h a weekend leaves.
SESSION_GAP_HOURS = 12
# The verdict is RELATIVE, not absolute, and the absolute version had to be abandoned to
# get it right. A first cut failed any series whose reopen did not concentrate on one
# (weekday, hour) bucket at 60%, and that is not the same question: `XPT/USD` reads 0.596
# on its own correct clock because platinum keeps more irregular holidays than gold, while
# `WTI` reads 0.542 on a clock that is ten hours wrong. An absolute floor cannot separate a
# ragged market from a wrong timezone; the comparison against every OTHER candidate zone
# can, and it is what identified `Australia/Sydney` in the first place rather than merely
# suspecting it — 0.46 declared against 0.80 there.
#
# So a class fails when some other zone explains its session boundary MATERIALLY better.
CLOCK_FIT_MARGIN = 0.10
# ...and below this, no zone explains the boundary and the series cannot be judged at all
# — reported as unknown rather than as a pass, because a pass here would be a claim.
CLOCK_UNJUDGEABLE = 0.45


def clock_fit(index: pd.DatetimeIndex, read_as: str, anchor: str) -> tuple[float, str]:
    """How sharply this series' weekly reopen lands on one instant, read in `read_as`.

    Returns `(share, label)` — the fraction of reopens falling in the modal (weekday,
    hour) bucket of the `anchor` zone, and what that bucket is.

    **This is the only kind of test that can see a wrong clock, and it is worth saying
    why no bar-level test can.** A series stamped in the wrong timezone is not malformed:
    every bar's High brackets its own Low, the volume ties out, the instrument is the
    right one, the sequence is complete. Only the *joint* between the stamps and the
    world is wrong. It is the same family as the foreign namesakes and as EEM, and it
    needs the same kind of answer — an external fact, checked against.

    The external fact here is that a market reopens at a wall clock somebody published:
    spot metals at 18:00 New York on a Sunday, Globex at 17:00 Chicago, a US equity
    session at 09:30 New York. Read in the right zone every weekly gap ends at that one
    instant; read an hour or ten out, the reopen smears across the year as the two zones'
    daylight-saving calendars slide past each other.
    """
    if len(index) < 3:
        return 0.0, "too few bars"
    stamps = pd.Series(index)
    reopen = stamps[stamps.diff() > pd.Timedelta(hours=SESSION_GAP_HOURS)]
    if len(reopen) < 20:
        return 0.0, "too few weekly gaps"
    idx = pd.DatetimeIndex(reopen.values)
    # `shift_forward`/`ambiguous=True` only ever move a handful of stamps by an hour and
    # this is a modal statistic over hundreds; a DST edge cannot decide the answer.
    local = (idx.tz_localize(read_as, ambiguous=True, nonexistent="shift_forward")
             .tz_convert(anchor))
    # Bucketed on the HOUR alone, not on (weekday, hour). The hour is the only dimension
    # a timezone error moves, and adding the weekday makes the statistic answer a
    # different question on a class that closes every night: `us_etfs` reopens at 09:30
    # New York five days a week, so a (weekday, hour) mode caps at ~20% and the check
    # would call a perfectly correct equity cache unjudgeable. On the hour alone it reads
    # ~100%, and the commodity separation is unchanged — 0.81 on the true clock against
    # 0.52 on UTC, exactly as it was.
    top = pd.Series(local.hour).value_counts()
    hour, n = int(top.index[0]), int(top.iloc[0])
    days = pd.Series(local.dayofweek)[local.hour == hour].mode()
    name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][int(days.iloc[0])]
    return n / len(local), f"{name} {hour:02d}:00 x{n}/{len(local)}"


def clock_verdict(index: pd.DatetimeIndex, asset_class: str) -> dict | None:
    """Does this series' observed session boundary agree with its DECLARED clock?

    `None` when the class has no weekly boundary to measure (crypto is 24/7) or the
    series is too short. Otherwise a row naming the declared clock's fit, and the
    best-fitting candidate — which is how `Australia/Sydney` was identified rather than
    merely suspected.
    """
    anchor = session_anchor_tz(asset_class)
    if anchor is None:
        return None
    declared = cache_tz(asset_class)
    fit, label = clock_fit(index, declared, anchor)
    if not fit:
        return None
    best_tz, best_fit, best_label = declared, fit, label
    for cand in CANDIDATE_TZS:
        f, lab = clock_fit(index, cand, anchor)
        if f > best_fit:
            best_tz, best_fit, best_label = cand, f, lab
    return {"declared": declared, "fit": round(fit, 3), "boundary": label,
            "best_tz": best_tz, "best_fit": round(best_fit, 3),
            "best_boundary": best_label,
            "judgeable": bool(best_fit >= CLOCK_UNJUDGEABLE),
            "ok": bool(best_fit < CLOCK_UNJUDGEABLE
                       or best_fit - fit <= CLOCK_FIT_MARGIN)}


def peak_dollar_volume(df: pd.DataFrame) -> float | None:
    """Best rolling-1y median of dollars traded per DAY. None when it cannot be judged.

    Summed to calendar days before any median is taken, so the same threshold means the
    same thing on 1d and on 5m. A per-bar figure would scale with the timeframe and
    quarantine an entire intraday sheet for being sliced thinner.
    """
    if "Volume" not in df.columns:
        return None
    c = df["Close"].to_numpy(dtype="float64")
    v = df["Volume"].to_numpy(dtype="float64")
    ok = np.isfinite(c) & (c > 0) & np.isfinite(v)
    if ok.sum() < MIN_PRICED_BARS:
        return None
    daily = pd.Series(c[ok] * v[ok], index=df.index[ok]).resample("D").sum()
    daily = daily[daily.index.dayofweek < 5]        # weekends are not thin days
    if len(daily) < LIQUIDITY_MIN_DAYS:
        return None
    # Median inside the window, max across windows: one busy week cannot rescue a series
    # that was never liquid, and one quiet year cannot condemn one that was.
    # All-zero volume is the vendor declining to report, not a security nobody traded.
    # It still fails — but as a missing identity, not as a measured one.
    peak = daily.rolling(LIQUIDITY_WINDOW, min_periods=LIQUIDITY_MIN_DAYS).median().max()
    return None if not np.isfinite(peak) else float(peak)


# ---------------------------------------------------------------- wrong instrument
#
# The liquidity test above catches an impostor that is THIN. It is blind to one that is
# FAT, and the fat ones are the dangerous ones because nothing else in this file can see
# them either: the series is internally consistent, its highs bracket its lows, it has no
# 10x bar, and it turns over billions a day. It is simply a different company.
#
# `CTRA` is the case that exposed it. Twelve Data has no US listing for Coterra Energy, so
# a bare-symbol fetch returned **Ciputra Development Tbk PT** on the Indonesia Stock
# Exchange — 6,405 bars of rupiah, priced ~$851, turning over an apparent $18.8B/day. It
# passed every structural check, it passed the liquidity floor by three orders of
# magnitude, and it ranked **3rd largest US stock in 2026** in the point-in-time top-100
# universe. AGN, CA and STJ won slots the same way.
#
# The test cannot be computed from the bars, because the bars are not wrong — they are
# somebody else's. It has to ask the vendor whether a US listing exists at all, which is a
# network call, so the probe and its application are deliberately split:
#
#   `--probe-listing` does the network round and CACHES a verdict per symbol;
#   `quarantine_reason` reads that cache offline and quarantines on it, so an ordinary
#   `check_data.py --fix` applies the finding without needing a key.
#
# Measured 2026-08-12 over all 739 cached us_stocks names: 85 have no US listing on this
# vendor at any point in history — not a stale quote, no bars ever — so nothing is
# recoverable by refetching and quarantine is the only correct action. Most are S&P names
# the vendor purged after they were acquired (DFS, JNPR, K, X, MRO, SWN), which is the
# already-documented "Twelve Data serves no delisted equities"; the new part is that it
# does not return *nothing* for them, it returns *somebody else*.
#
# `td_loader.US_LISTED_CLASSES` stops this at the source for every future fetch. This is
# the check that the existing cache is clean.
LISTING_PROBE_CSV = "us_listing_probe.csv"
_LISTING_CACHE: dict[str, dict] | None = None


def listing_verdicts() -> dict[str, dict]:
    """`symbol -> probe row`, from the cached `--probe-listing` result. Empty if never run.

    Empty means "unknown", never "clean": a symbol absent from the cache is not
    quarantined, so a repo that has not probed behaves exactly as it did before.
    """
    global _LISTING_CACHE
    if _LISTING_CACHE is None:
        path = DATA_DIR / "reference" / LISTING_PROBE_CSV
        _LISTING_CACHE = {}
        if path.exists():
            try:
                p = pd.read_csv(path)
                for row in p.to_dict("records"):
                    _LISTING_CACHE[str(row["symbol"])] = row
            except Exception:
                _LISTING_CACHE = {}
    return _LISTING_CACHE


def probe_us_listings(symbols: list[str], key: str) -> pd.DataFrame:
    """Ask the vendor whether each ticker resolves to a US listing, and to what if not.

    Two questions, because they have different answers and only the second is fatal:

      1. is there a US quote TODAY? A no is not yet damning — the company may simply have
         been acquired, and its historical bars can still be genuine.
      2. is there US HISTORY? This is the one that decides. If the vendor can serve US
         bars for the ticker in any window, the cached series is the wrong instrument and
         is refetchable. If it cannot, it never had the company at all and every cached
         bar is a foreign namesake.
    """
    import requests
    import td_loader

    rows = []
    for i, sym in enumerate(symbols, 1):
        api = td_loader._api_symbol(sym)
        us_now = us_hist = None
        bare_name = bare_exch = None
        try:
            td_loader._spend_credits(1)
            q = requests.get("https://api.twelvedata.com/quote",
                             params={"symbol": api, "country": "United States",
                                     "apikey": key}, timeout=30).json()
            us_now = float(q["close"]) if q.get("close") else None
        except Exception:
            pass
        if us_now is None:
            try:
                td_loader._spend_credits(1)
                h = requests.get("https://api.twelvedata.com/time_series",
                                 params={"symbol": api, "interval": "1day",
                                         "country": "United States",
                                         "start_date": "2010-01-01",
                                         "end_date": "2012-01-01", "adjust": "all",
                                         "order": "ASC", "apikey": key}, timeout=60).json()
                us_hist = len(h["values"]) if h.get("status") == "ok" and h.get("values") else 0
                td_loader._spend_credits(1)
                b = requests.get("https://api.twelvedata.com/quote",
                                 params={"symbol": api, "apikey": key}, timeout=30).json()
                bare_name, bare_exch = b.get("name"), b.get("exchange")
            except Exception:
                pass
        rows.append({"symbol": sym, "us_quote": us_now,
                     "us_history_bars": us_hist if us_now is None else None,
                     "has_us_listing": bool(us_now is not None or (us_hist or 0) > 0),
                     "resolves_to": bare_name, "resolves_exchange": bare_exch})
        if i % 50 == 0:
            print(f"  probed {i}/{len(symbols)}", flush=True)
    return pd.DataFrame(rows)


def wrong_instrument_reason(symbol: str, asset_class: str | None) -> str | None:
    """Quarantine reason from the cached listing probe, or None."""
    if asset_class not in EQUITY_CLASSES:
        return None
    row = listing_verdicts().get(symbol)
    if row is None or bool(row.get("has_us_listing")):
        return None
    who = row.get("resolves_to")
    where = row.get("resolves_exchange")
    if isinstance(who, str) and who.strip():
        return (f"no US listing on this vendor -- the cached bars are "
                f"{who} ({where}), a different company that shares the ticker")
    return ("no US listing on this vendor, at any point in history -- the cached bars "
            "cannot be this member and are not refetchable")


def quarantine_reason(df: pd.DataFrame, asset_class: str | None = None,
                      symbol: str | None = None) -> str | None:
    """Why this symbol must not enter a sweep, or None if it may."""
    if symbol is not None:
        why = wrong_instrument_reason(symbol, asset_class)
        if why:
            return why
    frac = unusable_fraction(df)
    if frac > UNUSABLE_QUARANTINE:
        return f"impossible closes {frac:.1%} of bars"
    sig, raw = implausible_return(df)
    if abs(raw) > SPIKE_QUARANTINE:
        return f"surviving {raw:+.0%} bar ({sig:.0f} robust sigma)"
    if asset_class in EQUITY_CLASSES:
        # The LATEST close, not the median and not any historical bar. Under `adjust=all`
        # every earlier price is today's share reflated backwards through its splits, so
        # only the last one is a number anybody could actually pay. See the note on
        # `config.MIN_PRICE_USD`: applied to the stored history instead, this deletes NVDA
        # and NFLX and catches nothing at all.
        c = df["Close"].to_numpy(dtype="float64")
        c = c[np.isfinite(c) & (c > 0)]
        if c.size and c[-1] < MIN_PRICE_USD:
            return f"trades at ${c[-1]:,.2f} -- under the ${MIN_PRICE_USD:,.2f} floor"
        dv = peak_dollar_volume(df)
        if dv is not None and dv < DOLLAR_VOLUME_QUARANTINE:
            px = float(np.nanmedian(df["Close"].to_numpy(dtype="float64")))
            return (f"peak 1y volume ${dv:,.0f}/day at ${px:,.2f} -- never an "
                    f"index-sized listing, so not the member this ticker names")
    return None


def fat_tail_note(df: pd.DataFrame) -> str | None:
    """A large-but-plausible extreme, reported so a human can look. Never excludes."""
    sig, raw = implausible_return(df)
    if abs(raw) <= SPIKE_QUARANTINE and sig > SIGMA_REPORT:
        return f"{raw:+.0%} bar at {sig:.0f} robust sigma"
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fix", action="store_true")
    # Scoping exists because a full scan reads ~60M bars, most of them crypto 1-minute,
    # to re-check a cache that was already repaired. After fetching one new sheet the
    # only thing worth scanning is that sheet.
    ap.add_argument("--class", dest="classes", nargs="+", choices=list(CLASSES),
                    default=list(CLASSES))
    ap.add_argument("--tf", dest="timeframes", nargs="+", choices=list(TIMEFRAMES),
                    default=list(TIMEFRAMES))
    ap.add_argument("--check-clock", action="store_true",
                    help="does each class's DECLARED intraday clock match the session "
                         "boundary its bars actually show? Offline. Catches a timezone "
                         "the vendor never declared, which no bar-level test can see.")
    ap.add_argument("--probe-listing", action="store_true",
                    help="ask Twelve Data whether each equity ticker has a US listing at "
                         "all, and cache the verdict. Network + ~1-3 credits per symbol. "
                         "Catches a FAT impostor, which no bar-level test can see.")
    args = ap.parse_args()

    if args.check_clock:
        # Read the parquet directly rather than through `td_loader.load`. The clock is a
        # property of the FILE, and `load` applies `BACKTEST_START`, the quarantine and
        # each class's head cut — every one of which could trim away the weekly gaps this
        # measures, and none of which changes what zone the stamps are in.
        rows = []
        for asset_class in args.classes:
            for timeframe in args.timeframes:
                if not TIMEFRAMES[timeframe]["intraday"]:
                    continue
                # Only sizes at or under an hour can answer this, and the reason is the
                # test's own arithmetic rather than anything about the data. A bar is
                # labelled by its OPEN, so the reopen shows up on the label of whatever
                # bar contains it; converting that label into the anchor zone moves it by
                # one hour across the anchor's own DST change. Under an hour that wobble
                # lands on separate buckets and the modal one is still the boundary. At
                # 4h it does not: the true 22:00/23:00 UTC reopen sits inside the SAME
                # 20:00 UTC bucket all year, so the raw UTC label is sharper than the
                # anchor-local one and the test scores the correct cache as wrong. The
                # clock is a property of the CLASS, not of a sheet — every size came from
                # the same vendor stamps — so the fine sheets settle it for all of them.
                if 60 % (int(timeframe[:-1]) * (60 if timeframe.endswith("h") else 1)):
                    continue
                for path in sorted(cache_dir(asset_class, timeframe).glob("*.parquet")):
                    v = clock_verdict(pd.read_parquet(path).index, asset_class)
                    if v:
                        rows.append({"class": asset_class, "timeframe": timeframe,
                                     "symbol": path.stem, **v})
        if not rows:
            print("no intraday series with a measurable weekly session boundary")
            return
        clocks = pd.DataFrame(rows)
        bad = clocks[~clocks["ok"]]
        print("=== declared intraday clock vs observed session boundary ===")
        print(clocks.to_string(index=False))
        if not bad.empty:
            print(f"\n{len(bad)} of {len(clocks)} series CONTRADICT their declared clock. "
                  f"A wrong clock is not a malformed bar - fix it in "
                  f"`config.INTRADAY_CLOCK` and restamp the cache with "
                  f"`migrate_cache_clock.py --write`, then rerun this.")
            raise SystemExit(1)
        unjudged = int((~clocks["judgeable"]).sum())
        print(f"\nall {len(clocks)} series agree with `config.INTRADAY_CLOCK`"
              + (f" ({unjudged} could not be judged: no zone concentrates their session "
                 f"boundary, so this is 'not contradicted', not 'confirmed')"
                 if unjudged else ""))
        return

    if args.probe_listing:
        import td_loader as _td
        syms = sorted({p.stem.replace("_", "/") if "_" in p.stem else p.stem
                       for cls in args.classes if cls in EQUITY_CLASSES
                       for tf in args.timeframes
                       for p in cache_dir(cls, tf).glob("*.parquet")})
        print(f"probing {len(syms)} equity tickers for a US listing...")
        probe = probe_us_listings(syms, _td.api_key())
        ppath = DATA_DIR / "reference" / LISTING_PROBE_CSV
        probe.to_csv(ppath, index=False)
        bad = probe[~probe["has_us_listing"]]
        print(f"wrote {ppath}  ({len(probe)} probed, {len(bad)} with NO US listing)")
        if not bad.empty:
            print(bad[["symbol", "resolves_to", "resolves_exchange"]].to_string(index=False))
        # Drop the memoised verdicts so the scan below sees what was just written.
        globals()["_LISTING_CACHE"] = None
    full_scan = (len(args.classes) == len(CLASSES)
                 and len(args.timeframes) == len(TIMEFRAMES))

    rows = []
    quarantined: list[dict] = []
    fat_tails: list[dict] = []
    gapped: list[dict] = []
    gapped: list[dict] = []
    for asset_class in args.classes:
        for timeframe in args.timeframes:
            data = td_loader.load(asset_class, timeframe, skip_quarantined=False)
            for symbol, df in data.items():
                f = faults(df)
                total = int(sum(int(v.sum()) for v in f.values()))
                if total:
                    rows.append({"class": asset_class, "timeframe": timeframe,
                                 "symbol": symbol, "n_bars": len(df),
                                 **{k: int(v.sum()) for k, v in f.items()},
                                 "total_faults": total,
                                 "pct": 100.0 * total / len(df)})
                # Truncation is judged before repair and applied with it, because the two
                # answer different questions: `repair` fixes bars that are wrong, this
                # drops a period that is absent. A series can need both.
                cut = (gap_break(df, MAX_GAP_DAYS[asset_class])
                       if asset_class in MAX_GAP_DAYS else None)
                if cut is not None:
                    gapped.append({"class": asset_class, "timeframe": timeframe,
                                   "symbol": symbol, "usable_from": str(cut.date()),
                                   "bars_dropped": int((df.index < cut).sum())})
                    if args.fix:
                        df = df.loc[df.index >= cut]
                        df.to_parquet(cache_dir(asset_class, timeframe)
                                      / f"{safe_symbol(symbol)}.parquet")
                if args.fix and total:
                    df, n = repair(df)
                    df.to_parquet(cache_dir(asset_class, timeframe)
                                  / f"{safe_symbol(symbol)}.parquet")
                # Judged on the REPAIRED series, because repairing the bars is not the
                # same as repairing the series.
                why = quarantine_reason(df, asset_class, symbol)
                if why:
                    quarantined.append({"class": asset_class, "timeframe": timeframe,
                                        "symbol": symbol, "reason": why})
                else:
                    note = fat_tail_note(df)
                    if note:
                        fat_tails.append({"class": asset_class, "timeframe": timeframe,
                                          "symbol": symbol, "extreme": note})

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    qpath = DATA_DIR / "reference" / QUARANTINE_CSV
    qpath.parent.mkdir(parents=True, exist_ok=True)
    cols = ["class", "timeframe", "symbol", "reason"]
    fresh = pd.DataFrame(quarantined, columns=cols)
    if full_scan:
        fresh.to_csv(qpath, index=False)
    else:
        # MERGE, never replace. A scoped rescan re-derives the truth only for the scope it
        # read; writing that over the whole file silently un-quarantines every class it did
        # not look at, and `td_loader.load` would then feed IMX/USD's +657,666% bar back
        # into the next crypto sweep because someone rechecked the equities.
        #
        # Within the scope the new verdict is authoritative — a symbol that has been
        # repaired must be able to leave the list — so drop the scanned cells first, then
        # concatenate. That is the difference between merging and merely appending.
        prior = pd.read_csv(qpath) if qpath.exists() else pd.DataFrame(columns=cols)
        if not prior.empty:
            scanned = (prior["class"].isin(args.classes)
                       & prior["timeframe"].isin(args.timeframes))
            kept = prior.loc[~scanned, cols]
            dropped = int(scanned.sum()) - len(fresh)
            if dropped > 0:
                print(f"{dropped} symbol(s) left quarantine in the rescanned scope")
            print(f"merged: {len(kept)} row(s) preserved outside "
                  f"{args.classes} x {args.timeframes}")
        else:
            kept = prior
        pd.concat([kept, fresh], ignore_index=True).sort_values(cols[:3]).to_csv(
            qpath, index=False)
    if gapped:
        how = "applied" if args.fix else "run --fix to apply"
        print(f"\n=== {len(gapped)} series TRUNCATED at a vendor outage ({how}) ===")
        print(pd.DataFrame(gapped).to_string(index=False))
    if quarantined:
        print("=== QUARANTINED (excluded from every sweep by td_loader.load) ===")
        print(pd.DataFrame(quarantined).to_string(index=False))
        print()
    if fat_tails:
        print(f"=== {len(fat_tails)} large-but-plausible extremes (KEPT; a crash the "
              f"sample must contain) ===")
        print(pd.DataFrame(fat_tails).head(12).to_string(index=False))
        print()
    if not rows:
        print("no OHLC integrity faults found")
        # Only a full scan may declare the whole cache clean. A scoped run that truncated
        # this file would erase the record of faults in the sheets it never looked at.
        if full_scan:
            (RESULTS_DIR / "data_faults.csv").write_text("", encoding="utf-8")
        return

    df = pd.DataFrame(rows).sort_values("total_faults", ascending=False)
    df.to_csv(RESULTS_DIR / "data_faults.csv", index=False)

    by_tf = df.groupby(["class", "timeframe"]).agg(
        symbols_affected=("symbol", "size"),
        bars=("n_bars", "sum"),
        faults=("total_faults", "sum")).reset_index()
    by_tf["pct_of_bars"] = 100.0 * by_tf["faults"] / by_tf["bars"]
    print("=== OHLC integrity faults ===")
    print(by_tf.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print(f"\nworst symbols:\n{df.head(8).to_string(index=False)}")
    if args.fix:
        print("\nrepaired in place (High/Low widened to contain Open/Close)")
    else:
        print("\nrun with --fix to widen High/Low to contain Open and Close")


if __name__ == "__main__":
    main()
