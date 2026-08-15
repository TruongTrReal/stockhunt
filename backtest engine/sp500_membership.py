"""Point-in-time S&P 500 membership, reconstructed by walking the changelog backwards.

Why this exists
---------------
Every sheet in this repo until now tested a universe chosen because it is large *today*.
That is survivorship bias in its purest form, and it flatters two things at once: the
buy-and-hold benchmark (measured at 4.85pp of CAGR elsewhere in this repo) and, more
subtly, every mean-reversion rule — a sample containing no bankruptcies is precisely the
sample in which buying dips is safest. `ibs` has never once been asked to buy the dip in
a company that did not recover.

The fix is to know who was *actually* in the index on each date. Wikipedia publishes the
current constituents and a changelog of additions and removals, so membership on any past
date is recoverable by starting from today's list and undoing each change in reverse:

    members_before(D) = members_after(D) - {added on D} + {removed on D}

Two limits, and neither is cosmetic
-----------------------------------
**1. The changelog is only dense from 2007.** It carries ~20 events a year from 2007 to
today, which matches the index's real turnover. Before that it is 20 events for the whole
of 1976-2006, so it is a *selection* of changes, not the record. Membership reconstructed
before `RELIABLE_FROM` therefore drifts back towards "today's list", which is the very
bias this module exists to remove. `membership_on()` will serve those dates; it is
`reliable_on()` that says whether to believe them.

**2. Twelve Data does not serve delisted equities.** Probed directly: Lehman's `LEH` now
resolves to an unrelated 2012 listing, `ENRNQ` and `SHLDQ` are invalid symbols. So a name
removed for bankruptcy is correctly *dropped* from the universe on its removal date, but
its final descent can never be priced, and the return it delivered while it was still a
member is only partly recoverable. This corrects the "we knew the winners" half of
survivorship and leaves the "we never held the losers" half in place. `coverage_report()`
quantifies what remains, and that number belongs beside any result computed on this
universe rather than in a footnote.

Run::

    python sp500_membership.py              # rebuild the table, print the coverage report
    python sp500_membership.py --probe      # ...and ask Twelve Data which removals price
"""

from __future__ import annotations

import argparse
import io

import pandas as pd
import requests

from config import DATA_DIR

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
USER_AGENT = "stockhunt-research/1.0 (quant backtest; contact via repo owner)"

REFERENCE_DIR = DATA_DIR / "reference"
MEMBERSHIP_CSV = REFERENCE_DIR / "sp500_membership.csv"
CHANGES_CSV = REFERENCE_DIR / "sp500_changes.csv"

# Before this the changelog is a highlight reel, not a record: 20 events across 1976-2006
# against ~20 a year afterwards. Reconstruction still runs, but every backtest that spans
# earlier dates is carrying the survivorship it was built to remove.
RELIABLE_FROM = pd.Timestamp("2007-01-01")

# Wikipedia writes tickers with a dot for share classes; Twelve Data uses nothing at all
# for some and a slash for others. Only the dot form appears in the source tables.
_TICKER_FIXUPS = {"BRK.B": "BRK.B", "BF.B": "BF.B"}


def _fetch_tables() -> tuple[pd.DataFrame, pd.DataFrame]:
    r = requests.get(WIKI_URL, headers={"User-Agent": USER_AGENT}, timeout=60)
    r.raise_for_status()
    tables = pd.read_html(io.StringIO(r.text))
    current, changes = tables[0], tables[1]

    current = current.rename(columns={"Symbol": "symbol", "Security": "security",
                                      "GICS Sector": "sector", "Date added": "date_added"})
    current = current[["symbol", "security", "sector", "date_added"]].copy()
    current["symbol"] = current["symbol"].map(_clean)

    changes.columns = ["date", "add_t", "add_s", "rem_t", "rem_s", "reason", "_x"]
    changes = changes.drop(columns="_x")
    changes["date"] = pd.to_datetime(changes["date"], errors="coerce", format="mixed")
    changes["add_t"] = changes["add_t"].map(_clean)
    changes["rem_t"] = changes["rem_t"].map(_clean)
    changes = changes.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    return current, changes


def _clean(t) -> str | None:
    if not isinstance(t, str):
        return None
    t = t.strip().upper()
    return _TICKER_FIXUPS.get(t, t) or None


def build() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Reconstruct membership intervals by undoing the changelog from today backwards.

    Returns `(intervals, changes)`. `intervals` is one row per continuous spell of
    membership — a name that left and came back gets two rows, which is the whole point.
    """
    current, changes = _fetch_tables()
    members = set(current["symbol"].dropna())

    # `end` of NaT means "still a member". Walking backwards, a removal *opens* a spell
    # that ends on the removal date, and an addition *closes* the spell that started then.
    open_spells: dict[str, pd.Timestamp] = {s: pd.NaT for s in members}
    intervals: list[dict] = []

    for row in changes.sort_values("date", ascending=False).itertuples():
        d = row.date
        add, rem = row.add_t, row.rem_t

        if add and add in members:
            # This name joined on `d`, so its current spell began here. Close it out.
            intervals.append({"symbol": add, "start": d, "end": open_spells.pop(add),
                              "reason_in": row.reason})
            members.discard(add)

        if rem:
            # It was a member right up to `d`. Reopen a spell whose end is `d`.
            if rem in members:
                # Already present: the changelog re-added it later without a matching
                # removal. Close the newer spell at `d` rather than losing it.
                intervals.append({"symbol": rem, "start": d, "end": open_spells.pop(rem),
                                  "reason_in": "re-entry"})
            members.add(rem)
            open_spells[rem] = d

    for sym, end in open_spells.items():
        intervals.append({"symbol": sym, "start": pd.NaT, "end": end,
                          "reason_in": "pre-changelog"})

    iv = pd.DataFrame(intervals).dropna(subset=["symbol"])
    iv = iv.sort_values(["symbol", "start"]).reset_index(drop=True)
    iv["start_known"] = iv["start"].notna()
    # A spell with no recorded start predates the changelog. Date it to the changelog's
    # own beginning rather than to NaT so interval logic has something to compare, and
    # keep `start_known` so nobody mistakes the placeholder for a fact.
    iv["start"] = iv["start"].fillna(changes["date"].min())
    return iv, changes


def membership_on(intervals: pd.DataFrame, date) -> set[str]:
    """Tickers in the index on `date`. Half-open: a name removed on D is out on D."""
    d = pd.Timestamp(date)
    m = (intervals["start"] <= d) & (intervals["end"].isna() | (intervals["end"] > d))
    return set(intervals.loc[m, "symbol"])


def reliable_on(date) -> bool:
    return pd.Timestamp(date) >= RELIABLE_FROM


def universe(intervals: pd.DataFrame) -> list[str]:
    """Every ticker that was a member at any point the changelog covers."""
    return sorted(set(intervals["symbol"].dropna()))


def coverage_report(intervals: pd.DataFrame, changes: pd.DataFrame,
                    priceable: set[str] | None = None) -> str:
    ever = set(intervals["symbol"].dropna())
    now = membership_on(intervals, pd.Timestamp.today())
    gone = ever - now
    reliable = changes[changes["date"] >= RELIABLE_FROM]

    lines = [
        f"changelog        : {len(changes)} events, "
        f"{changes['date'].min().date()} -> {changes['date'].max().date()}",
        f"dense era        : {len(reliable)} events from {RELIABLE_FROM.date()} "
        f"({len(reliable) / max(1, (changes['date'].max() - RELIABLE_FROM).days / 365.25):.1f}/yr)",
        f"sparse era       : {len(changes) - len(reliable)} events before it "
        f"-- membership there is NOT trustworthy",
        f"names ever seen  : {len(ever)}   still members: {len(now)}   departed: {len(gone)}",
        f"spells           : {len(intervals)} "
        f"({(~intervals['start_known']).sum()} with no recorded start)",
    ]
    if priceable is not None:
        lost = sorted(gone - priceable)
        lines += [
            f"departed & priceable : {len(gone & priceable)} of {len(gone)}",
            f"departed & UNPRICEABLE: {len(lost)} -- residual survivorship bias lives here",
            f"  {', '.join(lost[:25])}{' ...' if len(lost) > 25 else ''}",
        ]
    return "\n".join(lines)


def probe_priceable(symbols: list[str], key: str) -> set[str]:
    """Ask Twelve Data which of these still resolve to a daily series."""
    ok = set()
    for i, sym in enumerate(symbols, 1):
        try:
            r = requests.get("https://api.twelvedata.com/earliest_timestamp",
                             params={"symbol": sym, "interval": "1day", "apikey": key},
                             timeout=30).json()
            if r.get("datetime"):
                ok.add(sym)
        except Exception:
            pass
        if i % 50 == 0:
            print(f"  probed {i}/{len(symbols)}")
    return ok


def load() -> pd.DataFrame:
    iv = pd.read_csv(MEMBERSHIP_CSV, parse_dates=["start", "end"])
    return iv


UNIVERSES_PY = __file__.replace("sp500_membership.py", "universes.py")

_UNIVERSES_HEADER = '''"""Generated by `sp500_membership.py --emit` — do not hand-edit.

The S&P 500 universe, point-in-time. `SP500_CURRENT` is today's index; `SP500_DEPARTED`
is every name that left it during the changelog's dense era (2007-) and that Twelve Data
will still price. `SP500_ALL` is the union, and it is what gets *fetched* — which names
are actually *tradeable on a given date* is decided per-bar by
`sp500_membership.membership_on`, never by this list.

Fetching the union rather than today's index is the whole point: {n_dep} of these names
are in the file precisely because they are no longer in the index, and a backtest that
only ever saw survivors is the bias this repo has carried since study one.

What is still missing, and it is not nothing. {n_lost} departures do not price at all —
Twelve Data does not serve delisted equities — and a further 13 pass the
`/earliest_timestamp` probe but return no bars: AKS, AVP, CXO, DNB, DNR, ETFC, FNM, GAS,
MEE, RAD, RTN, SCG, WIN. That second list is the more damaging one, because it is not
random: **RAD** (Chapter 11, 2023), **WIN** (Chapter 11, 2019), **DNR** (Chapter 11,
2020) and **FNM** (delisted from the NYSE after conservatorship) are exactly the
outcomes a mean-reversion rule needs to be tested against. Wikipedia's free-text reason
field labels only one removal a bankruptcy; the ticker probe says otherwise, so trust the
probe. Roughly 120 of 858 names cannot be held, they skew toward failures, and every
survivorship claim made on this universe should say so.

Rebuild with::

    python sp500_membership.py --probe --emit
"""

'''


def emit_universes(intervals: pd.DataFrame, priceable: set[str],
                   path: str | None = None) -> str:
    """Write `universes.py` from the membership table. The literal list is deliberate:
    `config` imports it at module scope and is itself imported by the dashboard and the
    paper desk, so resolving the universe through a CSV read would make a missing data
    file break every entry point in the repo rather than just this stage."""
    now = sorted(membership_on(intervals, pd.Timestamp.today()))
    departed = sorted(priceable)
    every = sorted(set(now) | set(departed))
    gone = set(intervals["symbol"].dropna()) - set(now)

    def block(names: list[str], per: int = 8) -> str:
        return "\n".join("    " + " ".join(f'"{s}",' for s in names[i:i + per])
                         for i in range(0, len(names), per))

    body = _UNIVERSES_HEADER.format(n_dep=len(departed), n_lost=len(gone - priceable))
    body += (f"# Today's index, {len(now)} lines including multiple share classes "
             f"(GOOG/GOOGL, FOX/FOXA).\nSP500_CURRENT = [\n{block(now)}\n]\n\n")
    body += ("# Left the index 2007-2026 and still priceable. Held only for the dates "
             f"they were members.\nSP500_DEPARTED = [\n{block(departed)}\n]\n\n")
    body += ("SP500_ALL = sorted(set(SP500_CURRENT) | set(SP500_DEPARTED))\n"
             f'assert len(SP500_ALL) == {len(every)}, "regenerate: membership changed"\n')

    target = path or UNIVERSES_PY
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(body)
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", action="store_true",
                    help="ask Twelve Data which departed names still price")
    ap.add_argument("--emit", action="store_true",
                    help="regenerate universes.py (needs a probe result, cached or fresh)")
    args = ap.parse_args()

    iv, changes = build()
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    iv.to_csv(MEMBERSHIP_CSV, index=False)
    changes.to_csv(CHANGES_CSV, index=False)
    print(f"wrote {MEMBERSHIP_CSV}  ({len(iv)} spells)")
    print(f"wrote {CHANGES_CSV}    ({len(changes)} events)")
    print()

    priceable = None
    if args.probe:
        import td_loader
        gone = sorted(set(iv["symbol"]) - membership_on(iv, pd.Timestamp.today()))
        print(f"probing {len(gone)} departed tickers against Twelve Data...")
        priceable = probe_priceable(gone, td_loader.api_key())
        pd.Series(sorted(priceable)).to_csv(
            REFERENCE_DIR / "sp500_departed_priceable.csv", index=False, header=["symbol"])

    if args.emit:
        cached = REFERENCE_DIR / "sp500_departed_priceable.csv"
        if priceable is None and cached.exists():
            priceable = set(pd.read_csv(cached)["symbol"])
        if priceable is None:
            ap.error("--emit needs --probe, or a cached sp500_departed_priceable.csv")
        print(f"wrote {emit_universes(iv, priceable)}")

    print(coverage_report(iv, changes, priceable))


if __name__ == "__main__":
    main()
