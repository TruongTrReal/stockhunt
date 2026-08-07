"""Shared harness for the edge hunt: locked holdout, cost model, trial ledger.

The single rule this file exists to enforce: **the holdout is never touched during
search.** Every hypothesis is developed and killed on TRAIN only. A candidate earns
exactly one look at HOLDOUT, and that look is recorded in the trial ledger so the
multiple-testing correction knows how many bites at the apple were actually taken.

This matters here specifically. The project has already produced two false positives
by searching until something looked good (the retracted memmel_z significance, and
the 5m sheet that compared cost-free rules to a crippled benchmark). An open-ended
search without a locked holdout reproduces that failure by construction.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STOCKHUNT = HERE.parent.parent
CACHE_TD = STOCKHUNT / "test research" / "data" / "cache_td"      # Twelve Data, adjust=all
CACHE_YF = STOCKHUNT / "test research" / "data" / "cache"          # yfinance daily
LEDGER = HERE / "trials.jsonl"

# Locked at the start of the hunt. 2015-2022 to search in, 2023-07 onward sealed.
TRAIN_END = "2022-12-31"
HOLDOUT_START = "2023-01-01"

UNIVERSE = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "JPM", "JNJ", "XOM",
    "UNH", "V", "PG", "HD", "MA", "CVX", "ABBV", "PEP", "KO", "WMT",
]

TRADING_DAYS = 252
COST_BPS_GRID = [0.0, 1.0, 5.0, 10.0, 20.0]
HEADLINE_COST_BPS = 5.0


# --------------------------------------------------------------------------- data

def load_daily(tickers: list[str] | None = None, source: str = "td") -> dict[str, pd.DataFrame]:
    """Daily OHLCV. `td` is Twelve Data with adjust=all (dividend+split adjusted)."""
    cache = CACHE_TD if source == "td" else CACHE_YF
    out = {}
    for t in (tickers or UNIVERSE):
        p = cache / f"{t}.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            df.index = pd.DatetimeIndex(df.index).tz_localize(None)
            out[t] = df.sort_index()
    return out


def split(df: pd.DataFrame | pd.Series):
    """(train, holdout). Never evaluate on holdout during search."""
    return df.loc[:TRAIN_END], df.loc[HOLDOUT_START:]


# ---------------------------------------------------------------------- statistics

def sharpe(r: pd.Series, ppy: int = TRADING_DAYS) -> float:
    r = r.dropna()
    s = r.std()
    return float(r.mean() / s * np.sqrt(ppy)) if s > 0 and len(r) > 1 else np.nan


def cagr(r: pd.Series, ppy: int = TRADING_DAYS) -> float:
    r = r.dropna().clip(lower=-0.999)
    if r.empty:
        return np.nan
    eq = float((1 + r).prod())
    yrs = len(r) / ppy
    return eq ** (1 / yrs) - 1 if yrs > 0 and eq > 0 else np.nan


def max_dd(r: pd.Series) -> float:
    eq = (1 + r.dropna().clip(lower=-0.999)).cumprod()
    return float((eq / eq.cummax() - 1).min()) if len(eq) else np.nan


def summarise(r: pd.Series, ppy: int = TRADING_DAYS) -> dict:
    return {"sharpe": sharpe(r, ppy), "cagr": cagr(r, ppy), "vol": float(r.std() * np.sqrt(ppy)),
            "max_dd": max_dd(r), "n": int(r.dropna().shape[0])}


def block_bootstrap_se(r: pd.Series, stat=sharpe, block: int = 21,
                       n_boot: int = 2000, seed: int = 0) -> float:
    """SE of a statistic under serial dependence. Blocks preserve autocorrelation and
    vol clustering, which an iid bootstrap would destroy and understate the SE."""
    r = r.dropna().to_numpy()
    n = len(r)
    if n < block * 3:
        return np.nan
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n - block, size=(n_boot, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_boot, -1)[:, :n]
    vals = [stat(pd.Series(r[row])) for row in idx]
    return float(np.nanstd(vals))


def deflated_threshold(n_trials: int, se: float, alpha: float = 0.05) -> float:
    """Effect size a candidate must clear given `n_trials` independent-ish attempts.

    Uses the expected maximum of n_trials standard normals plus an alpha-level margin
    — the same logic as the Z_BAR=4.91 constant already in sharpe_finalists.py, which
    is the one piece of statistical discipline this project got right.
    """
    from statistics import NormalDist
    ppf = NormalDist().inv_cdf
    if n_trials < 1:
        n_trials = 1
    if n_trials == 1:
        return float(ppf(1 - alpha) * se)
    gamma = 0.5772156649
    e_max = (1 - gamma) * ppf(1 - 1 / n_trials) + gamma * ppf(1 - 1 / (n_trials * np.e))
    return float((e_max + ppf(1 - alpha)) * se)


# -------------------------------------------------------------------- cost model

def net_of_cost(gross: pd.Series, turnover: pd.Series, bps: float) -> pd.Series:
    """Charge `bps` on |position change|, matching sweep.py so results stay comparable."""
    return (gross - turnover.abs() * bps / 1e4).clip(lower=-0.999)


# ------------------------------------------------------------------- trial ledger

def log_trial(plan: str, hypothesis: str, dataset: str, result: str,
              metrics: dict | None = None, verdict: str = "") -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "plan": plan,
           "hypothesis": hypothesis, "dataset": dataset, "result": result,
           "metrics": metrics or {}, "verdict": verdict}
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def n_trials() -> int:
    return sum(1 for _ in open(LEDGER, encoding="utf-8")) if LEDGER.exists() else 0
