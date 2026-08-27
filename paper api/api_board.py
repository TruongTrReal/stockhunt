"""The dashboard, behind the login.

`../Stockhunt Dashboard/serve.py` still serves the same `web/` directory on loopback with
no login at all; this is how it reaches anyone else. Two servers, one directory, and the
allowlist and traversal check are imported from `web_files` rather than copied, because
two implementations of a traversal check is one implementation and one liability.

**Why the board had to move into this process.** A page cannot put an `Authorization`
header on `<script src="app.js">`, on `<link rel="stylesheet">` or on a WebSocket
handshake — the browser issues those, not the application. So gating the board needs a
cookie, a cookie is scoped to an origin, and a quick tunnel exposes exactly one port.
Serving the board from anywhere other than the process that issues the cookie means the
login page and the board cannot both be reachable from outside this machine.

**The routes are generated from the allowlist, not written out.** A catch-all
`/{path:path}` would work and would also quietly shadow anything added to the API later;
declaring the routes from `web_files.ALLOWED` keeps the list in one place and keeps every
served URL visible in the route table.

Nothing here reads the trading engine. It reads files off disk that another process
wrote, which is the same contract the dashboard folder has.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes

from fastapi import (APIRouter, Depends, HTTPException, Request, WebSocket,
                     WebSocketDisconnect, status)
from fastapi.responses import FileResponse, RedirectResponse, Response

import api_auth
import api_config
import api_live
import api_paths
import authdb

api_paths.use_dashboard()
import web_files                                                 # noqa: E402

log = logging.getLogger("stockhunt.api.board")

router = APIRouter(include_in_schema=False)

WEB = api_paths.DASHBOARD_WEB
LOGIN_PAGE = api_paths.WEB / "login.html"
# This folder's OWN page, not the generated board. `../Stockhunt Dashboard/web/` is build
# output and hand-editing it means the change survives until the next build and then
# disappears; the manager console therefore lives here, like the login screen.
DESK_PAGE = api_paths.WEB / "desk.html"
DOCS_PAGE = api_paths.WEB / "docs.html"
# The integration contract, and the only copy of it. `docs.html` renders this file rather
# than restating it: the same words are read by a person scrolling and by a model being
# handed the raw text, and two copies of a contract drift — with the machine-readable one
# being the copy nobody proofreads.
AGENT_DOC = api_paths.WEB / "agent.md"

# How often the socket re-reads `live.json`, and how many of those polls pass before it
# re-checks that the session is still valid. A stream is the one place where "revoked" can
# quietly mean "revoked for new requests only" — this socket is open for hours.
POLL_EVERY = 1.0
RECHECK_EVERY = 30


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@router.get("/login")
def login_page(request: Request) -> Response:
    """The sign-in screen. The only page here that does not need a session."""
    if api_auth.optional_session(request) is not None:
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)
    if not LOGIN_PAGE.is_file():
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="login page missing")
    return _no_store(FileResponse(LOGIN_PAGE, media_type="text/html; charset=utf-8"))


def _serve(request: Request, url_path: str) -> Response:
    """Serve one allowlisted file to a signed-in reader.

    An unauthenticated request for the *page* is redirected, because a browser typing the
    address deserves the login screen. An unauthenticated request for an *asset* gets a
    401: it is a `fetch` from a page that has just lost its session, and answering it with
    HTML would hand `JSON.parse` a login form.
    """
    session = api_auth.optional_session(request)
    if session is None:
        if url_path in ("/", "/index.html"):
            return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sign in first.")

    target = web_files.resolve(WEB, url_path)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not found.")

    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    response = FileResponse(target, media_type=ctype)
    # `private`: this is one reader's view of the desk, and a shared cache anywhere between
    # here and them must not keep a copy for the next person who asks.
    response.headers["Cache-Control"] = web_files.cache_control(target.name, private=True)
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@router.get("/")
def index(request: Request) -> Response:
    return _serve(request, "/index.html")


NEXT_OUT = api_paths.NEXT_OUT


@router.get("/next")
@router.get("/next/")
@router.get("/next/{path:path}")
def next_board(request: Request, path: str = "") -> Response:
    """The Next.js board, served from its static export under the same login.

    MOUNTED AT A SUB-PATH ON PURPOSE, and only while it is being built. `/` still serves
    the vanilla board, so the two coexist and the new one can be wrong without taking the
    working one down with it. Flipping it to the root is then a routing change here plus
    `NEXT_PUBLIC_BASE_PATH` in the build, not a rewrite.

    The same session gate as `_serve`, and for the same two reasons: a browser typing the
    address gets the login screen, and a `fetch` from a page whose session just lapsed gets
    a 401 rather than a login form that `JSON.parse` will choke on. The export's own asset
    requests are the second case, which is why the redirect is limited to the document.
    """
    session = api_auth.optional_session(request)
    if session is None:
        if path in ("", "/") or path.endswith(".html") or path.endswith("/"):
            return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sign in first.")

    target = web_files.resolve_export(NEXT_OUT, "/" + path)
    if target is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=("Not found. If this is the whole board, `dashboard-next` has not been "
                    "built -- run `npm run build` there."))

    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    response = FileResponse(target, media_type=ctype)
    # Next writes a content hash into every filename under `_next/static`, so those may be
    # held forever; the route documents are rewritten by each build under a stable name and
    # must be revalidated, or a deploy leaves a browser on the previous one indefinitely.
    immutable = "/_next/static/" in target.as_posix()
    response.headers["Cache-Control"] = (
        "private, max-age=31536000, immutable" if immutable else "private, no-cache")
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@router.get("/curves/{name}")
def curve(request: Request, name: str) -> Response:
    """One strategy's equity curve, fetched by the detail view.

    `name` is a path segment, so it cannot contain a slash — but it can contain `..`, and
    `web_files.resolve` is what refuses that. Do not shortcut it here.
    """
    return _serve(request, f"/curves/{name}")


def _handler_for(url_path: str):
    """A GET handler bound to one URL.

    It must be a real annotated function, not a lambda: FastAPI reads the signature to
    decide what to inject, and an unannotated `request` parameter would be treated as a
    query string field rather than the request.
    """
    def handler(request: Request) -> Response:
        return _serve(request, url_path)

    return handler


# Files this process does NOT serve straight off disk, because a handler below answers
# them instead. Excluding them from the generated routes is the whole of the mechanism, so
# it is a named constant rather than a condition buried in the loop: FastAPI matches in
# registration order, and a static route registered for one of these would shadow the
# handler — which for `live.json` means handing every member the entire desk.
#
# `serve.py` on loopback still serves both straight from `web/`, which is correct there:
# it is the owner's own machine and authenticates nobody.
PER_ACCOUNT = {"/live.json", "/catalog.json"}


def _register_static() -> None:
    """One route per allowlisted file, taken from the list itself."""
    for path in sorted(web_files.ALLOWED):
        if path in ("", "/", "/index.html") or path in PER_ACCOUNT:
            continue
        router.add_api_route(path, _handler_for(path), methods=["GET"],
                             name=f"board{path.replace('/', '_')}")


_register_static()


@router.get("/index.html")
def index_html(request: Request) -> Response:
    return _serve(request, "/index.html")


@router.get("/desk")
def desk_page(request: Request) -> Response:
    """The manager console: keys, registrations, and — for the owner — promotion.

    Behind the session like everything else. It is a skin over `/v1`: it holds no state
    and makes no trading decision, so a wrong number here is a wrong number in the ledger.
    """
    if api_auth.optional_session(request) is None:
        return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)
    if not DESK_PAGE.is_file():
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="desk page missing")
    return _no_store(FileResponse(DESK_PAGE, media_type="text/html; charset=utf-8"))


@router.get("/desk/docs")
def docs_page(request: Request) -> Response:
    """The API reference. A renderer; the words come from `/desk/agent.md`."""
    if api_auth.optional_session(request) is None:
        return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)
    if not DOCS_PAGE.is_file():
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="docs page missing")
    return _no_store(FileResponse(DOCS_PAGE, media_type="text/html; charset=utf-8"))


@router.get("/desk/agent.md")
def agent_doc(request: Request,
              who: dict = Depends(api_auth.current_principal)) -> Response:
    """The integration brief, as markdown, to either credential.

    `current_principal` and not a session, which is the one route on the board where that
    is true: the point of this document is that a manager can point their agent straight at
    it — `curl -H "Authorization: Bearer sk_live_..."` — and have it read the contract with
    the same key it will trade under. A browser without a session gets a 401 here rather
    than the login redirect the pages give, which is correct for something fetched.

    `{{BASE}}` is substituted per request so the URLs in a document somebody is about to
    paste into code name the host they actually reached, scheme included. Behind the tunnel
    that is only knowable from the forwarded header, which is why it is not a constant.
    """
    if not AGENT_DOC.is_file():
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="agent brief missing")
    body = AGENT_DOC.read_text(encoding="utf-8").replace(
        "{{BASE}}", api_auth.public_base_url(request))
    response = Response(content=body, media_type="text/markdown; charset=utf-8")
    # `private`, not because the words differ per reader — they do not — but because the
    # base URL does, and because this sits behind the same login as everything else.
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def _account_of(request: Request) -> tuple[str, bool] | None:
    """The caller's account id from their session, or None if they are not signed in."""
    session = api_auth.optional_session(request)
    if session is None:
        return None
    account = session.get("account_id")
    return (account, bool(session["is_admin"])) if account else None


@router.get("/live.json")
def live(request: Request) -> Response:
    """The desk, cut down to this reader.

    Deliberately NOT served off disk like the rest of `web/`. The file on disk describes
    every system on the desk, including other members' books; what goes out here is the
    caller's own plus the house. `api_live.visible_to` is the one place that cut is made.
    """
    who = _account_of(request)
    if who is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sign in first.")
    account, is_admin = who
    body = api_live.live_for(account, is_admin)
    response = Response(content=json.dumps(body),
                        media_type="application/json; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@router.get("/catalog.json")
def catalog(request: Request) -> Response:
    """The promotable rules, for the board's own picker. Same for every reader — it is
    research output, not anybody's book."""
    if api_auth.optional_session(request) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sign in first.")
    body = api_live.catalog() or {"sheets": {}, "health": {"warning": "not built yet"}}
    response = Response(content=json.dumps(body),
                        media_type="application/json; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.websocket("/ws")
async def live_stream(ws: WebSocket) -> None:
    """Push `live.json` whenever it changes, to a signed-in reader only.

    A file watcher, exactly as in `serve.py`, and deliberately not a connection to the
    trading node: a browser cannot reach the desk through it and a crash here cannot stall
    it. The cookie arrives on the handshake, which is the one thing a browser WebSocket
    sends that an `Authorization` header cannot be added to.
    """
    row = api_auth.session_for_token(ws.cookies.get(api_config.SESSION_COOKIE))
    if row is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # The socket carries the same per-account cut the route does. It is the easier of the
    # two to forget, because it is a file watcher rather than a handler — and forgetting
    # it streams the whole desk to every reader for as long as their tab is open.
    account = row.get("account_id")
    if not account:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    is_admin = bool(row["is_admin"])

    await ws.accept()
    token_hash = row["token_hash"]
    path = WEB / "live.json"
    last = None
    ticks = 0
    try:
        while True:
            ticks += 1
            # Revocation has to reach an open socket too. Without this a signed-out or
            # revoked reader keeps receiving the live desk until they close the tab.
            if ticks % RECHECK_EVERY == 0:
                if authdb.session(token_hash, touch=False) is None:
                    await ws.close(code=status.WS_1008_POLICY_VIOLATION)
                    return
            try:
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if stamp != last:
                    last = stamp
                    await ws.send_text(json.dumps(
                        api_live.visible_to(api_live.read(path), account, is_admin)))
            except OSError:
                pass
            await asyncio.sleep(POLL_EVERY)
    except WebSocketDisconnect:
        pass
