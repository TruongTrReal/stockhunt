"""Fama-French daily factors, and the regression that asks whether a rule has alpha.

Why this file exists
--------------------
Every comparison in this repo until now has been Sharpe against Sharpe, or IR against a
single asset's buy-and-hold. Neither can answer the question that decides whether any of
this is a finding: **is this a new edge, or a loading on a premium anyone can buy?**

`ibs` is the case in point. It is long ~46% of the time, it buys bars that closed near
their low, it de-risks in calm markets and it is negatively skewed. That is not a
description of an anomaly; it is a description of **short-term reversal** with a dash of
**betting-against-beta** and short volatility. All three are documented, published, and
purchasable at low cost. If `ibs`'s excess return regresses onto STR and BAB with an
intercept indistinguishable from zero, then the honest writeup is "we rediscovered
short-term reversal", and no amount of risk-matched wealth changes that.

Reading the output
------------------
The number that matters is `alpha_ann` and its `alpha_t`, with Newey-West errors because
daily strategy returns are autocorrelated and heteroskedastic and OLS errors would be
optimistic. **alpha_t is subject to exactly the same multiplicity problem as every other
t-stat in this project** — it is one row of a search — so it is a necessary condition,
never a sufficient one. Pair it with `metrics.deflated_sharpe`.

Data source
-----------
Ken French's data library, which is free, canonical, and daily back to 1926 for the
three-factor set. Cached under `../data/reference/` after the first download; delete the
files to refetch. `RF` there is the one-month bill, slightly different from the DTB3
three-month series `riskmatch_wf` uses for the cash leg — close enough that the choice
does not move an alpha, and both are stated rather than assumed.

`ST_Rev` annualises to roughly **+56%/yr** and that is not a parsing error — verified
against the raw file. French's daily short-term reversal factor is reconstructed *daily*
from the prior 20 days' returns, and daily-rebalanced reversal is the textbook case of an
anomaly that is enormous gross and approximately zero net of trading costs. That is
exactly why it is the right regressor here: `ibs` is the same trade at retail turnover,
so a large positive loading on `ST_Rev` says the rule is harvesting a premium whose whole
literature is about how it does not survive execution.

BAB is not published on French's site. The proxy here is `SPLV - SPHB` (S&P 500 Low
Volatility minus High Beta), which is the same trade in ETF form but starts only in 2011,
so it is offered as an *optional* extra regressor rather than folded into the default
set — adding it silently would quietly truncate every regression to 15 years.

Run::

    python factors.py                 # download/refresh the cache, print coverage
"""

from __future__ import annotations

import io
import zipfile
from statistics import NormalDist

import numpy as np
import pandas as pd
import requests

from config import DATA_DIR

REFERENCE_DIR = DATA_DIR / "reference"
FRENCH_BASE = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/")

# name -> (zip file on French's server, columns it contributes)
FRENCH_FILES = {
    "ff5": ("F-F_Research_Data_5_Factors_2x3_daily_CSV.zip",
            ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]),
    "mom": ("F-F_Momentum_Factor_daily_CSV.zip", ["Mom"]),
    "str": ("F-F_ST_Reversal_Factor_daily_CSV.zip", ["ST_Rev"]),
}

DEFAULT_FACTORS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom", "ST_Rev"]


def _parse_french(text: str) -> pd.DataFrame:
    """Pull the daily block out of a French CSV.

    The files carry a paragraph of prose, then the header, then the data, and some carry
    a second annual block underneath. Selecting rows whose first field is exactly eight
    digits picks the daily block and nothing else, without depending on how many lines of
    preamble that particular file happens to have this year.
    """
    header = None
    prev = None
    rows = []
    for line in text.splitlines():
        if not line.strip():
            prev = None            # a blank line separates the prose from the table
            continue
        parts = [p.strip() for p in line.split(",")]
        # The header's own first field is EMPTY (',Mkt-RF,SMB,...'), so an emptiness test
        # here silently discards the one line whose names are being looked for.
        if parts[0].isdigit() and len(parts[0]) == 8:
            # The header is whatever non-data line sat immediately above the first data
            # row — not the first non-numeric line in the file, which is prose and has
            # no commas in it to split on.
            if header is None and prev is not None:
                header = prev[1:] if not prev[0] else prev
            rows.append(parts)
        else:
            prev = parts
    if not rows or not header:
        raise ValueError("no daily block found in French CSV")
    width = len(rows[0]) - 1
    df = pd.DataFrame([r[1:width + 1] for r in rows],
                      index=pd.to_datetime([r[0] for r in rows], format="%Y%m%d"),
                      columns=header[:width])
    df = df.apply(pd.to_numeric, errors="coerce")
    # French publishes percent; everything else in this repo is decimal.
    df = df / 100.0
    # -99.99 is the library's missing marker and becomes -0.9999 after the divide.
    return df.mask(df <= -0.99)


def download(refresh: bool = False) -> pd.DataFrame:
    """Fetch (or load) every French file and join them on date."""
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for key, (zip_name, cols) in FRENCH_FILES.items():
        cache = REFERENCE_DIR / f"french_{key}.csv"
        if cache.exists() and not refresh:
            df = pd.read_csv(cache, index_col=0, parse_dates=True)
        else:
            r = requests.get(FRENCH_BASE + zip_name, timeout=120,
                             headers={"User-Agent": "stockhunt-research/1.0"})
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                name = z.namelist()[0]
                df = _parse_french(z.read(name).decode("latin-1"))
            df.to_csv(cache)
            print(f"  downloaded {zip_name} -> {cache.name}  "
                  f"({len(df)} rows, {df.index.min().date()}..{df.index.max().date()})")
        keep = [c for c in cols if c in df.columns]
        out.append(df[keep])
    joined = pd.concat(out, axis=1).sort_index()
    return joined[~joined.index.duplicated(keep="first")]


def load(refresh: bool = False) -> pd.DataFrame:
    return download(refresh=refresh)


def bab_proxy(splv: pd.Series, sphb: pd.Series) -> pd.Series:
    """Betting-against-beta in ETF form: low-volatility minus high-beta, daily.

    Not the academic factor — it is long-only legs, unlevered, and net of two expense
    ratios — but it is the tradeable version and it starts 2011, which is the point: an
    alpha that survives against something you can actually buy is worth more than one
    that survives against a paper portfolio.
    """
    return (splv - sphb).rename("BAB")


def newey_west_ols(y: np.ndarray, X: np.ndarray, lags: int | None = None) -> dict:
    """OLS with HAC (Newey-West) standard errors. `X` gets an intercept prepended.

    Daily strategy returns are autocorrelated — a rule that holds a position for a week
    produces five correlated observations of the same bet — and heteroskedastic, since
    volatility clusters. Plain OLS errors ignore both and are therefore too small, which
    would inflate every t-stat this function exists to report. The Bartlett kernel with
    `lags ~ 4(T/100)^(2/9)` is the standard automatic choice.
    """
    y = np.asarray(y, dtype="float64")
    X = np.asarray(X, dtype="float64")
    if X.ndim == 1:
        X = X[:, None]
    ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
    y, X = y[ok], X[ok]
    n = y.size
    if n < 30:
        return {"n": n}
    Z = np.column_stack([np.ones(n), X])
    k = Z.shape[1]
    XtX_inv = np.linalg.pinv(Z.T @ Z)
    beta = XtX_inv @ (Z.T @ y)
    resid = y - Z @ beta

    if lags is None:
        lags = int(np.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    u = Z * resid[:, None]
    S = u.T @ u
    for L in range(1, lags + 1):
        w = 1.0 - L / (lags + 1.0)
        G = u[L:].T @ u[:-L]
        S += w * (G + G.T)
    cov = XtX_inv @ S @ XtX_inv
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))

    tss = float(((y - y.mean()) ** 2).sum())
    rss = float((resid ** 2).sum())
    with np.errstate(invalid="ignore", divide="ignore"):
        t = beta / se
    nd = NormalDist()
    return {
        "n": n, "k": k, "lags": lags,
        "beta": beta, "se": se, "t": t,
        "p": np.array([2.0 * (1.0 - nd.cdf(abs(float(v)))) if np.isfinite(v) else np.nan
                       for v in t]),
        "r2": 1.0 - rss / tss if tss > 0 else float("nan"),
        "resid": resid,
    }


def regress(strategy_excess: pd.Series, factors: pd.DataFrame,
            names: list[str] | None = None, bpy: float = 252.0) -> dict:
    """Regress a strategy's excess return on the factor set. Returns annualised alpha.

    `strategy_excess` must already be net of the risk-free rate — the intercept is only
    an alpha if the left-hand side is an excess return.
    """
    names = names or [c for c in DEFAULT_FACTORS if c in factors.columns]
    df = pd.concat([strategy_excess.rename("y"), factors[names]], axis=1).dropna()
    if len(df) < 60:
        return {"n": len(df), "names": names}
    res = newey_west_ols(df["y"].to_numpy(), df[names].to_numpy())
    res.update({
        "names": names,
        "alpha_bar": float(res["beta"][0]),
        "alpha_ann": float(res["beta"][0] * bpy),
        "alpha_t": float(res["t"][0]),
        "alpha_p": float(res["p"][0]),
        "loadings": {nm: float(b) for nm, b in zip(names, res["beta"][1:])},
        "loading_t": {nm: float(v) for nm, v in zip(names, res["t"][1:])},
        "start": df.index.min(), "end": df.index.max(),
    })
    return res


def summarise(res: dict) -> str:
    if "alpha_ann" not in res:
        return f"  too few overlapping observations (n={res.get('n', 0)})"
    lines = [
        f"  window     {res['start'].date()} .. {res['end'].date()}  "
        f"n={res['n']}  NW lags={res['lags']}  R2={res['r2']:.3f}",
        f"  ALPHA      {res['alpha_ann']:+.2%}/yr   t = {res['alpha_t']:+.2f}   "
        f"p = {res['alpha_p']:.3f}",
        "  loadings   " + "  ".join(
            f"{nm} {res['loadings'][nm]:+.3f}(t{res['loading_t'][nm]:+.1f})"
            for nm in res["names"]),
    ]
    return "\n".join(lines)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true", help="re-download even if cached")
    args = ap.parse_args()
    f = load(refresh=args.refresh)
    print(f"\nfactors: {list(f.columns)}")
    print(f"rows   : {len(f)}   {f.index.min().date()} .. {f.index.max().date()}")
    print(f"\ncoverage (non-null):\n{f.notna().sum().to_string()}")
    print(f"\nannualised means:\n{(f.mean() * 252).map('{:+.2%}'.format).to_string()}")


if __name__ == "__main__":
    main()
