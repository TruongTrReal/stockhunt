"""One origin for the whole dashboard: static files and the tick stream on a single port.

The desk previously needed two — `python -m http.server` on 8765 and the WebSocket on 8766
— which is fine on localhost and breaks the moment it is shared:

* **Mixed content.** A tunnel serves the page over HTTPS, and a browser refuses to let an
  HTTPS page open an insecure `ws://` socket. The stream would silently never connect.
* **Two URLs.** Every tunnel exposes one port, so sharing the desk would mean handing
  someone a page address *and* a socket address and hoping they stay in step.

Serving both from one port fixes both at once: the page fetches its own origin, the socket
upgrades on `/ws` of that same origin, and `wss://` follows automatically from `https://`.
One URL to hand out.

This process only serves. The trading node is separate and stays separate — it writes
`live.json` and owns the upstream Twelve Data socket, and nothing here can place an order
or touch a position. If this dies the desk keeps trading.

Run::

    python serve.py                    # 127.0.0.1:8765
    python serve.py --host 0.0.0.0     # reachable on the LAN / through a tunnel
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
from pathlib import Path

import websockets
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

import fwd_config

WEB = fwd_config.HERE / "web"
WS_PATH = "/ws"
POLL_EVERY = 1.0

# Served to browsers; anything else is a 404. An allowlist rather than "whatever is under
# `web/`" because this process may face the public internet through a tunnel, and the
# directory also holds the build script and the demo fixture.
ALLOWED = {"", "/", "/index.html", "/app.js", "/app.css", "/data.js",
           "/live.json", "/paper_curves.json"}
ALLOWED_PREFIXES = ("/curves/",)


def _resolve(path: str) -> Path | None:
    """Map a URL path to a file, refusing anything outside the allowlist.

    `Path.resolve()` plus an `is_relative_to` check is what stops `/curves/../../.env.local`
    — the tunnel makes traversal a real concern rather than a theoretical one.
    """
    clean = path.split("?", 1)[0]
    if clean in ("", "/"):
        clean = "/index.html"
    if clean not in ALLOWED and not clean.startswith(ALLOWED_PREFIXES):
        return None
    target = (WEB / clean.lstrip("/")).resolve()
    if not target.is_relative_to(WEB.resolve()) or not target.is_file():
        return None
    return target


def http_handler(connection, request):
    """Serve a file for any request that is not the WebSocket upgrade.

    Returning `None` tells `websockets` to continue with the handshake, which is exactly
    what should happen on `/ws` and nowhere else.
    """
    if request.path.split("?", 1)[0] == WS_PATH:
        return None
    target = _resolve(request.path)
    if target is None:
        return Response(404, "Not Found", Headers({"Content-Type": "text/plain"}),
                        b"not found")
    body = target.read_bytes()
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    headers = Headers({
        "Content-Type": ctype,
        "Content-Length": str(len(body)),
        # `live.json` changes every couple of seconds; a cached copy would freeze the desk
        # for whoever opened it first. The rest is versioned by query string already.
        "Cache-Control": "no-store" if target.name == "live.json" else "public, max-age=60",
    })
    return Response(200, "OK", headers, body)


async def ws_handler(ws):
    """Push `live.json` whenever it changes.

    Deliberately a file watcher rather than a second connection to the trading node. The
    node already writes that file atomically on every mark, so this stays a reader: a
    browser cannot reach the node through it, and a crash here cannot stall the desk. The
    node's own richer per-tick stream still runs on its own port for local use.
    """
    path = WEB / "live.json"
    last = None
    try:
        while True:
            try:
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if stamp != last:
                    last = stamp
                    await ws.send(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            await asyncio.sleep(POLL_EVERY)
    except websockets.exceptions.ConnectionClosed:
        pass


async def main_async(host: str, port: int) -> None:
    async with serve(ws_handler, host, port, process_request=http_handler,
                     ping_interval=20, ping_timeout=20, max_size=None):
        where = "localhost" if host in ("127.0.0.1", "localhost") else host
        print(f"dashboard on http://{where}:{port}   (ws on {WS_PATH}, same origin)")
        print("serving from", WEB)
        await asyncio.Future()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to accept connections from the LAN or a tunnel")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
