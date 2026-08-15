"""Golden-output harness: prove a refactor changed nothing.

This repo has retracted two findings. The thing that makes it worth anything is that
its numbers are reproducible, so a refactor that silently moves one is worse than no
refactor at all. `../strategies/CLAUDE.md` records the precedent: the 976-line catalog
split was verified by hashing 1,012 (cell, symbol) position series before and after and
requiring every one to be byte-identical, and that is what caught the one real breakage
— `momo_regime` and `voltgt_tsmom` would have *silently vanished* from every sheet
rather than failing loudly.

So: capture before, verify after. Nothing else is evidence.

What is hashed, per (class, timeframe, rule):

* the **int8 position series** on a deterministic sample of symbols — the thing every
  downstream number is a function of, and the earliest place a break shows up;
* the **scored output** — final equity, Sharpe, IR against buy-and-hold, turnover —
  because two different position series can still hash apart while the arithmetic that
  consumes them is what actually broke.

One digest per (sheet, rule) rather than one for the whole run, so a failure names the
rule instead of saying "something moved". Symbols are sampled on a fixed stride over the
sorted symbol list, which spreads the sample across the long and short series instead of
taking 25 names that all start in 1970.

Run::

    python tools/golden.py capture                    # before the refactor
    python tools/golden.py verify                     # after; exits nonzero on any drift
    python tools/golden.py capture --sheets us_stocks/1d crypto/1d
    python tools/golden.py verify --per-symbol        # narrow a failure to one symbol
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ENGINE = REPO / "backtest engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

import numpy as np                                          # noqa: E402

from config import (BASELINE_NAME, CAPITAL_PER_TICKER,       # noqa: E402
                    CLASSES, MIN_BARS, TIMEFRAMES, headline_key, scenario)
from engines import vector                                   # noqa: E402
import metrics                                               # noqa: E402
import signals                                               # noqa: E402
import td_loader                                             # noqa: E402

from strategies.talib_signals import get_all_indicator_names  # noqa: E402

GOLDEN_DIR = HERE / "golden"

# Sheets worth gating on. The 1d/4h pairs are what every headline number is read off;
# the fine intraday grids are the same code path on more bars, so they add runtime
# without adding coverage of anything that could break independently.
DEFAULT_SHEETS = [
    ("us_stocks", "1d"), ("us_stocks", "4h"),
    ("us_etfs", "1d"), ("us_etfs", "4h"),
    ("crypto", "1d"), ("crypto", "4h"),
    ("commodities", "1d"), ("commodities", "4h"),
]

# Enough symbols to catch a break, few enough to run in minutes. 25 x 232 rules is
# ~5,800 position series per sheet.
SAMPLE_SYMBOLS = 25

# Everything is rounded before hashing. Float64 arithmetic is not associative, so a
# refactor that merely reorders a sum — exactly what vectorising or parallelising does —
# moves the last bits without changing any answer. 12 significant figures is far tighter
# than anything this repo reports (three) and still immune to reassociation noise.
ROUND_SIG = 12


def _sample(data: dict) -> list[str]:
    """A deterministic, length-spread symbol sample.

    Fixed stride over the sorted list rather than the first N: `sorted(data)[:25]` on
    us_stocks is 25 tickers beginning with A, which correlates with nothing useful but
    would happily miss a break that only shows on short series.
    """
    syms = sorted(data)
    if len(syms) <= SAMPLE_SYMBOLS:
        return syms
    stride = len(syms) / SAMPLE_SYMBOLS
    return [syms[int(i * stride)] for i in range(SAMPLE_SYMBOLS)]


def _round(x: float) -> float:
    """Round to `ROUND_SIG` significant figures; NaN and inf pass through as themselves."""
    if x is None:
        return float("nan")
    x = float(x)
    if not np.isfinite(x) or x == 0.0:
        return x
    from math import floor, log10
    return round(x, ROUND_SIG - 1 - int(floor(log10(abs(x)))))


def _digest(parts: list[bytes]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
        h.update(b"\x00")
    return h.hexdigest()[:32]


def capture_sheet(asset_class: str, timeframe: str, per_symbol: bool = False) -> dict:
    """Per-rule digests for one sheet. `{rule: digest}`, plus `{rule|symbol: digest}`
    when `per_symbol` is set, which is how a failure gets narrowed to one series."""
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        return {}

    symbols = _sample(data)
    names = list(get_all_indicator_names())
    if BASELINE_NAME not in names:
        names.append(BASELINE_NAME)
    runnable, _ = signals.usable_rules(names, asset_class, timeframe)

    free = {"key": "gross", "commission_bps": 0.0, "half_spread_bps": 0.0,
            "sell_fee_bps": 0.0, "borrow_annual": 0.0}
    bench = {}
    for s in symbols:
        df = data[s]
        close = df["Close"].to_numpy(dtype="float64")
        bpy = vector.bars_per_year(df.index)
        bench[s] = {"net": vector.net_returns(np.ones(len(df)), close, free, bpy),
                    "bpy": bpy, "close": close}

    # The real fee schedule for this sheet, not `gross`. Costs are charged on |d position|
    # and on the sell side only, so a break in the turnover or the short accounting is
    # invisible at zero cost — which is most of what an engine refactor can plausibly hurt.
    #
    # This read `scenarios_for(asset_class, timeframe)[0]`, which is `gross` on every
    # full-grid sheet — the exact thing the paragraph above says it must not be. So the
    # harness charged nothing and was blind to precisely the regressions it exists to
    # catch, while its comment asserted the opposite. Same defect as `config.headline_key`
    # and `sweep._headline_for`; go through `headline_key` so there is one answer.
    fee = scenario(asset_class, headline_key(asset_class, timeframe))
    if fee["key"] == "gross":
        # Reachable only if a sheet genuinely collapsed to gross, in which case there is
        # no paid schedule to hash and the gate is weaker than it looks. Say so rather
        # than pass silently — a green golden run that charged nothing is worse than no
        # golden run, because it is trusted.
        print(f"  WARNING: {asset_class} {timeframe} has no paid scenario; digests are "
              f"at ZERO cost and cannot see a turnover or short-accounting break",
              file=sys.stderr)

    out: dict[str, str] = {}
    for rule in runnable:
        parts: list[bytes] = []
        for s in symbols:
            df = data[s]
            b = bench[s]
            pos = signals.position_for(rule, df, asset_class, timeframe,
                                       baseline_name=BASELINE_NAME)
            if pos is None:
                cell = [b"none"]
            else:
                use = free if rule == BASELINE_NAME else fee
                net = vector.net_returns(pos, b["close"], use, b["bpy"])
                st = vector.stats(pos, b["close"], df.index, use, CAPITAL_PER_TICKER)
                ir = metrics.information_ratio(net, b["net"], b["bpy"])
                cell = [
                    # The positions themselves, exactly. int8 is lossless here: every
                    # value is -1, 0 or 1, and rounding does not apply to an integer.
                    np.asarray(pos, dtype="float64").astype(np.int8).tobytes(),
                    repr([_round(v) for v in (
                        st["final_equity"], st["sharpe"], st["cagr"],
                        st["max_drawdown"], st["exposure"],
                        st["turnover_per_year"], float(st["n_trades"]), ir,
                    )] if st is not None else [None]).encode(),
                ]
            parts.extend(cell)
            if per_symbol:
                out[f"{rule}|{s}"] = _digest(cell)
        out[rule] = _digest(parts)
    return out


def capture(sheets: list[tuple[str, str]], per_symbol: bool) -> dict:
    snap = {"sheets": {}, "sample_symbols": SAMPLE_SYMBOLS, "round_sig": ROUND_SIG}
    for asset_class, timeframe in sheets:
        t0 = time.time()
        tag = f"{asset_class}/{timeframe}"
        digests = capture_sheet(asset_class, timeframe, per_symbol)
        if not digests:
            print(f"  {tag:<22} no cached data, skipped")
            continue
        snap["sheets"][tag] = digests
        n_rules = len([k for k in digests if "|" not in k])
        print(f"  {tag:<22} {n_rules} rules  ({time.time() - t0:.0f}s)")
    return snap


def verify(snap: dict, sheets: list[tuple[str, str]], per_symbol: bool) -> int:
    """Recompute and diff. Returns the number of drifted entries."""
    drift = 0
    for asset_class, timeframe in sheets:
        tag = f"{asset_class}/{timeframe}"
        old = snap["sheets"].get(tag)
        if old is None:
            print(f"  {tag:<22} not in the captured snapshot, skipped")
            continue
        t0 = time.time()
        new = capture_sheet(asset_class, timeframe, per_symbol)
        keys = sorted(set(old) | set(new))
        bad = [k for k in keys if old.get(k) != new.get(k)]
        drift += len(bad)
        status = "OK" if not bad else f"DRIFT on {len(bad)}"
        print(f"  {tag:<22} {status}  ({time.time() - t0:.0f}s)")
        for k in bad[:20]:
            was, now = old.get(k, "<absent>"), new.get(k, "<absent>")
            print(f"      {k:<40} {was} -> {now}")
        if len(bad) > 20:
            print(f"      ... and {len(bad) - 20} more")
    return drift


def _parse_sheets(raw: list[str] | None) -> list[tuple[str, str]]:
    if not raw:
        return DEFAULT_SHEETS
    out = []
    for item in raw:
        asset_class, _, timeframe = item.partition("/")
        if asset_class not in CLASSES or timeframe not in TIMEFRAMES:
            raise SystemExit(f"bad sheet {item!r}; expected <class>/<tf>")
        out.append((asset_class, timeframe))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["capture", "verify"])
    ap.add_argument("--sheets", nargs="+",
                    help="e.g. us_stocks/1d crypto/4h (default: the eight 1d/4h sheets)")
    ap.add_argument("--name", default="baseline", help="snapshot file name")
    ap.add_argument("--per-symbol", action="store_true",
                    help="also hash each (rule, symbol) so a failure names the series")
    args = ap.parse_args()

    sheets = _parse_sheets(args.sheets)
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / f"{args.name}.json"

    if args.mode == "capture":
        print(f"capturing {len(sheets)} sheets -> {path}")
        snap = capture(sheets, args.per_symbol)
        path.write_text(json.dumps(snap, indent=1, sort_keys=True), encoding="utf-8")
        total = sum(len([k for k in d if "|" not in k]) for d in snap["sheets"].values())
        print(f"\nwrote {path}  ({len(snap['sheets'])} sheets, {total} rule digests)")
        return

    if not path.exists():
        raise SystemExit(f"no snapshot at {path} — run `capture` first")
    snap = json.loads(path.read_text(encoding="utf-8"))
    print(f"verifying against {path}")
    drift = verify(snap, sheets, args.per_symbol)
    if drift:
        print(f"\nFAIL: {drift} digests moved. The refactor changed a number.")
        raise SystemExit(1)
    print("\nOK: every digest identical. The refactor changed nothing.")


if __name__ == "__main__":
    main()
