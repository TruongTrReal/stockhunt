#!/usr/bin/env bash
# The six-criteria verdict at 1d and 4h, re-run so the 13 conversions, the 130 megacellar
# forecasters and sp100_momentum have an `edge_standard.csv` record.
#
# THAT RECORD IS THE GATE. `board_rank.build_sheet` DROPS any leaderboard row with no
# edge row -- deliberately, because carrying one prints a stale diagnostic beside a fresh
# verdict -- so a rule with a `strat_summary` row and no edge row is scored and invisible.
# That is exactly the state the 143 were in at 1d and 4h.
#
# ONE CLASS AT A TIME, and this is the trap `run_riskmatch_intraday.sh` documents:
#
#     scoped = (bool(args.rules) or len(args.classes) < len(CLASSES)) and not args.promote
#
# `--tf` is not in it. "Every class at 1d and 4h" is therefore NOT scoped, writes the real
# `edge_standard.csv`, and silently deletes the 1h and 15m cells. One class is scoped,
# lands in `edge_standard.partial.csv`, and is copied out before the next run overwrites
# it. `merge_edge_standard.py` splices the cells back in afterwards.
#
# The nine cells below are exactly the nine (class, timeframe) scopes the batches were
# pre-registered under in `data/reference/trials.csv`. `cme_futures` is 1d only.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs results/edge_cells

echo "=== RISKMATCH 1d/4h $(date -u +%H:%M:%S) ==="
for spec in us_etfs:1d us_etfs:4h commodities:1d commodities:4h crypto:1d crypto:4h \
            cme_futures:1d us_stocks:4h us_stocks:1d; do
  cls="${spec%%:*}"; tf="${spec##*:}"
  out="results/edge_cells/edge_${cls}_${tf}.csv"
  t0=$SECONDS
  echo "=== $cls $tf start $(date -u +%H:%M:%S)"
  "$PY" -u riskmatch_wf.py --class "$cls" --tf "$tf" \
      > "logs/rm_promote_${cls}_${tf}.log" 2>&1
  rc=$?
  if [ $rc -eq 0 ] && [ -f results/edge_standard.partial.csv ]; then
    cp results/edge_standard.partial.csv "$out"
    echo "=== $cls $tf exit=$rc $((SECONDS - t0))s -> $(wc -l < "$out" | tr -d ' ') rows"
  else
    echo "=== $cls $tf FAILED exit=$rc $((SECONDS - t0))s -- logs/rm_promote_${cls}_${tf}.log"
  fi
done
echo "=== RISKMATCH DONE $(date -u +%H:%M:%S) ==="
