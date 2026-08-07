# Locked directories

Two study folders are frozen as of **2026-08-08**. They are finished, their
results are cited by later work, and they must stay reproducible exactly as they
are.

| Folder | What it was | State at freeze |
|---|---|---|
| `test research/` | Every TA-Lib rule (161 functions, ~231 variants) as a standalone long/flat/short rule across 501 S&P tickers, daily bars | 0 rules beat buy-and-hold |
| `top 20 stocks/` | The depth counterpart: 20 mega-caps at 1d / 1h / 5m with costs and EOD flattening, plus the P0–P9 edge hunt | 0 rules beat buy-and-hold; finer timeframes strictly worse |

## How the lock works

`.claude/settings.json` denies `Edit`, `Write` and `NotebookEdit` against both
directories, plus the obvious `rm` / `mv` / `git mv` / `git rm` shapes. The
Edit/Write denies are exact and enforced. The Bash patterns are best-effort —
command-string matching cannot catch every spelling of a destructive command, so
treat them as a speed bump, not a guarantee.

To unlock, remove the relevant lines from `.claude/settings.json` deliberately.
Do not work around them.

## Reads are still allowed, and still happen

Freezing does not mean unused. These paths are live read-only dependencies:

- `engine-bakeoff/common.py:33` reads `test research/data/cache/`
- `top 20 stocks/edge/common.py:25-26` reads `test research/data/cache_td/` and `cache/`
- `top 20 stocks/edge/{mass_ic,p4_breadth,p5_ic,p6_illiquidity}.py` read the same caches

## The one dependency that was cut

`backtest master/` used to put `test research/src/` on `sys.path` so it could
`import talib_signals` — the shared 231-rule signal layer. That made a frozen
folder a runtime dependency of live code.

`strategies/talib_signals.py` is now a byte-identical copy at the repo root, and
all new code imports that. The original stays where it is because 17 modules in
`test research/src/` still import it. The two must never diverge, which is safe
precisely because the original is frozen.

## Getting back

The pre-refactor state of the whole repo is tagged `pre-refactor`.
