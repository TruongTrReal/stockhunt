#!/usr/bin/env bash
# The 1d/4h books again at the PESSIMISTIC fill, so the promoted rules carry a range and
# not just the optimistic bound.
#
# `run_open_fill.sh` walks all twenty cells and SKIPS any whose output exists, which is
# right when it is filling gaps and wrong here: the nine 1d/4h `_open` sheets exist and
# are exactly the ones whose rule list grew. This overwrites those nine and leaves the
# eleven intraday ones -- which nothing in this promotion changed, and which cost hours --
# alone.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
fail=0
while IFS=, read -r cls tf n_rules n_folds start; do
  [ "$cls" = "class" ] && continue
  case "$tf" in 1d|4h) ;; *) continue ;; esac
  start=$(echo "$start" | tr -d '\r')
  rules="book_rules/${cls}_${tf}.txt"
  [ -f "$rules" ] || { echo "=== $cls $tf SKIP (no rule list)"; continue; }
  t0=$SECONDS
  echo "=== $cls $tf open-fill start $(date -u +%H:%M:%S) ($(grep -cv '^#' "$rules") labels)"
  "$PY" -u portfolio_wf.py \
      --class "$cls" --tf "$tf" --pit --start "$start" --cash-rate 0 \
      --rules-file "$rules" --fill open --out "book_${cls}_${tf}_open.csv" \
      > "logs/book_promote_${cls}_${tf}_open.log" 2>&1
  code=$?
  echo "=== $cls $tf open-fill exit=$code $((SECONDS - t0))s"
  [ $code -ne 0 ] && fail=1
done < book_rules/starts.csv
echo "=== OPEN-FILL DONE fail=$fail $(date -u +%H:%M:%S) ==="
exit $fail
