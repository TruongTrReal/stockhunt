"""On-disk cache of generated position series.

**Why this exists.** Turning a rule name into a position series is 68% of the cost of
every stage in this pipeline — measured, not assumed: 0.64 ms/cell to generate against
0.30 ms/cell to score, on the real us_stocks 1d series. And seven stages generate the
*same* series from the same bars: `sweep`, `walkforward`, `variants`, `combo_wf`,
`strat_wf`, `curves` and `riskmatch_wf` each rebuild the identical tensor and throw it
away. Six of those seven regenerations are pure waste.

Positions are -1/0/1, so they compress extraordinarily well. Measured on us_stocks 1d:
**1.09 GB raw -> 0.12 GB on disk, 9x**, for the whole 704-asset x 232-rule sheet.

**Layout: one file per (class, timeframe, rule).** Not per symbol, and that is the whole
design. Every consumer in this repo loops rule-outer / symbol-inner, so a per-rule file
is opened once per rule and holds one rule across all symbols — about 3.5 MB for
us_stocks 1d. A per-symbol layout would either reopen 232 files per symbol or hold the
full 2.3 GB tensor in memory, and not materialising that tensor is the documented reason
the fine timeframes are runnable at all. The memory contract in `sweep.py` and
`walkforward.score_folds` is preserved exactly.

**Correctness is fingerprinted, never assumed.** A stale position cache would be the
worst bug this repo could have — it would silently serve last week's signal definitions
under this week's rule name, and every gate, ceiling and verdict downstream would be
computed on it without complaint. Two fingerprints guard each entry:

* **data** — SHA-256 over the actual OHLCV bytes of that symbol, per symbol, so
  refetching one ticker invalidates that ticker and not the sheet;
* **code** — SHA-256 over the source of every module that can change what a rule means
  (`strategies/**.py`, `signals.py`, `engines/vector.py`). Edit a rule and every entry
  it could have touched is invalidated automatically.

Anything that does not match is a miss and is regenerated. There is no "trust the
mtime" path and no way to disable the check, because a cache you have to remember to
clear is a cache that will eventually be wrong.

Set `STOCKHUNT_NO_POSCACHE=1` to bypass it entirely — useful when bisecting a suspected
cache bug, and the honest first move if a result ever looks surprising.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np
import pandas as pd

# Bumped to 2 when the float32 fallback in `RuleCache.put` became lossless. Entries
# written before that could be a rounded copy of a continuous position, and there is no
# way to tell one apart from an exact one after the fact — so they are all discarded and
# the next run of each stage regenerates cold, once.
CACHE_VERSION = "2"

_OHLCV = ("Open", "High", "Low", "Close", "Volume")


def disabled() -> bool:
    return os.environ.get("STOCKHUNT_NO_POSCACHE", "").strip() not in ("", "0")


def _safe(symbol: str) -> str:
    """`BTC/USD` -> `BTC_USD`. Mirrors `config.safe_symbol` without importing it —
    this package must not depend on a pipeline folder."""
    return symbol.replace("/", "_").replace("\\", "_")


def code_fingerprint(paths) -> str:
    """SHA-256 over the source of everything that can change a position series.

    Directories are walked for `*.py` in sorted order so the digest is stable across
    filesystems. A missing path contributes its name only — deleting a module is itself
    a change worth invalidating on.
    """
    h = hashlib.sha256()
    h.update(CACHE_VERSION.encode())
    for p in sorted(Path(x) for x in paths):
        if p.is_dir():
            files = sorted(p.rglob("*.py"))
        elif p.exists():
            files = [p]
        else:
            h.update(f"<missing:{p.name}>".encode())
            continue
        for f in files:
            # Name relative to the anchor, so moving the checkout does not bust the cache.
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:24]


def data_fingerprint(df: pd.DataFrame) -> str:
    """SHA-256 over one symbol's bars.

    Hashes the raw float64 bytes of all five OHLCV columns plus the index, not a
    summary. A length-and-endpoints check would miss exactly the case `check_data.py`
    exists to create: a repaired bar in the middle of an otherwise unchanged series.
    """
    h = hashlib.sha256()
    h.update(np.asarray(df.index.view("int64")).tobytes())
    for col in _OHLCV:
        if col in df.columns:
            h.update(np.ascontiguousarray(
                df[col].to_numpy(dtype="float64")).tobytes())
        else:
            h.update(b"<absent>")
    return h.hexdigest()[:24]


class RuleCache:
    """Read/write handle for one (class, timeframe, rule). Use via `Sheet.rule`."""

    def __init__(self, path: Path, fingerprints: dict[str, str], enabled: bool):
        self._path = path
        self._fp = fingerprints
        self._enabled = enabled
        self._store: dict[str, np.ndarray] = {}
        self._dirty = False
        if enabled and path.exists():
            try:
                with np.load(path, allow_pickle=False) as z:
                    self._store = {k: z[k] for k in z.files}
            except Exception:
                # A truncated or half-written npz is a miss, never a crash. The cost of
                # being wrong here is one regeneration; the cost of raising is a dead run.
                self._store = {}

    def get(self, symbol: str) -> np.ndarray | None:
        if not self._enabled:
            return None
        key = _safe(symbol)
        want = self._fp.get(symbol)
        if want is None or self._store.get(f"f:{key}") is None:
            return None
        if str(self._store[f"f:{key}"].item()) != want:
            return None
        pos = self._store.get(f"p:{key}")
        return None if pos is None else np.asarray(pos, dtype="float64")

    def put(self, symbol: str, pos: np.ndarray | None) -> None:
        if not self._enabled or pos is None:
            return
        want = self._fp.get(symbol)
        if want is None:
            return
        arr = np.asarray(pos, dtype="float64")
        # Narrowed only as far as round-trips EXACTLY, and float64 when nothing does.
        #
        # int8 covers every TA-Lib rule (-1/0/1) and the baseline, which is the bulk of
        # the sheet and where the 9x compression comes from. A published strategy returns
        # a continuous target exposure in -1..1 and fits neither int8 nor float32.
        #
        # float32 used to be the unconditional fallback for that case, and it made the
        # cache non-transparent the moment anything read a continuous rule through it:
        # `strat_wf.py` does, and a stored exposure came back ~8e-6 different, which moved
        # `net_return_pct` in the sixth decimal on the commodities and crypto sheets.
        # A lossy cache is a different answer wearing the same rule name, and the whole
        # contract here is that it changes nothing.
        store = arr
        for dt in (np.int8, np.float32):
            cand = arr.astype(dt)
            if np.array_equal(cand.astype("float64"), arr):
                store = cand
                break
        key = _safe(symbol)
        self._store[f"p:{key}"] = store
        self._store[f"f:{key}"] = np.asarray(want)
        self._dirty = True

    def close(self) -> None:
        if not (self._enabled and self._dirty):
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a unique temp then replace, so a killed run (this pipeline's long jobs
        # get killed at 10 minutes) can never leave a half-written npz that a later run
        # would read as real. The pid keeps parallel workers from colliding.
        tmp = self._path.with_suffix(f".{os.getpid()}.tmp.npz")
        try:
            np.savez_compressed(tmp, **self._store)
            os.replace(tmp, self._path)
        except Exception:
            # A cache that cannot write must not break a run that does not need it.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        finally:
            self._dirty = False

    def __enter__(self) -> "RuleCache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Sheet:
    """Cache bound to one (class, timeframe) and one loaded `{symbol: DataFrame}`."""

    def __init__(self, root: Path, asset_class: str, timeframe: str,
                 data: dict, code_fp: str, enabled: bool = True):
        self._dir = Path(root) / code_fp / asset_class / timeframe
        self._enabled = enabled and not disabled()
        self._fp = ({s: data_fingerprint(df) for s, df in data.items()}
                    if self._enabled else {})

    @property
    def enabled(self) -> bool:
        return self._enabled

    def rule(self, name: str) -> RuleCache:
        """Handle for one rule. Use as a context manager so it flushes on exit."""
        return RuleCache(self._dir / f"{_safe(name)}.npz", self._fp, self._enabled)
