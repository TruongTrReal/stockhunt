#!/usr/bin/env bash
# Book-level (portfolio) scores for EVERY rule on the leaderboard, on the walk-forward
# out-of-sample span, for all four classes at 1d and 4h.
#
# Written 2026-08-13. The dashboard leaderboard reports per-ASSET medians over each
# name's own out-of-sample bars -- a median single stock, ~12 years for us_stocks. That
# is a fair comparison (every column is the same shape) but it is not what an account
# holding the whole book would have made. These runs supply that second number.
#
# BASH, NOT POWERSHELL, and that is deliberate -- see the header of run_top100.sh. PS 5.1
# turns numpy's harmless RuntimeWarning into a terminating error, `*>` buffers until exit,
# and orphaned multiprocessing workers hold the redirected handle so the launcher never
# returns. A dead job that looks busy for 83 minutes.
#
# --start IS THE WHOLE POINT and it differs per sheet. It is fold 0's `is_end` from
# `walkforward.generate_folds` over that class's union span -- the first bar that was ever
# out-of-sample. Without it the book is scored on the full history including the bars the
# rules were selected on, and ranking on that is ranking on in-sample fit. The dates come
# from `make_book_rules.py`, which writes them to book_rules/starts.csv beside the rule
# lists; regenerate both together or they drift apart.
#
#   nohup ./run_book.sh > logs/book_driver.log 2>&1 &
set -u
cd "$(dirname "$0")"
W="$(pwd)"
PY="$W/../.venv/Scripts/python.exe"
mkdir -p logs

# Six, not the default ten: these stages call `signals.position_for` directly, get no help
# from the position cache, and ten workers drove free memory to 3 GB of 32 GB on this box.
export STOCKHUNT_WORKERS=6

# class tf oos_start -- keep in step with book_rules/starts.csv
# THE SHEET LIST IS READ, NOT TYPED (2026-08-23). It used to be a hardcoded array of
# nine `class tf start` rows, and the file's own header already admitted the hazard: the
# dates duplicate what `make_book_rules.py` computes and writes to `book_rules/starts.csv`,
# "regenerate both together or they drift apart". Two copies of a number that must agree
# is a drift waiting to happen, and it also meant every new (class, timeframe) had to be
# remembered here by hand -- when the axis grew to 1d/4h/1h/15m across five classes the
# array would have silently booked nine cells out of twenty and reported success.
#
# `starts.csv` is written by the stage immediately upstream and carries exactly the three
# fields this loop needs, so it is the source now and the array is gone.
STARTS="book_rules/starts.csv"
if [ ! -f "$STARTS" ]; then
  echo "no $STARTS -- run make_book_rules.py first"; exit 1
fi
mapfile -t SHEETS < <(tail -n +2 "$STARTS" | awk -F, 'NF>=5 {print $1" "$2" "$5}' | tr -d '')

fail=0
for row in "${SHEETS[@]}"; do
  set -- $row
  cls="$1"; tf="$2"; start="$3"
  rules="book_rules/${cls}_${tf}.txt"
  if [ ! -f "$rules" ]; then
    echo "=== $cls $tf SKIPPED -- no $rules (run make_book_rules.py)"; fail=1; continue
  fi
  t0=$SECONDS
  echo "=== $cls $tf start $(date +%H:%M:%S)  --start $start  ($(grep -cv '^#' "$rules") labels)"
  # --curves writes book_curves_<cls>_<tf>.json beside the CSV: the equity series behind
  # every row, which is what the dashboard's detail page draws. It comes from the same
  # `build_book` call that produced the row, so the chart and the row cannot disagree —
  # they did for months, by 14% on `ibs`, when `curves.py` built a second portfolio of its
  # own with no T-bill credit and no point-in-time membership.
  # --cash-rate 0: idle capital earns NOTHING. Both sides of every comparison lose the
  # credit together -- the cash-matched benchmark and the constant-weight control hold
  # their idle share at 0% too -- so what goes is a return that was never the signal's,
  # not a handicap on one side. It costs a part-time rule roughly 0.9%/yr over this
  # window and it is deliberate; see build_book.
  "$PY" -u portfolio_wf.py \
      --class "$cls" --tf "$tf" --pit --start "$start" --cash-rate 0 \
      --rules-file "$rules" \
      --out "book_${cls}_${tf}.csv" --curves \
      > "logs/book_${cls}_${tf}.log" 2>&1
  code=$?
  echo "=== $cls $tf exit=$code $((SECONDS - t0))s"
  [ $code -ne 0 ] && fail=1
done

echo "BOOK RUNS DONE fail=$fail"
exit $fail
