#!/usr/bin/env bash
# Re-score the verdict for every cell the 2026-08-26/27 data work invalidated or unblocked.
#
# SAME SCOPING TRAP as `run_riskmatch_intraday.sh`, and it has not gone away:
#
#     scoped = (bool(args.rules) or len(args.classes) < len(CLASSES)) and not args.promote
#
# `--tf` does NOT make a run scoped. Every class at once, even at one timeframe, rewrites
# `results/edge_standard.csv` whole and deletes every cell it did not just score -- from the
# only file in this repo where a pass or fail exists. One class at a time lands in
# `edge_standard.partial.csv`; each is copied to `results/edge_cells/` before the next
# starts, and `merge_edge_standard.py` splices them back by (class, tf).
#
# WHAT CHANGED, AND WHY EACH CELL IS HERE:
#
# * CRYPTO IS BACK. `run_riskmatch_intraday.sh` excluded it because `repair_spikes.py` had
#   clamped the real 2025-10-10 liquidation cascade out of the intraday bars, so crypto
#   1h/15m disagreed with crypto 1d about the largest move in the sample. The bars were
#   refetched on 2026-08-26 and 157 decimal-shifted 2023 bars repaired; 1m now agrees with
#   the independently fetched 5m/15m to 0.001-0.010 bp mean. That blocker is gone.
#
# * CME_FUTURES 15m IS BACK, with a worker cap. It was dropped for memory: ten workers each
#   holding 16 roots at 251,082 bars exhausted 32 GB, the pool died, and the serial fallback
#   sat at 52% CPU paging ~17 GB. `riskmatch_wf` now honours STOCKHUNT_WORKERS, so the cell
#   runs at a width that fits instead of not running at all. It is still the most expensive
#   cell here by a wide margin -- it goes LAST so a failure costs nothing that came before.
#
# * CME_FUTURES 4h has never been scored, and its bars were resampled from a fresh 1m pull
#   on 2026-08-27.
#
# * COMMODITIES 1d is here because of a cut I made, not a fetch: gold and silver now enter
#   2006-02-03 and 2006-02-06 (`commodity_entry.csv`), because the vendor's Open was exactly
#   the High or the Low on every bar before then. The sheet says 23.7 years; two of the five
#   names now carry 20.6. `metrics.se_ir` falls as 1/sqrt(years), so the bar those names must
#   clear moved -- this is not a cosmetic re-run.
#
# * EVERY 1h AND 15m CELL is stale on mtime alone: the sheets are from 08-24 and every bar
#   file under them was rewritten on 08-26 or 08-27.
#
# 5m IS DELIBERATELY ABSENT. No 5m cell has ever been scored here, and the column is ~83% of
# the total cost of the grid; `cme_futures 15m` is the memory experiment that tells us
# whether 5m is feasible on this box at all. Decide 5m after seeing what 15m does.
#
#   nohup ./run_riskmatch_refresh.sh > logs/rm_refresh_driver.log 2>&1 &
#   FORCE=1 ./run_riskmatch_refresh.sh     # re-score cells already in results/edge_cells/
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
FORCE="${FORCE:-}"
mkdir -p logs results/edge_cells

# Six workers, not the default ten: these stages call `signals.position_for` directly, get
# no help from the position cache, and ten drove free memory to 3 GB of 32 GB.
export STOCKHUNT_WORKERS=6

# "class tf" -- cheapest first, so the quick cells bank before the expensive ones start.
# Cost tracks PER-SYMBOL series length superlinearly, not symbol count: commodities is three
# symbols and cost 4,476s at 15m while us_etfs is ten and cost 459s.
CELLS="
commodities:1d
cme_futures:4h
us_etfs:1h
commodities:1h
crypto:1h
us_stocks:1h
cme_futures:1h
us_etfs:15m
commodities:15m
us_stocks:15m
crypto:15m
cme_futures:15m
"

echo "=== RISKMATCH REFRESH $(date -u +%H:%M:%S)  workers=$STOCKHUNT_WORKERS ==="
for cell in $CELLS; do
  cls="${cell%%:*}"; tf="${cell##*:}"
  out="results/edge_cells/edge_${cls}_${tf}.csv"
  if [ -f "$out" ] && [ -z "$FORCE" ]; then
    echo "=== $cls $tf SKIP (have $(basename "$out"); FORCE=1 to redo)"; continue
  fi
  t0=$SECONDS
  echo "=== $cls $tf start $(date -u +%H:%M:%S)"
  "$PY" -u riskmatch_wf.py --class "$cls" --tf "$tf" > "logs/rm_${cls}_${tf}.log" 2>&1
  rc=$?
  if [ $rc -eq 0 ] && [ -f results/edge_standard.partial.csv ]; then
    cp results/edge_standard.partial.csv "$out"
    echo "=== $cls $tf exit=$rc $((SECONDS - t0))s -> $(($(wc -l < "$out") - 1)) rows"
  else
    echo "=== $cls $tf FAILED exit=$rc $((SECONDS - t0))s -- see logs/rm_${cls}_${tf}.log"
  fi
done
echo "=== RISKMATCH REFRESH DONE $(date -u +%H:%M:%S) ==="
