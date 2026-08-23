#!/usr/bin/env bash
# One-shot: fill the 1m gaps for the intraday HA study. Sequential on purpose --
# every call shares the same per-minute credit budget.
set -x
PY=../.venv/Scripts/python
$PY -u td_loader.py --class crypto --tf 1m --symbols XMR/USD BCH/USD XLM/USD VET/USD DOT/USD ATOM/USD HBAR/USD UNI/USD XTZ/USD LTC/USD
$PY -u td_loader.py --class us_etfs --tf 1m
$PY -u td_loader.py --class commodities --tf 1m --symbols XAU/USD XAG/USD WTI/USD
echo "FETCH BATCH DONE rc=$?"
