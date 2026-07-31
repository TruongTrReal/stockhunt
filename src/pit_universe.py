"""Reconstruct point-in-time S&P 500 membership, to measure survivorship bias.

`sp500_tickers.py` fetches the CURRENT constituent list and every backtest runs
it from 2015. 186 of 503 names (37%) joined the index after 2015-01-01, so they
are in the sample from day one *because they later grew enough to be added* -
forward-looking selection. Worse for this project specifically, every company
deleted since 2015 (bankruptcy, acquisition, decline) is absent entirely, and a
sample where nothing ever dies flatters dip-buying above all else: you buy the
dip and it always recovers, because names that dipped and stayed down were never
in the universe.

Wikipedia's page carries a second table of index changes (date, ticker added,
ticker removed). Replaying it backwards from today's membership reconstructs who
was actually in the index on any past date.

Outputs `../data/sp500_pit_membership.csv` (date, ticker, in_index) plus the list
of tickers that were in the index during the backtest window but are missing from
the current cache - the ones whose absence is the bias.

    python pit_universe.py
"""

import io
import time

import pandas as pd
import requests

from sp500_tickers import HEADERS, WIKI_URL, get_sp500_tickers

OUT_MEMBERSHIP = "../data/sp500_pit_membership.csv"
OUT_MISSING = "../data/sp500_pit_missing_tickers.csv"
START = pd.Timestamp("2015-01-01")


def fetch_changes() -> pd.DataFrame:
    resp = requests.get(WIKI_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    # The changes table is the one carrying both an "Added" and a "Removed"
    # column group; its exact index on the page is not stable, so find it.
    for t in tables:
        cols = [" ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in t.columns]
        joined = " ".join(cols).lower()
        if "added" in joined and "removed" in joined and "date" in joined:
            return t
    raise SystemExit("could not locate the index-changes table on the page")


def main():
    t0 = time.time()
    current = set(get_sp500_tickers())
    raw = fetch_changes()
    print(f"changes table: {raw.shape[0]} rows, columns {list(raw.columns)[:6]}")

    def col(*want):
        for c in raw.columns:
            name = " ".join(map(str, c)).lower() if isinstance(c, tuple) else str(c).lower()
            if all(w in name for w in want):
                return c
        return None

    c_date, c_add, c_rem = col("date"), col("added", "ticker"), col("removed", "ticker")
    if c_add is None:
        c_add = col("added")
    if c_rem is None:
        c_rem = col("removed")
    ch = pd.DataFrame({
        "date": pd.to_datetime(raw[c_date], errors="coerce"),
        "added": raw[c_add].astype(str).str.replace(".", "-", regex=False).str.strip(),
        "removed": raw[c_rem].astype(str).str.replace(".", "-", regex=False).str.strip(),
    }).dropna(subset=["date"]).sort_values("date", ascending=False)
    ch = ch[(ch.added.str.match(r"^[A-Z\-]{1,6}$")) | (ch.removed.str.match(r"^[A-Z\-]{1,6}$"))]
    print(f"parsed {len(ch)} usable change rows, {ch.date.min():%Y-%m-%d} to {ch.date.max():%Y-%m-%d}")

    # Walk backwards through the change log, undoing each change to recover the
    # membership at earlier dates. One reverse pass over changes sorted newest
    # first, snapshotting at each month end.
    TICKER = __import__("re").compile(r"^[A-Z][A-Z\-]{0,5}$")
    changes = [(r.date, str(r.added).strip(), str(r.removed).strip())
               for r in ch.itertuples()]
    members = set(current)
    snapshots, idx = {}, 0
    months = list(pd.date_range(START, pd.Timestamp.today().normalize(), freq="MS"))[::-1]
    for d in months:
        while idx < len(changes) and changes[idx][0] > d:
            _, add, rem = changes[idx]
            if TICKER.match(add):
                members.discard(add)        # added after d -> not a member at d
            if TICKER.match(rem):
                members.add(rem)            # removed after d -> was a member at d
            idx += 1
        snapshots[d] = set(members)

    rows = []
    for d, mem in snapshots.items():
        for t in mem:
            rows.append({"date": d.strftime("%Y-%m-%d"), "ticker": t})
    pit = pd.DataFrame(rows)
    pit.to_csv(OUT_MEMBERSHIP, index=False)

    ever = set(pit.ticker)
    missing = sorted(ever - current)
    pd.DataFrame({"ticker": missing}).to_csv(OUT_MISSING, index=False)

    sizes = pit.groupby("date").size()
    print(f"\nreconstructed membership for {len(snapshots)} month-ends "
          f"({sizes.min()}-{sizes.max()} names, median {int(sizes.median())})")
    print(f"tickers EVER in the index {START:%Y-%m}..now : {len(ever)}")
    print(f"  still in it today                        : {len(ever & current)}")
    print(f"  since removed (absent from every backtest): {len(missing)}")
    print(f"\n-> {OUT_MEMBERSHIP}\n-> {OUT_MISSING}   [{time.time()-t0:.0f}s]")
    print(f"sample of removed names: {missing[:25]}")


if __name__ == "__main__":
    main()
