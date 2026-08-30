#!/usr/bin/env bash
# Stage 2b at 1h, 15m and 5m -- the pairs the intraday board has never had.
#
# WHY THE INTRADAY BOARD IS SHORTER THAN THE DAILY ONE, and it is not a defect. The 1d and
# 4h sheets carry a 560-rule population; 1h, 15m and 5m carry 420, and the whole 140-rule
# difference is PAIRS -- `CDLDOJI~CORREL|vote` and its family. Not one single-rule strategy
# is missing at 1h. `combo_wf.py` has only ever been run at 1d and 4h, so the intraday cells
# were singles-only by construction and the board could not say so.
#
# THE CHAIN IS FOUR STAGES AND THE ORDER IS A DEPENDENCY, not a preference:
#
#   combo_wf.py   -> cwf_<cls>_<tf>.csv    the pairs exist and have walk-forward scores
#   gap_rules.py  -> they are now IN `leaderboard_universe`, and so are missing verdicts
#   riskmatch     -> edge_standard rows    without one, `board_rank` DROPS the row
#   portfolio_wf  -> book_<cls>_<tf>.csv   the money columns the board ranks on
#
# Skipping any of the last three puts the pairs in a results CSV and nowhere the board can
# see them, which is the state `cme_futures 4h` was in for a day.
#
# A PAIR IS A FRESH SEARCH over K-choose-2 x 4 operators, so the multiplicity bar moves
# while you look. `--top-k 8` is what 1d and 4h were run at; keeping it identical is what
# makes an intraday pair comparable to a daily one at all.
#
# 5m IS THE EXPENSIVE ONE and goes last, so a kill costs the cheapest cells nothing. Every
# stage banks per class, so nothing here has to be restarted whole.
#
#   nohup ./run_pairs_intraday.sh > logs/pairs_intraday.log 2>&1 &
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs
export STOCKHUNT_WORKERS=6

echo "=== waiting for the verdict gap filler $(date -u +%H:%M:%S)"
while powershell.exe -NoProfile -Command \
    "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*riskmatch_wf.py*' -or \$_.CommandLine -like '*portfolio_wf.py*' }).Count" \
    2>/dev/null | tr -d '\r' | grep -qv '^0$'; do
  sleep 60
done
echo "=== clear $(date -u +%H:%M:%S)"

for tf in 1h 15m 5m; do
  for cls in us_stocks us_etfs crypto commodities cme_futures; do
    out="results/cwf_${cls}_${tf}.csv"
    if [ -f "$out" ]; then echo "=== $cls $tf SKIP (have $(basename "$out"))"; continue; fi
    t0=$SECONDS
    echo "=== combo_wf $cls $tf start $(date -u +%H:%M:%S)"
    "$PY" -u combo_wf.py --class "$cls" --tf "$tf" --top-k 8 \
        > "logs/cwf_${cls}_${tf}.log" 2>&1
    echo "=== combo_wf $cls $tf exit=$? $((SECONDS - t0))s"
  done
done

echo "=== PAIRS SCORED $(date -u +%H:%M:%S)."
echo "    Next: ./run_fill_verdict_gaps.sh (the pairs now need verdicts),"
echo "    then the books for every widened cell, then tools/ingest_results.py."
