#!/usr/bin/env bash
# Stage 1g + 1h for the two re-scored cells. Run AFTER run_rescore_2026_08_28.sh.
#
# riskmatch_wf is run ONE CELL AT A TIME so `scoped` is true and each lands in
# edge_standard.partial.csv, leaving the verdict of record for the other 15 cells alone.
# Each partial is copied to results/edge_cells/ (the per-cell record) AND to a staging
# dir holding ONLY these three, because merge_edge_standard.py splices every edge_*.csv
# it finds -- and results/edge_cells/ also holds cme_futures_4h and crypto_1h, two cells
# that are deliberately NOT on edge_standard.csv today.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs results/edge_cells
STAGE=.rescore_cells_20260828
rm -rf "$STAGE"; mkdir -p "$STAGE"

echo "### STAGE 1g riskmatch $(date -u +%H:%M:%S)"
for spec in cme_futures:1d cme_futures:1h commodities:4h; do
  cls="${spec%%:*}"; tf="${spec##*:}"
  t0=$SECONDS
  echo "=== rm $cls $tf start $(date -u +%H:%M:%S)"
  rm -f results/edge_standard.partial.csv
  "$PY" -u riskmatch_wf.py --class "$cls" --tf "$tf" \
      > "logs/rescore_rm_${cls}_${tf}.log" 2>&1
  rc=$?
  if [ $rc -eq 0 ] && [ -f results/edge_standard.partial.csv ]; then
    cp results/edge_standard.partial.csv "results/edge_cells/edge_${cls}_${tf}.csv"
    cp results/edge_standard.partial.csv "$STAGE/edge_${cls}_${tf}.csv"
    echo "=== rm $cls $tf exit=$rc $((SECONDS-t0))s -> $(wc -l < "$STAGE/edge_${cls}_${tf}.csv" | tr -d ' ') rows"
  else
    echo "=== rm $cls $tf FAILED exit=$rc $((SECONDS-t0))s"; exit 1
  fi
done

echo "### MERGE into edge_standard.csv $(date -u +%H:%M:%S)"
"$PY" -u merge_edge_standard.py --cells-dir "$STAGE" --dry-run
"$PY" -u merge_edge_standard.py --cells-dir "$STAGE" || exit 1

echo "### make_book_rules $(date -u +%H:%M:%S)"
"$PY" -u make_book_rules.py > logs/rescore_make_book_rules.log 2>&1
echo "=== make_book_rules exit=$?"

echo "### STAGE 1h books $(date -u +%H:%M:%S)"
export STOCKHUNT_WORKERS=6
for spec in cme_futures:1d cme_futures:1h commodities:4h; do
  cls="${spec%%:*}"; tf="${spec##*:}"
  start=$(awk -F, -v c="$cls" -v t="$tf" 'NR>1 && $1==c && $2==t {print $5}' book_rules/starts.csv | tr -d '\r')
  rules="book_rules/${cls}_${tf}.txt"
  for fill in close open; do
    if [ "$fill" = close ]; then out="book_${cls}_${tf}.csv"; extra="--curves"; else out="book_${cls}_${tf}_open.csv"; extra=""; fi
    t0=$SECONDS
    echo "=== book $cls $tf $fill start $(date -u +%H:%M:%S) --start $start ($(grep -cv '^#' "$rules") labels)"
    "$PY" -u portfolio_wf.py --class "$cls" --tf "$tf" --pit --start "$start" \
        --cash-rate 0 --rules-file "$rules" --fill "$fill" --out "$out" $extra \
        > "logs/rescore_book_${cls}_${tf}_${fill}.log" 2>&1
    echo "=== book $cls $tf $fill exit=$? $((SECONDS-t0))s"
  done
done
echo "### RESCORE VERDICT+BOOK DONE $(date -u +%H:%M:%S)"
