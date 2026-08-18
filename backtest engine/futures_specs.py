"""CME contract specifications: what one contract of each root is actually worth.

A futures root is not a price the way a share is. `ES` at 6,411.25 is not $6,411.25 of
anything — it is 50 index points, so one contract is $320,562 of S&P 500 exposure. `ZC`
at 438.25 is quoted in *cents* per bushel on 5,000 bushels, so it is $21,912. `ZN` at
111.23 is a percentage of a $100,000 par value, so it is $111,234. Three roots, three
different sentences, and no rule can be derived from the price alone.

Everything downstream that has to turn a price into money needs this table:

* the liquidity screen, which ranks on notional turnover and cannot compare 1.4M ZN
  contracts against 141k GC contracts without it;
* `db_loader`, which asserts the notional it computes is inside a sane band, so a wrong
  scale here fails loudly rather than silently demoting a major contract;
* the paper desk, when it sizes a position in whole contracts.

**`price_scale` is the whole point of the table and it is the field that will bite.** The
vendor's `definition` record gives `unit_of_measure_qty` honestly — 5,000 bushels, 100
troy ounces, $100,000 par — but it does NOT say what unit the *price* is quoted in, and
that is not derivable from the unit of measure. `HG` and `ZL` are both quoted per pound;
copper is in dollars and soybean oil is in cents, a factor of 100 apart. So the quantity
is read from the vendor and cross-checked (`verify_against_vendor`), while the scale is
stated here and defended by the notional band.

    notional_usd = price * price_scale * qty

`sector` exists because `metrics.se_ir` assumes independent assets, and six points on one
yield curve are not six assets. `futures_screen.py` uses it the way `universe_screen.py`
uses correlation for the ETFs — see the note there on how ten funds ranked on listing
date turned out to be one bet wearing ten names.
"""

from __future__ import annotations

# root -> (unit_of_measure_qty, price_scale, sector, exchange, tick, description)
#
# `qty` and `tick` mirror the vendor's `unit_of_measure_qty` and `min_price_increment`
# and are asserted against them. `tick` is the exchange's minimum price increment in
# quoted units — the screen reads it as a fraction of a day's range, because a grid that
# is coarse relative to how far the thing moves is a grid a mean-reversion rule harvests
# instead of a market. That is what a recycled penny stock's 1-cent grid did to this repo
# once; see `../CLAUDE.md`.
#
# `price_scale` converts the quoted price into the unit `qty` is counted in:
#   1.0    price is already in the quote currency per unit (index points, $/bbl, $/ozt)
#   0.01   price is in cents per unit (grains, livestock) or in percent of par (rates)
#   0.001  price is per thousand units (lumber, quoted per 1,000 board feet)
CME_CONTRACTS: dict[str, dict] = {
    # ---- equity index -------------------------------------------------------
    "ES":  dict(qty=50,        price_scale=1.0,   sector="equity",    exchange="XCME", tick=0.25, desc="E-mini S&P 500"),
    "NQ":  dict(qty=20,        price_scale=1.0,   sector="equity",    exchange="XCME", tick=0.25, desc="E-mini Nasdaq-100"),
    "YM":  dict(qty=5,         price_scale=1.0,   sector="equity",    exchange="XCBT", tick=1.0, desc="E-mini Dow"),
    "RTY": dict(qty=50,        price_scale=1.0,   sector="equity",    exchange="XCME", tick=0.1, desc="E-mini Russell 2000"),
    "EMD": dict(qty=100,       price_scale=1.0,   sector="equity",    exchange="XCME", tick=0.1, desc="E-mini S&P MidCap 400"),
    "NKD": dict(qty=5,         price_scale=1.0,   sector="equity",    exchange="XCME", tick=5.0, desc="Nikkei 225 (USD)"),
    # ---- rates --------------------------------------------------------------
    "ZT":  dict(qty=200_000,   price_scale=0.01,  sector="rates",     exchange="XCBT", tick=0.00390625, desc="2-Year T-Note"),
    "ZF":  dict(qty=100_000,   price_scale=0.01,  sector="rates",     exchange="XCBT", tick=0.0078125, desc="5-Year T-Note"),
    "ZN":  dict(qty=100_000,   price_scale=0.01,  sector="rates",     exchange="XCBT", tick=0.015625, desc="10-Year T-Note"),
    "TN":  dict(qty=100_000,   price_scale=0.01,  sector="rates",     exchange="XCBT", tick=0.015625, desc="Ultra 10-Year T-Note"),
    "ZB":  dict(qty=100_000,   price_scale=0.01,  sector="rates",     exchange="XCBT", tick=0.03125, desc="30-Year T-Bond"),
    "UB":  dict(qty=100_000,   price_scale=0.01,  sector="rates",     exchange="XCBT", tick=0.03125, desc="Ultra T-Bond"),
    "SR3": dict(qty=2_500,     price_scale=0.01,  sector="rates",     exchange="XCME", tick=0.005, desc="3-Month SOFR"),
    "SR1": dict(qty=4_167,     price_scale=0.01,  sector="rates",     exchange="XCME", tick=0.0025, desc="1-Month SOFR"),
    "ZQ":  dict(qty=4_167,     price_scale=0.01,  sector="rates",     exchange="XCBT", tick=0.005, desc="30-Day Fed Funds"),
    # ---- FX -----------------------------------------------------------------
    "6E":  dict(qty=125_000,   price_scale=1.0,   sector="fx",        exchange="XCME", tick=5e-05, desc="Euro FX"),
    "6J":  dict(qty=12_500_000, price_scale=1.0,  sector="fx",        exchange="XCME", tick=5e-07, desc="Japanese Yen"),
    "6B":  dict(qty=62_500,    price_scale=1.0,   sector="fx",        exchange="XCME", tick=0.0001, desc="British Pound"),
    "6A":  dict(qty=100_000,   price_scale=1.0,   sector="fx",        exchange="XCME", tick=5e-05, desc="Australian Dollar"),
    "6C":  dict(qty=100_000,   price_scale=1.0,   sector="fx",        exchange="XCME", tick=5e-05, desc="Canadian Dollar"),
    "6S":  dict(qty=125_000,   price_scale=1.0,   sector="fx",        exchange="XCME", tick=5e-05, desc="Swiss Franc"),
    "6N":  dict(qty=100_000,   price_scale=1.0,   sector="fx",        exchange="XCME", tick=5e-05, desc="New Zealand Dollar"),
    "6M":  dict(qty=500_000,   price_scale=1.0,   sector="fx",        exchange="XCME", tick=1e-05, desc="Mexican Peso"),
    "6L":  dict(qty=100_000,   price_scale=1.0,   sector="fx",        exchange="XCME", tick=5e-05, desc="Brazilian Real"),
    "6Z":  dict(qty=500_000,   price_scale=1.0,   sector="fx",        exchange="XCME", tick=2.5e-05, desc="South African Rand"),
    # ---- energy -------------------------------------------------------------
    "CL":  dict(qty=1_000,     price_scale=1.0,   sector="energy",    exchange="XNYM", tick=0.01, desc="WTI Crude Oil"),
    "BZ":  dict(qty=1_000,     price_scale=1.0,   sector="energy",    exchange="XNYM", tick=0.01, desc="Brent Crude (NYMEX)"),
    "NG":  dict(qty=10_000,    price_scale=1.0,   sector="energy",    exchange="XNYM", tick=0.001, desc="Henry Hub Natural Gas"),
    "RB":  dict(qty=42_000,    price_scale=1.0,   sector="energy",    exchange="XNYM", tick=0.0001, desc="RBOB Gasoline"),
    "HO":  dict(qty=42_000,    price_scale=1.0,   sector="energy",    exchange="XNYM", tick=0.0001, desc="NY Harbor ULSD"),
    "QM":  dict(qty=500,       price_scale=1.0,   sector="energy",    exchange="XNYM", tick=0.025, desc="E-mini Crude Oil"),
    "MCL": dict(qty=100,       price_scale=1.0,   sector="energy",    exchange="XNYM", tick=0.01, desc="Micro WTI Crude Oil"),
    # ---- metals -------------------------------------------------------------
    "GC":  dict(qty=100,       price_scale=1.0,   sector="metals",    exchange="XCEC", tick=0.1, desc="Gold"),
    "SI":  dict(qty=5_000,     price_scale=1.0,   sector="metals",    exchange="XCEC", tick=0.005, desc="Silver"),
    "HG":  dict(qty=25_000,    price_scale=1.0,   sector="metals",    exchange="XCEC", tick=0.0005, desc="Copper"),
    "PL":  dict(qty=50,        price_scale=1.0,   sector="metals",    exchange="XNYM", tick=0.1, desc="Platinum"),
    "PA":  dict(qty=100,       price_scale=1.0,   sector="metals",    exchange="XNYM", tick=0.5, desc="Palladium"),
    "MGC": dict(qty=10,        price_scale=1.0,   sector="metals",    exchange="XCEC", tick=0.1, desc="Micro Gold"),
    "SIL": dict(qty=1_000,     price_scale=1.0,   sector="metals",    exchange="XCEC", tick=0.005, desc="Micro Silver"),
    # ---- grains and oilseeds -------------------------------------------------
    "ZC":  dict(qty=5_000,     price_scale=0.01,  sector="grains",    exchange="XCBT", tick=0.25, desc="Corn"),
    "ZS":  dict(qty=5_000,     price_scale=0.01,  sector="grains",    exchange="XCBT", tick=0.25, desc="Soybeans"),
    "ZW":  dict(qty=5_000,     price_scale=0.01,  sector="grains",    exchange="XCBT", tick=0.25, desc="Chicago SRW Wheat"),
    "KE":  dict(qty=5_000,     price_scale=0.01,  sector="grains",    exchange="XCBT", tick=0.25, desc="KC HRW Wheat"),
    "ZO":  dict(qty=5_000,     price_scale=0.01,  sector="grains",    exchange="XCBT", tick=0.25, desc="Oats"),
    "ZL":  dict(qty=60_000,    price_scale=0.01,  sector="grains",    exchange="XCBT", tick=0.01, desc="Soybean Oil"),
    "ZM":  dict(qty=100,       price_scale=1.0,   sector="grains",    exchange="XCBT", tick=0.1, desc="Soybean Meal"),
    "ZR":  dict(qty=2_000,     price_scale=1.0,   sector="grains",    exchange="XCBT", tick=0.005, desc="Rough Rice"),
    # ---- livestock -----------------------------------------------------------
    "LE":  dict(qty=40_000,    price_scale=0.01,  sector="livestock", exchange="XCME", tick=0.025, desc="Live Cattle"),
    "GF":  dict(qty=50_000,    price_scale=0.01,  sector="livestock", exchange="XCME", tick=0.025, desc="Feeder Cattle"),
    "HE":  dict(qty=40_000,    price_scale=0.01,  sector="livestock", exchange="XCME", tick=0.025, desc="Lean Hogs"),
    # ---- crypto --------------------------------------------------------------
    "BTC": dict(qty=5,         price_scale=1.0,   sector="crypto",    exchange="XCME", tick=5.0, desc="Bitcoin"),
    "ETH": dict(qty=50,        price_scale=1.0,   sector="crypto",    exchange="XCME", tick=0.5, desc="Ether"),
    "MBT": dict(qty=0.1,       price_scale=1.0,   sector="crypto",    exchange="XCME", tick=5.0, desc="Micro Bitcoin"),
    # ---- equity index micros -------------------------------------------------
    "MES": dict(qty=5,         price_scale=1.0,   sector="equity",    exchange="XCME", tick=0.25, desc="Micro E-mini S&P 500"),
    "MNQ": dict(qty=2,         price_scale=1.0,   sector="equity",    exchange="XCME", tick=0.25, desc="Micro E-mini Nasdaq-100"),
    "MYM": dict(qty=0.5,       price_scale=1.0,   sector="equity",    exchange="XCBT", tick=1.0, desc="Micro E-mini Dow"),
    "M2K": dict(qty=5,         price_scale=1.0,   sector="equity",    exchange="XCME", tick=0.1, desc="Micro E-mini Russell 2000"),
    # ---- other ---------------------------------------------------------------
    "LBR": dict(qty=27_500,    price_scale=0.001, sector="other",     exchange="XCME", tick=0.5, desc="Lumber"),
    "DC":  dict(qty=200_000,   price_scale=0.01,  sector="other",     exchange="XCME", tick=0.01, desc="Class III Milk"),
    "CSC": dict(qty=20_000,    price_scale=1.0,   sector="other",     exchange="XCME", tick=0.001, desc="Cash-Settled Cheese"),
}

# A root that is the same underlying as another, at a fraction of the size. Two contracts
# on one index are one asset: they share every bar of return, so keeping both doubles the
# apparent breadth of the universe and adds no information at all. `metrics.se_ir` assumes
# independence and would be told a lie it cannot detect.
#
# The full-size contract wins each pair because it is the one with the history — the
# micros all list in 2019 or later, against 2010 for the parent.
MICRO_OF = {"MES": "ES", "MNQ": "NQ", "MYM": "YM", "M2K": "RTY", "MGC": "GC",
            "SIL": "SI", "MCL": "CL", "QM": "CL", "MBT": "BTC"}

# The date GLBX.MDP3 itself begins. Nothing before this is obtainable at any price: it is
# the start of Databento's CME archive, not a choice this project made. Every futures
# result is therefore a ~16-year sample where the us_stocks daily sheet is ~26, and
# `metrics.se_ir` falls as 1/sqrt(years) — so the noise ceiling on this class is roughly
# 1.3x the equity one and every gate is correspondingly harder to clear.
GLBX_START = "2010-06-06"

# The band a single contract's notional must sit inside for the table to be believed.
#
# The ceiling is the load-bearing half and it is deliberately tight. The largest contract
# in the pool is NQ at ~$470,000 and the smallest is SR3 at ~$2,400, so $1M leaves room
# for a doubling of the Nasdaq and still sits below 100x the *smallest* plausible root —
# which is what makes a `price_scale` that is wrong by a factor of 100 land outside by
# construction. Widen this and the guard stops guarding: at $20M a corn contract priced
# in dollars instead of cents reads as $2.19M and passes.
#
# Review it if a root is added whose contract is genuinely larger than ~$1M, and say so
# here rather than quietly raising the number.
NOTIONAL_BAND_USD = (2_000.0, 1_000_000.0)


def spec(root: str) -> dict:
    try:
        return CME_CONTRACTS[root]
    except KeyError:
        raise KeyError(f"{root!r} is not a known CME root; add it to CME_CONTRACTS") from None


def notional_usd(root: str, price: float) -> float:
    """What one contract of `root` is worth in USD at `price`."""
    s = spec(root)
    return float(price) * s["price_scale"] * s["qty"]


def sector(root: str) -> str:
    return spec(root)["sector"]


def is_micro(root: str) -> bool:
    return root in MICRO_OF


def check_notional(root: str, price: float) -> None:
    """Raise if `price` implies an impossible contract size. The scale guard."""
    n = notional_usd(root, price)
    lo, hi = NOTIONAL_BAND_USD
    if not (lo <= n <= hi):
        raise ValueError(
            f"{root} at {price} implies ${n:,.0f} per contract, outside the plausible "
            f"band ${lo:,.0f}-${hi:,.0f}. `price_scale` in futures_specs.py is probably "
            f"wrong by a factor of 100 -- check whether {root} is quoted in cents.")


def verify_against_vendor(definitions) -> list[str]:
    """Cross-check `qty` against the vendor's own `unit_of_measure_qty`.

    `definitions` is a frame from the Databento `definition` schema carrying `symbol`
    (continuous, e.g. `ES.v.0`) and `unit_of_measure_qty`. Returns a list of complaints;
    empty means the table agrees with the exchange.

    The scale is deliberately NOT checked here — the vendor does not publish it. That is
    what `check_notional` is for.
    """
    out = []
    for _, row in definitions.iterrows():
        root = str(row["symbol"]).split(".")[0]
        if root not in CME_CONTRACTS:
            continue
        want = CME_CONTRACTS[root]["qty"]
        got = float(row["unit_of_measure_qty"])
        if abs(got - want) > max(1e-9, 1e-9 * want):
            out.append(f"{root}: table says qty={want}, vendor says {got}")
    return out


def tick(root: str) -> float:
    """The exchange's minimum price increment, in the units the root is quoted in."""
    return float(spec(root)["tick"])


def verify_ticks(definitions) -> list[str]:
    """Cross-check `tick` against the vendor's `min_price_increment`. See above."""
    out = []
    for _, row in definitions.iterrows():
        root = str(row["symbol"]).split(".")[0]
        if root not in CME_CONTRACTS:
            continue
        want = CME_CONTRACTS[root]["tick"]
        got = float(row["min_price_increment"])
        if abs(got - want) > 1e-12 * max(1.0, want):
            out.append(f"{root}: table says tick={want}, vendor says {got}")
    return out
