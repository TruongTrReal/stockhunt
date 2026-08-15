"""Point-in-time TOP 100 US stocks, ranked by trailing dollar volume.

Why this exists
---------------
`sp500_membership.py` answers *who was in the index*. This module answers the narrower
question this study now asks: **which 100 names were the large, liquid ones on that
date**. It is a second point-in-time layer stacked on the first — a name is eligible only
if it was an S&P 500 member on the rebalance date, and it is selected only if its trailing
dollar volume ranked it in the top 100 of that eligible pool.

The ranking metric is **median daily Close x Volume over the trailing 252 trading days**,
measured strictly before the rebalance date. Nothing about a bar on or after the rebalance
date can reach the selection, which is the whole point.

Dollar volume, not market cap, and that is a limit rather than a preference
--------------------------------------------------------------------------
"Top 100 US stocks" ordinarily means top 100 by market capitalisation. **This repo cannot
compute that.** Market cap is price x shares outstanding, and Twelve Data serves no
historical shares-outstanding series; nothing under `data/` carries one either. A true
point-in-time market cap would need a second data pipeline (SEC Financial Statement Data
Sets, quarterly, 2009 onward) and would still leave 2000-2008 to a proxy.

Trailing dollar volume is the best proxy computable from the bars already on disk, it
costs no API credits, and it is unambiguously point-in-time. It is not market cap, and the
two disagree in a specific, directional way that belongs beside any result:

* it **favours high-turnover names** — TSLA, NVDA, AMD, meme-era names — whose shares
  change hands far more often than their cap implies;
* it **penalises quiet giants** — BRK.B, and the low-beta staples — that are large but
  rarely traded;
* it is therefore a *liquidity* universe. Every name in it is genuinely tradeable at size,
  which is arguably the more honest filter for a backtest that charges a spread, but it is
  not the S&P 100 and must never be described as such.

One further distortion, small and worth naming: prices are stored `adjust=all`, so a
historical close is reflated backwards through splits **and dividends**. Splits cancel in
the product (volume is adjusted the opposite way), dividends do not — a high-yield payer's
early-2000s close is scaled down without its volume being scaled up, so its dollar volume
is understated in the oldest periods by roughly the compounded yield. On a 2% yielder over
26 years that is ~1.6x, enough to move a borderline rank and not enough to move a top-20
name.

Two limits inherited from the layer underneath
----------------------------------------------
**1. Membership is only trustworthy from 2007.** `sp500_membership.RELIABLE_FROM` is
2007-01-01; before it the Wikipedia changelog is a highlight reel (20 events for all of
1976-2006 against ~20 a year after). The eligible pool for the 2000-2006 rebalances
therefore drifts back towards today's index, so the top 100 selected there is partly a top
100 *of the survivors*. The dollar-volume ranking does not fix this — it only re-orders
whatever pool it is handed.

**2. Twelve Data prices no delisted equities.** A name that went to zero cannot be ranked,
so it can never enter this universe however large it was. Lehman, Bear Stearns, Worldcom
and Enron were all top-100 by dollar volume before they failed, and all four are absent.
This is the residual survivorship the `--stress-delisted` bound in `portfolio_wf.py`
exists to price, and on a 100-name book that bound matters *more* than it did on 500, not
less: the missing names were disproportionately large.

Output
------
`data/reference/top100_membership.csv` carries one row per continuous spell of top-100
membership, in the **same `symbol,start,end` schema** as `sp500_membership.csv`, so
`portfolio_wf.membership_mask` reads it without a change. `universes_top100.py` is
generated beside this file and holds the union that gets *fetched*.

Run::

    python top100_membership.py                # rebuild table + universes_top100.py
    python top100_membership.py --no-emit      # rebuild the table only, print the report
    python top100_membership.py --show 2008    # who was in the universe that year
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from config import BACKTEST_START, DATA_DIR, cache_dir, safe_symbol

import sp500_membership

REFERENCE_DIR = DATA_DIR / "reference"
MEMBERSHIP_CSV = REFERENCE_DIR / "top100_membership.csv"
UNIVERSES_PY = __file__.replace("top100_membership.py", "universes_top100.py")

# How many names the universe holds at any moment.
TARGET_N = 100

# An incumbent is only dropped once it falls out of the top BUFFER_RANK. Without a buffer
# a name oscillating either side of the 100th rank enters and leaves every single year,
# which manufactures universe turnover that has nothing to do with the company and shows
# up in a portfolio backtest as real rebalancing cost.
BUFFER_RANK = 120

# Trading days in the ranking lookback, and the minimum that must actually be present. A
# name that listed nine months before a rebalance is ranked on what it has rather than
# excluded — but a name with a handful of bars is not rankable at all, and admitting one
# on a thin median is how an illiquid stub wins a slot.
LOOKBACK_BARS = 252
MIN_LOOKBACK_BARS = 126

# Rebalance on the first trading day of each year. Annual, because the alternative is
# churn: measured on this data a quarterly re-rank moves ~2x the names for a universe that
# is ~95% the same either way, and every one of those moves is a real trade in
# `portfolio_wf`.
REBALANCE_MONTH = 1
REBALANCE_DAY = 1


def _rebalance_dates(first_year: int, last_year: int) -> list[pd.Timestamp]:
    return [pd.Timestamp(year=y, month=REBALANCE_MONTH, day=REBALANCE_DAY)
            for y in range(first_year, last_year + 1)]


def load_bars(symbols: list[str], timeframe: str = "1d") -> dict[str, pd.DataFrame]:
    """Read daily bars straight off disk, WITHOUT `config.BACKTEST_START`.

    `td_loader.load` cuts at 2000-01-01, which is correct for every stage that scores
    bars and wrong here: the 2000-01-01 rebalance needs 1999 bars to rank on, and ranking
    it on bars from inside its own period would be the look-ahead this module exists to
    avoid. So the lookback deliberately reaches into history that no backtest scores.
    """
    out_dir = cache_dir("us_stocks", timeframe)
    bars: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        path = out_dir / f"{safe_symbol(sym)}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        if df.empty or "Volume" not in df.columns:
            continue
        bars[sym] = df
    return bars


def dollar_volume(bars: dict[str, pd.DataFrame], asof: pd.Timestamp) -> dict[str, float]:
    """Median daily Close x Volume over the LOOKBACK_BARS bars strictly before `asof`."""
    out: dict[str, float] = {}
    for sym, df in bars.items():
        window = df.loc[df.index < asof]
        if len(window) < MIN_LOOKBACK_BARS:
            continue
        window = window.iloc[-LOOKBACK_BARS:]
        dv = (window["Close"].to_numpy("float64")
              * window["Volume"].to_numpy("float64"))
        dv = dv[np.isfinite(dv) & (dv > 0)]
        if len(dv) < MIN_LOOKBACK_BARS:
            continue
        out[sym] = float(np.median(dv))
    return out


def select(ranked: list[str], incumbents: set[str]) -> list[str]:
    """Top `TARGET_N`, with incumbents held until they fall past `BUFFER_RANK`.

    `ranked` is every eligible name, best first. Returns the new membership list.
    """
    rank = {sym: i + 1 for i, sym in enumerate(ranked)}
    keep = [s for s in ranked if s in incumbents and rank[s] <= BUFFER_RANK]
    # An incumbent set larger than the target can only happen if TARGET_N shrank between
    # runs, but it must not silently produce a 130-name "top 100": drop the worst-ranked.
    keep = keep[:TARGET_N]
    kept = set(keep)
    fill = [s for s in ranked if s not in kept][:TARGET_N - len(keep)]
    return sorted(set(keep) | set(fill), key=lambda s: rank[s])


def build(quiet: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Reconstruct top-100 membership forward through the rebalance calendar.

    Returns `(intervals, periods)`. `intervals` is the spell table written to CSV;
    `periods` is one row per (rebalance date, symbol) with the rank and metric that put it
    there, kept so a selection can be audited rather than trusted.
    """
    import td_loader

    sp = sp500_membership.load()
    quarantined = td_loader.quarantined("us_stocks", "1d")
    pool = sorted(set(sp["symbol"].dropna().astype(str)) - set(quarantined))
    if not quiet:
        print(f"S&P names ever          : {len(pool)} (after {len(quarantined)} quarantined)")

    bars = load_bars(pool)
    if not quiet:
        print(f"priceable on disk (1d)  : {len(bars)}")

    last_bar = max(df.index.max() for df in bars.values())
    dates = _rebalance_dates(pd.Timestamp(BACKTEST_START).year, int(last_bar.year))

    members: set[str] = set()
    history: dict[pd.Timestamp, list[str]] = {}
    rows: list[dict] = []

    for asof in dates:
        eligible_names = sp500_membership.membership_on(sp, asof) & set(bars)
        metric = dollar_volume({s: bars[s] for s in eligible_names}, asof)
        ranked = sorted(metric, key=lambda s: metric[s], reverse=True)
        members = set(chosen := select(ranked, members))
        history[asof] = chosen
        for i, sym in enumerate(chosen, 1):
            rows.append({"date": asof, "symbol": sym, "rank": i,
                         "median_dollar_volume": metric[sym],
                         "eligible": len(ranked)})
        if not quiet:
            flag = "" if sp500_membership.reliable_on(asof) else "  (membership NOT reliable)"
            print(f"  {asof.date()}  eligible {len(ranked):3d}  chosen {len(chosen):3d}"
                  f"  median $vol cutoff ${metric[chosen[-1]]:,.0f}{flag}")

    periods = pd.DataFrame(rows)
    intervals = _spells(history, dates)
    return intervals, periods


def _spells(history: dict[pd.Timestamp, list[str]],
            dates: list[pd.Timestamp]) -> pd.DataFrame:
    """Collapse per-period membership into continuous spells.

    Half-open, matching `sp500_membership`: a spell `[start, end)` means the name is a
    member on `start` and not on `end`. The final period leaves `end` as NaT — still a
    member — which is what `membership_mask` reads as "runs to the end of the series".
    """
    open_at: dict[str, pd.Timestamp] = {}
    out: list[dict] = []
    for i, d in enumerate(dates):
        now = set(history[d])
        for sym in sorted(set(open_at) - now):
            out.append({"symbol": sym, "start": open_at.pop(sym), "end": d,
                        "reason_in": "rank"})
        for sym in sorted(now - set(open_at)):
            open_at[sym] = d
    for sym, start in open_at.items():
        out.append({"symbol": sym, "start": start, "end": pd.NaT, "reason_in": "rank"})

    iv = pd.DataFrame(out).sort_values(["symbol", "start"]).reset_index(drop=True)
    # `sp500_membership` carries this column and `portfolio_wf` does not read it, but the
    # two tables are interchangeable at the call site and a missing column is how they
    # stop being.
    iv["start_known"] = True
    return iv


def current(intervals: pd.DataFrame) -> list[str]:
    return sorted(intervals.loc[intervals["end"].isna(), "symbol"].astype(str))


def universe(intervals: pd.DataFrame) -> list[str]:
    """Every name that held a top-100 slot at any point. This is what gets FETCHED."""
    return sorted(set(intervals["symbol"].dropna().astype(str)))


def load() -> pd.DataFrame:
    return pd.read_csv(MEMBERSHIP_CSV, parse_dates=["start", "end"])


def coverage_report(intervals: pd.DataFrame, periods: pd.DataFrame) -> str:
    ever = universe(intervals)
    now = current(intervals)
    spells = intervals.groupby("symbol").size()
    churn = periods.groupby("date")["symbol"].apply(set)
    moves = [len(b - a) for a, b in zip(churn.tolist()[:-1], churn.tolist()[1:])]
    tenure = []
    for row in intervals.itertuples():
        end = row.end if pd.notna(row.end) else periods["date"].max()
        tenure.append((pd.Timestamp(end) - pd.Timestamp(row.start)).days / 365.25)

    unreliable = [d for d in periods["date"].unique()
                  if not sp500_membership.reliable_on(d)]
    return "\n".join([
        f"rebalances       : {periods['date'].nunique()} "
        f"({periods['date'].min().date()} -> {periods['date'].max().date()}), annual",
        f"names ever held  : {len(ever)}   in the universe today: {len(now)}",
        f"spells           : {len(intervals)} "
        f"({(spells > 1).sum()} names entered more than once)",
        f"median tenure    : {np.median(tenure):.1f} years",
        f"churn / year     : {np.mean(moves):.1f} names in "
        f"(min {min(moves)}, max {max(moves)})",
        f"eligible pool    : {periods.groupby('date')['eligible'].first().min()}"
        f"-{periods.groupby('date')['eligible'].first().max()} names per rebalance",
        f"UNRELIABLE eras  : {len(unreliable)} rebalances before "
        f"{sp500_membership.RELIABLE_FROM.date()} -- the S&P pool they rank is "
        f"reconstructed, so the top 100 there is partly a top 100 of survivors",
    ])


_UNIVERSES_HEADER = '''"""Generated by `top100_membership.py` -- do not hand-edit.

The point-in-time TOP 100 US stocks, ranked by trailing 252-day median dollar volume among
S&P 500 members on each rebalance date, annually, with a {buffer}-rank buffer on
incumbents.

`TOP100_ALL` is the union of every name that ever held a slot and it is what gets
*fetched*. Which names are actually *held on a given date* is decided per-bar by
`top100_membership.load()` + `portfolio_wf.membership_mask`, never by this list -- holding
all {n_all} of them at once would be a top-{n_all} study, not a top-100 one.

`TOP100_CURRENT` is the {n_now} names in the universe as of the last rebalance.

Dollar volume is a PROXY for size: this repo has no historical shares-outstanding series,
so a true market-cap ranking is not computable here. It favours high-turnover names and
penalises quiet giants; see the module docstring of `top100_membership.py` for the full
statement of what that costs.

Rebuild with::

    python top100_membership.py
"""

'''


def emit_universes(intervals: pd.DataFrame, path: str | None = None) -> str:
    """Write `universes_top100.py`. A literal list, for the same reason `universes.py` is
    one: `config` imports it at module scope and is itself imported by the dashboard and
    the paper desk, so resolving the universe through a CSV read would make a missing data
    file break every entry point in the repo rather than just this stage."""
    every, now = universe(intervals), current(intervals)

    def block(names: list[str], per: int = 8) -> str:
        return "\n".join("    " + " ".join(f'"{s}",' for s in names[i:i + per])
                         for i in range(0, len(names), per))

    body = _UNIVERSES_HEADER.format(buffer=BUFFER_RANK, n_all=len(every), n_now=len(now))
    body += (f"# Every name that ever held a top-100 slot, {len(every)} in total.\n"
             f"TOP100_ALL = [\n{block(every)}\n]\n\n")
    body += (f"# The {len(now)} names in the universe at the most recent rebalance.\n"
             f"TOP100_CURRENT = [\n{block(now)}\n]\n\n")
    body += (f'assert len(TOP100_ALL) == {len(every)}, "regenerate: membership changed"\n'
             f'assert len(TOP100_CURRENT) == {len(now)}, "regenerate: membership changed"\n')

    target = path or UNIVERSES_PY
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(body)
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-emit", action="store_true",
                    help="rebuild the table but leave universes_top100.py alone")
    ap.add_argument("--show", type=int, default=None, metavar="YEAR",
                    help="print that year's universe, best-ranked first, and exit")
    args = ap.parse_args()

    intervals, periods = build()
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    intervals.to_csv(MEMBERSHIP_CSV, index=False)
    periods.to_csv(REFERENCE_DIR / "top100_ranks.csv", index=False)
    print(f"\nwrote {MEMBERSHIP_CSV}  ({len(intervals)} spells)")
    print(f"wrote {REFERENCE_DIR / 'top100_ranks.csv'}  ({len(periods)} rows)")

    if not args.no_emit:
        print(f"wrote {emit_universes(intervals)}")

    print()
    print(coverage_report(intervals, periods))

    if args.show:
        sel = periods[periods["date"].dt.year == args.show].sort_values("rank")
        print(f"\n--- top 100 as of {args.show} ---")
        for row in sel.itertuples():
            print(f"  {row.rank:3d}  {row.symbol:<6s}  ${row.median_dollar_volume:>15,.0f}/day")


if __name__ == "__main__":
    main()
