#!/usr/bin/env bash
# Stage 1e at 1d and 4h over the WHOLE catalogue, so the 13 conversions, the 130
# megacellar forecasters and sp100_momentum are scored on the sheets of record rather
# than only at 1h/15m.
#
# NOT SCOPED, deliberately. `--rules` would land as *.partial and would compute IS#1,
# the noise ceilings and ranking stability over the narrowed set -- and those three
# numbers are exactly what the promotion is for: a rule joining the board has to be
# ranked against the search it actually competed in.
set -u
cd "$(dirname "$0")"
PY=../.venv/Scripts/python
echo "=== strat_wf 1d/4h, full catalogue, $(date -u +%H:%M:%S) ==="
"$PY" -u strat_wf.py --tf 1d 4h
echo "=== strat_wf DONE rc=$? $(date -u +%H:%M:%S) ==="
