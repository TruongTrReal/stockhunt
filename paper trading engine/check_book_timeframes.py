r"""Would a class-wide book reproduce its backtest at a timeframe the desk does not yet run?

This is check 3 of the three `paper_config.BOOK_TIMEFRAMES` documents. A live book has no
full series: `book_strategy` keeps a rolling buffer of the last `DEFAULT_WINDOW_BARS` bars
and recomputes the rule on every close. A rule whose state depends on all prior history
therefore trades something the backtest never scored, silently -- `lorentzian_knn` agreed
with itself on only 67-83% of 5m bars for exactly that reason, and is barred here as a
result.

Same method as `parity_live.py`, one level up. `parity_live` asks "how big must the window
be for THIS rule", over a couple of hand-picked symbols and the TA-Lib dispatcher. This
asks the question the desk actually faces: **at the window the desk already uses, over the
whole universe a book would hold, do the rules that would actually be promoted reproduce?**
So it differs in three ways, and each of them is the point:

* **`live_signal.position_for`, not `generate_position`.** The board's leaders at these
  sizes are published strategies and combos, which the TA-Lib dispatcher raises on. The
  live book calls `live_signal`; so does this.
* **`paper_config.book_universe(cls)`**, which for `us_stocks` is the live top 100 -- the
  names a book holds, not the 23-name pinned roster.
* **The candidates come from the board's own order**, through `catalog.cells`, which is
  what `promote_top.py` promotes from. Inventing a second ranking here would measure rules
  nobody would run.

Where a board sheet is empty -- no `edge_standard` rows for that cell, so nothing is
scored -- it falls back to `book_<cls>_<tf>.csv` ranked on `cashmatch_excess_cagr`, the
board's own tiebreak, and labels those rows `book_csv` in the `source` column. That is an
approximation of a ranking, not the ranking, and it is marked so it cannot be mistaken for
one.

Run::

    python -u check_book_timeframes.py --tf 1h 15m
    python -u check_book_timeframes.py --tf 1h --cls crypto --top 5

Long. Launch it detached from bash, never from PowerShell::

    nohup python -u check_book_timeframes.py --tf 1h 15m > logs/book_tf_check.log 2>&1 &
"""

from __future__ import annotations

import argparse
import time

import numpy as np
import pandas as pd

import paper_config                                   # noqa: F401  (wires sys.path)
import live_signal

N_EVAL_BARS = 200
TOP_N = 5

# A rule that misses is missing for one of two reasons and they have opposite answers, so
# the failure is followed up rather than merely reported.
#
#   window_too_short   the rule's own lookback is stated in CALENDAR time and does not FIT
#                      in the buffer. `tsmom12` wants twelve months; 1,500 bars is six
#                      years at 1d and ten months at 1h, so the same rule fits at one size
#                      and not at the other. A bigger buffer fixes it, up to the ceiling
#                      `td_live.OUTPUT_SIZE` puts on a single warm-up request.
#   anchored_state     the rule's value at bar t depends on bar 0 — `_causal_median` and
#                      `regime._causal_quantile` are expanding, not rolling — so it slides
#                      with the buffer and NO window reproduces it. This is
#                      `lorentzian_knn`'s failure, and no buffer size is an answer to it.
#
# The diagnosis is a re-measure at successively larger windows on a sample of the names,
# never a reading of the rule's source: that is the same reason `test_causality.py` proves
# causality by truncation instead of by inspection.
DIAGNOSE_MULTIPLES = (3, 12)
DIAGNOSE_SYMBOLS = 3
DIAGNOSE_EVAL = 40

# The board's own idle filter, restated rather than imported: `board_rank` lives behind the
# dashboard's bootstrap and this script must run without it when the fallback is used.
MIN_EXPOSURE = 0.01


def board_candidates(cls: str, tf: str, top: int) -> list[dict]:
    """The first `top` TRADABLE cells on the board, exactly as `promote_top.picks` takes them."""
    import catalog
    out = []
    for cell in catalog.cells(cls, tf, depth=max(top * 5, 25)):
        if not cell.get("tradable"):
            continue
        out.append({"rule": cell["rule"], "family": cell.get("family"),
                    "source": "board"})
        if len(out) >= top:
            break
    return out


def book_csv_candidates(cls: str, tf: str, top: int) -> list[dict]:
    """The fallback, for a cell the board cannot rank because nothing scored it.

    Ranked on `cashmatch_excess_cagr` -- the board's tiebreak -- after the board's own idle
    filter. It is NOT `edge_passed, then book_cm_excess_cagr`, because the first key does
    not exist for these cells; that is why the rows are labelled.
    """
    path = paper_config.WFO_RESULTS / f"book_{cls}_{tf}.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    if "exposure" in df:
        df = df[pd.to_numeric(df["exposure"], errors="coerce").fillna(0) >= MIN_EXPOSURE]
    key = "cashmatch_excess_cagr" if "cashmatch_excess_cagr" in df else "cagr"
    df = df.sort_values(key, ascending=False)
    out, seen = [], []
    for r in df.itertuples():
        rule = str(getattr(r, "rule", "") or "")
        if not rule or any(paper_config._same_idea(rule, s) for s in seen):
            continue
        fam = live_signal.family(rule)
        if fam == live_signal.UNKNOWN:
            continue
        seen.append(rule)
        out.append({"rule": rule, "family": fam, "source": "book_csv"})
        if len(out) >= top:
            break
    return out


def candidates(cls: str, tf: str, top: int) -> tuple[list[dict], str]:
    try:
        rows = board_candidates(cls, tf, top)
    except Exception as exc:
        print(f"    ! board sheet for {cls} {tf} did not build: "
              f"{type(exc).__name__}: {exc}")
        rows = []
    if rows:
        return rows, ""
    rows = book_csv_candidates(cls, tf, top)
    note = ("board sheet empty (no edge_standard rows for this cell); "
            "ranked from book CSV on cashmatch_excess_cagr")
    return rows, (note if rows else "no board sheet and no book CSV - nothing to check")


def _clean(raw, n):
    if raw is None:
        return None
    arr = np.nan_to_num(np.asarray(raw, dtype="float64"), nan=0.0,
                        posinf=0.0, neginf=0.0)
    return arr if arr.size == n else None


def full_series(rule: str, df: pd.DataFrame, symbol: str):
    """The backtest's own position series — the thing the rolling buffer has to match.

    Split out and passed in by `diagnose` rather than recomputed per window: on the 15m
    sheets a frame is a quarter of a million bars and a state-machine strategy takes
    minutes over one, so recomputing it once per candidate window turned a three-minute
    follow-up into a forty-minute one.
    """
    return _clean(live_signal.position_for(rule, df, symbol), len(df))


def agreement(rule: str, df: pd.DataFrame, symbol: str,
              window: int, n_eval: int, truth=None) -> tuple[int, int]:
    """(agreeing bars, bars compared) for one rule on one symbol at this window."""
    truth = full_series(rule, df, symbol) if truth is None else truth
    if truth is None:
        return 0, 0
    ends = np.unique(np.linspace(max(1, len(df) - n_eval), len(df) - 1,
                                 n_eval).astype(int))
    agree = 0
    for e in ends:
        lo = max(0, int(e) - window + 1)
        sub = df.iloc[lo:int(e) + 1]
        got = _clean(live_signal.position_for(rule, sub, symbol), len(sub))
        if got is not None and got[-1] == truth[int(e)]:
            agree += 1
    return agree, len(ends)


def diagnose(rule: str, frames: dict, window: int) -> tuple[str, str]:
    """Is a bigger buffer the answer, or is nothing? Returns (verdict, evidence)."""
    sample = dict(list(frames.items())[:DIAGNOSE_SYMBOLS])
    truths = {s: full_series(rule, d, s) for s, d in sample.items()}
    parts = []
    best = 0.0
    for mult in DIAGNOSE_MULTIPLES:
        w = window * mult
        a = t = 0
        for sym, d in sample.items():
            x, y = agreement(rule, d, sym, w, DIAGNOSE_EVAL, truth=truths[sym])
            a += x
            t += y
        if not t:
            continue
        frac = a / t
        best = max(best, frac)
        parts.append(f"w{w}={frac:.0%}")
        if frac == 1.0:
            return "window_too_short", " ".join(parts)
    verdict = "anchored_state" if best < 0.999 else "window_too_short"
    return verdict, " ".join(parts)


def run(classes, timeframes, top, window, n_eval, do_diagnose=True) -> pd.DataFrame:
    import td_loader
    rows = []
    for cls in classes:
        universe = paper_config.book_universe(cls)
        for tf in timeframes:
            print(f"\n=== {cls} {tf} - {len(universe)} names in the book, "
                  f"window {window}, {n_eval} bars each ===", flush=True)
            picks, note = candidates(cls, tf, top)
            if not picks:
                print(f"    {note}")
                rows.append({"cls": cls, "tf": tf, "rank": None, "rule": None,
                             "family": None, "source": None, "note": note})
                continue
            if note:
                print(f"    ! {note}")

            try:
                frames = td_loader.load(cls, tf, universe)
            except (KeyError, FileNotFoundError) as exc:
                miss = f"no cached {tf} bars for {cls}: {exc}"
                print(f"    ! {miss}")
                rows.append({"cls": cls, "tf": tf, "rank": None, "rule": None,
                             "family": None, "source": None, "note": miss})
                continue
            frames = {s: d for s, d in frames.items() if d is not None and len(d) > 50}
            absent = [s for s in universe if s not in frames]
            if not frames:
                miss = f"no cached {tf} bars for any of {len(universe)} {cls} names"
                print(f"    ! {miss}")
                rows.append({"cls": cls, "tf": tf, "rank": None, "rule": None,
                             "family": None, "source": None, "note": miss})
                continue
            if absent:
                print(f"    ! {len(absent)} of {len(universe)} names have no cached {tf} "
                      f"bars: {', '.join(absent[:8])}"
                      f"{' ...' if len(absent) > 8 else ''}")

            for i, pick in enumerate(picks, 1):
                rule = pick["rule"]
                t0 = time.time()
                per_symbol, failed = {}, []
                for sym, d in frames.items():
                    a, n = agreement(rule, d, sym, window, n_eval)
                    if n == 0:
                        failed.append(sym)
                        continue
                    per_symbol[sym] = (a, n)
                if not per_symbol:
                    rows.append({"cls": cls, "tf": tf, "rank": i, "rule": rule,
                                 "family": pick["family"], "source": pick["source"],
                                 "n_symbols": 0, "note": "rule failed to build anywhere"})
                    print(f"    {i}. {rule:<28} FAILED TO BUILD")
                    continue
                agree = sum(a for a, _ in per_symbol.values())
                total = sum(n for _, n in per_symbol.values())
                fracs = {s: a / n for s, (a, n) in per_symbol.items()}
                worst = min(fracs, key=fracs.get)
                below = sum(1 for f in fracs.values() if f < 1.0)
                verdict, evidence = ("reproduces", "")
                if agree != total:
                    verdict = ("not_diagnosed" if not do_diagnose else verdict)
                    if do_diagnose:
                        verdict, evidence = diagnose(rule, frames, window)
                rows.append({
                    "cls": cls, "tf": tf, "rank": i, "rule": rule,
                    "family": pick["family"], "source": pick["source"],
                    "window": window, "n_eval_per_symbol": n_eval,
                    "n_symbols": len(per_symbol), "n_symbols_failed": len(failed),
                    "n_bars_compared": total, "n_bars_agree": agree,
                    "agree_frac": round(agree / total, 6),
                    "n_symbols_below_100pct": below,
                    "worst_symbol": worst, "worst_symbol_frac": round(fracs[worst], 6),
                    "verdict": verdict, "diagnosis": evidence,
                    "note": note,
                })
                flag = "" if agree == total else f"   <-- DOES NOT REPRODUCE ({verdict})"
                print(f"    {i}. {rule:<28} {agree/total:7.2%} over {total:6d} bars "
                      f"/ {len(per_symbol)} names   worst {worst} "
                      f"{fracs[worst]:.2%}  [{time.time() - t0:.0f}s]{flag}"
                      f"{('  ' + evidence) if evidence else ''}", flush=True)
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tf", nargs="+", default=["1h", "15m"])
    ap.add_argument("--cls", nargs="+", default=list(paper_config.UNIVERSE))
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--window", type=int, default=paper_config.DEFAULT_WINDOW_BARS)
    ap.add_argument("--n-eval", type=int, default=N_EVAL_BARS)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-diagnose", action="store_true",
                    help="skip the larger-window follow-up on every rule that missed")
    args = ap.parse_args()

    out = run(args.cls, args.tf, args.top, args.window, args.n_eval,
              do_diagnose=not args.no_diagnose)
    dest = (paper_config.RESULTS_DIR / "book_timeframe_check.csv"
            if args.out is None else args.out)
    out.to_csv(dest, index=False)
    print(f"\nwrote {dest}")

    if "agree_frac" in out:
        bad = out[out["agree_frac"].notna() & (out["agree_frac"] < 1.0)]
        if bad.empty:
            print("  every checked rule reproduced the full series on every sampled bar")
        else:
            print("  DID NOT reproduce at this window - do not promote as it stands:")
            for r in bad.itertuples():
                print(f"    {r.cls} {r.tf} {r.rule:<30} {r.agree_frac:7.2%}  "
                      f"{getattr(r, 'verdict', '')}  {getattr(r, 'diagnosis', '')}")


if __name__ == "__main__":
    main()
