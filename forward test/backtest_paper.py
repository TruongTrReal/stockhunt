"""Run the live strategy through Nautilus on cached bars, before it ever sees a live feed.

This exists because of a specific gap: `run_paper.py` builds a node and connects, but no
order had ever *filled* through `TalibRuleStrategy._rebalance`. A path that has never
executed is not working code, it is untested code, and the first time it runs would have
been against a live feed at a real bar close with nobody watching.

Here the same strategy class, the same signal layer and the same rebalance logic run
against `backtest engine/data/cache_*/SYMBOL.parquet` inside a `BacktestEngine`. The
event flow is identical to paper trading — bar in, signal, order, fill, position, P&L —
with the clock driven by the data instead of the wall. If it fills here it will fill live.

It also answers MK's second ask directly: the top rules, running on NautilusTrader.

Run::

    python backtest_paper.py                          # SOXL + TQQQ, 1d, SMA_200
    python backtest_paper.py --symbols BTC/USD --rule EMA_200 --timeframe 4h
    python backtest_paper.py --symbols SPY --bars 800 --write-state

`--write-state` dumps `results/paper_state.json` from the run, which is how the dashboard
can be checked end to end without waiting days for live bars. The document it writes is
clearly marked as a simulation: it must never be mistaken for live paper trading, so the
feed status reads `backtest` and not `ok`.
"""

from __future__ import annotations

import argparse
from decimal import Decimal

import numpy as np
import pandas as pd

import fwd_config
import paper_state
from strategy import TalibRuleConfig, TalibRuleStrategy

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Money, Price, Quantity

VENUE = "SIM"
PRICE_PRECISION = 6
SIZE_PRECISION = 6
CAPITAL = 100_000.0

BAR_SPEC = {"1d": "1-DAY-LAST-EXTERNAL", "4h": "4-HOUR-LAST-EXTERNAL"}
def cache_dirs(timeframe: str) -> list:
    """Everywhere in the repo that cached bars for this timeframe might live.

    Two studies, two layouts. `backtest engine/` splits by asset class
    (`cache_us_stocks_1d`, `cache_crypto_1d`) and holds the 20 mega-caps plus the 10
    crypto pairs. `top 20 stocks/` is flat (`cache_1d`) and is the **only** place SOXL and
    TQQQ exist — MK's two leveraged ETFs were added to that study, not to the master
    sweep. Searching both is what lets this run over the forward-test universe rather
    than only the research one.
    """
    bm, t20 = fwd_config.BACKTEST_ENGINE / "data", fwd_config.REPO / "top 20 stocks" / "data"
    return [bm / f"cache_us_stocks_{timeframe}", bm / f"cache_crypto_{timeframe}",
            t20 / f"cache_{timeframe}"]


def safe(symbol: str) -> str:
    """A Nautilus `Symbol`: no separators at all. `BTC/USD` -> `BTCUSD`."""
    return symbol.replace("/", "").replace("-", "")


def filenames(symbol: str) -> list[str]:
    """Cache filename candidates. Not the same convention as `safe()` — the loaders write
    `BTC_USD.parquet`, keeping the pair separator as an underscore, while a Nautilus
    Symbol cannot carry one. Both spellings are tried so neither study's layout has to
    change to be readable from here."""
    return [f"{symbol.replace('/', '_')}.parquet", f"{safe(symbol)}.parquet"]


def load_bars(symbol: str, timeframe: str) -> pd.DataFrame:
    """Cached OHLCV for one cell, from whichever study holds it."""
    for d in cache_dirs(timeframe):
        for name in filenames(symbol):
            p = d / name
            if p.exists():
                df = pd.read_parquet(p)
                df.index = pd.to_datetime(df.index, utc=True)
                return df.sort_index()
    looked = "\n    ".join(str(d) for d in cache_dirs(timeframe))
    raise FileNotFoundError(
        f"no cached {timeframe} bars for {symbol} (tried {', '.join(filenames(symbol))}). "
        f"Looked in:\n    {looked}")


def make_instrument(symbol: str):
    """A fractional-quantity instrument, deliberately.

    A real `Equity` has `size_precision=0` and would round every order to whole shares.
    That is realistic, and `../backtest engine/validate.py` is where it belongs. Here the
    question is whether the order path works at all, and whole-share rounding on a
    $100k account can silently zero out a small rebalance and look like "no fills".
    """
    iid = InstrumentId(Symbol(safe(symbol)), Venue(VENUE))
    return CurrencyPair(
        instrument_id=iid, raw_symbol=Symbol(safe(symbol)),
        base_currency=USD, quote_currency=USD,
        price_precision=PRICE_PRECISION, size_precision=SIZE_PRECISION,
        price_increment=Price(10 ** -PRICE_PRECISION, PRICE_PRECISION),
        size_increment=Quantity(10 ** -SIZE_PRECISION, SIZE_PRECISION),
        lot_size=None, max_quantity=None, min_quantity=None,
        max_notional=None, min_notional=None, max_price=None, min_price=None,
        margin_init=Decimal(0), margin_maint=Decimal(0),
        maker_fee=Decimal(0), taker_fee=Decimal(0), ts_event=0, ts_init=0,
    )


def build_bars(df: pd.DataFrame, bar_type: BarType) -> list[Bar]:
    # `.astype("int64")` on a millisecond index yields milliseconds, not the nanoseconds
    # Nautilus expects — hence the explicit `as_unit("ns")`.
    idx = pd.to_datetime(df.index, utc=True).as_unit("ns")
    ts = idx.astype("int64").to_numpy()
    vol = df["Volume"].to_numpy() if "Volume" in df else np.zeros(len(df))
    vol = np.where(np.isfinite(vol), vol, 0.0)     # crypto ships no volume at all
    return [
        Bar(bar_type=bar_type,
            open=Price(o, PRICE_PRECISION), high=Price(h, PRICE_PRECISION),
            low=Price(lo, PRICE_PRECISION), close=Price(c, PRICE_PRECISION),
            volume=Quantity(v, SIZE_PRECISION), ts_event=int(t), ts_init=int(t))
        for o, h, lo, c, v, t in zip(
            df["Open"].to_numpy(), df["High"].to_numpy(), df["Low"].to_numpy(),
            df["Close"].to_numpy(), vol, ts)
    ]


def run_cell(symbol: str, timeframe: str, rule: str, bars_limit: int | None,
             allow_short: bool, write_state: bool, log_level: str) -> dict:
    df = load_bars(symbol, timeframe)
    if bars_limit:
        df = df.tail(bars_limit)

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("FWDBT-001"),
        logging=LoggingConfig(bypass_logging=log_level == "OFF", log_level=log_level),
    ))
    engine.add_venue(venue=Venue(VENUE), oms_type=OmsType.NETTING,
                     account_type=AccountType.MARGIN, base_currency=USD,
                     starting_balances=[Money(CAPITAL, USD)])
    inst = make_instrument(symbol)
    engine.add_instrument(inst)

    bar_type = BarType.from_str(f"{inst.id}-{BAR_SPEC[timeframe]}")
    engine.add_data(build_bars(df, bar_type))

    # `min_warmup_bars` is lowered to what this slice can actually supply. The live
    # default is 250 and is a correctness floor there, where the vendor can be asked for
    # more history. Here the slice is whatever was requested, and refusing to trade would
    # report "no fills" for a reason that has nothing to do with the order path.
    warmup = min(fwd_config.MIN_WARMUP_BARS, max(len(df) // 4, 30))
    strat = TalibRuleStrategy(config=TalibRuleConfig(
        order_id_tag=f"{safe(symbol)}-{timeframe}",
        instrument_id=inst.id, bar_type=bar_type, rule=rule,
        allow_short=allow_short, window_bars=fwd_config.DEFAULT_WINDOW_BARS,
        min_warmup_bars=warmup,
        asset_class="crypto" if "/" in symbol else "equity",
        timeframe=timeframe, export_state=write_state, display_symbol=symbol,
        note=f"Nautilus backtest over {len(df)} cached bars — not live paper trading."))
    engine.add_strategy(strat)
    engine.run()

    account = engine.portfolio.account(Venue(VENUE))
    cash = account.balance_total(USD).as_double()
    unreal = engine.portfolio.unrealized_pnl(inst.id)
    equity = cash + (unreal.as_double() if unreal is not None else 0.0)

    close = df["Close"].to_numpy(dtype="float64")
    bh = float(close[-1] / close[0] - 1.0) * 100.0
    out = {
        "symbol": symbol, "timeframe": timeframe, "rule": rule,
        "bars": len(df), "warmup": warmup,
        "first": str(df.index[0].date()), "last": str(df.index[-1].date()),
        "fills": strat._n_fills,
        "final_equity": round(equity, 2),
        "return_pct": round((equity / CAPITAL - 1.0) * 100.0, 2),
        "buyhold_pct": round(bh, 2),
        "final_target": strat._target,
    }
    engine.dispose()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+", default=["SOXL", "TQQQ"])
    ap.add_argument("--timeframe", default="1d", choices=list(BAR_SPEC))
    ap.add_argument("--rule", default="SMA_200")
    ap.add_argument("--bars", type=int, default=1200,
                    help="use only the last N cached bars (0 = all)")
    ap.add_argument("--allow-short", action="store_true")
    ap.add_argument("--write-state", action="store_true",
                    help="write results/paper_state.json from this run")
    ap.add_argument("--log-level", default="OFF")
    args = ap.parse_args()

    if args.write_state:
        paper_state.reset()
        paper_state.set_feed(source="cached Twelve Data bars", plan="pro",
                             status="backtest", last_bar="—")
        # Each cell here runs its own engine with its own starting balance, all under the
        # venue name "SIM". They are separate accounts that happen to share a label, so
        # the per-venue aggregation would collapse them to one; totals are set explicitly
        # below instead.
        paper_state.set_venue(name="Nautilus backtest (SIM)", balance=CAPITAL,
                              equity=CAPITAL, aggregate=False)

    rows = []
    for symbol in args.symbols:
        try:
            rows.append(run_cell(symbol, args.timeframe, args.rule,
                                 args.bars or None, args.allow_short,
                                 args.write_state, args.log_level))
        except FileNotFoundError as exc:
            print(f"  {symbol}: {exc}")

    if not rows:
        raise SystemExit("nothing ran")

    print(f"\n  {'symbol':<10} {'bars':>6} {'fills':>6} {'return':>9} "
          f"{'buy&hold':>9}  window")
    for r in rows:
        print(f"  {r['symbol']:<10} {r['bars']:>6} {r['fills']:>6} "
              f"{r['return_pct']:>8.2f}% {r['buyhold_pct']:>8.2f}%  "
              f"{r['first']} .. {r['last']}")

    total_fills = sum(r["fills"] for r in rows)
    print(f"\n  {total_fills} fills across {len(rows)} instruments")
    if total_fills == 0:
        print("  ORDER PATH UNPROVEN: no order filled. Do not start live paper trading.")
        raise SystemExit(1)
    print("  order path works: bars -> signal -> order -> fill -> position -> P&L")

    if args.write_state:
        paper_state.set_venue(balance=CAPITAL * len(rows),
                              equity=round(sum(r["final_equity"] for r in rows), 2))
        p = paper_state.flush()
        print(f"  wrote {p}")
        print("  NOTE: this is a *backtest* state document, marked status=backtest. "
              "Regenerate the site from a live run before showing it as paper trading.")


if __name__ == "__main__":
    main()
