#!/usr/bin/env bash
# One asset class, end to end, on its own box. Started detached by fleet.sh.
#
# WHY ONE CLASS PER BOX. The account caps the monthly fee of a SINGLE instance (~$96), not
# the account total -- six $20 boxes were accepted while one $160 box was refused. So the
# fleet buys width instead of size, which suits this work: riskmatch_wf must be run one
# class at a time anyway (it decides a run is "scoped" from --class and NEVER from --tf, so
# an all-class run silently rewrites edge_standard.csv whole), and portfolio_wf is
# single-threaded per cell. Five boxes therefore finish in the time of the SLOWEST class
# rather than the sum of all five.
#
# 16 GB, SO THREE WORKERS AND NOT EIGHT. A cell peaked at 9.6 GB on a 32 GB workstation with
# six workers: ~5.7 GB in the parent holding the panel, ~1.3 GB per worker. Three workers
# lands near 9.6 GB and leaves the rest of the box as margin. This is the number that failed
# when it was optimistic, so it is set pessimistically.
#
# EACH CELL IS BANKED THE MOMENT IT FINISHES. A box that dies loses the cell in flight and
# nothing else; fleet.sh can fetch a half-finished box and still keep everything it did.
set -uo pipefail
CLS="${1:?usage: run_class.sh <asset_class>}"
cd /opt/stockhunt
PY=/opt/stockhunt/.venv/bin/python
WFO="/opt/stockhunt/walk-forward optimization"
OUT=/opt/stockhunt/wfo-results
mkdir -p "$OUT/edge_cells" "$WFO/logs" "$WFO/results/edge_cells"
export STOCKHUNT_WORKERS=4

say() { echo "[$(date -u +%H:%M:%S)] $*"; }
# Accepts a LEADING MINUS. Commit-free goes negative when the box is over-committed, and a
# digits-only test rejected that as "unreadable" -- which then refused every remaining stage
# and marked the class done with no books written. A negative number is not unreadable, it is
# the most emphatic possible "no headroom".
is_num() { case "$1" in ''|-|*[!0-9-]*|?*-*) return 1 ;; *) return 0 ;; esac; }

# Watches COMMIT as well as RAM, and fails CLOSED. On the workstation collapse free RAM read
# 12 GB the whole time; what was exhausted was the commit charge, held by orphaned workers
# with a zero working set. Watching MemAvailable alone would have missed it.
have_headroom() {
  local avail commit_free
  avail=$(awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo 2>/dev/null)
  commit_free=$(awk '/CommitLimit/ {l=$2} /Committed_AS/ {c=$2} END {if (l>0 && c>0) print int((l-c)/1048576)}' /proc/meminfo 2>/dev/null)
  is_num "$avail" || { say "  MemAvailable unreadable -- refusing"; return 1; }
  is_num "$commit_free" || { say "  commit charge unreadable -- refusing"; return 1; }
  if [ "$avail" -lt 3 ] || [ "$commit_free" -lt 3 ]; then
    return 1
  fi
  return 0
}

# WAIT for headroom rather than skip. The first version skipped a stage the moment memory was
# tight, which on a box running one big book job meant every remaining cell was skipped in the
# same second and the class reported DONE having written nothing. Memory here is transient --
# it is released when the stage before finishes -- so the right response is to queue, not to
# give up. Caps out so a genuinely wedged box still ends rather than blocking for ever.
wait_for_headroom() {
  local what="$1" waited=0
  while ! have_headroom; do
    if [ "$waited" -ge 3600 ]; then
      say "  $what: still no headroom after 60 min -- skipping"
      return 1
    fi
    [ "$((waited % 300))" -eq 0 ] && say "  $what: waiting for memory ($((waited/60))m)"
    sleep 30
    waited=$((waited + 30))
  done
  return 0
}

publish() {
  cp -f "$WFO/results"/*.csv "$OUT"/ 2>/dev/null
  cp -f "$WFO/results"/*.json "$OUT"/ 2>/dev/null
  cp -f "$WFO/results/edge_cells"/*.csv "$OUT/edge_cells"/ 2>/dev/null
  true
}

# CHUNK SIZE, and why chunking at all. Memory here scales with the NUMBER OF RULES, not the
# number of bars: one worker on a full commodities 5m sheet reached 11.4 GB and the kernel
# OOM-killed it on this 16 GB box, while a 20-rule chunk peaked at 0.5 GB and finished in
# 44s on the same data. So a cell is scored in slices, each a fresh process, and the box
# never has to hold the whole sheet at once. 50 rules across 4 workers is ~13 rules each.
CHUNK="${CHUNK:-50}"

# EVERY CHUNK MUST CARRY THE TRUE POPULATION SIZE. `_n_trials` falls back to the rules in
# hand, so a chunked run would otherwise correct for multiplicity against 50 candidates
# instead of ~420 -- riskmatch_wf warns about exactly this: "every t bar and noise ceiling
# is understated". The controls need no such care: `control_curve` builds BUYHOLD and all
# six no-signal controls itself, memoised per (sheet, symbol, side), so edge_vs_random and
# edge_vs_constant are identical whether a rule is scored alone or beside four hundred.
# Run one slice of rules. Returns 0 if it produced a partial, 1 otherwise.
# A SLICE GETS A CLOCK. Bisection is only cheap if a doomed slice fails cheaply, and these
# do not: a 50-rule slice took 29 MINUTES to fill 16 GB and reach the OOM killer, and the
# 25-rule half took another 28. Descending six levels to isolate one rule would cost hours,
# almost all of it waiting for memory to fill rather than computing.
#
# The cap is set well above honest work: successful slices here run 60-470s, so 15 minutes
# is more than twenty times the median and only bites on a slice that is going to die
# anyway. A slice killed by the clock is treated exactly like one killed by the kernel --
# split it and try the halves.
SLICE_TIMEOUT="${SLICE_TIMEOUT:-900}"

score_slice() {
  local tf="$1" names="$2" tag="$3"
  ( cd "$WFO" && timeout "$SLICE_TIMEOUT" "$PY" -u riskmatch_wf.py --class "$CLS" --tf "$tf"       --rules $names --n-trials "$POP" > "logs/rm_${CLS}_${tf}_${tag}.log" 2>&1 )
  local rc=$?
  [ $rc -eq 0 ] && [ -f "$WFO/results/edge_standard.partial.csv" ] || return 1
  # ONE FILE PER SLICE, not one shared accumulator. The accumulator was cleared on entry to
  # riskmatch(), so any restart -- to pick up a fixed script, or after a stalled pool -- threw
  # away every chunk already scored. That cost 21 minutes twice. Per-slice files make a
  # restart resume instead: `slice_done` skips what is already on disk.
  cp "$WFO/results/edge_standard.partial.csv" "$SLICES/${tag}.csv"
  return 0
}

slice_done() { [ -f "$SLICES/${1}.csv" ]; }

# ADAPTIVE BISECTION. A fixed chunk size cannot work here, because the cost is not spread
# evenly across rules: a 50-rule chunk was OOM-killed at 16 GB anon-rss while its neighbours
# finished in 63 seconds, and the difference is a handful of genuinely heavy rules --
# `lorentzian_knn` is a nearest-neighbour model, `ichimoku` carries five stacked windows --
# sitting in the same slice as ninety cheap candlestick patterns.
#
# So a failed slice is retried as two halves rather than abandoned, recursively, down to a
# single rule. The cheap rules in a poisoned chunk are recovered instead of lost with it,
# and what finally fails is ONE NAMED RULE that genuinely does not fit -- which is a fact
# worth having, not a silent hole in the sheet.
bisect() {
  local tf="$1" names="$2" depth="$3"
  local n; n=$(echo $names | wc -w)
  [ "$n" -lt 1 ] && return 0
  local tag="d${depth}_$(echo $names | md5sum | cut -c1-6)"
  if slice_done "$tag"; then say "    slice $tag already banked"; return 0; fi
  if score_slice "$tf" "$names" "$tag"; then
    return 0
  fi
  if [ "$n" -eq 1 ]; then
    say "    RULE TOO BIG FOR THIS BOX: $names"
    echo "$CLS,$tf,$names" >> "$WFO/results/oversized_rules.csv"
    return 1
  fi
  local half=$(( (n + 1) / 2 ))
  say "    slice of $n failed -- splitting into $half + $((n - half))"
  # cut -d' ', not tr. Three separate attempts to write a newline into this split were
  # mangled on the way to the file -- twice an escape collapsed into a literal newline
  # inside the quotes, and once $(printf) returned empty because command substitution
  # strips trailing newlines. cut splits on the space that is already there.
  local first last
  first=$(echo $names | cut -d" " -f1-$half)
  last=$(echo $names | cut -d" " -f$((half + 1))-)
  bisect "$tf" "$first" $((depth + 1))
  bisect "$tf" "$last" $((depth + 1))
  return 0
}

# EVERY SLICE CARRIES THE TRUE POPULATION SIZE. `_n_trials` falls back to the rules in hand,
# so a chunked run would otherwise correct for multiplicity against 50 candidates instead of
# ~420 -- riskmatch_wf warns about exactly this: "every t bar and noise ceiling is
# understated". The controls need no such care: `control_curve` builds BUYHOLD and all six
# no-signal controls itself, memoised per (sheet, symbol, side), so edge_vs_random and
# edge_vs_constant are identical whether a rule is scored alone or beside four hundred.
CHUNK="${CHUNK:-50}"

riskmatch() {
  local tf="$1"
  local out="$WFO/results/edge_cells/edge_${CLS}_${tf}.csv"
  local t0=$SECONDS
  [ -f "$out" ] && { say "SKIP $CLS $tf (banked)"; return 0; }
  wait_for_headroom "riskmatch $CLS $tf" || return 0

  local rules_file="$WFO/rules_${CLS}_${tf}.txt"
  ( cd "$WFO" && "$PY" rules_for.py "$CLS" "$tf" > "$rules_file" 2>/dev/null )
  POP=$(grep -cve '^$' "$rules_file" 2>/dev/null || echo 0)
  if [ "${POP:-0}" -lt 1 ]; then
    say "  $CLS $tf: no rule population (no wf_summary yet) -- skipping"
    return 0
  fi
  SLICES="$WFO/results/slices_${CLS}_${tf}"
  mkdir -p "$SLICES"
  local nchunks=$(( (POP + CHUNK - 1) / CHUNK ))
  say "riskmatch $CLS $tf: $POP rules in $nchunks chunks of $CHUNK"

  local i=0 n=0
  while [ "$i" -lt "$POP" ]; do
    n=$((n + 1))
    local names
    names=$(sed -n "$((i + 1)),$((i + CHUNK))p" "$rules_file" | tr '
' ' ')
    if slice_done "$n"; then say "  chunk $n/$nchunks already banked -- skipping"; i=$((i + CHUNK)); continue; fi
    wait_for_headroom "chunk $n" || break
    if score_slice "$tf" "$names" "$n"; then
      say "  chunk $n/$nchunks ok ($((SECONDS-t0))s)"
    else
      say "  chunk $n/$nchunks failed -- splitting to save the rules that do fit"
      # SPLIT FIRST, do not hand the whole slice back to bisect. bisect begins by scoring
      # what it is given, so passing the slice that just failed makes it fail again before
      # splitting -- measured at 28 wasted minutes on crypto 15m chunk 6, re-learning what
      # the caller already knew.
      local w half f1 f2
      w=$(echo $names | wc -w)
      half=$(( (w + 1) / 2 ))
      f1=$(echo $names | cut -d" " -f1-$half)
      f2=$(echo $names | cut -d" " -f$((half + 1))-)
      bisect "$tf" "$f1" 1
      bisect "$tf" "$f2" 1
      say "  chunk $n/$nchunks recovered ($((SECONDS-t0))s)"
    fi
    i=$((i + CHUNK))
  done

  # Stitch the slices: header from the first, rows from all of them.
  local first=1
  rm -f "$out.tmp"
  for sf in "$SLICES"/*.csv; do
    [ -f "$sf" ] || continue
    if [ "$first" = "1" ]; then cat "$sf" > "$out.tmp"; first=0
    else tail -n +2 "$sf" >> "$out.tmp"; fi
  done
  if [ -f "$out.tmp" ] && [ "$(wc -l < "$out.tmp")" -gt 1 ]; then
    mv "$out.tmp" "$out"
    say "  $CLS $tf DONE $((SECONDS-t0))s rows=$(($(wc -l < "$out") - 1))"
  else
    say "  $CLS $tf produced nothing"
  fi
  publish
  return 0
}

book() {
  local tf="$1" start="$2" fill="$3"
  local suf="" curves=""
  local t0=$SECONDS
  [ "$fill" = "open" ] && suf="_open"
  [ -f "$WFO/book_rules/${CLS}_${tf}.txt" ] || { say "SKIP book $CLS $tf (no rule list)"; return 0; }
  # Close fill owns the curves: the JSON is named from (class, tf) alone, so an open-fill
  # run passing --curves overwrites the charts the close-fill rows are drawn against.
  [ "$fill" = "close" ] && curves="--curves"
  wait_for_headroom "book $CLS $tf $fill" || return 0
  say "book $CLS $tf $fill ..."
  ( cd "$WFO" && "$PY" -u portfolio_wf.py --class "$CLS" --tf "$tf" --pit --start "$start" \
      --cash-rate 0 --rules-file "book_rules/${CLS}_${tf}.txt" --fill "$fill" \
      --out "book_${CLS}_${tf}${suf}.csv" $curves > "logs/book_${CLS}_${tf}${suf}.log" 2>&1 )
  say "  $CLS $tf $fill rc=$? $((SECONDS-t0))s"
  publish
  return 0
}

say "=== $CLS START  workers=$STOCKHUNT_WORKERS"
free -g | head -2
nproc

# --- verdicts -----------------------------------------------------------------------------
# 15m before 5m: 15m is the cheaper of the two and its runtime is the only measurement that
# can calibrate 5m. The 5m cost is extrapolated from 15m by a ratio measured on the book
# stage, and that ratio spans 5.1x to 13.8x across classes -- so finishing 15m first turns a
# guess into a number while the expensive cell has not yet committed.
# WALKFORWARD BEFORE THE VERDICTS, and the order is load-bearing rather than tidy.
#
# `riskmatch_wf` takes its candidate set from `leaderboard_universe`, which reads the rule
# names straight out of `wf_summary_*` and `cwf_summary_*` and only falls back to CATALOG
# when those are missing. No 5m sheet has ever been written, so running the verdict first
# scores THE PUBLISHED STRATEGIES ONLY: measured on this box, us_stocks 5m resolved to 175
# rules beginning `atr_chandelier, bar_updn` where 15m resolves to 420 beginning `MININDEX,
# MAXINDEX, TRIX` -- the 231 TA-Lib rules that are most of the leaderboard simply absent.
#
# It is worse than a smaller sheet. `--n-trials` would then correct for multiplicity against
# a search of 175 when the real one is 420, so every t bar and noise ceiling on the 5m column
# would be understated, and the 5m sheet would not cover the same population as the 15m one
# it gets compared against.
#
# walkforward is also the cheap stage -- cached, ten workers, the whole 15m+1h sweep across
# eight cells took 6m40s -- so there is no cost to putting it first.
say "--- walkforward 5m (writes the wf_summary the verdict takes its population from)"
if [ ! -f "$WFO/results/wf_summary_${CLS}_5m.csv" ]; then
  if wait_for_headroom "walkforward $CLS 5m"; then
    ( cd "$WFO" && STOCKHUNT_WORKERS=6 "$PY" -u walkforward.py --class "$CLS" --tf 5m         > "logs/wf_${CLS}_5m.log" 2>&1 )
    say "  walkforward 5m rc=$?"
  fi
  publish
else
  say "  wf_summary_${CLS}_5m.csv already present"
fi

say "--- verdicts"
riskmatch 15m
riskmatch 5m

# --- rules -------------------------------------------------------------------------------
# make_book_rules reads its rule lists and every --start out of edge_standard.csv, so the
# cells this box just produced have to be merged into it first. Only this class matters
# here: the sheet is per (class, timeframe), so a box that knows nothing about the other
# four still writes correct lists and starts for its own.
say "--- merge + book rules"
( cd "$WFO" && "$PY" -u merge_edge_standard.py > logs/merge.log 2>&1 ); say "  merge rc=$?"
( cd "$WFO" && "$PY" -u make_book_rules.py > logs/bookrules.log 2>&1 ); rc=$?
say "  make_book_rules rc=$rc"
if [ $rc -ne 0 ]; then
  say "ABORT: without starts.csv every book below would be scored on the wrong window"
  publish; echo DONE > "$OUT/.finished"; exit 1
fi
publish

# --- books --------------------------------------------------------------------------------
# 1h and 15m are stale (sheets from 08-24, bars rewritten 08-26/27); commodities 1d is stale
# from the gold/silver entry cut; cme_futures 4h and everything at 5m have a verdict for the
# first time.
#
# 5m IS OPEN FILL ONLY. At 78 bars a day a close-fill number measures the look-ahead rather
# than the rule -- ibs on commodities reads 5.5 pct/yr at 1d and 1970 pct/yr at 15m on close
# fill. No close-fill 5m sheet exists in this repo and none should be created.
# BOOKS ARE OFF BY DEFAULT ON A 16 GB BOX, and this is measured rather than cautious.
# `portfolio_wf` holds the whole cell in pandas and does not chunk the way riskmatch does:
# commodities 15m -- five symbols, the SMALLEST cell in the grid -- reached 15.9 GB resident
# with 5.5 GB swapped and sat at 87% CPU IDLE for 47 minutes, paging rather than computing.
#
# It could be chunked like the verdicts, but not safely in a hurry: `--n-trials` and
# `--trial-dispersion` must be passed TOGETHER (supplying the count while letting the spread
# be estimated from a handful of rules lowers the bar instead of raising it, inverting the
# correction), and `--curves` writes one JSON per (class, tf) that would need merging across
# chunks. Wrong numbers that look right are worse than numbers computed somewhere else.
#
# So the split is by what each machine is good at: the rented boxes do the VERDICTS, which
# chunk cleanly and are the expensive part, and the 32 GB workstation does the BOOKS, which
# already complete there. Set BOOKS=1 to override.
if [ "${BOOKS:-0}" != "1" ]; then
  say "--- books SKIPPED (BOOKS=1 to enable; portfolio_wf does not fit 16 GB)"
else
say "--- books"
while IFS=, read -r c tf nr nf start; do
  [ "$c" = "class" ] && continue
  [ "$c" != "$CLS" ] && continue
  start=$(echo "$start" | tr -d '\r')
  fills=""
  case "$tf" in
    1h|15m) fills="close open" ;;
    5m)     fills="open" ;;
    1d)     [ "$CLS" = "commodities" ] && fills="close open" ;;
    4h)     [ "$CLS" = "cme_futures" ] && fills="close open" ;;
  esac
  [ -z "$fills" ] && continue
  for f in $fills; do book "$tf" "$start" "$f"; done
done < "$WFO/book_rules/starts.csv"
fi

# Nothing may still be running when this is written: fleet.sh treats .finished as licence to
# fetch and DESTROY, and a box torn down mid-book throws away the cell it was computing.
while pgrep -f "riskmatch_wf.py|portfolio_wf.py" >/dev/null 2>&1; do
  say "waiting for the last stage to finish before signalling done"
  sleep 60
done
publish
# fleet.sh polls for this file: it is the signal to fetch this box and destroy it, so a box
# stops costing money the moment ITS class is done rather than when the slowest one is.
echo DONE > "$OUT/.finished"
say "=== $CLS DONE"
free -g | head -2
