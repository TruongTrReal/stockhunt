#!/usr/bin/env bash
# Book-level scores for the 1d and 4h sheets only, after the conversions were promoted
# into `edge_standard.csv`.
#
# NOT `run_book.sh`. That reads every row of `book_rules/starts.csv`, which is twenty
# cells now that the research axis carries 1h and 15m -- and the minute sheets cost hours
# each for a rule set this run did not change. The nine cells below are the ones whose
# rule list actually grew.
#
# `--start` per sheet is fold 0's `is_end`, read from `starts.csv` rather than typed, for
# the reason `run_book.sh` gives: two copies of a number that must agree is a drift
# waiting to happen.
set -u
cd "$(dirname "$0")"
W="$(pwd)"
PY="$W/../.venv/Scripts/python.exe"
mkdir -p logs
export STOCKHUNT_WORKERS=6      # not ten: these stages bypass the position cache

STARTS="book_rules/starts.csv"
[ -f "$STARTS" ] || { echo "no $STARTS -- run make_book_rules.py first"; exit 1; }

fail=0
while IFS=, read -r cls tf nr nf start; do
  [ "$cls" = "class" ] && continue
  case "$tf" in 1d|4h) ;; *) continue ;; esac
  start=$(echo "$start" | tr -d '\r')
  rules="book_rules/${cls}_${tf}.txt"
  [ -f "$rules" ] || { echo "=== $cls $tf SKIPPED -- no $rules"; fail=1; continue; }
  t0=$SECONDS
  echo "=== $cls $tf start $(date -u +%H:%M:%S)  --start $start  ($(grep -cv '^#' "$rules") labels)"
  "$PY" -u portfolio_wf.py \
      --class "$cls" --tf "$tf" --pit --start "$start" --cash-rate 0 \
      --rules-file "$rules" \
      --out "book_${cls}_${tf}.csv" --curves \
      > "logs/book_promote_${cls}_${tf}.log" 2>&1
  code=$?
  echo "=== $cls $tf exit=$code $((SECONDS - t0))s"
  [ $code -ne 0 ] && fail=1
done < "$STARTS"
echo "BOOK RUNS DONE fail=$fail $(date -u +%H:%M:%S)"
exit $fail
