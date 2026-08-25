#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
echo "=== commodities 1h $(date -u +%H:%M:%S) ==="
$PY -u td_loader.py --class commodities --tf 1h --symbols XAU/USD XAG/USD WTI/USD
echo "=== crypto 1h $(date -u +%H:%M:%S) ==="
$PY -u td_loader.py --class crypto --tf 1h
echo "=== us_stocks 1h $(date -u +%H:%M:%S) ==="
$PY -u td_loader.py --class us_stocks --tf 1h
echo "=== done $(date -u +%H:%M:%S) ==="
