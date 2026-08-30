#!/usr/bin/env bash
# The three 5m books, so the 5m leaderboards have numbers on them.
#
# WHY THEY WERE MISSING. The cloud fleet ran `riskmatch_wf` -- the VERDICT -- and every 5m
# rule was scored there. It did not run `portfolio_wf`, because that stage holds a whole cell
# in pandas and does not fit the 16 GB boxes: commodities 15m, the smallest cell in the grid,
# reached 15.9 GB resident with 5.5 GB swapped and sat at 87% CPU idle. So the 5m verdicts
# came home and the 5m books were never built, and the board -- which prints book numbers,
# not verdicts -- had rows with a verdict and nothing else.
#
# OPEN FILL ONLY, and that is deliberate. At 78 bars a day a rule computed from a bar's own
# close and filled at that same close measures the look-ahead rather than the rule: `ibs` on
# commodities reads 5.5%/yr at 1d and 1,970%/yr at 15m on close fill. `book_<cls>_5m.csv`
# does not exist and must not be created; `ingest_results.py` now reads the `_open` sheet for
# any timeframe with no close-fill book, which is 5m and only 5m.
#
# NO --curves. The curves JSON is named from (class, timeframe) alone, so an open-fill run
# writing it would overwrite charts a close-fill sheet is drawn against. At 5m there is no
# close-fill sheet to collide with, but the rule is kept because the collision is silent.
#
# ONE LANE. This is the machine the user works on, and three chains at once on it is what
# took it down on 2026-08-27.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs
# cheapest first, from measured runtimes: us_stocks 6,026s, commodities 6,500s, crypto 14,760s
for cell in us_stocks commodities crypto; do
  start=$(awk -F, -v c="$cell" '$1==c && $2=="5m" {print $5}' book_rules/starts.csv | tr -d '\r')
  [ -n "$start" ] || { echo "=== $cell 5m SKIP (no start row)"; continue; }
  [ -f "book_rules/${cell}_5m.txt" ] || { echo "=== $cell 5m SKIP (no rule list)"; continue; }
  t0=$SECONDS
  echo "=== $cell 5m open start $(date -u +%H:%M:%S)  --start $start"
  "$PY" -u portfolio_wf.py --class "$cell" --tf 5m --pit --start "$start" --cash-rate 0 \
      --rules-file "book_rules/${cell}_5m.txt" --fill open \
      --out "book_${cell}_5m_open.csv" > "logs/book_${cell}_5m_open.log" 2>&1
  echo "=== $cell 5m exit=$? $((SECONDS - t0))s"
done
echo "=== 5m BOOKS DONE $(date -u +%H:%M:%S) ==="
