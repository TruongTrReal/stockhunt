#!/usr/bin/env bash
# Re-score the converted-strategy sheets WITH `--curves`, so every row on the dashboard's
# second board has the equity series behind it and a detail page to draw it on.
#
# The rules are not retyped here -- they are read back out of the sheets that already
# exist, so this cannot silently score a different set from the one the board ranks. A
# (class, timeframe) that was scored by more than one run (`us_stocks 1d` was three:
# `portfolio.csv`, `convert_us_longflat.csv`, `convert_us_lf2.csv`) collapses into ONE
# run over the union, because `--curves` writes one file per sheet and three runs would
# each overwrite the other two's curves.
#
# `--curves-out convert_curves` is not optional. The stem defaults to `book_curves`,
# which is `run_book.sh`'s output -- the whole leaderboard's ~409 rules per sheet -- and
# a scoped run would replace it with this handful, taking every house detail page's chart
# with it. The failure is silent: the file exists and is valid JSON.
#
# BASH, nohup, `python -u`, per the repo's launch rule. The PowerShell form turns numpy's
# RuntimeWarnings into terminating errors and dies silently two minutes in.
#
# Cost is set by bar count, not by rule count: the five 1d sheets are seconds each, where
# the minute sheets that produced these CSVs took 36 hours between them.
#
#   ./run_convert_curves.sh 1d          # the daily sheets, ~2 minutes
#   ./run_convert_curves.sh 5m          # ~2.5 hours
#   ./run_convert_curves.sh 3m 2m 1m    # ~30 hours

set -u
PY=../.venv/Scripts/python
mkdir -p logs results
TFS=${*:-1d}

# `run_sheet <class> <tf>`: rebuild the sheet from whatever the board already reads.
run_sheet () {
    local cls=$1 tf=$2
    local out="convert_book_${cls}_${tf}.csv"
    local log="logs/convert_curves_${cls}_${tf}.log"

    # The rule union for this (class, timeframe), read off the sheets themselves.
    local rules
    rules=$($PY - "$cls" "$tf" <<'PY'
import csv, glob, os, sys
cls, tf = sys.argv[1], sys.argv[2]
CTRL = {"BUYHOLD", "RANDOM_25", "RANDOM_50", "RANDOM_75", "RANDOM_90",
        "ALWAYS_FLAT", "ALWAYS_LONG"}
# Every sheet the dashboard merges for this cell, and nothing else: the fee / fill /
# universe re-runs are answers to a different question and are not on the ranking.
SKIP = ("_fee_", "_fill_", "_controls", "_all34", "_wf.csv", "_grid_", "_pilot")
seen, ctrl = [], []
for path in sorted(glob.glob("results/convert_*.csv")) + ["results/portfolio.csv"]:
    name = os.path.basename(path)
    if name.startswith("convert_book_") or any(k in name for k in SKIP):
        continue
    try:
        rows = list(csv.DictReader(open(path, encoding="utf-8", errors="replace")))
    except OSError:
        continue
    if not rows or rows[0].get("class") != cls or rows[0].get("tf") != tf:
        continue
    for r in rows:
        rule = r["rule"]
        bucket = ctrl if rule in CTRL else seen
        if rule not in bucket:
            bucket.append(rule)
# Controls last so the printed list reads strategies-then-bar, and because `_vs_random`
# needs them present in the panel to compute the `vs random` column at all.
print(" ".join(seen + ctrl))
PY
)
    if [ -z "$rules" ]; then
        echo "=== skip ${cls}/${tf} (no sheet to rebuild) ==="
        return 0
    fi

    # `--pit` only where the sheet says it was used. It is point-in-time membership, and
    # turning it on for a class that has none is not a no-op -- it is a different book.
    local pit=""
    if $PY -c "
import csv, glob, sys
for p in glob.glob('results/convert_*_${tf}.csv') + ['results/portfolio.csv', 'results/convert_us_longflat.csv', 'results/convert_etf_lf.csv', 'results/convert_crypto_longflat.csv', 'results/convert_commodities_book.csv', 'results/convert_cme_futures_book.csv']:
    try: r = next(csv.DictReader(open(p, encoding='utf-8', errors='replace')), None)
    except OSError: continue
    if r and r.get('class') == '${cls}' and r.get('tf') == '${tf}':
        sys.exit(0 if r.get('pit') == 'True' else 1)
sys.exit(1)"; then
        pit="--pit"
    fi

    echo "=== ${cls}/${tf}: $(echo $rules | wc -w) rules ${pit:-（no pit)} ==="
    $PY -u portfolio_wf.py --class "$cls" --tf "$tf" $pit \
        --rules $rules --curves --curves-out convert_curves --out "$out" \
        > "$log" 2>&1
    echo "=== done ${cls}/${tf} rc=$? -> results/$out ==="
    tail -n 3 "$log"
}

for tf in $TFS; do
    for cls in us_stocks us_etfs crypto commodities cme_futures; do
        run_sheet "$cls" "$tf"
    done
done
echo "=== all done ==="
