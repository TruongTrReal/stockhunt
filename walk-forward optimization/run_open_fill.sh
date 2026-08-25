#!/usr/bin/env bash
# The same twenty cells at the PESSIMISTIC fill, so every number has a range.
#
# `--fill close` computes a signal from a bar's own high, low and close and then transacts
# at that same close -- a price nobody knew when the decision was made. The repo has always
# labelled that an optimistic bound; what the 2026-08-24 run showed is that the bound stops
# being a nudge and becomes the whole result as bars get finer, because the bias is per-bar
# and compounds. `ibs` on commodities, close fill, same instruments and period:
#
#     1d   6,511 bars      5.5%/yr   Sharpe 0.45
#     4h   6,067 bars     37.9%/yr   Sharpe 2.13
#     1h  23,249 bars    122.7%/yr   Sharpe 4.61
#     15m 75,909 bars  1,970.1%/yr   Sharpe 13.04      <- not a strategy, an artifact
#
# Re-filled at `open` that last cell drops to 280%/yr and Sharpe 5.75. So a robustness
# matrix built on close-fill alone would rank every reversion rule higher at finer
# timeframes for a reason that has nothing to do with the rule.
#
# `open` is not "the truth" either -- it charges a full session of delay a trader using a
# market-on-close order would not pay, so it double-counts in the other direction. The
# honest report is the RANGE, which is what these files provide: `_open` beside the
# published close-fill sheet, never replacing it.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
fail=0
while IFS=, read -r cls tf n_rules n_folds start; do
  [ "$cls" = "class" ] && continue
  start=$(echo "$start" | tr -d '\r')
  rules="book_rules/${cls}_${tf}.txt"
  out="book_${cls}_${tf}_open.csv"
  [ -f "$rules" ] || { echo "=== $cls $tf SKIP (no rule list)"; continue; }
  [ -f "results/$out" ] && { echo "=== $cls $tf SKIP (already have $out)"; continue; }
  t0=$SECONDS
  echo "=== $cls $tf open-fill start $(date -u +%H:%M:%S) ($(grep -cv '^#' "$rules") labels)"
  "$PY" -u portfolio_wf.py \
      --class "$cls" --tf "$tf" --pit --start "$start" --cash-rate 0 \
      --rules-file "$rules" --fill open --out "$out" \
      > "logs/book_${cls}_${tf}_open.log" 2>&1
  code=$?
  echo "=== $cls $tf open-fill exit=$code $((SECONDS - t0))s"
  [ $code -ne 0 ] && fail=1
done < book_rules/starts.csv
echo "=== OPEN-FILL RUNS DONE fail=$fail $(date -u +%H:%M:%S) ==="
exit $fail
