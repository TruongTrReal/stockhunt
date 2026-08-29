#!/usr/bin/env bash
# Finish the 1d/4h/1h/15m board after the 2026-08-27 commit-charge collapse.
#
# WHAT WENT WRONG, because the fix is structural and not "be more careful":
#
# Three chains were in flight and each waited for the others by polling the Windows process
# list through powershell.exe. That guard FAILED OPEN. Under load a `powershell.exe` spawn
# died with "fork: Resource temporarily unavailable", the pipeline produced no output, and
# `grep -qv '^0$'` on empty input exits 1 -- which the loop read as "nothing running, go".
# So the 5m chain launched while riskmatch was mid-cell, and the book chain launched while
# both were running.
#
# It was NOT RAM that ran out. RAM sat at 12 GB free the whole time. What ran out was the
# COMMIT CHARGE: 0.1 GB free of 127.8 GB, because `riskmatch_wf --class commodities --tf
# 15m` deadlocked and its six orphaned workers held 85.3 GB of commit with a ZERO working
# set -- entirely paged out, doing nothing, reserving everything. Every process that started
# afterwards died on allocation, including `make_book_rules.py` while merely importing
# pandas, and including the tooling used to diagnose it.
#
# THE FIX IS TO HAVE NO CROSS-PROCESS GUARD AT ALL. This script is one process and runs its
# phases in order. Nothing polls for anything. A guard that can fail open is worse than no
# concurrency, because the failure is silent and the symptom appears three stages later.
#
# The second fix is `commit_ok`: refuse to start a cell unless the commit charge has real
# headroom. That is the resource that actually binds here, and watching free RAM -- which
# looked healthy throughout -- would have missed the entire event.
#
#   nohup ./run_recover.sh > logs/recover.log 2>&1 &
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
mkdir -p logs results/edge_cells

# Four, not six. `commodities 15m` is the cell that deadlocked, and its universe grew from
# three symbols to five on 2026-08-26 when the platinum and palladium intraday gap was
# filled -- 67% more data per worker than when the 4,476s runtime was measured.
export STOCKHUNT_WORKERS=4

commit_free_gb() {
  powershell.exe -NoProfile -Command \
    "[math]::Floor((Get-CimInstance Win32_OperatingSystem).FreeVirtualMemory/1MB)" \
    2>/dev/null | tr -d '\r'
}

commit_ok() {   # fails CLOSED: an unreadable number is treated as "not enough"
  local g; g=$(commit_free_gb)
  case "$g" in (''|*[!0-9]*) echo "  commit charge unreadable -- refusing to start"; return 1 ;; esac
  [ "$g" -ge 25 ] && return 0
  echo "  only ${g} GB commit free -- refusing to start (need 25)"
  return 1
}

echo "=== RECOVER $(date -u +%H:%M:%S)  workers=$STOCKHUNT_WORKERS  commit_free=$(commit_free_gb)GB ==="

echo
echo "--- PHASE A: the four 15m verdicts still outstanding"
for cls in commodities us_stocks crypto cme_futures; do
  out="results/edge_cells/edge_${cls}_15m.csv"
  commit_ok || { echo "=== ABORT before $cls 15m"; exit 1; }
  t0=$SECONDS
  echo "=== $cls 15m start $(date -u +%H:%M:%S)  commit_free=$(commit_free_gb)GB"
  "$PY" -u riskmatch_wf.py --class "$cls" --tf 15m > "logs/rm_${cls}_15m.log" 2>&1
  rc=$?
  if [ $rc -eq 0 ] && [ -f results/edge_standard.partial.csv ]; then
    cp results/edge_standard.partial.csv "$out"
    echo "=== $cls 15m exit=0 $((SECONDS - t0))s -> $(($(wc -l < "$out") - 1)) rows"
  else
    echo "=== $cls 15m FAILED exit=$rc $((SECONDS - t0))s -- see logs/rm_${cls}_15m.log"
  fi
done

echo
echo "--- PHASE B: merge the cells, then regenerate rules and starts"
"$PY" -u merge_edge_standard.py > logs/recover_merge.log 2>&1
echo "=== merge exit=$?"
"$PY" -u make_book_rules.py > logs/recover_bookrules.log 2>&1
rc=$?
echo "=== make_book_rules exit=$rc  ($(($(wc -l < book_rules/starts.csv) - 1)) sheets)"
[ $rc -ne 0 ] && { echo "=== ABORT: no starts.csv means every book run below would be wrong"; exit 1; }

echo
echo "--- PHASE C: rebuild every stale book cell, 3 wide, both fills"
# 1h and 15m are stale on mtime (sheets 08-24, bars rewritten 08-26/27). commodities 1d is
# stale because of the gold/silver entry cut. cme_futures 4h and crypto 1h are NEW verdicts,
# so they have rule lists and starts for the first time.
wanted() { case "$1:$2" in *:1h|*:15m|commodities:1d|cme_futures:4h) return 0;; *) return 1;; esac; }
book_cell() {
  cls="$1"; tf="$2"; start="$3"; fill="$4"
  suf=""; [ "$fill" = "open" ] && suf="_open"
  rules="book_rules/${cls}_${tf}.txt"
  [ -f "$rules" ] || { echo "=== $cls $tf $fill SKIP (no rule list)"; return 0; }
  # Close fill owns the curves: the JSON is named from (class, tf) alone, so an open-fill
  # run passing --curves would overwrite the charts the close-fill rows are drawn against.
  curves=""; [ "$fill" = "close" ] && curves="--curves"
  t0=$SECONDS
  echo "=== $cls $tf $fill start $(date -u +%H:%M:%S)"
  "$PY" -u portfolio_wf.py --class "$cls" --tf "$tf" --pit --start "$start" --cash-rate 0 \
      --rules-file "$rules" --fill "$fill" --out "book_${cls}_${tf}${suf}.csv" $curves \
      > "logs/book_${cls}_${tf}${suf}.log" 2>&1
  echo "=== $cls $tf $fill exit=$? $((SECONDS - t0))s"
}
for fill in close open; do
  echo "--- books, $fill fill"
  n=0
  while IFS=, read -r cls tf nr nf start; do
    [ "$cls" = "class" ] && continue
    wanted "$cls" "$tf" || continue
    book_cell "$cls" "$tf" "$(echo "$start" | tr -d '\r')" "$fill" &
    n=$((n + 1))
    if [ "$n" -ge 3 ]; then wait -n; n=$((n - 1)); fi
  done < book_rules/starts.csv
  wait
done

echo
echo "--- PHASE D: ingest"
(cd .. && "./.venv/Scripts/python" -u tools/ingest_results.py) > logs/recover_ingest.log 2>&1
echo "=== ingest exit=$?"
echo "=== RECOVER DONE $(date -u +%H:%M:%S)  commit_free=$(commit_free_gb)GB ==="
