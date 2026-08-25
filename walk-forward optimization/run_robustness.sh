#!/usr/bin/env bash
# Book scores for the leaderboard's own rules on the cells they have never been run on.
#
# The population is `book_rules/leaderboard_union.txt`: every rule shipped on any sheet of
# the live board (152), plus the thirteen converted third-party strategies as ordinary
# catalogue cells -- published default and long/flat variant, 21 labels -- because they are
# strategies like any other and rank on the same key from now on. 180 labels with the
# baseline and controls.
#
# ONLY THE ELEVEN UNSCORED CELLS. The nine that already have a `book_*.csv` carry ~409
# labels each and are left alone; rebuilding them from this shorter list would shrink the
# live leaderboard by two thirds.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
fail=0
while IFS=, read -r cls tf n_rules n_folds start; do
  [ "$cls" = "class" ] && continue
  start=$(echo "$start" | tr -d '\r')
  book="results/book_${cls}_${tf}.csv"
  rules="book_rules/${cls}_${tf}.txt"
  [ -f "$book" ] && { echo "=== $cls $tf SKIP (already scored)"; continue; }
  [ -f "$rules" ] || { echo "=== $cls $tf SKIP (no rule list)"; continue; }
  t0=$SECONDS
  echo "=== $cls $tf start $(date -u +%H:%M:%S) --start $start ($(grep -cv '^#' "$rules") labels)"
  "$PY" -u portfolio_wf.py \
      --class "$cls" --tf "$tf" --pit --start "$start" --cash-rate 0 \
      --rules-file "$rules" --out "book_${cls}_${tf}.csv" --curves \
      > "logs/book_${cls}_${tf}.log" 2>&1
  code=$?
  echo "=== $cls $tf exit=$code $((SECONDS - t0))s"
  [ $code -ne 0 ] && fail=1
done < book_rules/starts.csv
echo "=== ROBUSTNESS RUNS DONE fail=$fail $(date -u +%H:%M:%S) ==="
exit $fail
