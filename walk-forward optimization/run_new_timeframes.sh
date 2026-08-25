#!/usr/bin/env bash
# Walk-forward sheets for the timeframes added to the research axis on 2026-08-22
# (1h and 15m), every class whose bars are trustworthy.
#
# SAFE TO RUN PIECEMEAL: `walkforward.py` writes `wf_summary_<class>_<tf>.csv`, one file
# per cell, and touches nothing else. That is NOT true of the stage after it --
# `riskmatch_wf.py` rewrites `edge_standard.csv` whole, with no merge, so the verdict has
# to be regenerated for every class AND every timeframe in a single run or the cells left
# out are silently deleted from the file.
#
# `crypto` is deliberately absent at 1h/15m/4h: 8 of 20 pairs carry vendor bad ticks
# (five coins "crashing" 78-99% and recovering within minutes in one week of December
# 2023), the daily bars are clean but every intraday one is not, and IBS is
# (Close-Low)/(High-Low) -- a bogus Low pins it near 1.0. Scoring those sheets would
# manufacture exactly the kind of result this repo exists to catch.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
for tf in 15m 1h; do
  for cls in us_stocks us_etfs commodities cme_futures; do
    have=$("$PY" -c "import sys;sys.path.insert(0,'../backtest engine');from td_loader import cache_dir;print(len(list(cache_dir('$cls','$tf').glob('*.parquet'))))" 2>/dev/null || echo 0)
    if [ "$have" -lt 3 ]; then echo "=== SKIP $cls $tf: only $have symbols cached ==="; continue; fi
    echo "=== $cls $tf  ($have symbols)  $(date -u +%H:%M:%S) ==="
    "$PY" -u walkforward.py --class "$cls" --tf "$tf" 2>&1 | tail -3
  done
done
echo "=== walk-forward sweep done $(date -u +%H:%M:%S) ==="
