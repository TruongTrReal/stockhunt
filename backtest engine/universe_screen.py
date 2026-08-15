"""Tradability screen for the ETF and crypto universes.

`us_etfs` was 65 names and `crypto` was 34, both assembled for breadth rather than for
whether the thing could actually be traded across the window it is scored on. This module
answers two questions per symbol, in order:

1. **Is it tradable at all over the study window?** Hard gates. A fund that did not exist
   in 2000, or that turns over a million dollars a day, or whose price moves on a grid
   coarse enough to be harvested by a mean-reversion rule, is not a candidate.
2. **Of the survivors, which are the most tradable?** A rank, capped at `--top`.

The output is `results/universe_screen_<class>.csv` — every candidate, every measurement,
every gate verdict — plus `../data/reference/etf_entry.csv`, the liquidity-gated entry
dates that `td_loader.membership_span` applies.

## Why an entry date and not just a symbol list

An ETF's ticker existing in 2000 does not mean it was buyable in 2000. The nine original
sector SPDRs launched in December 1998 and then traded **under two million dollars a day
until roughly 2004** — XLU's worst year is $0.1M/day. Scoring a rule on those bars and
reporting the result as an ETF result is the same error as scoring NVDA's small-cap decade
and calling it a top-100 result, which is what `top100_membership` exists to prevent on the
equity side. So a name enters the basket on the first date its trailing-252-bar median
dollar volume clears `DV_FLOOR_USD` **and never falls back below it**, and its earlier bars
are cut. That is a head cut, identical in shape and intent to the one
`td_loader.membership_span` already applies to `us_stocks`.

There is no tail cut. Unlike a company, none of these funds died or shrank out of the
basket; every survivor's last five years sit two orders of magnitude above the floor. If
that ever stops being true the screen will say so — `dv_last5y` is on the sheet.

**Once the entry cut exists, "did it list before 2000?" is the wrong question and asking
it does harm.** It admits XLV — listed 1998, unbuyable until 2006 — and rejects TLT, which
listed in 2002 and cleared the floor within a year. The gate that survived is
`ETF_MIN_TRADABLE_YEARS`, on the span after the cut. It is worth stating what that changed:
the ten names chosen on listing date were all US equity beta, mean pairwise return
correlation 0.72, because the bond, credit, gold and international funds all list after
2001. Chosen on tradable years, four of the ten are not US equities and the correlation is
0.44. Same window, same floor, same cap — nearly twice the independent information.

## Why crypto is screened differently

**Twelve Data serves no volume for crypto** — the field is absent, not zero — so there is
no dollar-volume ranking to be had and no liquidity-gated entry date to compute. Two
estimators stand in, both of which read a trading cost straight off OHLC:

* **Corwin & Schultz (2012)**, from the high-low range over consecutive bars. It is the
  headline spread number on the sheet.
* **Roll (1984)**, from the serial covariance of close-to-close changes. Defined only
  where that covariance is negative, which is why it is a second opinion and not the
  headline: on a trending series it is frequently undefined.

Neither is a substitute for a real quote, and both are biased upward on a volatile asset,
so they are used to *rank* pairs against each other rather than to assert a cost in basis
points. The cost model in `config.FEE_SCENARIOS` is what actually charges the book.

The third crypto measurement has no equity analogue and is the one that has already cost
this repo a result. **Relative tick size**: the minimum price increment as a fraction of
the price. A pair quoted on a grid that is a meaningful share of a typical move is a pair
where an oscillator can harvest the grid rather than the asset — the mechanism behind the
recycled-penny-stock blowup in `check_data`. SHIB/USD prints on a grid nine basis points
wide; nothing that coarse belongs in a mean-reversion study.

Usage:

    python universe_screen.py                          # scan both, print, write nothing
    python universe_screen.py --class us_etfs --top 10
    python universe_screen.py --write                  # sheets + etf_entry.csv
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

import config
import td_loader

# ---------------------------------------------------------------- thresholds
#
# Each of these is a judgement call and every one of them is stated on the sheet it
# produces, so a reader can see what a different call would have done.

# **Liquidity.** $20M/day of median turnover. Not derived from anything — it is the level
# at which a retail-sized order is invisible and the half-spread in `FEE_SCENARIOS` is a
# fair charge. The sheet carries `dv_min_year` and `dv_p10_year` so the sensitivity is
# visible: at $10M the sector SPDRs enter about a year earlier, at $50M about two later.
DV_FLOOR_USD = 20e6
DV_WINDOW = 252

# **The window the rank is measured on.** Turnover must be compared across names on the
# SAME stretch of calendar, and the obvious choice — "median over the span each name was
# held" — is not that. It ranks a fund higher for having been untradeable longer, because
# a late entry means the average is taken only over the modern, high-turnover era. XLP
# enters in 2007 and scores $396M/day on its held span; SMH enters in 2000 and scores
# $345M/day on its own, despite trading more than XLP does today. The last ten years is
# the longest stretch every survivor covers in full and tradably, so it is the comparison.
RANK_WINDOW_YEARS = 10

# **Enough tradable history to score.** Twenty of the study's 26.5 years, measured AFTER
# the entry cut. This is the only history gate, and it replaced a "must have listed by
# 2000" test that was quietly incoherent with the entry cut sitting next to it: that test
# admitted XLV, which listed in 1998 and was not buyable until 2006, while rejecting TLT,
# which listed in 2002 and was buyable within a year. Asking when a ticker first printed
# is asking the wrong question once the liquidity gate exists — ask how long it was
# *tradable*, which is what a backtest actually consumes.
#
# The threshold is what it is because 20 is the most demanding value that still fills ten
# slots. It matters more than a history gate usually would: at 22 years the basket loses
# its gold leg and mean pairwise correlation goes 0.44 -> 0.58, and at 16 it gains nothing
# but shortens the sample. See `basket_correlation`.
ETF_MIN_TRADABLE_YEARS = 20.0

# **Crypto history.** Shorter, necessarily: the class's own window opens 2017-08-29 and
# the vendor's deepest pair is nine years long. Four years is three walk-forward folds
# plus warmup.
CRYPTO_MIN_YEARS = 4.0

# **Grid coarseness.** A minimum price increment wider than this fraction of the price is
# a harvestable grid rather than a quote. Five basis points is roughly a tenth of the
# tightest spread any pair in this class shows.
MAX_TICK_BPS = 5.0

# **Staleness.** A bar whose close equals the previous close did not trade, or did not
# trade at a new price. Bond ETFs legitimately print flat days at 1.6% annual volatility,
# so the equity floor is looser than the crypto one; a 24/7 market has no excuse.
ETF_MAX_STALE_PCT = 6.0
CRYPTO_MAX_STALE_PCT = 2.0

# **Zero-volume bars.** A session the vendor reports as having traded no shares. A handful
# is a vendor artifact; several percent means the series is not what it claims.
ETF_MAX_ZEROVOL_PCT = 1.0

# **Structural.** A leveraged, inverse or volatility product is a path-dependent
# derivative of an index, not an asset, and its buy-and-hold is a mechanical loss rather
# than a benchmark — VXX is down 99.5% and UNG 99.9% over their lives with no view
# expressed. Scoring "beat buy-and-hold" against a benchmark engineered to decay is not a
# measurement. None of these list before 2008 so `ETF_FIRST_BAR_BY` already removes them;
# the name check is here so the reason on the sheet is the real one.
STRUCTURAL_EXCLUDE = set(config.ETF_LEVERAGED) | {"USO", "UNG", "DBC", "DBA", "CORN",
                                                  "WEAT", "UGA"}

# The pool the screen ranks over, which is deliberately NOT `CLASSES[cls]["symbols"]`.
# Those lists are the screen's own OUTPUT — reading them back would make this module
# re-elect the incumbents and lose the ability to re-derive its choice, or to notice that
# a name it rejected has since become the better one. `top100_membership` ranks over the
# S&P 500 rather than over the top 100 for exactly this reason.
CANDIDATES = {"us_etfs": config.ETF_ALL65, "crypto": config.CRYPTO_ALL34}


# ---------------------------------------------------------------- estimators

def corwin_schultz_bps(df: pd.DataFrame) -> float:
    """Median Corwin-Schultz (2012) effective spread over the sample, in basis points.

    Two-day high-low ranges separate volatility (which scales with time) from the spread
    (which does not). The estimator goes negative on quiet pairs — that is the estimator
    saying "below my resolution", not a negative spread — so negatives are floored at zero
    before the median is taken.
    """
    high, low = df["High"].to_numpy(float), df["Low"].to_numpy(float)
    if len(high) < 3:
        return float("nan")
    with np.errstate(divide="ignore", invalid="ignore"):
        beta = np.log(high[1:] / low[1:]) ** 2 + np.log(high[:-1] / low[:-1]) ** 2
        hi2 = np.maximum(high[1:], high[:-1])
        lo2 = np.minimum(low[1:], low[:-1])
        gamma = np.log(hi2 / lo2) ** 2
        k = 3.0 - 2.0 * np.sqrt(2.0)
        alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / k - np.sqrt(gamma / k)
        spread = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
    spread = spread[np.isfinite(spread)]
    if not len(spread):
        return float("nan")
    return float(np.median(np.maximum(spread, 0.0))) * 1e4


def roll_bps(df: pd.DataFrame) -> float:
    """Roll (1984) effective spread from close-to-close serial covariance, in bps.

    `2*sqrt(-cov(dp_t, dp_{t-1}))`, and undefined where that covariance is positive — an
    asset that trends bar to bar hides its own bounce. Reported as NaN there rather than
    floored, because unlike Corwin-Schultz the sign carries information: a positive
    covariance says the series is trending, not that the spread is small.
    """
    r = np.diff(np.log(df["Close"].to_numpy(float)))
    r = r[np.isfinite(r)]
    if len(r) < 30:
        return float("nan")
    cov = float(np.cov(r[1:], r[:-1])[0, 1])
    return 2.0 * np.sqrt(-cov) * 1e4 if cov < 0 else float("nan")


def tick_bps(close: pd.Series) -> float:
    """Minimum price increment as basis points of the median price.

    The 5th percentile of the gaps between adjacent *distinct* observed prices, not the
    smallest gap: one rounding artifact anywhere in a 3,000-bar series would otherwise
    report a grid finer than the venue actually quotes.
    """
    px = np.unique(np.sort(close.to_numpy(float)))
    gaps = np.diff(px)
    gaps = gaps[gaps > 0]
    if not len(gaps):
        return float("nan")
    med = float(np.median(close.to_numpy(float)))
    return float(np.percentile(gaps, 5)) / med * 1e4 if med > 0 else float("nan")


def liquidity_entry(df: pd.DataFrame, floor: float = DV_FLOOR_USD,
                    window: int = DV_WINDOW) -> pd.Timestamp | None:
    """First date after which trailing median dollar volume never falls below `floor`.

    "Never falls back below" rather than "first crosses", deliberately. A first-crossing
    rule lets a name that touched the floor once in 2003 and spent the next three years
    under it into the basket for those three years. Taking the date after the *last*
    breach makes entry monotone and means the span carries one promise: from here on, this
    was continuously tradable.

    The trailing window is back-filled over its own warmup rather than treated as a
    breach. Requiring a full 252 bars before the first verdict would hand SPY an entry
    date of 2000-03-31 — three months after the study opens, on a fund turning over
    $615M/day that year — which is an artifact of the estimator, not a fact about the
    fund. Twenty bars is enough to judge a month, and the back-fill carries that first
    verdict backwards to the series start.

    Returns `None` if the name never clears the floor at all.
    """
    dv = (df["Close"] * df["Volume"]).astype(float)
    if not dv.notna().any():
        return None
    trailing = dv.rolling(window, min_periods=min(20, len(dv))).median().bfill()
    below = trailing.index[(trailing < floor) | trailing.isna()]
    if not len(below):
        return df.index[0]
    last_bad = below[-1]
    after = df.index[df.index > last_bad]
    return after[0] if len(after) else None


# ---------------------------------------------------------------- measurement

def measure(asset_class: str, timeframe: str = "1d") -> pd.DataFrame:
    """One row per symbol in the class's *full* configured universe, all measurements.

    Loaded with `skip_quarantined=False` for the same reason `check_data` does it: this is
    the code deciding what belongs in the universe, so it has to see everything, including
    the bars before `BACKTEST_START`.
    """
    raw = td_loader.load(asset_class, timeframe, symbols=CANDIDATES[asset_class],
                         skip_quarantined=False)
    cut = pd.Timestamp(config.BACKTEST_START)
    rows = []
    for symbol, full in sorted(raw.items()):
        df = full.loc[full.index >= cut]
        if df.empty:
            continue
        vol = df["Volume"].astype(float)
        has_volume = bool(vol.notna().any())
        dv = (df["Close"].astype(float) * vol) if has_volume else pd.Series(np.nan, index=df.index)
        by_year = dv.resample("YE").median() if has_volume else pd.Series(dtype=float)

        entry = liquidity_entry(df) if has_volume else None
        # Everything about *how it trades* is measured on the bars the basket would
        # actually hold. Judging XLU's spread on its 2000-2005 bars describes a stretch
        # the entry cut has already removed from the study, and it is the stretch where
        # every one of these funds looks worst.
        held = df.loc[df.index >= entry] if entry is not None else df
        held_years = (
            (held.index[-1] - held.index[0]).days / 365.25 if len(held) > 1 else 0.0
        )
        close = held["Close"].astype(float)
        ret = close.pct_change()
        hvol = held["Volume"].astype(float)
        hdv = (close * hvol) if has_volume else pd.Series(np.nan, index=close.index)

        rows.append({
            "symbol": symbol,
            "first_bar": full.index[0].date(),
            "last_bar": full.index[-1].date(),
            "n_bars": len(held),
            "span_years": round((df.index[-1] - df.index[0]).days / 365.25, 2),
            "entry": entry.date() if entry is not None else None,
            "tradable_years": round(held_years if has_volume else 0.0, 2),
            "dv_rank_window": (
                round(float(dv.tail(RANK_WINDOW_YEARS * DV_WINDOW).median()) / 1e6, 1)
                if has_volume else np.nan
            ),
            "dv_med_held": round(float(hdv.median()) / 1e6, 1) if has_volume else np.nan,
            "dv_min_year": round(float(by_year.min()) / 1e6, 1) if has_volume else np.nan,
            "dv_p10_year": round(float(by_year.quantile(0.10)) / 1e6, 1) if has_volume else np.nan,
            "dv_last5y": (
                round(float(dv.tail(5 * DV_WINDOW).median()) / 1e6, 1) if has_volume else np.nan
            ),
            "spread_cs_bps": round(corwin_schultz_bps(held), 1),
            "spread_roll_bps": round(roll_bps(held), 1),
            "tick_bps": round(tick_bps(close), 2),
            "stale_pct": round(100.0 * float((ret.abs() < 1e-12).mean()), 2),
            "zerovol_pct": round(100.0 * float((hvol == 0).mean()), 2) if has_volume else np.nan,
            "vol_ann_pct": round(100.0 * float(ret.std() * np.sqrt(252)), 1),
            "maxdd_pct": round(100.0 * float((close / close.cummax() - 1.0).min()), 1),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- gates and rank

def _gate_etfs(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    fails = []
    for _, r in d.iterrows():
        why = []
        if r["symbol"] in STRUCTURAL_EXCLUDE:
            why.append("leveraged/inverse/roll-decay product")
        if r["entry"] is None:
            why.append(f"never clears ${DV_FLOOR_USD/1e6:.0f}M/day")
        elif r["tradable_years"] < ETF_MIN_TRADABLE_YEARS:
            why.append(f"only {r['tradable_years']:.1f}y tradable")
        if r["dv_last5y"] < DV_FLOOR_USD / 1e6:
            why.append(f"illiquid now (${r['dv_last5y']:.0f}M/day)")
        if r["stale_pct"] > ETF_MAX_STALE_PCT:
            why.append(f"{r['stale_pct']:.1f}% flat closes")
        if r["zerovol_pct"] > ETF_MAX_ZEROVOL_PCT:
            why.append(f"{r['zerovol_pct']:.1f}% zero-volume bars")
        if r["tick_bps"] > MAX_TICK_BPS:
            why.append(f"{r['tick_bps']:.1f}bps price grid")
        fails.append("; ".join(why))
    d["reject"] = fails
    d["passes"] = d["reject"] == ""
    d["score"] = d["dv_rank_window"]
    d = d.sort_values(["passes", "score"], ascending=[False, False]).reset_index(drop=True)
    d["rank"] = np.where(d["passes"], np.arange(1, len(d) + 1), 0)
    return d


def _gate_crypto(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    fails = []
    for _, r in d.iterrows():
        why = []
        if r["span_years"] < CRYPTO_MIN_YEARS:
            why.append(f"only {r['span_years']:.1f}y of history")
        if r["tick_bps"] > MAX_TICK_BPS:
            why.append(f"{r['tick_bps']:.1f}bps price grid")
        if r["stale_pct"] > CRYPTO_MAX_STALE_PCT:
            why.append(f"{r['stale_pct']:.1f}% flat closes")
        if r["n_bars"] < config.MIN_BARS:
            why.append(f"{r['n_bars']} bars < MIN_BARS")
        fails.append("; ".join(why))
    d["reject"] = fails
    d["passes"] = d["reject"] == ""

    # No volume, so no turnover ranking. Three standardised components instead, equally
    # weighted because there is no basis in this data for weighting them otherwise:
    # longer history, tighter estimated spread, finer price grid. The z-scores are taken
    # over the SURVIVORS only — including the rejects would let SHIB's nine-basis-point
    # grid set the scale and compress every real difference between the majors.
    #
    # The equal weighting is not load-bearing, which is the only reason it is defensible
    # to pick one arbitrarily. Ranking on any SINGLE component alone still returns 17-18
    # of the same 20, doubling any one component returns 19-20, and fourteen pairs appear
    # in every weighting tried. What moves is the last few slots — AVAX, LTC, UNI, VET,
    # ATOM and TRX trade places with ALGO, NEAR, ETC, SAND, GRT, AAVE and INJ — which is
    # the boundary doing what a boundary does. Anyone re-weighting this should expect to
    # change the marginal names and nothing else; if a re-weighting moves BTC or ETH,
    # something is broken.
    ok = d["passes"]

    def z(col: pd.Series, invert: bool = False) -> pd.Series:
        v = col.where(ok)
        sd = v.std(ddof=0)
        out = (v - v.mean()) / sd if sd and np.isfinite(sd) else v * 0.0
        return (-out if invert else out).fillna(0.0)

    d["z_history"] = z(np.log(d["span_years"].clip(lower=0.1)))
    d["z_spread"] = z(d["spread_cs_bps"], invert=True)
    d["z_grid"] = z(np.log(d["tick_bps"].clip(lower=1e-4)), invert=True)
    d["score"] = (d["z_history"] + d["z_spread"] + d["z_grid"]).round(3)
    d.loc[~ok, "score"] = np.nan

    d = d.sort_values(["passes", "score"], ascending=[False, False]).reset_index(drop=True)
    d["rank"] = np.where(d["passes"], np.arange(1, len(d) + 1), 0)
    return d


GATES = {"us_etfs": _gate_etfs, "crypto": _gate_crypto}
DEFAULT_TOP = {"us_etfs": 10, "crypto": 20}


def screen(asset_class: str, timeframe: str = "1d", top: int | None = None) -> pd.DataFrame:
    """Measure, gate, rank, and mark the top `top` survivors as `selected`."""
    if asset_class not in GATES:
        raise SystemExit(f"no screen defined for {asset_class!r}")
    d = GATES[asset_class](measure(asset_class, timeframe))
    n = DEFAULT_TOP[asset_class] if top is None else top
    d["selected"] = d["passes"] & (d["rank"] <= n) & (d["rank"] > 0)
    return d


# ---------------------------------------------------------------- output

def basket_correlation(asset_class: str, symbols: list[str], timeframe: str = "1d") -> float:
    """Mean pairwise correlation of daily returns across the basket, on shared bars.

    The number that says whether a basket is ten measurements or one measurement repeated
    ten times. It is not a gate — a liquidity screen has no business rejecting a fund for
    being correlated — but it is the price of every other gate, and it belongs next to the
    result. `metrics.se_ir` assumes independent assets; at a mean pairwise correlation of
    0.9 a ten-name basket carries roughly the statistical weight of two.
    """
    data = td_loader.load(asset_class, timeframe, symbols=symbols, skip_quarantined=False)
    if len(data) < 2:
        return float("nan")
    rets = pd.DataFrame({s: df["Close"].astype(float).pct_change() for s, df in data.items()})
    rets = rets.loc[rets.index >= pd.Timestamp(config.BACKTEST_START)].dropna()
    if len(rets) < 100:
        return float("nan")
    c = rets.corr().to_numpy()
    off = c[~np.eye(len(c), dtype=bool)]
    return float(np.mean(off))


def _print(asset_class: str, d: pd.DataFrame, top: int) -> None:
    label = config.CLASSES[asset_class]["label"]
    sel = d[d["selected"]]
    print(f"\n{'=' * 78}\n{label}: {len(d)} candidates -> {int(d['passes'].sum())} pass "
          f"the gates -> top {top} selected\n{'=' * 78}")

    if asset_class == "us_etfs":
        cols = ["rank", "symbol", "first_bar", "entry", "tradable_years",
                "dv_rank_window", "dv_min_year", "spread_cs_bps", "stale_pct"]
    else:
        cols = ["rank", "symbol", "first_bar", "span_years", "spread_cs_bps",
                "spread_roll_bps", "tick_bps", "stale_pct", "score"]
    print("\nSELECTED")
    print(sel[cols].to_string(index=False))

    near = d[d["passes"] & ~d["selected"]]
    if len(near):
        print(f"\nPASSED THE GATES BUT MISSED THE CAP ({len(near)})")
        print(near[cols].to_string(index=False))

    bad = d[~d["passes"]]
    if len(bad):
        print(f"\nREJECTED ({len(bad)})")
        print(bad[["symbol", "first_bar", "reject"]].to_string(index=False))

    syms = list(sel["symbol"])
    rho = basket_correlation(asset_class, syms)
    print(f"\n{label} basket = [" + ", ".join(f'"{s}"' for s in syms) + "]")
    print(f"mean pairwise return correlation: {rho:.3f}"
          f"   (effective independent names ~ {len(syms) / (1 + (len(syms) - 1) * max(rho, 0)):.1f}"
          f" of {len(syms)})")


def _write(asset_class: str, d: pd.DataFrame) -> None:
    out = config.RESULTS_DIR / f"universe_screen_{asset_class}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    d.to_csv(out, index=False)
    print(f"wrote {out}")

    if asset_class == "us_etfs":
        sel = d[d["selected"]][["symbol", "entry"]].copy()
        sel["reason"] = f"trailing {DV_WINDOW}-bar median $vol >= ${DV_FLOOR_USD/1e6:.0f}M"
        path = config.DATA_DIR / "reference" / "etf_entry.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        sel.to_csv(path, index=False)
        print(f"wrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--class", dest="asset_class", choices=sorted(GATES),
                    action="append", help="default: both")
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--top", type=int, default=None,
                    help="cap (default: 10 for us_etfs, 20 for crypto)")
    ap.add_argument("--write", action="store_true",
                    help="write results/universe_screen_*.csv and etf_entry.csv")
    args = ap.parse_args()

    for cls in (args.asset_class or sorted(GATES)):
        top = args.top if args.top is not None else DEFAULT_TOP[cls]
        d = screen(cls, args.tf, top)
        _print(cls, d, top)
        if args.write:
            _write(cls, d)


if __name__ == "__main__":
    main()
