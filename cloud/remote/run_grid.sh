#!/usr/bin/env bash
# The whole grid, ON the rented box. Started detached by grid.sh; survives the SSH session.
#
# THIS SCRIPT IS DELIBERATELY SERIAL AND HAS NO CROSS-PROCESS GUARD. On 2026-08-27 three
# chains ran on the workstation, each waiting for the others by polling the process list.
# That guard FAILED OPEN: under load the poll itself failed to spawn, produced no output,
# and "no output" was read as "nothing is running". All three then ran at once, one cell
# deadlocked, its orphaned workers held 85 GB of commit with a zero working set, and every
# process afterwards died on allocation -- including make_book_rules.py while importing
# pandas. One process running phases in order cannot make that mistake.
#
# THE RESOURCE THAT BINDS IS COMMIT, NOT RAM. Free RAM read 12 GB throughout that failure.
# have_headroom therefore watches MemAvailable AND the commit charge, and fails CLOSED: an
# unreadable number stops the run rather than waving it through.
#
# EVERY CELL IS BANKED THE MOMENT IT FINISHES, into results/edge_cells/. riskmatch_wf
# rewrites edge_standard.csv WHOLE unless the run is scoped, and scoping is judged on
# --class/--rules and NEVER on --tf, so one class at a time is not a style choice: the
# obvious command would delete every cell it did not just score. Banking per cell also
# means a crash costs one cell, not the run.
#
# ORDER: cheapest first, 5m last. The 5m riskmatch cost is EXTRAPOLATED from 15m by a ratio
# measured on the book stage, and that ratio spans 5.1x to 13.8x across classes -- so 5m
# could be 6 hours or 16. us_etfs runs first at every timeframe to turn that extrapolation
# into a measurement before the expensive cells commit.
set -uo pipefail
cd /opt/stockhunt
PY=/opt/stockhunt/.venv/bin/python
WFO="/opt/stockhunt/walk-forward optimization"
OUT=/opt/stockhunt/wfo-results
mkdir -p "$OUT" "$WFO/logs" "$WFO/results/edge_cells"

# 16 vCPU on this box, but these stages call signals.position_for directly and get no help
# from the position cache, so each worker holds its own copy of the panel. Eight is the
# width that fits 64 GB with room for the parent.
export STOCKHUNT_WORKERS=8

say() { echo "[$(date -u +%H:%M:%S)] $*"; }

is_num() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }

have_headroom() {
  local avail commit_free
  avail=$(awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo 2>/dev/null)
  commit_free=$(awk '/CommitLimit/ {l=$2} /Committed_AS/ {c=$2} END {if (l>0 && c>0) print int((l-c)/1048576)}' /proc/meminfo 2>/dev/null)
  if ! is_num "$avail"; then say "  MemAvailable unreadable -- refusing"; return 1; fi
  if ! is_num "$commit_free"; then say "  commit charge unreadable -- refusing"; return 1; fi
  if [ "$avail" -lt 12 ] || [ "$commit_free" -lt 12 ]; then
    say "  headroom too low (avail ${avail}GB, commit ${commit_free}GB) -- refusing"
    return 1
  fi
  return 0
}

riskmatch() {
  local cls="$1" tf="$2" out="$WFO/results/edge_cells/edge_${cls}_${tf}.csv"
  if [ -f "$out" ]; then say "SKIP $cls $tf (already banked)"; return 0; fi
  have_headroom || { say "SKIP $cls $tf -- no headroom"; return 0; }
  local t0=$SECONDS rc
  say "riskmatch $cls $tf ..."
  ( cd "$WFO" && "$PY" -u riskmatch_wf.py --class "$cls" --tf "$tf" > "logs/rm_${cls}_${tf}.log" 2>&1 )
  rc=$?
  if [ $rc -eq 0 ] && [ -f "$WFO/results/edge_standard.partial.csv" ]; then
    cp "$WFO/results/edge_standard.partial.csv" "$out"
    say "  $cls $tf ok $((SECONDS-t0))s rows=$(($(wc -l < "$out") - 1))"
  else
    say "  $cls $tf FAILED rc=$rc $((SECONDS-t0))s"
  fi
  return 0
}

book() {
  local cls="$1" tf="$2" start="$3" fill="$4" suf="" curves="" t0=$SECONDS
  [ "$fill" = "open" ] && suf="_open"
  [ -f "$WFO/book_rules/${cls}_${tf}.txt" ] || { say "SKIP book $cls $tf (no rule list)"; return 0; }
  # Close fill owns the curves: book_curves_<cls>_<tf>.json is named from (class, tf) alone,
  # so an open-fill run passing --curves would overwrite the charts the close-fill rows are
  # drawn against, and every detail page would disagree with itself.
  [ "$fill" = "close" ] && curves="--curves"
  say "book $cls $tf $fill ..."
  ( cd "$WFO" && "$PY" -u portfolio_wf.py --class "$cls" --tf "$tf" --pit --start "$start" \
      --cash-rate 0 --rules-file "book_rules/${cls}_${tf}.txt" --fill "$fill" \
      --out "book_${cls}_${tf}${suf}.csv" $curves > "logs/book_${cls}_${tf}${suf}.log" 2>&1 )
  say "  $cls $tf $fill rc=$? $((SECONDS-t0))s"
  return 0
}

# Publish after every step, so a fetch mid-run is always a consistent snapshot rather than
# whatever happened to be half-written.
publish() {
  cp -f "$WFO/results"/*.csv "$OUT"/ 2>/dev/null
  cp -f "$WFO/results"/*.json "$OUT"/ 2>/dev/null
  mkdir -p "$OUT/edge_cells"
  cp -f "$WFO/results/edge_cells"/*.csv "$OUT/edge_cells"/ 2>/dev/null
  true
}

say "=== GRID START workers=$STOCKHUNT_WORKERS"
free -g | head -2

say "--- phase 1: the 15m verdicts"
for cls in us_etfs commodities us_stocks crypto cme_futures; do
  riskmatch "$cls" 15m
  publish
done

# board_rank.build_sheet returns None without wf_summary rows, so a 5m leaderboard cannot
# render at all until this runs. It is the cheap stage: cached, ten workers, and the whole
# 15m+1h sweep across eight cells took 6m40s.
say "--- phase 2: walkforward at 5m"
if have_headroom; then
  ( cd "$WFO" && STOCKHUNT_WORKERS=10 "$PY" -u walkforward.py --tf 5m > logs/wf_5m.log 2>&1 )
  say "  walkforward 5m rc=$?"
fi
publish

say "--- phase 3: the 5m verdicts"
for cls in us_etfs commodities us_stocks crypto cme_futures; do
  riskmatch "$cls" 5m
  publish
done

say "--- phase 4: merge and regenerate the rule lists"
( cd "$WFO" && "$PY" -u merge_edge_standard.py > logs/merge.log 2>&1 )
say "  merge rc=$?"
( cd "$WFO" && "$PY" -u make_book_rules.py > logs/bookrules.log 2>&1 )
rc=$?
say "  make_book_rules rc=$rc"
if [ $rc -ne 0 ]; then
  say "ABORT: without starts.csv every book run below would be scored on the wrong window"
  publish
  exit 1
fi
publish

say "--- phase 5: books"
# Rebuild what the data work invalidated (1h, 15m), what a cut invalidated (commodities 1d),
# and what has a verdict for the first time (cme_futures 4h, and everything at 5m).
# 5m IS OPEN FILL ONLY: at 78 bars a day a close-fill number measures the look-ahead rather
# than the rule -- ibs on commodities reads 5.5 pct/yr at 1d and 1970 pct/yr at 15m on close
# fill. There is no close-fill 5m sheet in this repo and none should be created.
while IFS=, read -r cls tf nr nf start; do
  [ "$cls" = "class" ] && continue
  start=$(echo "$start" | tr -d '\r')
  fills=""
  case "$tf" in
    1h|15m) fills="close open" ;;
    5m)     fills="open" ;;
    1d)     [ "$cls" = "commodities" ] && fills="close open" ;;
    4h)     [ "$cls" = "cme_futures" ] && fills="close open" ;;
  esac
  [ -z "$fills" ] && continue
  for f in $fills; do
    have_headroom && book "$cls" "$tf" "$start" "$f"
  done
  publish
done < "$WFO/book_rules/starts.csv"

say "--- phase 6: ingest into results.db"
( cd /opt/stockhunt && "$PY" -u tools/ingest_results.py > "$WFO/logs/ingest.log" 2>&1 )
say "  ingest rc=$?"
cp -f "$WFO/results/results.db" "$OUT/" 2>/dev/null
publish

say "=== GRID DONE"
free -g | head -2
