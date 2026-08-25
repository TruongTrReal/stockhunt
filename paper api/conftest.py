"""What every test in this folder needs set before the first import.

Run the suite from THIS directory::

    ..\.venv\Scripts\python -m pytest -q

**The board warmer is off here, and it has to be off before `api_config` is imported.**
`board_rank.start_warmer` puts a pandas job on a background thread that polls
`results.db` every thirty seconds; in production that is the point, but a test fixture
repoints the store with `resultsdb.use(tmp_path / "results.db")` between cases, and a
thread reading it across that swap is a race whose failure would look like a flaky
endpoint rather than like a test-only artifact. `api_config` reads the environment at
import time, so pytest's conftest -- which is imported before any test module -- is the
one place early enough to say so.
"""

from __future__ import annotations

import os

os.environ.setdefault("API_BOARD_WARM_SECONDS", "0")
