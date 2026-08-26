#!/usr/bin/env bash
# Re-score every crypto book that was built on the damaged intraday bars.
#
# WHY: an early `repair_spikes.py` pass clamped the real 2025-10-10 liquidation cascade out
# of crypto 4h/1h/15m/5m -- 8, 7, 4 and 2 live pairs respectively, each left unable to reach
# its own daily low. Every book below was scored against bars whose largest drawdown had
# been edited away, which flatters dip-buying rules, and dip-buyers are most of this
# leaderboard. The bars were refetched from Twelve Data on 2026-08-26 and verified with
# `verify_intraday_vs_daily.py`; these are the sheets that have to follow.
#
# 1d IS NOT HERE. The daily bars were never touched -- they are what proved the intraday
# ones wrong -- so `book_crypto_1d.csv` still describes the data it was built on.
#
# TWO FILLS, AND ONLY ONE OF THEM MAY WRITE CURVES. `book_curves_<cls>_<tf>.json` takes its
# name from (class, timeframe) alone, so an open-fill run passing `--curves` would overwrite
# the close-fill curves the dashboard draws and every crypto detail page would show a chart
# that disagrees with its own row. Close fill carries `--curves` because it is the published
# convention and the charts are its; open fill never does.
#
# 5m is open-fill ONLY, unchanged from the original decision: at 78 bars a day a close-fill
# number measures the look-ahead rather than the rule, so there is no close-fill 5m sheet to
# rebuild and none should be created.
#
# Cells run CONCURRENTLY because `portfolio_wf` is single-threaded -- see
# `run_cells_parallel.sh` for why that is fixed here and not inside the stage. Bounded by
# memory, not cores: JOBS=3 held ~3 GB per process last time.
#
#   ./rerun_crypto_books.sh              # every cell, JOBS=3
#   JOBS=2 ./rerun_crypto_books.sh       # gentler on a shared box
#   ./rerun_crypto_books.sh --dry-run    # print the commands, run nothing
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
JOBS="${JOBS:-3}"
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY=1
mkdir -p logs results/_pre_refetch

# tf:fill -- ordered longest-first so the slowest cell starts immediately and the short ones
# fill in behind it. crypto 5m is ~4h on its own; 4h is minutes.
CELLS="5m:open 15m:close 15m:open 1h:close 1h:open 4h:close 4h:open"

# --start IS DERIVED FROM THE BARS, not read out of `book_rules/starts.csv`.
#
# That file was written against the OLD cache, and the refetch moved the spans: crypto 5m
# now runs 2020-03-26..2026-08-26 where the resampled copy ran 2020-04-08..2026-08-03. Folds
# are generated from the available history, so a longer series moves fold 0's `is_end` --
# and `--start` IS fold 0's `is_end`, the first bar that was ever out-of-sample. Reading a
# stale date here would score the new bars over the old window and call it the same
# measurement. `oos_start` is the function `make_book_rules.py` uses to write that file in
# the first place, so this asks the source rather than the cache of it.
start_for() {
  "$PY" -c "
import sys, wfo_paths                      # noqa: F401  (path bootstrap, must precede config)
from make_book_rules import oos_start
s, n = oos_start('crypto', '$1')
print('' if s is None else __import__('pandas').Timestamp(s).strftime('%Y-%m-%d'))
" 2>/dev/null | tr -d '\r'
}

run_cell() {
  tf="$1"; fill="$2"
  suf=""; [ "$fill" = "open" ] && suf="_open"
  out="book_crypto_${tf}${suf}.csv"
  rules="book_rules/crypto_${tf}.txt"
  start=$(start_for "$tf")
  [ -f "$rules" ] || { echo "=== crypto $tf SKIP (no $rules)"; return 0; }
  [ -n "$start" ] || { echo "=== crypto $tf SKIP (no start in starts.csv)"; return 0; }
  curves=""; [ "$fill" = "close" ] && curves="--curves"
  if [ -n "$DRY" ]; then
    echo "would run: --class crypto --tf $tf --pit --start $start --cash-rate 0 \
--rules-file $rules --fill $fill --out $out $curves"
    return 0
  fi
  # Keep the sheet that is being replaced. These are the numbers currently on the board and
  # on the VPS; if the re-run is wrong, the diff against these is how anyone would know.
  [ -f "results/$out" ] && cp "results/$out" "results/_pre_refetch/$out"
  t0=$SECONDS
  echo "=== crypto $tf $fill start $(date -u +%H:%M:%S)  (start $start)"
  "$PY" -u portfolio_wf.py \
      --class crypto --tf "$tf" --pit --start "$start" --cash-rate 0 \
      --rules-file "$rules" --fill "$fill" --out "$out" $curves \
      > "logs/rebuild_crypto_${tf}${suf}.log" 2>&1
  echo "=== crypto $tf $fill exit=$? $((SECONDS - t0))s"
}

echo "=== CRYPTO BOOK REBUILD, $JOBS at a time, $(date -u +%H:%M:%S) ==="
n=0
for cell in $CELLS; do
  run_cell "${cell%%:*}" "${cell##*:}" &
  n=$((n + 1))
  if [ "$n" -ge "$JOBS" ]; then wait -n; n=$((n - 1)); fi
done
wait
echo "=== CRYPTO BOOK REBUILD DONE $(date -u +%H:%M:%S) ==="
