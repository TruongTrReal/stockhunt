"""The ASGI application: what is mounted, and what happens around every request.

This process is the public face of the desk, so it is the one place in the repo that
faces callers who are not the owner. Two consequences run through the whole folder:

* **It owns no trading.** Nothing here can place an order today, and when the strategy
  endpoints arrive they will write a *request* that the desk picks up — the same
  separation `serve.py` keeps for the dashboard, for the same reason: if this crashes,
  gets wedged or is taken down, `run_paper.py` keeps trading.
* **An unexpected exception says nothing.** The handler at the bottom turns anything
  uncaught into a bare 500. A stack trace in a response body is a map of the filesystem.

Run it with `run_api.py`, or point uvicorn at `api_app:app`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import api_auth
import api_config
import authdb

log = logging.getLogger("stockhunt.api")

DESCRIPTION = """
Programmatic access to the Stockhunt paper-trading desk.

**Access is by invitation.** There is no sign-up: an address works only if the desk owner
has put it on the allowlist. Signing in is two calls —

1. `POST /auth/otp` with your email. A six-digit code is mailed to you.
2. `POST /auth/session` with that code. You get a bearer token.

Then send `Authorization: Bearer <token>` on everything else.

A browser does the same thing at `/login` and gets an HttpOnly cookie instead, which is
what gates the dashboard itself.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    authdb.connect()
    _seed_owner()
    dropped = authdb.purge_expired()
    for line in api_config.startup_banner():
        log.info(line)
    log.info("  cleaned      %s expired sessions, %s old challenges",
             dropped["sessions"], dropped["challenges"])
    log.info("  users        %s on the allowlist", authdb.user_count())
    yield
    authdb.close()


def _seed_owner() -> None:
    """Put `API_OWNER_EMAIL` on an empty allowlist, once.

    Only when the table has no rows at all. A seed that ran on every start would quietly
    resurrect an account the owner had just revoked, and "the env var wins over the
    database" is not a property anybody expects from an allowlist.
    """
    if authdb.user_count() or not api_config.OWNER_EMAIL:
        return
    email = authdb.normalize_email(api_config.OWNER_EMAIL)
    authdb.allow(email, label="owner", is_admin=True)
    authdb.audit("user.seeded", email, detail="from API_OWNER_EMAIL")
    log.info("  seeded       %s as the first admin", email)


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{api_config.APP_NAME} API",
        version=api_config.API_VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
    )

    if api_config.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=api_config.CORS_ORIGINS,      # never "*": these calls are
            allow_credentials=True,                     # authenticated
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type"],
        )

    app.include_router(api_auth.router)

    # The manager desk. Registered before the board, which claims `/` and every
    # allowlisted asset path under it — FastAPI matches in registration order, so anything
    # mounted after the board can be shadowed by it.
    import api_house
    import api_orders
    import api_strategies

    app.include_router(api_strategies.limits_router)
    app.include_router(api_strategies.router)
    app.include_router(api_orders.router)
    app.include_router(api_house.router)

    @app.get("/healthz", tags=["meta"], summary="Liveness, with nothing sensitive in it")
    def healthz() -> dict:
        return {"status": "ok", "app": api_config.APP_NAME,
                "version": api_config.API_VERSION,
                "mail": "configured" if api_config.mail_configured() else "missing"}

    # Last, and only last: the board claims `/` and every allowlisted asset path under it,
    # so it must not be in a position to shadow an API route. FastAPI matches in
    # registration order.
    if api_config.SERVE_BOARD:
        import api_board

        app.include_router(api_board.router)
    else:
        @app.get("/", tags=["meta"], include_in_schema=False)
        def root() -> dict:
            return {"app": api_config.APP_NAME, "version": api_config.API_VERSION,
                    "docs": "/docs", "sign_in": "POST /auth/otp"}

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            content={"detail": "Internal error."})

    return app


app = create_app()
