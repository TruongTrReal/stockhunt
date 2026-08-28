"""Databento as a live bar source for the CME futures leg. `td_live`, one vendor over.

Twelve Data carries no CME contract at all and — the failure the root `CLAUDE.md`
documents — it does not answer "no": `CL` there is Colgate-Palmolive and `ES` is Eversource
Energy, returned as clean, plausible equity series. So this leg names its own vendor, and
this module is the only door it reaches it through.

**The historical archive IS the live feed, and that is a measurement, not an assumption.**
Probed 2026-08-27 at 12:58:10 UTC, `metadata.get_dataset_range` for `GLBX.MDP3` reported:

    dataset    2026-08-27T12:50:00Z     ~8 minutes behind the wall clock
    ohlcv-1m   2026-08-27T12:50:00Z     the same instant
    ohlcv-1h   2026-08-27T12:00:00Z     the last COMPLETED hourly boundary
    ohlcv-1d   2026-08-27T00:00:00Z     the last completed UTC day

So the archive runs about **eight minutes** behind real time, and the two schemas this leg
uses report that fact rounded down to their own bar boundary rather than to the minute. A
REST poller aligned to the bar close plus a lag comfortably past those eight minutes reads
the settled bar on the first attempt, which is exactly the shape `td_live` + `td_nautilus`
already run at 1d and 4h. Databento's paid Live API buys latency this desk has no use for:
a book that decides once a day does not care about eight minutes, and every stage of the
research it forward-tests was computed on these same daily bars.

`ARCHIVE_LAG_SECONDS` below is that measurement, and `db_nautilus.POLL_LAG` is sized
against it. Poll sooner than the lag and the vendor has nothing, `drop_forming` correctly
discards what it does have, and the bar is skipped in silence.

**It costs nothing.** `db_loader.cost_usd(CME_FUTURES, ..., 'ohlcv-1h')` prices at $0.00 on
this key; CME OHLCV is free across the whole archive. Every request here is still made
through `db_loader._call`, so the retry, the 206 handling and the 5,000-row cap are one
implementation rather than two.

**Two timeframes and no more.** `ohlcv-1d` and `ohlcv-1h` are the schemas measured clean;
`ohlcv-1m` carries the same folded-session defect as the hourly archive before 2016 and
nothing else exists at all. `can_feed` is the capability, and `db_nautilus` refuses
anything else *at subscribe time* — see `td_nautilus.timeframe_of` for the fifteen hours a
timeframe that could be spelled and not subscribed to cost this desk once.

**What comes out is back-adjusted, and that is the whole reason this file is not four
lines.** A raw poll of `ES.v.0` returns whichever contract carried the most volume that
day, so on a roll the series steps to a different contract's level — WTI printed 18.12 and
then 24.76 in April 2020, a +37% return nobody earned. `book_strategy` keeps a rolling
buffer and appends live bars to it, so an unhandled roll feeds that fabricated return
straight into a live signal. `fetch_bars` therefore adjusts the warm-up window through
`db_loader.back_adjust` — the same rank-0/rank-1 same-bar ratio the cache was built with,
anchored at the newest bar — and `db_nautilus` carries a cumulative forward factor from
there so the live buffer stays one continuous series. Back-adjustment is a multiplication
by a constant and every price indicator in this repo is equivariant under a common scale,
so which bar the anchor sits on does not matter; **that the buffer is internally consistent
does.**
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

import paper_config                       # noqa: F401  (puts `backtest engine/` on the path)

import db_loader
from db_intraday import INTRADAY_START
from futures_specs import GLBX_START

DATASET = db_loader.DATASET

# The measured lag of the historical archive behind real time. It is what makes a REST
# poller a live feed here, and it is what `db_nautilus.POLL_LAG` has to clear.
#
# **Re-measured 2026-08-28 by sampling the frontier every 20s for two minutes**, because
# the first reading was taken once and rounded: `ohlcv-1m`'s end ran 5.5 to 7.3 minutes
# behind, mean 6.4. The spread is the vendor advancing the frontier in steps rather than
# continuously, so the WORST case is what a poller has to clear, not the mean.
#
# Read the per-schema `end` carefully when re-measuring. `ohlcv-1d` reports 00:00 and
# `ohlcv-1h` reports the last completed HOUR, so both look far more stale than the archive
# is — at 02:25 they read 145 and 25 minutes behind while the frontier was 5. Only the
# finest schema tracks the frontier, and `metadata.get_dataset_range`'s top-level `end` is
# the frontier itself.
ARCHIVE_LAG_SECONDS = 8 * 60

# The only two schemas this class may be fed from, and the reason is a measured vendor
# defect rather than a product gap. The GLBX archive folds whole sessions into a handful
# of bars on scattered days, and **a folded day's volume ties out to the daily bar
# exactly** — every check in this repo passes on one; only the bar COUNT reveals it.
# Re-measured 2026-08-22, `ohlcv-1h` and `ohlcv-1m` are both clean from 2016 and both
# holed before it, while `ohlcv-1d` is complete for the whole archive. There is no 15m or
# 4h schema at all: the research sheets at those sizes are built from cached 1m, which is
# a file on disk and not something a live poll can ask for.
# `1m` joined on 2026-08-28. It was left out on the reasoning that a minute bar arriving
# after the archive lag is stale — which is TRUE and is not a reason to refuse it, because
# a member strategy does not compute its signal from this feed. It arrives over the
# webhook from TradingView's own real-time data; what the desk needs a bar for is a price
# to fill against and a mark. So the honest description is not "1m does not work here", it
# is "a 1m fill on this class is priced off a bar about seven minutes old" — which is a
# caveat to record on the system, not a door to close. `desk_control` says exactly that
# when a 1m futures registration is accepted.
SCHEMA = {"1d": "ohlcv-1d", "1h": "ohlcv-1h", "1m": "ohlcv-1m"}

# How long one bar of each lasts, in the same shape as `td_live.INTERVALS` so the two
# clients' cadence arithmetic reads alike. A GLBX "day" is a UTC calendar day — that is
# what `db_loader` buckets on and what `data/futures/1d` is grouped by — so the daily
# boundary really is midnight UTC and the modular arithmetic in `db_nautilus` is right.
INTERVALS = {"1d": timedelta(days=1), "1h": timedelta(hours=1),
             "1m": timedelta(minutes=1)}

# The first date each schema is worth asking for. Daily is the hard floor of the vendor's
# CME archive; hourly is where the folded-session scan found every sampled weekday intact,
# which is `db_intraday`'s own start for the same reason.
HISTORY_START = {"1d": GLBX_START, "1h": INTRADAY_START, "1m": INTRADAY_START}

# A LOWER bound on how many bars a calendar day yields, per timeframe — used only to turn
# "n bars" into a wall-clock window to ask for, so it must under-estimate or the warm-up
# comes back short and the book sits warming with nothing in the log to say why.
#
# 5/7 for daily is the weekend. **4.0 for hourly is live cattle, not the index**, and the
# gap is the point: measured 2024, one UTC day is 23 hourly bars on ES, 19 on the grains
# and **6** on LE, which keeps pit hours. Sizing this window off ES would ask for a quarter
# of the history LE needs. Over-asking costs response bytes and $0.00; under-asking is
# silent.
# 240 for 1m is the same under-estimate one grid finer: LE's ~6 tradable hours is ~360
# minute bars, and 240 leaves the same margin the hourly row has. Getting it wrong in the
# generous direction costs bytes and $0.00; getting it wrong the other way returns a short
# warm-up and the book sits warming with nothing in the log to say why.
_BARS_PER_CALENDAR_DAY = {"1d": 5.0 / 7.0, "1h": 4.0, "1m": 240.0}

# Slack on top of that, for holidays and for the roots that trade a shortened week.
_WINDOW_SLACK = 1.15

# The desk's live anchor, per symbol, published by `db_nautilus` when it back-adjusts.
#
# It exists so the MARK and the FILLS are on one scale. A book's fills land on bars this
# module adjusted to the anchor its warm-up chose; a fresh read of the vendor is anchored
# at *its* newest bar instead, so after a roll the two differ by that roll's ratio — a
# median 0.56% on this universe, applied to the whole position, showing up as a P&L step
# nobody traded. `fetch_prices` multiplies by whatever the client last published here, and
# 1.0 — a fresh anchor, and the correct answer — is the default for a symbol no desk is
# holding.
FORWARD_FACTORS: dict[str, float] = {}


# What to say when the key is missing. One sentence, in one place, so the desk log, the
# refused registration and the startup banner all say the same thing.
NO_KEY = ("no Databento API key — the cme_futures leg will not be fed; set "
          "DATABENTO_API_KEY in .env.local")


def have_key() -> bool:
    """Is there a Databento credential at all — asked WITHOUT raising.

    `db_loader.api_key` raises, which is right for a research fetch that has nothing else
    to do and catastrophic for the live desk. `run_paper.py` runs under systemd with a
    restart policy and holds live positions on four other classes; a `RuntimeError` at
    node build, at `_connect`, or inside a poll task would put the whole desk into a
    restart loop and flatten books that have nothing to do with futures.
    """
    try:
        return bool(db_loader.api_key())
    except Exception:                        # noqa: BLE001 - absence is the answer here
        return False


def can_feed(timeframe: str) -> bool:
    """Whether a live futures bar of this size can be asked for at all."""
    return timeframe in SCHEMA


# How long an archive-end reading is reused. `db_loader.available_end` memoises the answer
# for the life of the process, which is right for a fetch job that runs for an hour and
# **wrong for a poller that runs for weeks**: it would ask for the same end forever and
# every bar after the first would be outside the window. Sixty seconds is short enough to
# follow the archive and long enough that a burst of subscriptions costs one request.
_END_TTL_SECONDS = 60
_ends: dict[str, tuple[float, pd.Timestamp]] = {}


def available_end(schema: str) -> pd.Timestamp:
    """The last instant this schema actually has, to the minute — not to the day.

    `db_loader.available_end` rounds to a date, which is exactly right for a daily fetch
    and destroys an hourly live feed: measured 2026-08-27 at 12:58 UTC, `ohlcv-1h` ended
    at **12:00** that day, so a request truncated to `2026-08-27` asks for nothing after
    midnight and every hourly bar of the current session goes missing. Asking rather than
    assuming "now" is not padding either — Databento answers a request whose `end` runs
    past the archive with a 422 and refuses the whole thing, so a poller that guessed
    would break at every boundary and work again minutes later.
    """
    import time as _time
    hit = _ends.get(schema)
    if hit is not None and _time.monotonic() - hit[0] < _END_TTL_SECONDS:
        return hit[1]
    r = db_loader._call("metadata.get_dataset_range", dataset=DATASET).json()
    stamp = r.get("schema", {}).get(schema, r)["end"]
    end = pd.Timestamp(stamp).tz_convert("UTC").tz_localize(None)
    _ends[schema] = (_time.monotonic(), end)
    return end


def rank_one(symbol: str) -> str:
    """`ES.v.0` -> `ES.v.1`: the contract behind the front one. `db_loader`'s definition."""
    return db_loader._rank_one(symbol)


# Databento takes an ISO instant, and it has to be one: a date alone means midnight, which
# for the hourly schema throws away the whole of the current session.
_STAMP = "%Y-%m-%dT%H:%M:%S"


def _window_start(timeframe: str, n: int, end: pd.Timestamp) -> str:
    days = n / _BARS_PER_CALENDAR_DAY[timeframe] * _WINDOW_SLACK + 5
    start = end - pd.Timedelta(days=days)
    floor = pd.Timestamp(HISTORY_START[timeframe])
    return max(start, floor).strftime(_STAMP)


def _drop_forming(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Discard the newest bar unless a full interval has elapsed since it opened.

    The same line `td_live.fetch_bars` calls the most important one in its file, and for
    the same reason: a bar that is still open has a close that has not happened yet, and
    trading it is look-ahead of the worst kind. Databento stamps a bar at its OPEN, so the
    test is against `open + interval` exactly as it is one vendor over.
    """
    if df.empty:
        return df
    last_open = df.index[-1]
    if last_open.tzinfo is None:
        last_open = last_open.tz_localize("UTC")
    if datetime.now(timezone.utc) < last_open + INTERVALS[timeframe]:
        return df.iloc[:-1]
    return df


def fetch_raw(symbol: str, timeframe: str, n: int = 1500,
              drop_forming: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The last `n` UNADJUSTED bars of rank 0, and rank 1 beside it, with contract ids.

    Both ranks in one request, because that is what makes an exact roll ratio possible:
    the incoming contract's price is read on the SAME BAR as the outgoing one, never
    across time. A close-to-close splice folds a session of market movement into the
    adjustment and is only ever the fallback here — see `db_loader.back_adjust`.

    Sunday's opening sliver is folded into the session it opens for the daily timeframe
    and left alone for the hourly one, matching `db_loader` and `db_intraday`
    respectively: at 1d a two-hour stub standing next to five full sessions is a fake day
    whose range feeds every volatility and reversion rule, while at 1h it is simply the
    first two hours of the trading week and every bar of it is real.

    **The hourly window is deliberately NOT session-screened**, and that is a difference
    from `db_intraday` worth stating. Its screen drops a day whose bar count falls below
    half that root's median day, which is right for an archive being written to disk and
    wrong for a live buffer: the newest session is partial by construction — the desk is
    standing inside it — so the screen's first casualty would be the bar the poller just
    fetched. The defect it defends against was measured absent from 2016 onward, and the
    live window never reaches back that far.
    """
    if not can_feed(timeframe):
        raise ValueError(
            f"{timeframe} is not a timeframe Databento can feed for this class. It serves "
            f"{', '.join(sorted(SCHEMA))} — the GLBX archive has no 15m or 4h schema at "
            f"all, and its 1m bars carry the folded-session defect before 2016.")

    schema = SCHEMA[timeframe]
    end = available_end(schema)
    stop = end.strftime(_STAMP)
    start = _window_start(timeframe, n, end)

    behind_symbol = rank_one(symbol)
    raw = db_loader._fetch_window([symbol, behind_symbol], start, stop, schema)
    if raw.empty:
        return pd.DataFrame(), pd.DataFrame()

    frames = {}
    for name, part in raw.groupby("symbol"):
        frame = db_loader._frame(part)
        if timeframe == "1d":
            frame = db_loader.merge_session_stubs(frame)
        frames[name] = frame

    front = frames.get(symbol, pd.DataFrame())
    behind = frames.get(behind_symbol, pd.DataFrame())
    if drop_forming and not front.empty:
        front = _drop_forming(front, timeframe)
        if not behind.empty:
            # Cut the second rank at the same instant. `back_adjust` reindexes it onto the
            # front's index anyway, but a rank-1 frame carrying a forming bar the front no
            # longer has would price a roll against a close that is still moving.
            behind = behind[behind.index <= front.index[-1]] if len(front) else behind
    return front.tail(n), behind


def roll_ratios(front: pd.DataFrame, behind: pd.DataFrame,
                symbol: str) -> dict[tuple[int, int], tuple[float, str]]:
    """Every roll inside this window: `(from_id, to_id) -> (ratio, method)`.

    Delegated to `db_loader.back_adjust` rather than re-derived, and that is not tidiness.
    The rule it applies — use rank 1's close on the same bar, but only after checking via
    `instrument_id` that rank 1 really was the contract rank 0 became, because ranks can
    skip a month — is the definition of an adjustment in this repo, and a second copy of
    it here would be a second thing to drift. `method` says which branch fired, exactly as
    the roll ledger records it, so a caller can refuse to publish a bar adjusted by the
    inexact one without being told.
    """
    if front.empty or "instrument_id" not in front.columns:
        return {}
    _, ledger = db_loader.back_adjust(front, behind, symbol)
    if ledger.empty:
        return {}
    return {(int(r.from_instrument_id), int(r.to_instrument_id)):
            (float(r.ratio), str(r.method)) for r in ledger.itertuples()}


def fetch_bars(symbol: str, timeframe: str, n: int = 1500,
               drop_forming: bool = True) -> pd.DataFrame:
    """The last `n` CLOSED bars for one root, ratio back-adjusted across every roll in
    the window and anchored at the newest of them.

    The repo's standard frame: `Open/High/Low/Close/Volume` on a tz-naive UTC
    DatetimeIndex, which is what `td_live.fetch_bars` returns and what every stage above
    both of them expects.

    Anchored at the NEWEST bar, as `db_loader` anchors the cache, so the last row is an
    untouched real quote and history is scaled to it. That orientation is the one a live
    desk needs: today's signal is computed on today's actual price.
    """
    front, behind = fetch_raw(symbol, timeframe, n, drop_forming)
    if front.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    bars, _ = db_loader.back_adjust(front, behind, symbol)
    return bars


# How far back `fetch_prices` looks for the newest print. Two calendar days covers a
# weekend-adjacent Monday morning and a holiday; the request is one batched call whatever
# the span, so there is nothing to save by cutting it finer.
_MARK_LOOKBACK_DAYS = 3


def fetch_prices(symbols: list[str]) -> dict[str, float]:
    """Latest price per root, for MARK-TO-MARKET only — never for a signal.

    One batched `ohlcv-1h` request for the whole list rather than one per symbol, and the
    reason is latency rather than credits: a Databento window costs ~25 seconds whether it
    carries 500 rows or 5,000, because the server spends it resolving continuous
    symbology. Sixteen serial requests would not fit inside any sane mark cadence; one
    does.

    Hourly rather than daily, because a daily bar's close is only current once a day and a
    mark that stale is not a mark. Hourly rather than minute, because the archive's 1m
    bars carry the folded-session defect and a mark is not worth a schema this repo has
    measured as unreliable.

    **Scaled by `FORWARD_FACTORS`**, so the number returned is on the same scale as the
    fills. See that constant for what goes wrong without it. A symbol nobody is holding
    gets 1.0, which is the raw quote and the right answer for it.

    Returns what it could price and omits the rest — the same contract `td_live.fetch_prices`
    keeps, so one unresolvable root never costs the other fifteen their marks.
    """
    if not symbols:
        return {}
    end = available_end(SCHEMA["1h"])
    start = end - pd.Timedelta(days=_MARK_LOOKBACK_DAYS)
    raw = db_loader._get_range(list(symbols), start.strftime(_STAMP),
                               end.strftime(_STAMP), SCHEMA["1h"])
    if raw.empty or "symbol" not in raw.columns:
        return {}
    out: dict[str, float] = {}
    for name, part in raw.groupby("symbol"):
        if name not in symbols:
            continue
        frame = db_loader._frame(part)
        if frame.empty:
            continue
        price = float(frame["Close"].iloc[-1]) * float(FORWARD_FACTORS.get(name, 1.0))
        if price > 0:
            out[str(name)] = price
    return out


def _smoke() -> None:
    """Prove the vendor path end to end without touching Nautilus.

    Also checks the adjusted window against the cached parquet on their overlap, which is
    the only check that can catch a roll handled differently here from in `db_loader`. A
    constant scale offset between the two is EXPECTED and harmless — the two series are
    anchored on different days — so what is reported is the spread of the ratio, not its
    level. A jump inside the window is the failure to look for.
    """
    import config as bt_config

    print(f"archive lag measured at ~{ARCHIVE_LAG_SECONDS // 60} minutes; "
          f"1d ends {available_end('ohlcv-1d')}, 1h ends {available_end('ohlcv-1h')}, "
          f"now {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    for symbol in list(bt_config.CME_FUTURES)[:3]:
        try:
            df = fetch_bars(symbol, "1d", n=400)
            cached = pd.read_parquet(
                bt_config.cache_dir("cme_futures", "1d")
                / f"{bt_config.safe_symbol(symbol)}.parquet")
            join = df.join(cached["Close"].rename("cached"), how="inner")
            ratio = join["Close"] / join["cached"]
            print(f"  {symbol:9s} {len(df):5d} bars, last {df.index[-1].date()} "
                  f"@ {df['Close'].iloc[-1]:.4f} | overlap {len(join)}, "
                  f"scale {ratio.median():.6f}, spread "
                  f"{100.0 * (ratio.max() / ratio.min() - 1.0):.4f}%")
        except Exception as exc:                    # noqa: BLE001 - reported, not hidden
            print(f"  {symbol:9s} FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    _smoke()
