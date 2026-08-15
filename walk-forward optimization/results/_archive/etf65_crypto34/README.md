# Superseded: the 65-ETF and 34-pair universes

Every `us_etfs` and `crypto` sheet here was measured on the universes that ran until
**2026-08-12**, before `universe_screen.py` existed:

| class | then | now |
|---|---|---|
| `us_etfs` | `ETF_ALL65`, 65 funds, held whole from their first bar | `ETF_TOP10`, 10 funds, each held only from the date it became liquid |
| `crypto` | `CRYPTO_ALL34`, 34 pairs | `CRYPTO_TOP20`, 20 pairs |

Both old lists are still named in `config.py`, so any sheet here is regenerable — pass
`--symbols` or repoint `CLASSES`.

## Do not compare a number here to a number in `results/`

Three things changed at once and each moves a score on its own:

1. **Who is in the basket.** 55 ETFs and 14 pairs left.
2. **When each name is held.** The ETF sheets here score SPDR sector funds on their
   1999–2005 bars, when they traded under $2M a day. The new sheets do not: a fund enters
   on the date its trailing-252-bar median dollar volume first clears $20M and never falls
   back. That cut removes between 0 and 6.5 years per name.
3. **What the basket measures.** The old ten-by-liquidity would have been all US equity
   beta. The new ten are not, and mean pairwise return correlation falls 0.72 → 0.44, so
   the same nominal breadth carries roughly twice the independent information. A breadth
   or hit-rate statistic is not comparable across that change even where the rule is.

The old ETF universe also carried the leveraged, inverse and commodity-roll block — TQQQ,
SQQQ, SPXL, UPRO, SOXL, SVXY, VXX, USO, UNG, DBC, DBA, CORN, WEAT, UGA. Any "beat
buy-and-hold" figure on those names is measured against a benchmark that decays by
construction (VXX −99.5% over its life, UNG −99.9%) and reads as skill. That is the single
most misleading thing in this folder.

`results/universe_screen_us_etfs.csv` and `results/universe_screen_crypto.csv` carry the
measurement and the rejection reason for all 65 and all 34.
