"""Paths and environment for the public API. Import this before anything else here.

Named `api_paths` and not `config.py` for the reason the rest of the repo carries
distinctly named bootstraps: `../backtest engine/` puts itself on `sys.path` and its
modules import each other by bare name, so a second `config.py` anywhere that lands on
the same path shadows it. See the table in `../CLAUDE.md`.

Unlike the other three bootstraps this one does **not** import the trading stack. The
authentication layer needs no bars, no `nautilus_trader` and no universe, and importing
`paper_config` for its paths would pull the whole engine into a process whose job is to
answer HTTP. `use_paper_engine()` is the seam for when the strategy endpoints need it,
and it is called at the point of use, never at import.

Secrets come from the process environment first and a gitignored `../.env.local` second,
which is the same precedence `backtest engine/config.py` and `td_live.py` use.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PAPER_ENGINE = REPO / "paper trading engine"
DASHBOARD = REPO / "Stockhunt Dashboard"
DASHBOARD_WEB = DASHBOARD / "web"
# The Next.js board, `next build` output. A directory of build artefacts and nothing
# else -- which is why it is served whole rather than through `web_files.ALLOWED`:
# that allowlist exists because `Stockhunt Dashboard/web/` also holds a build script
# and the demo fixture, and an export has no such neighbours to protect.
NEXT_OUT = REPO / "dashboard-next" / "out"
ENV_FILE = REPO / ".env.local"

# The repo root, so `stockhunt.*` resolves. That package is THE SHARED CORE and it is the
# one thing this process may import from outside its own folder: it is a real package, it
# depends on nothing but the standard library and numpy/pandas, and by the repo's own rule
# it may never import from a pipeline folder — so pulling it in cannot drag the engine,
# the universe or `nautilus_trader` along behind it.
#
# This is NOT `use_paper_engine()`. That seam puts a folder full of trading modules on the
# path and stays deliberately unused until something genuinely needs the desk. `stockhunt`
# is shared infrastructure; `stockhunt.deskdb` is the order ledger that this process writes
# and the desk reads, and it has to be one definition rather than two copies of a schema.
#
# Everything else at the repo root is a directory with a space in its name and therefore
# unimportable, or `strategies`/`tools`/`tests`, none of which anything here imports.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# This folder's own pages -- the login screen, and nothing else. Kept apart from the
# dashboard's `web/`, which is generated output this process only ever reads.
WEB = HERE / "web"

# The auth store, and the server secret that keys the OTP hashes. Neither is a "result"
# and neither is regenerable-from-elsewhere: this directory is the record of who is
# allowed in. It is gitignored, because a committed copy would publish the allowlist and
# every live session hash.
STATE_DIR = Path(os.environ.get("STOCKHUNT_API_STATE") or (HERE / "state"))
LOG_DIR = HERE / "logs"

for _d in (STATE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

AUTH_DB = STATE_DIR / "auth.db"
SECRET_FILE = STATE_DIR / "server_secret"

_env_cache: dict[str, str] | None = None


def _load_env_file() -> dict[str, str]:
    """Parse `../.env.local` once. Missing file is not an error."""
    global _env_cache
    if _env_cache is not None:
        return _env_cache
    out: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    _env_cache = out
    return out


def env(name: str, default: str | None = None) -> str | None:
    """Process environment first, then `.env.local`, then the default.

    That order lets a one-off run override a stored credential without editing the file,
    which is how the rest of the repo behaves and is what a deployment expects.
    """
    value = os.environ.get(name)
    if value is not None and value != "":
        return value
    value = _load_env_file().get(name)
    return value if value not in (None, "") else default


def env_int(name: str, default: int) -> int:
    raw = env(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def env_flag(name: str, default: bool = False) -> bool:
    raw = env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def server_secret() -> bytes:
    """The key the OTP hashes are computed under.

    `API_SERVER_SECRET` if it is set, otherwise a 32-byte random value generated once and
    kept in `state/server_secret`. Generating rather than defaulting is the point: a
    hardcoded fallback would make every stolen `auth.db` brute-forceable against a known
    key, and a six-digit code has only a million possibilities.

    Rotating it invalidates every outstanding OTP and nothing else — sessions are keyed
    on a 256-bit random token, not on this.
    """
    from_env = env("API_SERVER_SECRET")
    if from_env:
        return from_env.encode("utf-8")
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()
    import secrets

    value = secrets.token_bytes(32)
    SECRET_FILE.write_bytes(value)
    try:                                    # best effort; Windows ACLs are not chmod
        SECRET_FILE.chmod(0o600)
    except OSError:
        pass
    return value


def use_dashboard() -> None:
    """Put `../Stockhunt Dashboard/` on `sys.path`, for `web_files`.

    That module holds the served-file allowlist and the traversal check, and it is
    imported rather than copied because two implementations of a traversal check is one
    implementation and one liability. It imports nothing itself — in particular not
    `dash_config`, which would drag the backtest engine in behind it.
    """
    if str(DASHBOARD) not in sys.path:
        sys.path.insert(0, str(DASHBOARD))


def use_paper_engine() -> None:
    """Put `../paper trading engine/` on `sys.path`, for the strategy endpoints.

    Deliberately a function. Importing `paper_config` costs the whole backtest engine
    (universes, membership tables, `strategies`), and the authentication layer must be
    startable — and testable — without any of it.
    """
    if str(PAPER_ENGINE) not in sys.path:
        sys.path.insert(0, str(PAPER_ENGINE))
