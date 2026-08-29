#!/usr/bin/env bash
# Stop the riskmatch driver the moment it reaches cme_futures 15m, leaving every cell
# before it intact.
#
# That cell costs 76 minutes of riskmatch and owns the longest book cell in the grid
# (4,045s), which together is the entire difference between this run fitting a 3-4 hour
# budget and not. Dropping the VERDICT drops the BOOK for free: `make_book_rules.py` reads
# its rule lists out of `edge_standard.csv`, so a cell with no verdict rows gets no
# `--start` in starts.csv and `run_book.sh` skips it by design.
#
# Killing the driver (not the running python) is what makes this safe: the child finishes
# and writes, and no new cell is launched.
set -u
cd "$(dirname "$0")"
LOG=logs/rm_refresh_driver.log
while true; do
  if grep -q "cme_futures 15m start" "$LOG" 2>/dev/null; then
    for pid in $(powershell.exe -NoProfile -Command \
        "(Get-CimInstance Win32_Process -Filter \"Name='bash.exe'\" | Where-Object { \$_.CommandLine -like '*run_riskmatch_refresh*' }).ProcessId" \
        2>/dev/null | tr -d '\r'); do
      [ -n "$pid" ] && powershell.exe -NoProfile -Command "Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue"
    done
    # ...and the riskmatch python it just launched for that cell
    powershell.exe -NoProfile -Command \
      "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*riskmatch_wf.py --class cme_futures --tf 15m*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }" 2>/dev/null
    echo "CME_FUTURES_15M_CUT $(date -u +%H:%M:%S)"
    exit 0
  fi
  grep -q "REFRESH DONE" "$LOG" 2>/dev/null && { echo "DRIVER FINISHED ON ITS OWN"; exit 0; }
  sleep 20
done
