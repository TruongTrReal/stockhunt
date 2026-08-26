#!/usr/bin/env bash
# Does the WINDOW CONVENTION change the verdict on sp100_momentum?
#
# The repo reads a strategy's windows as calendar DAYS; StrategyQuant's `RSI(close, 2)`
# is a BAR COUNT. On 1d equities and ETFs the two coincide exactly (50/200/2/14, 100%
# position agreement) so the published cell is faithful. Nowhere else: RSI(2) becomes
# RSI(3) on crypto 1d, RSI(4) on equity 4h and RSI(17) on crypto 4h, and at 4h the two
# readings agree on only 60-80% of bars with up to 6x the turnover.
#
# `chart:` pins the Pine/SQ bar-count reading. It cannot go through `strat_wf --rules`,
# which decodes a label and rejects an overlay prefix, so this is a BOOK run per sheet --
# the same route `run_intraday_ha.sh` takes for the same reason. Both readings and the
# signal-free controls in one panel, so the comparison is inside one run.
#
# All six cells were registered in data/reference/trials.csv before this was launched.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs
CONTROLS="BUYHOLD RANDOM_25 RANDOM_50 RANDOM_75 ALWAYS_FLAT"
while IFS=, read -r cls tf nr nf start; do
  [ "$cls" = "class" ] && continue
  case "$cls/$tf" in
    us_stocks/4h|us_etfs/4h|crypto/4h|commodities/4h|cme_futures/1d|crypto/1d) ;;
    *) continue ;;
  esac
  start=$(echo "$start" | tr -d '\r')
  out="sp100_window_${cls}_${tf}.csv"
  t0=$SECONDS
  echo "=== $cls $tf start $(date -u +%H:%M:%S) --start $start"
  "$PY" -u portfolio_wf.py --class "$cls" --tf "$tf" --pit --start "$start" \
      --cash-rate 0 --rules sp100_momentum chart:sp100_momentum $CONTROLS \
      --out "$out" > "logs/sp100_window_${cls}_${tf}.log" 2>&1
  echo "=== $cls $tf exit=$? $((SECONDS - t0))s"
done < book_rules/starts.csv
echo "=== WINDOW CHECK DONE $(date -u +%H:%M:%S) ==="
