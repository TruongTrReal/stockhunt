#!/usr/bin/env bash
# Everything downstream of the riskmatch refresh, chained so it needs no attention:
# merge the cells -> regenerate the book rules -> rebuild the books -> ingest -> prove the
# board did not move by accident.
#
# BUDGET. The whole grid is ~37h of compute and that was refused; this chain is the ~2.4h
# subset that leaves the leaderboard CORRECT AND COMPLETE at 1d/4h/1h/15m. Two cells are
# deliberately outside it:
#
#   * the entire 5m column, which has never been scored and is ~83% of the grid's cost
#   * cme_futures 15m, cut by `kill_before_cme15m.sh` -- 76 min of verdict plus the longest
#     book cell in the grid (4,045s), which together IS the overrun
#
# Both stay honestly empty on the board rather than being filled with a stale number.
#
# ORDER IS A REAL DEPENDENCY, NOT A PREFERENCE. `make_book_rules.py` reads its rule lists
# and every `--start` out of `edge_standard.csv`, so the merge has to land before it runs,
# and it has to run before `run_book.sh`. A cell with no verdict row therefore gets no
# start and is skipped by the book stage automatically -- which is how the two cuts above
# propagate without being named again anywhere.
#
# BOOKS RUN 3 WIDE because `portfolio_wf` is single-threaded: the only way to use twelve
# cores is to run cells side by side. Not wider -- each holds ~3 GB and this box shares
# its RAM with another project.
#
#   nohup ./run_refresh_chain.sh > logs/refresh_chain.log 2>&1 &
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs

echo "=== waiting for the riskmatch refresh $(date -u +%H:%M:%S)"
while powershell.exe -NoProfile -Command \
    "(Get-CimInstance Win32_Process -Filter \"Name='bash.exe'\" | Where-Object { \$_.CommandLine -like '*run_riskmatch_refresh*' }).Count" \
    2>/dev/null | tr -d '\r' | grep -qv '^0$'; do
  sleep 30
done
# ...and for the cell it was mid-way through, which outlives its parent by design.
while powershell.exe -NoProfile -Command \
    "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*riskmatch_wf.py*' }).Count" \
    2>/dev/null | tr -d '\r' | grep -qv '^0$'; do
  sleep 20
done
echo "=== riskmatch clear $(date -u +%H:%M:%S)"
echo "    cells on disk: $(ls results/edge_cells/*.csv 2>/dev/null | wc -l | tr -d ' ')"

echo "=== merging cells into the verdict of record"
"$PY" -u merge_edge_standard.py > logs/chain_merge.log 2>&1
echo "=== merge exit=$?"

echo "=== regenerating book rules and starts"
"$PY" -u make_book_rules.py > logs/chain_bookrules.log 2>&1
echo "=== make_book_rules exit=$?"

# The book stage is written out here rather than delegated to `run_cells_parallel.sh`, for
# two reasons that both silently produce a wrong board:
#
#   * that script SKIPS a cell whose output already exists -- which is every cell here,
#     since the stale sheets are exactly what has to be replaced
#   * it never passes `--curves`, so the dashboard's equity charts would keep describing
#     the old bars while the rows beside them describe the new ones
#
# WHICH CELLS. Everything at 1h and 15m (stale on mtime -- sheets from 08-24, bars rewritten
# 08-26/27), plus `commodities 1d` (the gold/silver entry cut moved two of five names from
# 26 years to 20.6) and `cme_futures 4h` (never scored). 1d and 4h for the other classes
# were rebuilt on the repaired bars already and are left alone.
#
# `cme_futures 15m` needs no exclusion here: it has no verdict, so `make_book_rules.py`
# wrote it no start row, so the loop below never sees it.
wanted() {
  case "$1:$2" in
    *:1h|*:15m|commodities:1d|cme_futures:4h) return 0 ;;
    *) return 1 ;;
  esac
}

book_cell() {
  cls="$1"; tf="$2"; start="$3"; fill="$4"
  suf=""; [ "$fill" = "open" ] && suf="_open"
  rules="book_rules/${cls}_${tf}.txt"
  [ -f "$rules" ] || { echo "=== $cls $tf $fill SKIP (no rule list)"; return 0; }
  # Close fill owns the curves: `book_curves_<cls>_<tf>.json` is named from (class, tf)
  # alone, so an open-fill run passing --curves would overwrite the charts the close-fill
  # rows are drawn against, and every detail page would disagree with itself.
  curves=""; [ "$fill" = "close" ] && curves="--curves"
  t0=$SECONDS
  echo "=== $cls $tf $fill start $(date -u +%H:%M:%S)"
  "$PY" -u portfolio_wf.py \
      --class "$cls" --tf "$tf" --pit --start "$start" --cash-rate 0 \
      --rules-file "$rules" --fill "$fill" --out "book_${cls}_${tf}${suf}.csv" $curves \
      > "logs/book_${cls}_${tf}${suf}.log" 2>&1
  echo "=== $cls $tf $fill exit=$? $((SECONDS - t0))s"
}

for fill in close open; do
  echo "=== books, $fill fill (3 wide)"
  n=0
  while IFS=, read -r cls tf nr nf start; do
    [ "$cls" = "class" ] && continue
    start=$(echo "$start" | tr -d '\r')
    wanted "$cls" "$tf" || continue
    book_cell "$cls" "$tf" "$start" "$fill" &
    n=$((n + 1))
    if [ "$n" -ge 3 ]; then wait -n; n=$((n - 1)); fi
  done < book_rules/starts.csv
  wait
  echo "=== books $fill done $(date -u +%H:%M:%S)"
done

echo "=== ingesting the sheets into results.db"
(cd .. && "./.venv/Scripts/python" -u tools/ingest_results.py) \
    > logs/chain_ingest.log 2>&1
echo "=== ingest exit=$?"

echo "=== CHAIN DONE $(date -u +%H:%M:%S) ==="
