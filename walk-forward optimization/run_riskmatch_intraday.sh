#!/usr/bin/env bash
# The six-criteria verdict at 1h and 15m, one CELL at a time, so the verdict of record
# survives it.
#
# THE WHOLE POINT IS THE SCOPING RULE, AND IT IS A TRAP. `riskmatch_wf.main` decides a run
# is "scoped" from `--rules` and `--class` and NEVER from `--tf`:
#
#     scoped = (bool(args.rules) or len(args.classes) < len(CLASSES)) and not args.promote
#
# So the obvious command -- every class, `--tf 1h 15m` -- is NOT scoped, writes the real
# `edge_standard.csv`, and silently deletes 1d and 4h from the only file in this project
# where a pass or fail exists. One class at a time is scoped, lands in
# `edge_standard.partial.csv`, and leaves the record alone. The partial is overwritten by
# the next cell, so each one is copied out before the next starts.
#
# `merge_edge_standard.py` then splices the cells in by (class, tf). Nothing here promotes.
#
# CRYPTO IS ABSENT ON PURPOSE and it is not the reason `run_new_timeframes.sh` gives.
# That script blames vendor bad ticks in December 2023; those are real (TRX -98.9%,
# BCH -78.1%) and are now dropped. The live problem is the opposite one: an earlier
# `repair_spikes.py` run CLAMPED THE REAL 2025-10-10 LIQUIDATION CASCADE out of the
# intraday bars, so crypto 1h/15m/5m disagree with crypto 1d about the largest single-day
# move in the sample -- DOT's daily low is 0.633 and its 1h low that day is 2.452. Scoring
# a class whose worst drawdown has been edited out of the finer bars would flatter every
# dip-buying rule on it. Fix the bars first; then add crypto here.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs results/edge_cells

echo "=== RISKMATCH 1h/15m $(date -u +%H:%M:%S) ==="
for tf in 1h 15m; do
  # CHEAPEST FIRST, and the order is measured rather than guessed at. Cost here tracks
  # PER-SYMBOL series length superlinearly, not symbol count and not total bars, so the
  # ordering that looks right from the universe sizes is wrong: commodities is three
  # symbols and cost 4,476s at 15m while us_etfs is ten and cost 459s, because a
  # commodities series is 148,727 bars long and an ETF's is 41,741.
  #
  #   cell           bars/symbol @15m    1h -> 15m
  #   us_etfs               41,741         8.2x   (measured)
  #   us_stocks             41,773         8.2x   (assumed, same shape)
  #   commodities          148,727        37x     (measured)
  #   cme_futures          251,082      ~70x      (extrapolated -- hours, not minutes)
  #
  # `cme_futures 15m` IS DROPPED, 2026-08-26, and the reason is memory rather than time.
  # Ten workers each hold their own copy of 16 roots at 251,082 bars, which exhausted 32 GB;
  # the pool died, the fallback carried on correctly on ONE core, and 5.7 hours in it was
  # at 52% CPU because the box was paging and holding ~17 GB. Capped to a width that fits
  # (STOCKHUNT_WORKERS=4..6) the cell costs 30-45 hours of a shared machine, for ONE sheet
  # on the least-traded class -- which already has 1d and 1h scored and is already in the
  # robustness matrix from its book run. The other seven intraday sheets cost under three
  # hours between them. Its leaderboard keeps the honest empty state naming this stage.
  # Put it back by adding cme_futures to the 15m list, and pass STOCKHUNT_WORKERS.
  cells="commodities us_etfs us_stocks cme_futures"
  [ "$tf" = "15m" ] && cells="commodities us_etfs us_stocks"
  for cls in $cells; do
    out="results/edge_cells/edge_${cls}_${tf}.csv"
    [ -f "$out" ] && { echo "=== $cls $tf SKIP (have $(basename "$out"))"; continue; }
    t0=$SECONDS
    echo "=== $cls $tf start $(date -u +%H:%M:%S)"
    "$PY" -u riskmatch_wf.py --class "$cls" --tf "$tf" \
        > "logs/rm_${cls}_${tf}.log" 2>&1
    rc=$?
    if [ $rc -eq 0 ] && [ -f results/edge_standard.partial.csv ]; then
      cp results/edge_standard.partial.csv "$out"
      echo "=== $cls $tf exit=$rc $((SECONDS - t0))s -> $(wc -l < "$out" | tr -d ' ') rows"
    else
      echo "=== $cls $tf FAILED exit=$rc $((SECONDS - t0))s -- see logs/rm_${cls}_${tf}.log"
    fi
  done
done
echo "=== RISKMATCH DONE $(date -u +%H:%M:%S) ==="
