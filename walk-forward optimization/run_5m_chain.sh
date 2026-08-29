#!/usr/bin/env bash
# The 5-minute column, end to end, behind whatever is already running.
#
# 5m HAS NEVER BEEN SCORED. Not partially -- at all: zero `wf_summary_*_5m.csv`, zero rows
# in `results.db` for tf='5m'. The only 5m artefacts on disk are open-fill book sheets from
# 08-25, built on bars that have since been refetched and repaired. So this is three stages,
# not one, and they run in dependency order:
#
#   walkforward.py   -> wf_summary_*_5m.csv    `board_rank.build_sheet` returns None without
#                                              these; the sheet cannot render at all
#   riskmatch_wf.py  -> edge_standard rows     the six-criteria verdict. THE reason to
#                                              bother -- see below
#   portfolio_wf.py  -> book_*_5m_open.csv     the ranking basis and the equity curves
#
# WHY THE VERDICT IS NOT OPTIONAL AT THIS TIMEFRAME, of all timeframes. Two of the six
# criteria are signal-free controls: R (beats an exposure-matched random) and C (beats a
# constant weight at the same average exposure). Without them a leaderboard ranks rules by
# how much time they spend in the market, which this repo has already been burned by -- a
# coin flip at matched exposure reached 8 of 25 environments while `ibs` reached 5. And 5m
# is where the look-ahead is largest: `ibs` on commodities is 5.5%/yr at 1d and 1,970%/yr
# at 15m on close fill. A 5m board without the controls is the last one you would want to
# publish.
#
# CLOSE FILL IS DELIBERATELY ABSENT and this is not an economy. At 78 bars a day a rule
# computed from a bar's own close and filled at that same close is measuring the look-ahead
# rather than the rule. There is no close-fill 5m sheet in this repo and none should be
# created; `--fill open` is the honest pessimistic bound and it is the only one run here.
#
# ORDERING IS CHEAPEST-FIRST, ON PURPOSE. The riskmatch cost of 5m is EXTRAPOLATED from the
# 15m runs by a ratio measured on the book stage -- and that ratio spans 5.1x (cme_futures)
# to 13.8x (commodities), so the total could be 6 hours or 16. `us_etfs` runs first at a
# predicted ~20 minutes purely to convert that extrapolation into a measurement before the
# expensive cells commit. Each cell that finishes is banked in results/edge_cells/, so a
# failure or a kill costs only the cell in flight.
#
#   nohup ./run_5m_chain.sh > logs/chain_5m.log 2>&1 &
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs results/edge_cells

# Six workers, not ten: these stages call `signals.position_for` directly and get no help
# from the position cache. Ten holding 5m panels is what exhausted 32 GB before.
export STOCKHUNT_WORKERS=6

busy() {  # any of OUR stages still running?
  powershell.exe -NoProfile -Command \
    "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*riskmatch_wf.py*' -or \$_.CommandLine -like '*portfolio_wf.py*' -or \$_.CommandLine -like '*make_book_rules*' -or \$_.CommandLine -like '*ingest_results*' }).Count" \
    2>/dev/null | tr -d '\r' | grep -qv '^0$'
}

echo "=== waiting for the 1d/4h/1h/15m chain $(date -u +%H:%M:%S)"
while busy; do sleep 60; done
echo "=== clear, starting 5m $(date -u +%H:%M:%S)"

echo "=== stage 1b: walkforward at 5m (cached + 10 workers, the cheap stage)"
t0=$SECONDS
STOCKHUNT_WORKERS=10 "$PY" -u walkforward.py --tf 5m > logs/wf_5m.log 2>&1
echo "=== walkforward 5m exit=$? $((SECONDS - t0))s"

echo "=== stage 1g: the verdict, one class at a time (--tf does NOT scope a run)"
for cls in us_etfs commodities us_stocks crypto cme_futures; do
  out="results/edge_cells/edge_${cls}_5m.csv"
  [ -f "$out" ] && { echo "=== $cls 5m SKIP (have it)"; continue; }
  t0=$SECONDS
  echo "=== $cls 5m start $(date -u +%H:%M:%S)"
  "$PY" -u riskmatch_wf.py --class "$cls" --tf 5m > "logs/rm_${cls}_5m.log" 2>&1
  rc=$?
  if [ $rc -eq 0 ] && [ -f results/edge_standard.partial.csv ]; then
    cp results/edge_standard.partial.csv "$out"
    echo "=== $cls 5m exit=$rc $((SECONDS - t0))s -> $(($(wc -l < "$out") - 1)) rows"
  else
    echo "=== $cls 5m FAILED exit=$rc $((SECONDS - t0))s -- see logs/rm_${cls}_5m.log"
  fi
done

echo "=== merging every cell into the verdict of record"
"$PY" -u merge_edge_standard.py > logs/merge_5m.log 2>&1
echo "=== merge exit=$?"

echo "=== regenerating book rules (5m cells now have verdicts, so they get starts)"
"$PY" -u make_book_rules.py > logs/bookrules_5m.log 2>&1
echo "=== make_book_rules exit=$?"

echo "=== stage 1h: 5m books, OPEN FILL ONLY, 3 wide"
book5m() {
  cls="$1"; start="$2"
  rules="book_rules/${cls}_5m.txt"
  [ -f "$rules" ] || { echo "=== $cls 5m book SKIP (no rule list)"; return 0; }
  t0=$SECONDS
  echo "=== $cls 5m book start $(date -u +%H:%M:%S)"
  "$PY" -u portfolio_wf.py --class "$cls" --tf 5m --pit --start "$start" --cash-rate 0 \
      --rules-file "$rules" --fill open --out "book_${cls}_5m_open.csv" \
      > "logs/book_${cls}_5m_open.log" 2>&1
  echo "=== $cls 5m book exit=$? $((SECONDS - t0))s"
}
n=0
while IFS=, read -r cls tf nr nf start; do
  [ "$cls" = "class" ] && continue
  [ "$tf" != "5m" ] && continue
  book5m "$cls" "$(echo "$start" | tr -d '\r')" &
  n=$((n + 1))
  if [ "$n" -ge 3 ]; then wait -n; n=$((n - 1)); fi
done < book_rules/starts.csv
wait

echo "=== ingesting everything into results.db"
(cd .. && "./.venv/Scripts/python" -u tools/ingest_results.py) > logs/ingest_5m.log 2>&1
echo "=== ingest exit=$?"
echo "=== 5m CHAIN DONE $(date -u +%H:%M:%S) ==="
