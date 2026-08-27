"""The gate in front of the dashboard.

`test_auth.py` proves you cannot get a session you were not given. This proves the board
is actually behind that session — which is a separate question, and the one that decides
whether the tunnel is safe to hand out.

The dashboard's `web/` is generated output that may not exist on a fresh checkout, so
every case here runs against a temporary directory standing in for it. What is under test
is the gate and the path handling, and neither cares what the files contain.
"""

from __future__ import annotations

import os
import json
import tempfile
from pathlib import Path

_STATE = tempfile.mkdtemp(prefix="stockhunt-board-test-")
_WEB = Path(tempfile.mkdtemp(prefix="stockhunt-board-web-"))

os.environ["STOCKHUNT_API_STATE"] = _STATE
os.environ["API_SERVER_SECRET"] = "secret-for-tests-only"
os.environ["API_DEV_ECHO_OTP"] = "0"
os.environ["API_OWNER_EMAIL"] = ""

import pytest                                                    # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

import api_app                                                   # noqa: E402
import api_board                                                 # noqa: E402
import api_config                                                # noqa: E402
import authdb                                                    # noqa: E402
import mailer                                                    # noqa: E402

USER = "reader@example.com"

(_WEB / "curves").mkdir(exist_ok=True)
(_WEB / "index.html").write_text("<!doctype html><title>board</title>", encoding="utf-8")
(_WEB / "app.js").write_text("// the application", encoding="utf-8")
(_WEB / "data.js").write_text("window.PAYLOAD={};", encoding="utf-8")
(_WEB / "live.json").write_text('{"systems":[]}', encoding="utf-8")
(_WEB / "demo_data.js").write_text("window.DEMO=true;", encoding="utf-8")
(_WEB / "curves" / "ibs.json").write_text('{"equity":[]}', encoding="utf-8")
(_WEB.parent / "secret.txt").write_text("not under web/", encoding="utf-8")


@pytest.fixture(autouse=True)
def _point_at_the_fixture(monkeypatch):
    monkeypatch.setattr(api_board, "WEB", _WEB)


@pytest.fixture()
def sent(monkeypatch):
    box: list[tuple[str, str]] = []
    monkeypatch.setattr(mailer, "send_code",
                        lambda to, code, ttl: box.append((to, code)))
    return box


@pytest.fixture()
def client(sent):
    authdb.connect()
    for table in ("sessions", "otp_challenges", "users", "audit"):
        authdb.connect().execute(f"DELETE FROM {table}")
    authdb.allow(USER, label="reader")
    with TestClient(api_app.app) as c:
        yield c


def _sign_in(client, box) -> None:
    """Sign in the way the login page does: the cookie lands in the client's jar."""
    client.post("/auth/otp", json={"email": USER})
    r = client.post("/auth/browser", json={"email": USER, "code": box[-1][1]})
    assert r.status_code == 200, r.text
    assert api_config.SESSION_COOKIE in client.cookies


# ------------------------------------------------------------------------- locked shut

def test_the_board_redirects_a_stranger_to_the_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_assets_are_refused_not_redirected(client):
    """A `fetch` that got HTML back would hand `JSON.parse` a login form."""
    for path in ("/app.js", "/data.js", "/live.json", "/curves/ibs.json"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 401, path


def test_the_socket_is_refused(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()


def test_the_login_page_is_the_one_public_page(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text
    assert r.headers["cache-control"] == "no-store"


# -------------------------------------------------------------------------- signed in

def test_the_board_opens_once_signed_in(client, sent):
    _sign_in(client, sent)
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/app.js").status_code == 200
    assert client.get("/curves/ibs.json").status_code == 200


def test_the_socket_streams_once_signed_in(client, sent):
    """It streams the reader's OWN view, not the bytes on disk.

    This used to assert the file verbatim. It no longer can, and that is the point: the
    published document describes every account's systems, so the socket applies the same
    per-account cut the `/live.json` route does. A socket that echoed the file would
    stream the whole desk to every reader for as long as their tab stayed open —
    the easier of the two to forget, because it is a file watcher rather than a handler.
    """
    _sign_in(client, sent)
    with client.websocket_connect("/ws") as ws:
        body = json.loads(ws.receive_text())
    assert body["strategies"] == []
    assert body["account"], "the frame must say whose view it is"
    # The venue totals are re-derived from the surviving rows, never passed through.
    assert body["venue"]["equity"] == 0.0


def test_signed_in_responses_are_not_publicly_cacheable(client, sent):
    _sign_in(client, sent)
    assert "private" in client.get("/app.js").headers["cache-control"]
    assert client.get("/live.json").headers["cache-control"] == "no-store"


def test_the_login_page_steps_aside_when_already_signed_in(client, sent):
    _sign_in(client, sent)
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"


def test_signing_out_locks_the_board_again(client, sent):
    _sign_in(client, sent)
    assert client.delete("/auth/session").status_code == 204
    assert client.get("/", follow_redirects=False).status_code == 302
    assert client.get("/app.js").status_code == 401


def test_revoking_the_reader_locks_the_board_immediately(client, sent):
    _sign_in(client, sent)
    authdb.revoke(USER)
    assert client.get("/app.js").status_code == 401


# ------------------------------------------------------------------- the API reference

def test_the_reference_is_behind_the_login(client):
    r = client.get("/desk/docs", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    # The document itself is fetched, not navigated to, so it refuses rather than
    # redirecting — a `fetch` handed a login page would render HTML into the docs body.
    assert client.get("/desk/agent.md").status_code == 401


def test_the_reference_opens_once_signed_in(client, sent):
    _sign_in(client, sent)
    assert client.get("/desk/docs").status_code == 200
    r = client.get("/desk/agent.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")


def test_the_brief_names_the_host_it_was_fetched_from(client, sent):
    """It goes into code somebody is about to run, so the placeholder must never survive
    to a reader — a doc that says `{{BASE}}` is a doc that gets pasted verbatim."""
    _sign_in(client, sent)
    body = client.get("/desk/agent.md").text
    assert "{{BASE}}" not in body
    assert "http://testserver/v1/orders" in body or "http://testserver" in body


def test_an_api_key_can_read_the_brief(client, sent):
    """The one board route that takes either credential: the point of the document is that
    a manager can point their agent straight at it with the key it will trade under."""
    _sign_in(client, sent)
    raw = client.post("/auth/keys", json={"label": "agent"}).json()["key"]
    client.cookies.clear()
    r = client.get("/desk/agent.md", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200 and "client_order_id" in r.text
    # ...and it is still only the document. A key does not open the pages.
    assert client.get("/desk", headers={"Authorization": f"Bearer {raw}"},
                      follow_redirects=False).status_code == 302


# ------------------------------------------------------------- what must never be served

def test_the_demo_fixture_is_not_served(client, sent):
    """`demo_data.js` renders a page of invented numbers. It must never be reachable."""
    _sign_in(client, sent)
    assert client.get("/demo_data.js").status_code == 404


def test_traversal_out_of_web_is_refused(client, sent):
    """Over HTTP, and — because the client may normalise the path before it is sent — at
    the resolver directly, which is the code the refusal actually rests on."""
    import web_files

    _sign_in(client, sent)
    for path in ("/curves/../../secret.txt", "/curves/..%2f..%2fsecret.txt",
                 "/curves/....//secret.txt", "/curves/..\\..\\secret.txt"):
        r = client.get(path, follow_redirects=False)
        assert "not under web/" not in r.text, path
        assert web_files.resolve(_WEB, path) is None, path
    assert web_files.resolve(_WEB, "/curves/ibs.json") is not None      # the control


def test_the_next_export_is_behind_the_same_login(client, sent):
    """The Next board is a second front end, not a second security model.

    An unauthenticated DOCUMENT is redirected -- somebody typed the address and deserves
    the login screen. An unauthenticated ASSET is refused with 401, because it is a fetch
    from a page whose session lapsed and answering it with the login HTML hands a script
    tag a form. Same split as `_serve`, and it has to be: the export loads its own chunks.
    """
    r = client.get("/next/", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["location"]
    assert client.get("/next/_next/static/chunks/main.js").status_code == 401


def test_traversal_out_of_the_next_export_is_refused(client, sent, tmp_path):
    """An export has no allowlist -- its chunk names are content hashes nobody can
    enumerate -- so containment is the ONLY thing standing between this route and the
    repo. `.env.local` is two directories above the export root."""
    import web_files

    root = tmp_path / "out"
    (root / "_next").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (root / "_next" / "app.js").write_text("//", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")

    for path in ("/../secret.txt", "/_next/../../secret.txt", "/..\secret.txt",
                 "/_next/....//secret.txt"):
        assert web_files.resolve_export(root, path) is None, path

    # The controls: a real asset, and a DIRECTORY, which `trailingSlash` makes a route.
    assert web_files.resolve_export(root, "/_next/app.js") is not None
    assert web_files.resolve_export(root, "/") == (root / "index.html").resolve()
    assert web_files.resolve_export(root, "/nope.js") is None


def test_a_missing_curve_is_a_404_not_a_500(client, sent):
    _sign_in(client, sent)
    assert client.get("/curves/nope.json").status_code == 404


def test_the_bearer_token_also_opens_the_board(client, sent):
    """One sessions table, two carriers — a script does not need a second login."""
    client.post("/auth/otp", json={"email": USER})
    token = client.post("/auth/session",
                        json={"email": USER, "code": sent[-1][1]}).json()["token"]
    client.cookies.clear()
    r = client.get("/data.js", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
