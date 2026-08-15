"""Long while the close sits above the Ichimoku cloud."""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies._indicators import _bars, D


RULE = 'Long while the close sits above the Ichimoku cloud.'
SOURCE = "QuantConnect, 'Ichimoku Clouds in the Energy Sector'"
FAMILY = 'regime'
ANCHOR = False
CLASSES = None
NOTE = ''

LOGIC = """
What it measures
    The Ichimoku cloud: the span between two forward-shifted midlines of past highs and
    lows. Long while price sits above the cloud.

The claim
    The cloud is a support/resistance zone built from prior ranges; trading above it is
    meant to mark an established uptrend.

What the position is really exposed to
    Trend, with a built-in forward displacement.

How it fails
    The forward shift is the part to check carefully — a plotted cloud is drawn ahead of
    price, and only the causal portion is usable. This implementation is
    truncation-tested like every other rule here.
"""

# grid[0] IS the published parameter set; everything after it is a variant.
GRID = (
        {'tenkan_days': 9.0, 'kijun_days': 26.0, 'senkou_days': 52.0},
        {'tenkan_days': 20.0, 'kijun_days': 60.0, 'senkou_days': 120.0},
        {'tenkan_days': 5.0, 'kijun_days': 13.0, 'senkou_days': 26.0},
)

def position(df, close, bpy, tenkan_days=9.0, kijun_days=26.0, senkou_days=52.0):
    """Long above the Kumo cloud. The cloud is displaced forward, so it reads backward."""
    hi, lo = df["High"].to_numpy("float64"), df["Low"].to_numpy("float64")
    nt, nk, ns = (_bars(bpy, tenkan_days * D), _bars(bpy, kijun_days * D),
                  _bars(bpy, senkou_days * D))
    mid = lambda n: (pd.Series(hi).rolling(n).max().to_numpy()
                     + pd.Series(lo).rolling(n).min().to_numpy()) / 2.0
    tenkan, kijun = mid(nt), mid(nk)
    # Displaced forward by `nk`, which is exactly what makes reading it causal: the
    # cloud above bar t was computed from data at or before bar t - nk.
    span_a = pd.Series((tenkan + kijun) / 2.0).shift(nk).to_numpy()
    span_b = pd.Series(mid(ns)).shift(nk).to_numpy()
    top = np.maximum(span_a, span_b)
    return np.nan_to_num((close > top).astype("float64"), nan=0.0)
