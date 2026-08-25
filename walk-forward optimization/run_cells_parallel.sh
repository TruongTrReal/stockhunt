#!/usr/bin/env bash
# Book runs for several cells AT ONCE, because `portfolio_wf.py` is single-threaded.
#
# THE BOTTLENECK IS NOT THE ARITHMETIC, IT IS ONE CORE. `portfolio_wf` contains no
# `map_rules`, no process pool and no `worker_count` -- unlike `walkforward.py` and
# `strat_wf.py`, which take `stockhunt.parallel` and run ten workers by default. So every
# book run so far has used 1 of this machine's 12 cores while 11 sat idle, which is most
# of why they take hours.
#
# The fix is applied HERE rather than inside the stage, and that is deliberate. Making
# `portfolio_wf` parallel means editing the code that produces published numbers, and the
# panel passes are order-dependent: `_deflate` -> `_vs_random` -> `_standard`, with
# `vs_random` interpolating the RANDOM_* rows' measured Sharpes across the whole panel.
# Parallelising across cells instead leaves each process byte-identical to a serial run --
# same rules, same panel, same order -- and simply runs four of them.
#
# CONCURRENCY IS BOUNDED BY MEMORY, NOT CORES. Each process holds its whole cell in pandas:
# `us_stocks 5m` is 217 symbols x ~125,000 bars before a single position is generated. The
# 10-worker `strat_wf` run on 2026-08-23 held ~18 GB across its pool on a 32 GB box, and the
# 1-minute fetch that preceded it was OOM-killed at 99.9% after eight and a half hours. So
# the default is 3, not 12, and `JOBS` is the knob to raise once you have watched it.
#
#   JOBS=3 ./run_cells_parallel.sh 5m open     # every 5m cell, pessimistic fill
#   JOBS=2 ./run_cells_parallel.sh 15m close
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
TF="${1:?usage: run_cells_parallel.sh <timeframe> <close|open>}"
FILL="${2:?usage: run_cells_parallel.sh <timeframe> <close|open>}"
JOBS="${JOBS:-3}"
SUF=""; [ "$FILL" = "open" ] && SUF="_open"

run_cell() {
  cls="$1"; tf="$2"; start="$3"
  rules="book_rules/${cls}_${tf}.txt"
  out="book_${cls}_${tf}${SUF}.csv"
  [ -f "$rules" ] || { echo "=== $cls $tf SKIP (no rule list)"; return 0; }
  [ -f "results/$out" ] && { echo "=== $cls $tf SKIP (have $out)"; return 0; }
  t0=$SECONDS
  echo "=== $cls $tf $FILL start $(date -u +%H:%M:%S)"
  "$PY" -u portfolio_wf.py \
      --class "$cls" --tf "$tf" --pit --start "$start" --cash-rate 0 \
      --rules-file "$rules" --fill "$FILL" --out "$out" \
      > "logs/book_${cls}_${tf}${SUF}.log" 2>&1
  echo "=== $cls $tf $FILL exit=$? $((SECONDS - t0))s"
}

echo "=== PARALLEL $TF $FILL, $JOBS at a time, $(date -u +%H:%M:%S) ==="
n=0
while IFS=, read -r cls tf nr nf start; do
  [ "$cls" = "class" ] && continue
  [ "$tf" != "$TF" ] && continue
  start=$(echo "$start" | tr -d '\r')
  run_cell "$cls" "$tf" "$start" &
  n=$((n + 1))
  # `wait -n` returns on the FIRST child to finish, so a slot reopens immediately rather
  # than the whole batch waiting on its slowest member -- which matters here, where
  # `commodities` is three symbols and `us_stocks` is 217.
  if [ "$n" -ge "$JOBS" ]; then wait -n; n=$((n - 1)); fi
done < book_rules/starts.csv
wait
echo "=== PARALLEL $TF $FILL DONE $(date -u +%H:%M:%S) ==="
