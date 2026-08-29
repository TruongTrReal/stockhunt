"""Print the rules riskmatch_wf would score for one sheet, one per line.

Used to CHUNK a cell. `riskmatch_wf --rules A B C` scores only those, and memory turns out
to scale with the number of rules rather than the number of bars: one worker on a full
commodities 5m sheet reached 11.4 GB and was OOM-killed on a 16 GB box, while a 20-rule
chunk peaked at 0.5 GB and finished in 44 seconds. Same bars either way.

This asks riskmatch_wf itself rather than re-deriving the list, so the chunks cover exactly
the population the unchunked run would have -- which is also the number that has to be
passed back as `--n-trials`, or every t bar and noise ceiling is computed against the chunk
size instead of the search.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wfo_paths  # noqa: F401,E402
from riskmatch_wf import leaderboard_universe  # noqa: E402

if __name__ == "__main__":
    cls, tf = sys.argv[1], sys.argv[2]
    for name in leaderboard_universe(cls, tf):
        print(name)
