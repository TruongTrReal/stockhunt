"""Fetch and cache the current S&P 500 constituent list from Wikipedia."""

import io
from pathlib import Path

import pandas as pd
import requests

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "sp500_constituents.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def fetch_sp500_table(use_cache: bool = True) -> pd.DataFrame:
    """Return the S&P 500 constituent table (Symbol, Security, GICS Sector, ...)."""
    if use_cache and CACHE_PATH.exists():
        return pd.read_csv(CACHE_PATH)

    resp = requests.get(WIKI_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    df = pd.read_html(io.StringIO(resp.text))[0]

    # Yahoo Finance uses '-' instead of '.' in tickers (e.g. BRK.B -> BRK-B)
    df["Symbol"] = df["Symbol"].str.replace(".", "-", regex=False)

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(CACHE_PATH, index=False)
    return df


def get_sp500_tickers(use_cache: bool = True) -> list[str]:
    return fetch_sp500_table(use_cache=use_cache)["Symbol"].tolist()


if __name__ == "__main__":
    tickers = get_sp500_tickers(use_cache=False)
    print(f"Fetched {len(tickers)} tickers")
    print(tickers[:10])
