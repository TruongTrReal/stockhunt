"""Alpaca's paper trading API, as thin a layer as six endpoints deserve.

**The base URL is a constant and there is no environment override.** Every function here
talks to `paper-api.alpaca.markets` and nothing else. Going live would be an edit to this
file and a review of it, never a variable somebody exports in a shell — the whole point of
this process is a *second record* to compare the sandbox against, and a record that can be
switched to real money by a typo is not one.

**No `alpaca-py`.** The SDK wraps six REST calls in an object model that would have to be
unwrapped again into the plain dicts `alpaca_map` works over, and it pulls a websocket
stack and a pandas-flavoured data client this process has no use for. `requests` is
already a dependency of `td_live`, which is the same shape of thing one vendor over.

**One key pair per asset class.** `us_stocks`, `us_etfs` and `crypto` each get their own
Alpaca paper account, so their buying power is separate and their P&Ls are separately
readable. That is what `for_class` resolves, from the environment first and `.env.local`
second — the same order and the same file `td_live.api_key` uses, because one convention
for a credential is worth more than a marginally better one.

**Retries are for the transport, never for the order.** A 429 or a 5xx is retried with
backoff; a 4xx is raised. That asymmetry matters: `submit` carries a deterministic
`client_order_id`, so a retried *timeout* is safe (Alpaca refuses the duplicate) but a
retried *rejection* is a bug being papered over.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

import paper_config

# Not configurable. See the module docstring.
PAPER_URL = "https://paper-api.alpaca.markets"

# The live host, named only so `_refuse_live` can recognise it if it ever appears in a
# call site. Nothing here ever sends a request to it.
LIVE_URL = "https://api.alpaca.markets"

# One paper account per class, one key pair per account.
CLASS_ENV = {
    "us_stocks": ("ALPACA_STOCKS_KEY_ID", "ALPACA_STOCKS_SECRET"),
    "us_etfs": ("ALPACA_ETFS_KEY_ID", "ALPACA_ETFS_SECRET"),
    "crypto": ("ALPACA_CRYPTO_KEY_ID", "ALPACA_CRYPTO_SECRET"),
}

# The classes this repo trades that Alpaca does not sell. Named rather than merely absent
# from `CLASS_ENV`, so a caller asking for one gets a reason instead of a KeyError.
UNSUPPORTED = {
    "commodities": "Alpaca sells no spot metals or FX",
    "cme_futures": "Alpaca sells no futures",
}

TIMEOUT = 20.0
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 1.5


class AlpacaError(RuntimeError):
    """A refusal from Alpaca, carrying the status and whatever it said about it."""

    def __init__(self, status: int, body: str, method: str, path: str):
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:400]}")


def _env_file_value(name: str) -> str | None:
    env = paper_config.REPO / ".env.local"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return None


def credential(name: str) -> str | None:
    """Environment first, `.env.local` second. Same order as `td_live.api_key`."""
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    return _env_file_value(name)


def credentials(cls: str) -> tuple[str, str]:
    if cls in UNSUPPORTED:
        raise KeyError(f"{cls}: {UNSUPPORTED[cls]}")
    try:
        key_name, secret_name = CLASS_ENV[cls]
    except KeyError:
        raise KeyError(f"{cls} has no Alpaca account; "
                       f"known classes are {', '.join(sorted(CLASS_ENV))}") from None
    key, secret = credential(key_name), credential(secret_name)
    missing = [n for n, v in ((key_name, key), (secret_name, secret)) if not v]
    if missing:
        raise RuntimeError(
            f"no Alpaca credentials for {cls}: set {' and '.join(missing)} in the "
            f"environment or in {paper_config.REPO / '.env.local'}")
    return key, secret


def configured_classes() -> list[str]:
    """Which classes have a usable key pair right now. Used by `--check` and by the
    mirror's startup, so a half-configured install names the half that is missing rather
    than dying on the first request."""
    out = []
    for cls in CLASS_ENV:
        try:
            credentials(cls)
        except RuntimeError:
            continue
        out.append(cls)
    return out


class AlpacaClient:
    """One paper account. Not thread-safe by design — the mirror is single-threaded."""

    def __init__(self, key_id: str, secret: str, *, label: str = "",
                 session: requests.Session | None = None):
        self.label = label
        self._session = session or requests.Session()
        self._session.headers.update({
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret,
            "accept": "application/json",
        })

    @classmethod
    def for_class(cls, class_name: str, **kw) -> "AlpacaClient":
        key, secret = credentials(class_name)
        return cls(key, secret, label=class_name, **kw)

    # ------------------------------------------------------------------ transport

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 payload: dict | None = None) -> Any:
        url = f"{PAPER_URL}{path}"
        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = self._session.request(method, url, params=params, json=payload,
                                             timeout=TIMEOUT)
            except requests.RequestException as exc:   # DNS, connect, read timeout
                last = exc
                time.sleep(BACKOFF_SECONDS * (2 ** attempt))
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                # 429 carries the free tier's 200/min limit. Honour Retry-After when it is
                # given rather than guessing, then fall back to the same backoff.
                wait = resp.headers.get("Retry-After")
                delay = float(wait) if wait and wait.isdigit() else \
                    BACKOFF_SECONDS * (2 ** attempt)
                last = AlpacaError(resp.status_code, resp.text, method, path)
                if attempt == MAX_ATTEMPTS - 1:
                    break
                time.sleep(delay)
                continue

            if resp.status_code >= 400:
                # A 4xx is a decision, not a transport failure. Raising immediately is
                # what keeps a rejected order from being retried into a duplicate.
                raise AlpacaError(resp.status_code, resp.text, method, path)

            if not resp.content:
                return None
            return resp.json()

        raise last if last else RuntimeError(f"{method} {path}: no attempts made")

    # ------------------------------------------------------------------ endpoints

    def account(self) -> dict:
        return self._request("GET", "/v2/account")

    def clock(self) -> dict:
        """The venue's own opinion of whether the market is open. Preferred over a local
        timezone calculation because it knows about holidays and half-days, which a
        hand-rolled 09:30-16:00 test does not."""
        return self._request("GET", "/v2/clock")

    def positions(self) -> list[dict]:
        return self._request("GET", "/v2/positions") or []

    def assets(self, asset_class: str | None = None, status: str = "active") -> list[dict]:
        params = {"status": status}
        if asset_class:
            params["asset_class"] = asset_class
        return self._request("GET", "/v2/assets", params=params) or []

    def orders(self, status: str = "open", limit: int = 200,
               after: str | None = None) -> list[dict]:
        params: dict = {"status": status, "limit": limit, "direction": "desc"}
        if after:
            params["after"] = after
        return self._request("GET", "/v2/orders", params=params) or []

    def submit(self, *, symbol: str, side: str, qty: float, client_order_id: str,
               time_in_force: str = "day", order_type: str = "market") -> dict:
        """One market order.

        `qty` is a string on the wire. Alpaca accepts up to nine decimal places and
        floats round-trip badly through JSON at that width — `0.1 + 0.2` reaching the
        venue as `0.30000000000000004` is refused by the fractional-quantity validator,
        which reads as a mysterious 422 rather than as a float problem.
        """
        payload = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
            "qty": f"{qty:.9f}".rstrip("0").rstrip("."),
            "client_order_id": client_order_id,
        }
        return self._request("POST", "/v2/orders", payload=payload)

    def cancel_all(self) -> Any:
        return self._request("DELETE", "/v2/orders")
