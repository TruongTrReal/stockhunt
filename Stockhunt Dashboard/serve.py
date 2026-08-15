"""The local dashboard: static files and the tick stream on a single port, no login.

**This server is loopback-only, and it refuses to be anything else.** It authenticates
nobody, so whoever reaches it sees the whole book — which was fine while the only way to
reach it was to be sitting at this machine. Sharing the desk now goes through
`../paper api/`, which serves the same `web/` directory behind an emailed sign-in code.

That is a hard refusal rather than a warning because the failure is silent and total: one
`--host 0.0.0.0` and a tunnel publishes every position, every fill and the whole research
record to anyone with the URL, and nothing on the page would look any different. See
`_check_host`.

The desk previously needed two ports — `python -m http.server` on 8765 and the WebSocket
on 8766 — which is fine on localhost and breaks the moment it is shared:

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

    python serve.py                    # 127.0.0.1:8765, and nowhere else
    cd "../paper api" && python run_api.py    # the same board, behind a login
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes

import websockets
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

import dash_config
import web_files

WEB = dash_config.WEB
WS_PATH = "/ws"
POLL_EVERY = 1.0

# Loopback, and the two spellings of it. Anything else is refused rather than warned
# about -- see the module docstring.
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class _DropNonGet(logging.Filter):
    """Silence the traceback a bare `HEAD /` produces.

    `websockets` rejects any method other than GET while parsing the request line, which
    is before `process_request` runs -- so this cannot be answered properly from
    `http_handler`, only muted. Uptime checkers and link previewers send HEAD constantly,
    and each one was logging a full traceback into serve.err. The connection is still
    refused; only the noise goes away.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "expected GET" not in record.getMessage()


logging.getLogger("websockets.server").addFilter(_DropNonGet())


def http_handler(connection, request):
    """Serve a file for any request that is not the WebSocket upgrade.

    Returning `None` tells `websockets` to continue with the handshake, which is exactly
    what should happen on `/ws` and nowhere else.
    """
    if request.path.split("?", 1)[0] == WS_PATH:
        return None
    target = web_files.resolve(WEB, request.path)
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
        "Cache-Control": web_files.cache_control(target.name),
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


def _check_host(host: str) -> None:
    """Refuse to serve the unauthenticated board to anything but this machine.

    The instruction to point a tunnel here used to be in this file's own docstring, so
    this is not a hypothetical someone else might do — it is the documented workflow being
    withdrawn, and a `--host` that quietly still worked would keep it alive.
    """
    if host in LOOPBACK:
        return
    raise SystemExit(
        f"serve.py refuses to bind {host}: it has no login, so anything that can reach it\n"
        "sees every position and every result.\n\n"
        "To share the desk, serve the same board behind an emailed sign-in code:\n\n"
        '    cd "../paper api"\n'
        "    python admin_users.py allow them@example.com\n"
        "    .\\run.ps1 -Tunnel\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1",
                    help="loopback only; sharing goes through ../paper api/")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    _check_host(args.host)
    try:
        asyncio.run(main_async(args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
