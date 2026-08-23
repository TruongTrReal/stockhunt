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
    python serve.py --lan              # ...plus this LAN, for testing on a phone. Loud,
                                       # explicit, and still no login: everyone on the
                                       # network sees everything while it runs
    cd "../paper api" && python run_api.py    # the same board, behind a login

`--lan` exists because "check the new view on the phone" is a real workflow and the
refusal below kept forcing it through a tunnel with a sign-in code. It is a separate
flag rather than a permitted `--host` value so the exposure can never be the side effect
of a copied command line: the flag names the intent, the banner names the audience, and
the default stays loopback-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import socket

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


# The one dynamic route this server has, and the reason it exists at all.
#
# The research board is queried per request from `results.db` now rather than baked into
# `data.js` (see `board_rank.py`). `app.js` asks for it on load and falls back to the baked
# payload when nobody answers — so without this, the loopback board silently keeps showing
# whatever the last `build_dashboard.py` run froze, which is exactly the staleness the
# store was built to remove.
#
# **Read-only, and there is no submission route here.** Queuing a rule for scoring is an
# act attributable to an account, and this process authenticates nobody. `paper api/`
# carries `POST /v1/research/trials`; this carries the two GETs and nothing else.
RESEARCH_BOARD = "/v1/research/board"


def _research_board() -> Response:
    """The same ranking `paper api` serves, from the same module. Imported lazily: it pulls
    in pandas, and a server whose job is static files should not pay for that until asked."""
    import board_rank                                                  # noqa: PLC0415

    body = json.dumps(board_rank.build_board(), separators=(",", ":")).encode("utf-8")
    return Response(200, "OK", Headers({
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body)),
        "Cache-Control": "no-store"}), body)


def http_handler(connection, request):
    """Serve a file for any request that is not the WebSocket upgrade.

    Returning `None` tells `websockets` to continue with the handshake, which is exactly
    what should happen on `/ws` and nowhere else.
    """
    path = request.path.split("?", 1)[0]
    if path == WS_PATH:
        return None
    if path == RESEARCH_BOARD:
        try:
            return _research_board()
        except Exception as exc:
            # A missing or empty `results.db` must not 500 the board. `app.js` treats any
            # non-200 as "no live board" and keeps the baked payload, which is the honest
            # degradation: yesterday's numbers, not a blank page.
            print(f"  {RESEARCH_BOARD}: {type(exc).__name__}: {exc}")
            return Response(503, "Service Unavailable",
                            Headers({"Content-Type": "text/plain"}),
                            b"no results store; run tools/ingest_results.py")
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


def _lan_ip() -> str | None:
    """This machine's address on the LAN, for printing the URL a phone should open.

    The UDP-connect trick: no packet is sent, the OS just picks the interface it would
    route through. Fails closed to None on a machine with no route at all.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))       # TEST-NET-1; nothing is transmitted
            return s.getsockname()[0]
    except OSError:
        return None


async def main_async(host: str, port: int, lan: bool = False) -> None:
    async with serve(ws_handler, host, port, process_request=http_handler,
                     ping_interval=20, ping_timeout=20, max_size=None):
        where = "localhost" if host in ("127.0.0.1", "localhost") else host
        print(f"dashboard on http://{where}:{port}   (ws on {WS_PATH}, same origin)")
        if lan:
            ip = _lan_ip()
            print("=" * 72)
            print("LAN MODE: no login. Every position, every fill and the whole research")
            print("record is readable by ANY device on this network while this runs.")
            print(f"  on this machine:  http://127.0.0.1:{port}")
            if ip:
                print(f"  from a device:    http://{ip}:{port}")
            else:
                print("  from a device:    http://<this machine's LAN address>:%d" % port)
            print("If the device cannot connect, Windows Firewall is blocking inbound")
            print("Python on private networks — allow it once in the prompt, or:")
            print('  netsh advfirewall firewall add rule name="stockhunt dash (test)"'
                  " dir=in action=allow protocol=TCP localport=%d" % port)
            print("=" * 72)
        # Flushed, because this process is normally started with stdout redirected to
        # logs/serve.log and block-buffered — the URL a phone should open would otherwise
        # sit invisible in the buffer until shutdown.
        print("serving from", WEB, flush=True)
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
        "To test on a device on YOUR OWN network, the explicit opt-in is:\n\n"
        "    python serve.py --lan\n\n"
        "To share the desk beyond it, serve the same board behind an emailed sign-in code:\n\n"
        '    cd "../paper api"\n'
        "    python admin_users.py allow them@example.com\n"
        "    .\\run.ps1 -Tunnel\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1",
                    help="loopback only; sharing goes through ../paper api/")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true",
                    help="ALSO answer this LAN (binds 0.0.0.0), for testing on a phone. "
                         "No login: everyone on the network sees everything. The named "
                         "flag is the point — `--host` alone still refuses.")
    args = ap.parse_args()
    if args.lan:
        args.host = "0.0.0.0"
    else:
        _check_host(args.host)
    try:
        asyncio.run(main_async(args.host, args.port, lan=args.lan))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
