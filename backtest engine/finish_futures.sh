#!/usr/bin/env bash
# Wait out the Databento 1m pull, then finish and verify the futures class.
#
# WHY THIS IS CHAINED RATHER THAN RUN NOW: futures 4h, 15m and 5m are RESAMPLED from the 1m
# cache, not fetched -- Databento serves only ohlcv-1m and ohlcv-1h (see `db_intraday.SCHEMA`),
# and everything between is derived so the whole class shares one basis. Deriving them from
# the cache that is mid-rewrite would bake in a half-written series, so each step below waits
# for the one before it.
#
# The 1m pull is slow for a reason worth writing down: 16 roots x 10 years of minute bars is
# ~3.7M bars PER ROOT, and the client streams the whole range before it writes anything, so
# the log sits silent at zero roots for hours while several GB arrive. A silent log here is
# not a hung job -- check ReadTransferCount on the process, not the log.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs

echo "=== waiting for the 1m pull, $(date -u +%H:%M:%S)"
while [ "$(grep -c 'kept,' logs/refresh_futures_1m.log 2>/dev/null)" -lt 16 ]; do
  if ! powershell.exe -NoProfile -Command \
      "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*db_intraday*' }).Count" \
      2>/dev/null | tr -d '\r' | grep -qv '^0$'; then
    echo "=== 1m pull is no longer running and did not reach 16 roots -- STOPPING"
    echo "    roots completed: $(grep -c 'kept,' logs/refresh_futures_1m.log 2>/dev/null)"
    exit 1
  fi
  sleep 60
done
echo "=== 1m pull complete, $(date -u +%H:%M:%S)  ($(grep -c 'kept,' logs/refresh_futures_1m.log) roots)"

# 4h is in the derivable set for a 24-hour class -- the "570 rule" that forbids it on
# exchange-local classes does not apply to CME, whose session does not open at 09:30 ET.
echo "=== resampling 15m/5m/4h from the fresh 1m"
"$PY" -u resample_intraday.py --class cme_futures --tf 5m 15m 4h \
    > logs/finish_futures_resample.log 2>&1
echo "=== resample exit=$?"

echo "=== spike scan (dry run first -- this class carries the negative-oil week)"
"$PY" -u repair_spikes.py --class cme_futures --tf 1d 4h 1h 15m 5m --dry-run \
    > logs/finish_futures_spikes.log 2>&1
echo "=== spike scan exit=$?"

echo "=== intraday vs daily"
"$PY" -u verify_intraday_vs_daily.py --class cme_futures --tf 4h 1h 15m 5m \
    > logs/finish_futures_verify.log 2>&1
echo "=== verify exit=$?"

echo "=== FUTURES FINISHED $(date -u +%H:%M:%S) ==="
