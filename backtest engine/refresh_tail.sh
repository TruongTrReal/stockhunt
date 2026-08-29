#!/usr/bin/env bash
# The last two Twelve Data cells worth fetching, once `commodities 5m` is clear.
#
# WHY THIS IS A SEPARATE, SHORTER LIST THAN refresh_stale.sh:
#
# Twelve Data began throttling partway through that run -- measured, requests went from
# ~2.5s to ~60s each, a 24x slowdown, after the day's ~5,400 calls. At that rate the two 1m
# cells still queued (`commodities 1m` 180 jobs, `us_etfs 1m` 392) price out at 3h and 6.5h,
# and they buy six days of tail on a timeframe NO book or riskmatch cell scores -- the
# pipeline's grid is 1d/4h/1h/15m/5m. They are dropped, not deferred.
#
# Dropping them is safe because the 1m cache is not stale in the way that matters. The
# freshly fetched `us_etfs 1h` and the 15m derived from the six-day-old 1m agree at a ratio
# of exactly 1.000000 across p5-p95, so no distribution was applied in between and the two
# carry ONE adjustment basis. Staleness here is a missing tail, not a wrong series.
#
# 15m and 5m ARE fetched rather than derived, and that is the right way round while the 1m
# cache is the older file: deriving would inherit its six-day-short tail, and the basis check
# above says there is nothing to lose by taking the vendor's own bars for these two sizes.
#
#   ./refresh_tail.sh
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs

# Wait out the orphaned commodities 5m rather than racing it -- one throttled client is slow,
# two are slower, and the vendor's limiter is per key, not per process.
while ls /proc/*/cmdline >/dev/null 2>&1 && \
      powershell.exe -NoProfile -Command \
        "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*commodities --tf 5m*' }).Count" \
      2>/dev/null | tr -d '\r' | grep -qv '^0$'; do
  sleep 30
done

for cell in us_etfs:15m us_etfs:5m; do
  cls="${cell%%:*}"; tf="${cell##*:}"
  t0=$SECONDS
  echo "=== $cls $tf start $(date -u +%H:%M:%S)"
  "$PY" -u td_loader.py --class "$cls" --tf "$tf" > "logs/refresh_${cls}_${tf}.log" 2>&1
  echo "=== $cls $tf exit=$? $((SECONDS - t0))s"
done
echo "=== TAIL REFRESH DONE $(date -u +%H:%M:%S) ==="
