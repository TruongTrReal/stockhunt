"""What of `web/` may be served, and how a URL path becomes a file.

Two processes now serve this directory — `serve.py` on loopback and `../paper api/`
behind a login — and both need the same allowlist and the same traversal check. This
module is that definition, held once.

Two copies would be a drift risk anywhere; here it is a security one. The allowlist is
not a convenience: `web/` also holds `demo_data.js`, the layout fixture that renders a
page of invented numbers, and the resolution below is the only thing standing between a
public URL and `/curves/../../../.env.local`. A rule enforced in two places is a rule
enforced in whichever place somebody remembered to update.

**Imports nothing.** Not `dash_config`, not the engine — so the API can use it without
putting the backtest stack into an HTTP process.
"""

from __future__ import annotations

from pathlib import Path

# Served to browsers; anything else is a 404. An allowlist rather than "whatever is under
# `web/`", because these processes face the public internet through a tunnel and the
# directory also holds the build script and the demo fixture.
#
# `demo_data.js` is deliberately absent. It sets `window.DEMO = true` and must never be
# the tag that ships.
ALLOWED = {"", "/", "/index.html", "/app.js", "/app.css", "/data.js",
           "/live.json", "/paper_curves.json", "/catalog.json"}
ALLOWED_PREFIXES = ("/curves/",)

# `live.json` changes every couple of seconds; a cached copy would freeze the desk for
# whoever opened it first.
NO_STORE = {"live.json", "catalog.json"}

# Stamped with a content hash by `build_dashboard.stamp_cache_busters`, so the URL changes
# whenever the bytes do and a cached copy can never be the wrong one. Those may be held
# forever; anything else has a stable URL and mutable contents, so it may not.
STAMPED = {"app.js", "app.css", "data.js"}

# `index.html` is the file that CARRIES the stamps, which makes it the one file that must
# never be served stale: a browser holding yesterday's copy asks for yesterday's `app.js`
# by name and gets it, correctly cached, forever. That is not hypothetical — it is how a
# second device kept rendering an old chart while every file on disk was current.
REVALIDATE = {"index.html"}


def resolve(web: Path, path: str) -> Path | None:
    """Map a URL path to a file under `web`, or None if it is not on offer.

    `Path.resolve()` plus `is_relative_to` is what stops traversal. The allowlist is
    checked on the *URL* and the containment on the *resolved file*, because either alone
    is bypassable: a permitted prefix can still walk upwards, and a path that lands inside
    `web/` can still be a file nobody was meant to fetch.
    """
    clean = path.split("?", 1)[0]
    if clean in ("", "/"):
        clean = "/index.html"
    if clean not in ALLOWED and not clean.startswith(ALLOWED_PREFIXES):
        return None
    target = (web / clean.lstrip("/")).resolve()
    if not target.is_relative_to(web.resolve()) or not target.is_file():
        return None
    return target


def cache_control(name: str, private: bool = False) -> str:
    """`Cache-Control` for one served file, by what its URL promises about its contents.

    `private` is passed by the authenticated server in `../paper api/`: behind a login the
    response is one reader's view, and a shared cache between here and them must not keep
    a copy to hand to the next person who asks.
    """
    if name in NO_STORE:
        return "no-store"
    if name in REVALIDATE:
        return "no-cache"                       # may be held, must be revalidated
    scope = "private" if private else "public"
    if name in STAMPED:
        return f"{scope}, max-age=31536000, immutable"
    # Curve JSONs and `paper_curves.json`: stable URL, contents change every rebuild. A
    # minute is short enough that a reload after a build is right, and `app.js` fetches
    # them with `cache: "no-cache"` anyway, which revalidates regardless of this.
    return f"{scope}, max-age=60"
