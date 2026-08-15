"""Run the rule loop across cores.

Every stage in this pipeline is the same shape — for each rule, for each asset, build a
position and score it — and every rule is independent of every other. That is an
embarrassingly parallel loop on a 12-core machine, and until now exactly one module
(`riskmatch_wf._run_rules_parallel`) used more than one core. The rest ran serial, which
is why us_stocks 1d walk-forward takes **1316 seconds** and its 4h sheet **387**.

This generalises riskmatch's pattern, which was already proven in this repo, and keeps
its two important properties:

**A pool failure is loud.** If the executor dies, the natural failure mode is a short
result frame — some rules missing, no error — which is indistinguishable from "those
rules legitimately produced nothing". That is the exact shape of every bug this
pipeline has ever hidden behind, so the fallback prints what happened and then re-runs
the whole set serially rather than returning a partial answer.

**Two cores stay free.** This machine is shared; saturating it makes the box unusable
and starves anything else running.

**Ordering is preserved.** `ex.map` yields in submission order, so the rows come back in
the same sequence a serial loop would produce them. Several result CSVs are written in
loop order, and a parallel run that shuffled them would produce a diff that looks like a
change but is not — the worst kind to have to investigate.

Windows uses `spawn`, so each worker re-imports the module and rebuilds its context from
scratch (~1.5s to reload a sheet's bars). That is amortised over hundreds of rules but
would dominate a small job, so below `min_tasks` this runs serial and does not pay it.

The function mapped over rules must be a **module-level** function — a closure or a
lambda cannot be pickled. The established pattern, which `riskmatch_wf` already uses, is
a module global set by the pool initializer:

    _CTX = None

    def _init(asset_class, timeframe):
        global _CTX
        _CTX = build_context(asset_class, timeframe)

    def _score(rule):
        return score(rule, _CTX)

    rows = parallel.map_rules(rules, _score, _init, (asset_class, timeframe),
                              serial_ctx=ctx, serial_fn=score)
"""

from __future__ import annotations

import os
import sys

# Below this many rules, process startup costs more than the loop saves.
MIN_TASKS = 24

# Leave this many cores to the rest of the machine.
RESERVED_CORES = 2


def worker_count(n_tasks: int, requested: int | None = None) -> int:
    """Workers to use: never more than tasks, never the whole machine.

    `STOCKHUNT_WORKERS` overrides everything, including `requested`. Setting it to 1
    forces the serial path, which is how a parallel run is proved to produce the same
    answer as a serial one and the first thing to try when a result looks surprising.
    """
    env = os.environ.get("STOCKHUNT_WORKERS", "").strip()
    if env:
        try:
            return max(1, min(n_tasks, int(env)))
        except ValueError:
            pass
    if requested is not None:
        return max(1, min(n_tasks, requested))
    if n_tasks <= 1:
        return 1
    return max(1, min(n_tasks, (os.cpu_count() or 2) - RESERVED_CORES))


def map_rules(rules, fn, initializer=None, initargs=(), workers=None,
              desc: str = "", min_tasks: int = MIN_TASKS, chunksize: int = 1,
              progress: bool = True, serial_fn=None):
    """Map `fn` over `rules`, in processes when that is worth it.

    `fn` is applied to one rule and returns a list of rows; the lists are concatenated in
    submission order. It must be importable by name in a fresh interpreter, and it reads
    its context from a module global that `initializer(*initargs)` sets.

    **`serial_fn` is not optional in practice.** `fn` reads a module global that only the
    pool initializer sets, so calling it in the parent process — which is what the serial
    path and the pool-failure fallback both do — finds that global still `None`. Pass
    `serial_fn=lambda r: score(r, ctx)`, closing over the context the parent already
    built; it is never pickled, so a closure is fine here. Without it this falls back to
    running `initializer(*initargs)` in-process, which is correct but rebuilds a context
    the caller is already holding.

    That distinction is exactly what `riskmatch_wf` got right by keeping two separate code
    paths, and what was briefly lost in generalising it: the serial path crashed with
    `'NoneType' object is not subscriptable` the first time it was exercised.
    """
    rules = list(rules)
    n = worker_count(len(rules), workers)
    local = serial_fn or _rebound(fn, initializer, initargs)

    if len(rules) < min_tasks or n <= 1:
        return _serial(rules, local, desc, progress)

    from concurrent.futures import ProcessPoolExecutor
    rows = []
    try:
        with ProcessPoolExecutor(max_workers=n, initializer=initializer,
                                 initargs=initargs) as ex:
            it = ex.map(fn, rules, chunksize=chunksize)
            for out in _bar(it, len(rules), f"{desc} x{n}", progress):
                rows.extend(out)
        return rows
    except Exception as exc:
        print(f"  parallel pool failed ({type(exc).__name__}: {exc}); "
              f"falling back to serial", file=sys.stderr)
        return _serial(rules, local, desc, progress)


def _rebound(fn, initializer, initargs):
    """`fn` with the initializer applied to *this* process, once, on first call."""
    if initializer is None:
        return fn
    state = {"ready": False}

    def call(rule):
        if not state["ready"]:
            initializer(*initargs)
            state["ready"] = True
        return fn(rule)
    return call


def _serial(rules, fn, desc, progress):
    rows = []
    for r in _bar(rules, len(rules), desc, progress):
        rows.extend(fn(r))
    return rows


def _bar(iterable, total, desc, progress):
    if not progress:
        return iterable
    try:
        from tqdm import tqdm
        return tqdm(iterable, total=total, desc=desc)
    except ImportError:
        return iterable
