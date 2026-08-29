#!/usr/bin/env bash
# Bring every Twelve Data class back to today, cheapest cell first.
#
# WHAT IS AND IS NOT HERE, and why each call costs what it does:
#
# `td_loader.fetch` IS NOT INCREMENTAL. It refetches from the class's configured start and
# overwrites the parquet whole -- there is no append and no resume. So the cost of a cell is
# its FULL history, not the gap, and "19 days stale" is not a cheap fix on an expensive cell.
# The windows table decides it: us_stocks 1d is 6 windows x 44 batches = 264 requests, but
# us_stocks 1m is 196 x 44 = 8,624, which is half a day.
#
# THAT NON-INCREMENTAL OVERWRITE IS ALSO WHY us_stocks 1h/15m/5m ARE ABSENT. Those cells
# carry repairs that live in the parquet and nowhere else: GE's intraday bars are rebased
# past its 2024 spin-off (`intraday_action_adjustments.csv`) and 49 files were mended by
# `check_data --fix`. A refetch silently reverts both. They are one day stale, which is not
# worth re-deriving that work for; if they are ever refetched, the order is
# fetch -> check_data --fix -> adjust_intraday_actions -> verify, never the reverse.
#
# us_stocks 1m is absent for cost alone: ~12h for 5 days of staleness.
#
# cme_futures is absent because it is not a Twelve Data class at all -- Databento serves it
# through db_loader.py / db_intraday.py, and td_loader.fetch refuses it by name.
#
#   ./refresh_stale.sh              # everything below, in order
#   ./refresh_stale.sh --dry-run    # print the calls, fetch nothing
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY=1
mkdir -p logs

# class:tf -- ordered by measured request count so the quick wins land first and a failure
# late in the run has already banked the sheets of record.
CELLS="
crypto:1d
commodities:1d
commodities:4h
us_etfs:1d
us_etfs:4h
us_etfs:1h
us_stocks:4h
us_stocks:1d
commodities:1h
commodities:15m
commodities:5m
us_etfs:15m
us_etfs:5m
commodities:1m
us_etfs:1m
"

for cell in $CELLS; do
  cls="${cell%%:*}"; tf="${cell##*:}"
  if [ -n "$DRY" ]; then echo "would fetch: --class $cls --tf $tf"; continue; fi
  t0=$SECONDS
  echo "=== $cls $tf start $(date -u +%H:%M:%S)"
  "$PY" -u td_loader.py --class "$cls" --tf "$tf" \
      > "logs/refresh_${cls}_${tf}.log" 2>&1
  echo "=== $cls $tf exit=$? $((SECONDS - t0))s"
done
echo "=== REFRESH DONE $(date -u +%H:%M:%S) ==="
