"""Stage 1i: the 101 published formulaic alphas, as a fixed pre-registered set.

Why these and not a mining run
------------------------------
The obvious next move after reading the alpha-mining literature is to build a generator
and search. This repo cannot afford that, and the reason is arithmetic rather than taste.
`metrics.apply_edge_standard` charges every result a **noise ceiling** --
`se(per-fold delta-Sharpe) * Phi^-1(1 - 1/(N+1))` for N candidates tried -- so the bar
rises with the size of the search:

        31 candidates -> +0.111        4,800 -> +0.210
       101 candidates -> +0.139       10,000 -> +0.222
                                     100,000 -> +0.254

The best result this project has ever produced is `ibs` at delta-Sharpe **+0.134**, and it
still failed on t. A mining run that evaluates candidates by the thousand raises its own
hurdle faster than it raises its best draw. So the honest version of the experiment is the
**fixed, published, pre-registered** set: N is 101 by construction, nothing is selected on
this data, and the answer is cheap.

It is also the more informative experiment. These are the field's own reference artifacts,
from a real production book. If they do not survive here, no generator that emits things
*like* them will either.

What actually runs
------------------
Only 52 of the 101 are expressible on this repo's data, and that is a data limit, not a
choice:

    52   OHLCV alone                       -> RUNNABLE, the headline
    30   need `vwap`                       -> runnable only under `--vwap-proxy`, flagged
    13   need `vwap` AND an industry map   -> excluded
     5   need an industry map              -> excluded
     1   needs market cap (Alpha#56)       -> excluded

`data/` holds Open/High/Low/Close/Volume and nothing else -- no intraday vwap, no
shares-outstanding series (the same absence that forced dollar-volume ranking on
`top100_membership`), and no GICS/BICS classification. `indneutralize` is therefore not
implemented rather than approximated: cross-sectionally demeaning within a sector you had
to guess is a different operator, and a wrong one silently.

`--vwap-proxy` substitutes typical price `(high + low + close) / 3`, which is the standard
daily stand-in and is **not** the paper's quantity. Rows produced that way carry
`vwap_proxy=True` and are reported separately. They are a second experiment, not 30 extra
data points for the first one.

The formulas are not retyped
----------------------------
`alpha101_formulas.py` is generated from the paper's PDF by `tools/extract_alpha101.py`.
This module parses that grammar directly -- the paper's own operators, precedence and
ternary -- so the expression string travels with the result and a number on a sheet can be
read back to the line that produced it. The alternative, 101 hand-written pandas
functions, has 101 chances to differ from the published formula by one window length in a
way no test would catch.

Two ambiguities in the paper, resolved here and worth knowing before quoting anything:

* **`ts_rank` is normalised to (0, 1]**, not left as a raw 1..d integer. Alpha#35 computes
  `(1 - Ts_Rank(...))`, which is nonsense on raw ranks (always <= 0) and sensible on
  normalised ones. Popular implementations differ on this and therefore disagree on the
  handful of alphas that use `ts_rank` outside a `rank()`.
* **`min(x, d)` / `max(x, d)` are ts_min/ts_max when `d` is a literal** -- the paper says
  so -- **and elementwise when the second argument is an expression**, which is the only
  reading under which Alpha#71, #73, #77, #87, #88, #92 and #96 mean anything.

How a cross-sectional score becomes something this repo can score
-----------------------------------------------------------------
A formulaic alpha is a score per (day, symbol); the repo's gauntlet scores a long/flat
exposure per symbol. The bridge is the standard quantile portfolio: each day, rank the
scores cross-sectionally and go long the top `--quantile` fraction, flat otherwise. That
is a real, tradable expression of the alpha and it is what `riskmatch_wf.py` then puts
through the six criteria, against each name's own buy-and-hold.

It is deliberately **long/flat, not the dollar-neutral book the paper runs.** Shorting was
measured on 2026-08-08 and made every one of 8 strategies worse; the skipped bars on this
universe carry +3.4 bps of drift, so selling them is a paid mistake. `--side short` still
exists upstream for anyone who wants the number.

The direct measure is IC, and it is the one that matters
--------------------------------------------------------
The quantile book answers "is it tradable". `--ic` answers "is there signal at all":
the cross-sectional rank correlation of today's score against tomorrow's return, which is
how the mining literature evaluates these and the only frame in which this data has real
statistical power (~1e6 observations instead of one time series -- see the project's own
detectability work). Both are reported, gross and net, because the last time this repo
measured cross-sectional TA signal it found t = -9.09 gross and nothing at all after
5 bps of turnover.

Run::

    python alpha101.py --audit                   # what parses, what runs, why not
    python alpha101.py --ic                      # the IC study -> results/alpha101_ic.csv
    python alpha101.py --positions               # build + cache the quantile books
    python alpha101.py --ic --vwap-proxy         # the flagged 30, separately

Then the gauntlet itself, which reads the cached books::

    python riskmatch_wf.py --class us_stocks --tf 1d --rules $(python alpha101.py --names)
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from wfo_paths import RESULTS_DIR                     # noqa: F401  (wires sys.path)
from config import DATA_DIR, MIN_BARS, scenario       # noqa: F401
import td_loader

from alpha101_formulas import FORMULAS

PREFIX = "alpha"                     # label grammar: `alpha001` ... `alpha101`
CACHE = Path(__file__).resolve().parents[1] / ".cache" / "alpha101"

# The inputs this repo can actually serve. `vwap` is added only under --vwap-proxy.
BASE_FIELDS = {"open", "high", "low", "close", "volume", "returns"}


def label(n: int) -> str:
    return f"{PREFIX}{n:03d}"


def unlabel(name: str) -> int | None:
    m = re.fullmatch(rf"{PREFIX}(\d{{3}})", name or "")
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------- parser
#
# The paper's grammar, in precedence order (loosest first). It is small enough to parse
# by recursive descent in under a hundred lines, and doing so means the formula string in
# `alpha101_formulas.py` IS the implementation -- there is no second copy to drift.

@dataclass(frozen=True)
class Num:
    v: float


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Call:
    fn: str
    args: tuple


@dataclass(frozen=True)
class Bin:
    op: str
    a: object
    b: object


@dataclass(frozen=True)
class Una:
    op: str
    a: object


@dataclass(frozen=True)
class Tern:
    c: object
    a: object
    b: object


_TOKEN = re.compile(r"""
    \s*(?:
        (?P<num>\d+\.\d*|\.\d+|\d+)
      | (?P<name>[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_0-9]+)*)
      | (?P<op>\|\||==|[-+*/^()<>,?:])
    )
""", re.VERBOSE)


def tokenize(src: str) -> list[tuple[str, str]]:
    out, i = [], 0
    while i < len(src):
        m = _TOKEN.match(src, i)
        if not m or m.end() == i:
            if src[i:].strip() == "":
                break
            raise SyntaxError(f"cannot tokenize at {src[i:i + 30]!r}")
        i = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


class Parser:
    def __init__(self, src: str):
        self.toks = tokenize(src)
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, val=None):
        k, v = self.peek()
        if val is not None and v != val:
            raise SyntaxError(f"expected {val!r}, got {v!r}")
        self.i += 1
        return v

    def parse(self):
        node = self.ternary()
        if self.i != len(self.toks):
            raise SyntaxError(f"trailing tokens from {self.peek()}")
        return node

    def ternary(self):
        c = self.logic()
        if self.peek()[1] == "?":
            self.take("?")
            a = self.ternary()
            self.take(":")
            b = self.ternary()
            return Tern(c, a, b)
        return c

    def logic(self):
        n = self.compare()
        while self.peek()[1] == "||":
            self.take()
            n = Bin("||", n, self.compare())
        return n

    def compare(self):
        n = self.addsub()
        while self.peek()[1] in ("<", ">", "=="):
            op = self.take()
            n = Bin(op, n, self.addsub())
        return n

    def addsub(self):
        n = self.muldiv()
        while self.peek()[1] in ("+", "-"):
            op = self.take()
            n = Bin(op, n, self.muldiv())
        return n

    def muldiv(self):
        n = self.power()
        while self.peek()[1] in ("*", "/"):
            op = self.take()
            n = Bin(op, n, self.power())
        return n

    def power(self):
        n = self.unary()
        if self.peek()[1] == "^":                     # right-associative
            self.take()
            return Bin("^", n, self.power())
        return n

    def unary(self):
        if self.peek()[1] == "-":
            self.take()
            return Una("-", self.unary())
        return self.primary()

    def primary(self):
        k, v = self.peek()
        if v == "(":
            self.take("(")
            n = self.ternary()
            self.take(")")
            return n
        if k == "num":
            self.take()
            return Num(float(v))
        if k == "name":
            self.take()
            if self.peek()[1] == "(":
                self.take("(")
                args = []
                if self.peek()[1] != ")":
                    args.append(self.ternary())
                    while self.peek()[1] == ",":
                        self.take(",")
                        args.append(self.ternary())
                self.take(")")
                return Call(v.lower(), tuple(args))
            return Var(v.lower())
        raise SyntaxError(f"unexpected token {v!r}")


def parse(src: str):
    return Parser(src).parse()


def free_vars(node) -> set[str]:
    if isinstance(node, Var):
        return {node.name}
    if isinstance(node, Num):
        return set()
    if isinstance(node, Call):
        # `IndClass.sector` tokenizes as Var('indclass'); the call name matters more.
        return {f"fn:{node.fn}"} | set().union(*(free_vars(a) for a in node.args)) \
            if node.args else {f"fn:{node.fn}"}
    if isinstance(node, Bin):
        return free_vars(node.a) | free_vars(node.b)
    if isinstance(node, Una):
        return free_vars(node.a)
    if isinstance(node, Tern):
        return free_vars(node.c) | free_vars(node.a) | free_vars(node.b)
    raise TypeError(node)


# ------------------------------------------------------------------ operators
#
# Every value is either a scalar or a panel: a DataFrame indexed by date with one column
# per symbol. `rank` and `scale` run ACROSS columns (the cross-section, one day at a
# time); every `ts_*` runs DOWN the index (the time series, one symbol at a time). Getting
# those two axes the wrong way round is the single easiest way to produce a plausible and
# entirely wrong number here, so they are never spelled the same way below.

def _win(d) -> int:
    """`ts_{O}(x, d)`: 'non-integer number of days d is converted to floor(d)'."""
    return max(1, int(np.floor(float(d))))


def _frame(x, like: pd.DataFrame) -> pd.DataFrame:
    return x if isinstance(x, pd.DataFrame) else pd.DataFrame(
        np.full(like.shape, float(x)), index=like.index, columns=like.columns)


def _any_frame(*vals):
    for v in vals:
        if isinstance(v, pd.DataFrame):
            return v
    return None


def _sliding(x: pd.DataFrame, d: int, fn) -> pd.DataFrame:
    """Rolling reduction along the index, vectorised over symbols.

    `DataFrame.rolling(d).apply(...)` calls back into Python once per (row, column) and is
    minutes rather than seconds on a 216 x 6,600 panel. A sliding-window view is a view,
    so this costs one output allocation and no copy of the input.
    """
    a = x.to_numpy("float64")
    if len(a) < d:
        return pd.DataFrame(np.nan, index=x.index, columns=x.columns)
    from numpy.lib.stride_tricks import sliding_window_view
    w = sliding_window_view(a, d, axis=0)            # (rows-d+1, cols, d)
    out = np.full(a.shape, np.nan)
    out[d - 1:] = fn(w)
    # A window that is not fully populated is undefined, exactly as `min_periods=d` makes
    # it for every pandas rolling op used here. Without this `np.argmax` over an all-NaN
    # window returns 0 rather than NaN, which hands a real score to a symbol that was not
    # in the universe that day -- and since `rank()` is CROSS-SECTIONAL, one such symbol
    # shifts the percentile of every genuine name beside it.
    gap = np.full(a.shape, True)
    gap[d - 1:] = np.isnan(w).any(axis=-1)
    out[gap] = np.nan
    return pd.DataFrame(out, index=x.index, columns=x.columns)


def _decay_linear(x: pd.DataFrame, d: int) -> pd.DataFrame:
    """Weighted MA with linearly decaying weights d, d-1, ..., 1, rescaled to sum to 1."""
    w = np.arange(d, 0, -1, dtype="float64")[::-1]   # oldest .. newest == 1 .. d
    w /= w.sum()
    return _sliding(x, d, lambda a: np.einsum("ijk,k->ij", a, w))


def _corr(x: pd.DataFrame, y: pd.DataFrame, d: int) -> pd.DataFrame:
    out = x.rolling(d, min_periods=d).corr(y)
    # A window in which either leg is constant divides by zero and yields +-inf, not NaN.
    return out.replace([np.inf, -np.inf], np.nan)


def _pow(a, b):
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.power(a, b)
    return out


UNARY = {
    "abs": lambda x: x.abs() if isinstance(x, pd.DataFrame) else abs(x),
    "sign": lambda x: np.sign(x),
    # log(volume) on a zero-volume bar is -inf, which then poisons every downstream
    # rolling window it touches. Non-positive input is undefined, so it is NaN.
    "log": lambda x: np.log(x.where(x > 0)) if isinstance(x, pd.DataFrame) else np.log(x),
}

TS = {
    "sum": lambda x, d: x.rolling(d, min_periods=d).sum(),
    "stddev": lambda x, d: x.rolling(d, min_periods=d).std(),
    "ts_min": lambda x, d: x.rolling(d, min_periods=d).min(),
    "ts_max": lambda x, d: x.rolling(d, min_periods=d).max(),
    "ts_rank": lambda x, d: x.rolling(d, min_periods=d).rank(pct=True),
    "ts_argmax": lambda x, d: _sliding(x, d, lambda a: np.argmax(a, axis=-1) + 1.0),
    "ts_argmin": lambda x, d: _sliding(x, d, lambda a: np.argmin(a, axis=-1) + 1.0),
    "product": lambda x, d: _sliding(x, d, lambda a: np.prod(a, axis=-1)),
    "decay_linear": _decay_linear,
}


def evaluate(node, env: dict) -> object:
    """AST -> panel (or scalar). `env` maps a bare name to its panel."""
    if isinstance(node, Num):
        return node.v

    if isinstance(node, Var):
        if node.name not in env:
            raise KeyError(node.name)
        return env[node.name]

    if isinstance(node, Una):
        return -evaluate(node.a, env)

    if isinstance(node, Tern):
        c = evaluate(node.c, env)
        a, b = evaluate(node.a, env), evaluate(node.b, env)
        like = _any_frame(c, a, b)
        cond = _frame(c, like)
        out = _frame(a, like).where(cond > 0, _frame(b, like))
        # An unknown condition gives an unknown value. Several alphas take a bare scalar
        # on one arm -- Alpha#7 and #21 fall through to `-1`, Alpha#23 to `0` -- so
        # without this a symbol with no bars at all takes the else branch and lands on
        # the sheet with a hard-coded score, ranked against names that really traded.
        return out.where(cond.notna())

    if isinstance(node, Bin):
        a, b = evaluate(node.a, env), evaluate(node.b, env)
        if node.op in ("+", "-", "*", "/"):
            return {"+": lambda: a + b, "-": lambda: a - b,
                    "*": lambda: a * b, "/": lambda: a / b}[node.op]()
        if node.op == "^":
            return _pow(a, b)
        like = _any_frame(a, b)
        # Comparisons become 0.0/1.0 rather than booleans: the paper uses them both as
        # conditions AND as arithmetic operands (Alpha#61, #75, #92, #95, #99 return one
        # directly), so a float is the only representation that works in both places.
        #
        # They also PRESERVE NaN. `NaN < NaN` is False in both numpy and pandas, so the
        # naive version scores an unlisted symbol 0.0 -- a real number, ranked against
        # real ones. Undefined input has to stay undefined, or the cross-section silently
        # fills up with names that were not in the universe.
        fa, fb = _frame(a, like), _frame(b, like)
        known = fa.notna() & fb.notna()
        if node.op in ("<", ">", "=="):
            cmp = {"<": fa < fb, ">": fa > fb, "==": fa == fb}[node.op]
            return cmp.astype("float64").where(known)
        if node.op == "||":
            return ((fa > 0) | (fb > 0)).astype("float64").where(known)
        raise SyntaxError(node.op)

    if isinstance(node, Call):
        fn = node.fn
        args = [evaluate(a, env) for a in node.args]

        if fn in UNARY:
            return UNARY[fn](args[0])
        if fn == "rank":                              # ACROSS symbols, one day at a time
            return args[0].rank(axis=1, pct=True)
        if fn == "scale":
            a = args[0]
            target = args[1] if len(args) > 1 else 1.0
            return a.div(a.abs().sum(axis=1), axis=0) * target
        if fn == "delay":
            return args[0].shift(_win(args[1]))
        if fn == "delta":
            return args[0] - args[0].shift(_win(args[1]))
        if fn == "correlation":
            return _corr(args[0], args[1], _win(args[2]))
        if fn == "covariance":
            return args[0].rolling(_win(args[2]), min_periods=_win(args[2])).cov(args[1])
        if fn == "signedpower":
            return np.sign(args[0]) * _pow(args[0].abs(), args[1])
        if fn in TS:
            return TS[fn](args[0], _win(args[1]))
        if fn in ("min", "max"):
            # "min(x, d) = ts_min(x, d)" per the paper -- but only when d is a literal
            # window. Alpha#71/#73/#77/#87/#88/#92/#96 pass two expressions and mean the
            # elementwise one; read as ts_* they would be reducing over a number of days
            # that is itself a panel, which is not a thing.
            if isinstance(node.args[1], Num):
                return TS[f"ts_{fn}"](args[0], _win(args[1]))
            like = _any_frame(*args)
            a, b = _frame(args[0], like), _frame(args[1], like)
            return a.where(a > b, b) if fn == "max" else a.where(a < b, b)
        raise KeyError(f"operator {fn!r} is not implemented")

    raise TypeError(node)


# ---------------------------------------------------------------- the panels

def _needs(n: int) -> set[str]:
    """Inputs and unimplemented operators Alpha#n depends on."""
    v = free_vars(parse(FORMULAS[n]))
    out = {x for x in v if not x.startswith("fn:")}
    # `IndClass.sector` and friends are one dependency, not one per level: what is
    # missing is a classification, and this repo has none at any granularity.
    ind = {x for x in out if x.startswith("indclass")}
    if ind or "fn:indneutralize" in v:
        out = (out - ind) | {"industry"}
    return out


def classify(vwap_proxy: bool = False) -> dict[int, str]:
    """Every alpha -> "" if runnable here, else the reason it is not."""
    have = set(BASE_FIELDS) | {f"adv{d}" for d in range(1, 400)}
    if vwap_proxy:
        have.add("vwap")
    out = {}
    for n in sorted(FORMULAS):
        missing = _needs(n) - have
        out[n] = "" if not missing else "+".join(sorted(missing))
    return out


def runnable(vwap_proxy: bool = False) -> list[int]:
    return [n for n, why in classify(vwap_proxy).items() if not why]


def _adv_windows() -> set[int]:
    w = set()
    for src in FORMULAS.values():
        w |= {int(d) for d in re.findall(r"adv(\d+)", src)}
    return w


def build_env(asset_class: str, timeframe: str,
              vwap_proxy: bool = False) -> tuple[dict, pd.DataFrame]:
    """Load the class and lay it out as aligned panels. Returns (env, close panel).

    Every frame shares one index (the union of trading days) and one column order. A
    symbol is NaN outside its own membership span, which is what keeps it out of that
    day's cross-section -- `td_loader.load` has already applied `span_for`, so the
    point-in-time universe arrives correct rather than being reconstructed here.
    """
    data = td_loader.load(asset_class, timeframe)
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS}
    if not data:
        raise SystemExit(f"no bars for {asset_class} {timeframe}")

    def panel(col: str) -> pd.DataFrame:
        return pd.DataFrame({s: df[col] for s, df in data.items()}).sort_index()

    close = panel("Close")
    env = {
        "open": panel("Open"), "high": panel("High"), "low": panel("Low"),
        "close": close, "volume": panel("Volume"),
        "returns": close.pct_change(),
    }
    dollar = env["close"] * env["volume"]
    for d in _adv_windows():
        env[f"adv{d}"] = dollar.rolling(d, min_periods=d).mean()
    if vwap_proxy:
        # NOT the paper's vwap. Typical price is the standard daily stand-in for it and
        # is the best this repo's OHLCV cache can do; anything computed from it is
        # reported separately and flagged.
        env["vwap"] = (env["high"] + env["low"] + env["close"]) / 3.0
    return env, close


def score_panel(n: int, env: dict) -> pd.DataFrame:
    """Alpha#n evaluated over the whole panel: one score per (day, symbol)."""
    out = evaluate(parse(FORMULAS[n]), env)
    if not isinstance(out, pd.DataFrame):
        raise TypeError(f"Alpha#{n} evaluated to a scalar")
    return out.replace([np.inf, -np.inf], np.nan)


def to_positions(score: pd.DataFrame, close: pd.DataFrame,
                 quantile: float = 0.2) -> pd.DataFrame:
    """Score panel -> long/flat book: long the top `quantile` of each day's cross-section.

    Ranked only among names that have BOTH a score and a price that day, so a symbol
    outside its membership span cannot be held and cannot dilute the ranking either.
    """
    valid = score.notna() & close.notna()
    masked = score.where(valid)
    # Hold every name at or above the day's (1 - quantile) percentile VALUE, rather than
    # everything whose percentile RANK clears it.
    #
    # The difference is only visible on ties, and roughly a fifth of the 101 are binary by
    # construction -- Alpha#61, #68, #75, #92, #95, #99 and friends evaluate to literal
    # 0/1. Ranking those with `pct=True` gives every 1 the same mid-rank, so if more than
    # `quantile` of the cross-section scores 1 that mid-rank lands BELOW the cutoff and a
    # strict `>` selects nothing at all. Alpha#95 and Alpha#99 built empty books that way,
    # and an empty book is indistinguishable on a leaderboard from a rule that simply
    # never fires -- the exact confusion `EDGE_MIN_EXPOSURE` exists to prevent.
    #
    # Holding the whole tied bucket is the honest reading of a binary signal: it says
    # "these names, no ordering among them". It makes the book wider than `quantile` on
    # those alphas, which is reported as exposure rather than hidden.
    thresh = masked.quantile(1.0 - quantile, axis=1)
    enough = valid.sum(axis=1) >= max(5, int(np.ceil(1.0 / quantile)))
    pos = masked.ge(thresh, axis=0).astype("float64")
    pos = pos.where(valid, 0.0)
    return pos.where(enough, 0.0)


# ------------------------------------------------------------------- the cache
#
# `riskmatch_wf.py` fans rules out across processes and hands each worker only a rule
# NAME -- the panel cannot be passed through that boundary, and recomputing 82 alphas per
# worker would dominate the run. So the books are built once, here, and the workers read
# a symbol's column back off disk.
#
# The key is a fingerprint over the bars and over this file, in the same spirit as
# `stockhunt.poscache`: refetch a ticker or change an operator and the affected books are
# unreachable rather than stale. There is no manual clear because a cache you have to
# remember to clear is one that will eventually be wrong.

def fingerprint(asset_class: str, timeframe: str, quantile: float,
                vwap_proxy: bool) -> str:
    h = hashlib.sha256()
    h.update(f"{asset_class}|{timeframe}|{quantile}|{int(vwap_proxy)}|v1".encode())
    h.update(Path(__file__).read_bytes())
    h.update((Path(__file__).parent / "alpha101_formulas.py").read_bytes())
    d = td_loader.cache_dir(asset_class, timeframe)
    for p in sorted(d.glob("*.parquet")):
        h.update(p.name.encode())
        h.update(str(p.stat().st_size).encode())
        h.update(str(int(p.stat().st_mtime)).encode())
    return h.hexdigest()[:16]


def cache_dir(asset_class: str, timeframe: str, quantile: float,
              vwap_proxy: bool) -> Path:
    return CACHE / fingerprint(asset_class, timeframe, quantile, vwap_proxy)


def book_path(n: int, asset_class: str, timeframe: str, quantile: float,
              vwap_proxy: bool) -> Path:
    return cache_dir(asset_class, timeframe, quantile, vwap_proxy) / f"{label(n)}.parquet"


_LOADED: dict[str, pd.DataFrame] = {}

# Which of the two experiments `riskmatch_wf` is scoring. An environment switch, in the
# same spirit as `STOCKHUNT_WORKERS` and `STOCKHUNT_NO_POSCACHE`, because the alternative
# is threading a flag through `resolve_position` -> `_score_rule` -> the worker
# initialiser, and a spawned worker inherits the environment for free.
#
# It is a switch and not a fallback ON PURPOSE. If a proxy book could quietly stand in for
# a missing exact one, the two experiments would merge on the same sheet under the same
# label, and `vwap_proxy` -- the column that says a result is not the paper's quantity --
# would stop meaning anything.
VWAP_PROXY_ENV = "STOCKHUNT_ALPHA101_VWAP_PROXY"


def _proxy_mode() -> bool:
    import os
    return os.environ.get(VWAP_PROXY_ENV, "") not in ("", "0")


def position_for(name: str, index: pd.DatetimeIndex, symbol: str,
                 asset_class: str, timeframe: str, quantile: float = 0.2,
                 vwap_proxy: bool | None = None) -> np.ndarray | None:
    """One symbol's long/flat series for `alphaNNN`, or None if there is no book.

    Reindexed onto the caller's own bar index, because `riskmatch_wf` hands us that
    symbol's frame and every downstream metric is aligned to it. A day the book does not
    cover is flat, never forward-filled -- carrying a stale holding across a gap would
    invent a trade nobody made.
    """
    n = unlabel(name)
    if n is None:
        return None
    if vwap_proxy is None:
        vwap_proxy = _proxy_mode()
    p = book_path(n, asset_class, timeframe, quantile, vwap_proxy)
    if not p.exists():
        return None
    key = str(p)
    if key not in _LOADED:
        _LOADED[key] = pd.read_parquet(p)
    book = _LOADED[key]
    if symbol not in book.columns:
        return None
    return book[symbol].reindex(index).fillna(0.0).to_numpy("float64")


def build_books(asset_class: str, timeframe: str, quantile: float,
                vwap_proxy: bool, only: list[int] | None = None) -> Path:
    env, close = build_env(asset_class, timeframe, vwap_proxy)
    out = cache_dir(asset_class, timeframe, quantile, vwap_proxy)
    out.mkdir(parents=True, exist_ok=True)
    todo = only or runnable(vwap_proxy)
    for i, n in enumerate(todo, 1):
        p = book_path(n, asset_class, timeframe, quantile, vwap_proxy)
        if p.exists():
            continue
        t0 = time.time()
        pos = to_positions(score_panel(n, env), close, quantile)
        pos.astype("float32").to_parquet(p)
        print(f"  [{i:>2}/{len(todo)}] {label(n)}  "
              f"exposure {pos.to_numpy().mean():.3f}  {time.time() - t0:.1f}s",
              flush=True)
    return out


# ------------------------------------------------------------------ the IC study

def book_returns(pos: pd.DataFrame, env: dict, fill: str,
                 fee_bps: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Equal-weighted long/flat book -> (gross, net, turnover) per bar.

    `fill` is the same first-class control it is everywhere else in this repo, and it
    matters more here than usual. Four of the 101 -- Alpha#42, #48, #53 and #54 -- are
    what the paper calls **delay-0**: it says outright that they "are assumed to be traded
    at or as close as possible to the close of the trading day for which they are
    computed." That is the published convention and it is also, measured on this repo's
    bars, a look-ahead: the signal reads a close that has not printed when the order must
    already be resting.

        close   signal at close t, filled at close t, earns close t -> t+1.
                The paper's own convention and an OPTIMISTIC bound.
        open    signal at close t, filled at open t+1, earns open t+1 -> t+2.
                A PESSIMISTIC bound: it removes the look-ahead but also charges a full
                session of delay that a market-on-close order would not pay.

    The truth is between them and neither end may be quoted alone.
    """
    if fill == "close":
        px = env["close"]
        held = pos.shift(1)
    elif fill == "open":
        px = env["open"]
        held = pos.shift(2)
    else:
        raise ValueError(fill)
    held = held.fillna(0.0)
    w = held.div(held.sum(axis=1).replace(0, np.nan), axis=0)
    gross = (w * px.pct_change()).sum(axis=1)
    turn = w.diff().abs().sum(axis=1).fillna(0.0)
    return gross, gross - turn * fee_bps / 1e4, turn


def equal_weight_benchmark(env: dict, fill: str, fee_bps: float = 0.0) -> pd.Series:
    """Own every live name, equal weight, rebalanced every bar, NET of the same fees.

    The only valid baseline for a book that rebalances to equal weight every bar. A
    buy-once-and-hold baseline is a DIFFERENT PORTFOLIO and the gap between the two is not
    signal -- the same trap `--charge-bench` and `portfolio_wf` exist to close.

    It is charged on its own turnover for the same reason. Rebalancing ~100 names to equal
    weight daily is not free, and a comparison that charges only the strategy has already
    decided the answer in the flattering direction. The baseline's turnover is small next
    to a 350x/yr alpha book, so this moves little -- but "it would not have mattered" is a
    thing you can only say after doing it.
    """
    px = env["close"] if fill == "close" else env["open"]
    live = px.notna().astype("float64")
    w = live.div(live.sum(axis=1).replace(0, np.nan), axis=0)
    w = w.shift(1 if fill == "close" else 2)
    gross = (w * px.pct_change()).sum(axis=1)
    turn = w.diff().abs().sum(axis=1).fillna(0.0)
    return gross - turn * fee_bps / 1e4


def _sharpe(r: pd.Series, bpy: float = 252.0) -> float:
    r = r.dropna()
    sd = r.std(ddof=1)
    return float(r.mean() / sd * np.sqrt(bpy)) if sd else np.nan


def ic_table(asset_class: str, timeframe: str, vwap_proxy: bool,
             fee_bps: float, only: list[int] | None = None) -> pd.DataFrame:
    """Rank-IC per alpha, plus the quantile spread the IC would actually have earned.

    IC is the mining literature's own yardstick and the only one this data has real power
    for: a daily cross-section of ~100 names over ~26 years is on the order of a million
    observations, where the standard error on a mean IC collapses. A portfolio Sharpe
    computed from the same bars is ONE time series and its standard error is ~0.38
    whatever the breadth.

    Every column is computed on a one-bar-forward return, so nothing here can see the bar
    it is scored against.
    """
    env, close = build_env(asset_class, timeframe, vwap_proxy)
    fwd = close.pct_change().shift(-1)                # tomorrow's return, per symbol
    fwd_rank = fwd.rank(axis=1, pct=True)
    todo = only or runnable(vwap_proxy)
    cls = classify(vwap_proxy)
    bench_c = equal_weight_benchmark(env, "close", fee_bps)
    bench_o = equal_weight_benchmark(env, "open", fee_bps)
    print(f"  benchmark: equal-weight universe, Sharpe {_sharpe(bench_c):.3f} "
          f"(close fill) / {_sharpe(bench_o):.3f} (open fill)\n", flush=True)

    rows = []
    for i, n in enumerate(todo, 1):
        t0 = time.time()
        s = score_panel(n, env)
        both = s.notna() & fwd.notna()
        n_names = both.sum(axis=1)
        keep = n_names >= 20                         # a cross-section worth ranking

        sr = s.where(both).rank(axis=1, pct=True)
        fr = fwd_rank.where(both)
        # Pearson on the two RANK panels, row by row = daily Spearman.
        ic = sr.sub(sr.mean(axis=1), axis=0).mul(fr.sub(fr.mean(axis=1), axis=0)).sum(axis=1)
        ic = ic / np.sqrt(sr.sub(sr.mean(axis=1), axis=0).pow(2).sum(axis=1)
                          * fr.sub(fr.mean(axis=1), axis=0).pow(2).sum(axis=1))
        ic = ic.where(keep).replace([np.inf, -np.inf], np.nan).dropna()

        # The tradable side of the same signal: long the top fifth, flat, equal weight.
        # Priced at BOTH fill conventions, because the two bound the answer and this repo
        # does not quote the flattering end of that range on its own.
        pos = to_positions(s, close, 0.2)
        g_c, n_c, t_c = book_returns(pos, env, "close", fee_bps)
        g_o, n_o, t_o = book_returns(pos, env, "open", fee_bps)

        bars = float(len(ic))
        m, sd = float(ic.mean()), float(ic.std(ddof=1))
        yrs = max(len(g_c), 1) / 252.0
        rows.append({
            "class": asset_class, "tf": timeframe, "alpha": label(n),
            "vwap_proxy": bool(vwap_proxy and "vwap" in _needs(n)),
            "ic_mean": m, "ic_std": sd,
            "icir": m / sd if sd else np.nan,
            "ic_t": m / sd * np.sqrt(bars) if sd else np.nan,
            "ic_days": int(bars),
            "ic_hit": float((ic > 0).mean()),
            "exposure": float(pos.to_numpy().mean()),
            "turnover_yr": float(t_c.mean() * 252),
            "gross_cagr": float((1 + g_c.fillna(0)).prod() ** (1 / yrs) - 1),
            "net_cagr": float((1 + n_c.fillna(0)).prod() ** (1 / yrs) - 1),
            "gross_sharpe": _sharpe(g_c),
            "net_sharpe": _sharpe(n_c),
            "net_sharpe_openfill": _sharpe(n_o),
            "net_cagr_openfill": float((1 + n_o.fillna(0)).prod() ** (1 / yrs) - 1),
            "bench_sharpe": _sharpe(bench_c),
            "bench_sharpe_openfill": _sharpe(bench_o),
            "bench_cagr": float((1 + bench_c.fillna(0)).prod() ** (1 / yrs) - 1),
            "sharpe_edge": _sharpe(n_c) - _sharpe(bench_c),
            "sharpe_edge_openfill": _sharpe(n_o) - _sharpe(bench_o),
            "formula": FORMULAS[n],
            "excluded_because": cls[n],
        })
        print(f"  [{i:>2}/{len(todo)}] {label(n)}  IC {m:+.4f}  t {rows[-1]['ic_t']:+.1f}  "
              f"edge close {rows[-1]['sharpe_edge']:+.2f}  "
              f"open {rows[-1]['sharpe_edge_openfill']:+.2f}  {time.time() - t0:.1f}s",
              flush=True)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------- the CLI

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--class", dest="asset_class", default="us_stocks")
    ap.add_argument("--tf", dest="timeframe", default="1d")
    ap.add_argument("--quantile", type=float, default=0.2,
                    help="fraction of each day's cross-section held long")
    ap.add_argument("--vwap-proxy", action="store_true",
                    help="substitute (high+low+close)/3 for vwap; adds 30 alphas, "
                         "flagged, and NOT the paper's quantity")
    ap.add_argument("--fee-bps", type=float, default=None,
                    help="per unit of turnover; defaults to the class's real schedule")
    ap.add_argument("--only", type=int, nargs="+", default=None, metavar="N")
    ap.add_argument("--audit", action="store_true", help="what parses and what runs")
    ap.add_argument("--names", action="store_true",
                    help="print runnable labels, space separated, for --rules")
    ap.add_argument("--ic", action="store_true", help="the IC study")
    ap.add_argument("--positions", action="store_true", help="build and cache the books")
    args = ap.parse_args()

    if args.fee_bps is None:
        fee = scenario(args.asset_class, "retail" if args.asset_class != "crypto"
                       else "binance")
        args.fee_bps = fee["commission_bps"] + fee["half_spread_bps"]

    if args.names:
        print(" ".join(label(n) for n in runnable(args.vwap_proxy)))
        return

    if args.audit:
        cls = classify(args.vwap_proxy)
        ok = [n for n, w in cls.items() if not w]
        print(f"{len(FORMULAS)} formulas; {len(ok)} runnable "
              f"(vwap_proxy={args.vwap_proxy})\n")
        by_reason: dict[str, list[int]] = {}
        for n, why in cls.items():
            by_reason.setdefault(why or "RUNNABLE", []).append(n)
        for why, ns in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            print(f"  {why:<24} {len(ns):>3}  {ns}")
        bad = []
        for n in FORMULAS:
            try:
                parse(FORMULAS[n])
            except Exception as exc:                  # noqa: BLE001
                bad.append((n, exc))
        print(f"\nparse failures: {bad if bad else 'none'}")
        return

    if args.positions:
        out = build_books(args.asset_class, args.timeframe, args.quantile,
                          args.vwap_proxy, args.only)
        print(f"books in {out}")
        return

    if args.ic:
        t = ic_table(args.asset_class, args.timeframe, args.vwap_proxy,
                     args.fee_bps, args.only)
        suffix = "_vwapproxy" if args.vwap_proxy else ""
        p = RESULTS_DIR / f"alpha101_ic_{args.asset_class}_{args.timeframe}{suffix}.csv"
        t.to_csv(p, index=False)
        print(f"\nwrote {p}  ({len(t)} rows)")
        show = t.sort_values("ic_t", key=abs, ascending=False).head(15)
        cols = ["alpha", "ic_mean", "ic_t", "icir", "turnover_yr",
                "gross_sharpe", "net_sharpe"]
        print(show[cols].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
        return

    ap.print_help()


if __name__ == "__main__":
    main()
