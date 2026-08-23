#!/usr/bin/env bash
# Recovery fetch, 2026-08-21: `WINDOWS` had no intraday entry for us_etfs or
# commodities, so the original 1m batch died on a bare KeyError for both classes and
# wrote nothing. Entries added to config.py; this refetches what was lost.
set -x
PY=../.venv/Scripts/python
$PY -u td_loader.py --class us_etfs --tf 1m
$PY -u td_loader.py --class us_etfs --tf 5m
$PY -u td_loader.py --class commodities --tf 1m --symbols XAU/USD XAG/USD WTI/USD
$PY -u td_loader.py --class commodities --tf 5m --symbols XAU/USD XAG/USD WTI/USD
$PY -u check_data.py --fix
$PY -u resample_intraday.py --class us_etfs commodities --tf 2m 3m
echo "ETF/COMMODITY INTRADAY FETCH DONE rc=$?"
