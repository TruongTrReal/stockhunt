"""The hypothesis space: ~100 systematically generated cross-sectional signals.

Organised into families rather than invented one at a time, so the search is a grid
with a known size instead of an open-ended hunt whose trial count nobody tracked. That
number matters: the multiple-testing bar rises with it, so every signal added here
makes every other signal harder to prove. Families are declared up front for exactly
that reason.

  A  price trend        momentum at many horizons, with and without a skip month
  B  reversal           short-horizon mean reversion, 1 day to 1 quarter
  C  volatility         realised, downside, idiosyncratic, vol-of-vol, at several windows
  D  volume/liquidity   dollar volume, turnover, Amihud, volume shocks
  E  range/microstruct  high-low range, close position in range, gaps, path efficiency
  F  session split      overnight vs intraday accumulation at several windows
  G  distribution       skew, kurtosis, max daily return (lottery), downside frequency
  H  reference levels   52-week high/low proximity, drawdown from peak
  I  market relative    beta, correlation, idiosyncratic return vs SPY

Every signal is a DataFrame of scores, dates x tickers, where HIGHER is the hypothesis's
own claim of "should outperform". Sign conventions are baked in here (e.g. low-vol is
entered as negative vol) so the mass test never has to guess which direction to read,
and so a signal cannot be silently sign-flipped after seeing its result.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MOM_HORIZONS = [21, 63, 126, 252, 504]
VOL_WINDOWS = [20, 60, 126, 252]
REV_WINDOWS = [1, 5, 10, 21, 63]
VOLUME_WINDOWS = [21, 63]


def _safe_log(df: pd.DataFrame) -> pd.DataFrame:
    return np.log(df.replace(0.0, np.nan).abs() + 1e-12)


def build(close: pd.DataFrame, opn: pd.DataFrame, high: pd.DataFrame,
          low: pd.DataFrame, vol: pd.DataFrame,
          bench: pd.Series | None = None) -> dict[str, pd.DataFrame]:
    """Return {name: score frame}. Higher score = predicted outperformance."""
    rets = close.pct_change()
    overnight = opn / close.shift(1) - 1.0
    intraday = close / opn - 1.0
    dollar_vol = close * vol
    sig: dict[str, pd.DataFrame] = {}

    # --- A: trend / momentum -------------------------------------------------
    for h in MOM_HORIZONS:
        sig[f"A_mom_{h}"] = close / close.shift(h) - 1.0
        if h > 21:
            sig[f"A_mom_{h}_skip21"] = close.shift(21) / close.shift(h) - 1.0
    # Path efficiency: net move divided by distance travelled. Separates a clean trend
    # from a volatile drift that happens to end up in the same place.
    for h in (63, 252):
        net = (close - close.shift(h)).abs()
        path = rets.abs().rolling(h).sum() * close.shift(h)
        sig[f"A_efficiency_{h}"] = (net / path.replace(0, np.nan)) * np.sign(
            close - close.shift(h))

    # --- B: reversal ---------------------------------------------------------
    for w in REV_WINDOWS:
        sig[f"B_reversal_{w}"] = -(close / close.shift(w) - 1.0)
    # Reversal measured against the cross-section rather than zero.
    for w in (5, 21):
        r = close / close.shift(w) - 1.0
        sig[f"B_rel_reversal_{w}"] = -(r.sub(r.mean(axis=1), axis=0))

    # --- C: volatility -------------------------------------------------------
    for w in VOL_WINDOWS:
        sig[f"C_lowvol_{w}"] = -rets.rolling(w).std()
        sig[f"C_downside_vol_{w}"] = -rets.clip(upper=0).rolling(w).std()
    for w in (60, 252):
        rv = rets.rolling(20).std()
        sig[f"C_vol_of_vol_{w}"] = -rv.rolling(w).std()
        sig[f"C_vol_trend_{w}"] = -(rets.rolling(20).std() / rets.rolling(w).std())
    if bench is not None:
        for w in (126, 252):
            b = bench.reindex(close.index).pct_change()
            beta = rets.rolling(w).cov(b).div(b.rolling(w).var(), axis=0)
            sig[f"I_lowbeta_{w}"] = -beta
            resid_var = rets.rolling(w).var() - beta.pow(2).mul(b.rolling(w).var(), axis=0)
            sig[f"C_idio_vol_{w}"] = -resid_var.clip(lower=0).pow(0.5)
            sig[f"I_corr_{w}"] = -rets.rolling(w).corr(b)
            sig[f"I_idio_mom_{w}"] = (rets - beta.mul(b, axis=0)).rolling(w).sum()

    # --- D: volume / liquidity ----------------------------------------------
    for w in VOLUME_WINDOWS:
        sig[f"D_neg_dollar_vol_{w}"] = -_safe_log(dollar_vol.rolling(w).mean())
        sig[f"D_illiquidity_{w}"] = (rets.abs() / dollar_vol.replace(0, np.nan)).rolling(w).mean()
        sig[f"D_neg_turnover_{w}"] = -_safe_log(vol.rolling(w).mean())
    for w in (5, 21):
        sig[f"D_vol_shock_{w}"] = -(vol.rolling(w).mean() / vol.rolling(126).mean())
        sig[f"D_dollar_vol_trend_{w}"] = -(dollar_vol.rolling(w).mean()
                                           / dollar_vol.rolling(126).mean())

    # --- E: range / microstructure ------------------------------------------
    for w in (21, 63):
        sig[f"E_neg_range_{w}"] = -((high - low) / close).rolling(w).mean()
        # Where the close sits inside the day's range, averaged: a crude buying-pressure
        # proxy that does not depend on the direction of the move itself.
        pos = (close - low) / (high - low).replace(0, np.nan)
        sig[f"E_close_position_{w}"] = pos.rolling(w).mean()
        gap = (opn / close.shift(1) - 1.0).abs()
        sig[f"E_neg_gap_{w}"] = -gap.rolling(w).mean()
        sig[f"E_amihud_range_{w}"] = -(((high - low) / close)
                                       / _safe_log(dollar_vol)).rolling(w).mean()

    # --- F: session split ----------------------------------------------------
    for w in (21, 63, 252):
        sig[f"F_overnight_{w}"] = overnight.rolling(w).sum()
        sig[f"F_intraday_{w}"] = intraday.rolling(w).sum()
        sig[f"F_on_minus_id_{w}"] = (overnight - intraday).rolling(w).sum()
        tot = rets.rolling(w).sum()
        sig[f"F_on_share_{w}"] = overnight.rolling(w).sum() / tot.abs().replace(0, np.nan)

    # --- G: return distribution ---------------------------------------------
    for w in (63, 252):
        sig[f"G_neg_skew_{w}"] = -rets.rolling(w).skew()
        sig[f"G_skew_{w}"] = rets.rolling(w).skew()
        sig[f"G_neg_kurt_{w}"] = -rets.rolling(w).kurt()
    for w in (21, 63):
        # MAX effect: lottery-like stocks with a huge recent single-day gain underperform.
        sig[f"G_neg_max_{w}"] = -rets.rolling(w).max()
        sig[f"G_neg_downdays_{w}"] = -(rets < 0).rolling(w).mean()

    # --- H: reference levels -------------------------------------------------
    for w in (126, 252):
        sig[f"H_high_prox_{w}"] = close / close.rolling(w).max()
        sig[f"H_low_prox_{w}"] = -(close / close.rolling(w).min())
        sig[f"H_drawdown_{w}"] = close / close.rolling(w).max() - 1.0
    return sig


def families(names) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for n in names:
        out.setdefault(n.split("_")[0], []).append(n)
    return out
