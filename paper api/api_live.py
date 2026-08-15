"""One reader's view of the desk, cut from the document the desk publishes.

The desk writes a single `live.json` describing every system it runs, each tagged with the
account that owns it. This module is what turns that into "your book, and the house's" —
and it is the only place that cut is made, so there is one implementation to get right.

**Filtering a list is not enough.** The document also carries venue totals that were
summed over every system on the desk. Passing those through unchanged would report one
manager the size of everybody else's book — a leak with no strategy names attached, which
is the kind that survives review. They are recomputed from the rows that survive.

**The house is `00` and everybody sees it.** Those are the desk's own rules, promoted off
the walk-forward sheets; they are the research made visible and the reason a manager
trusts the desk at all. A member's own systems are private to them.
"""

from __future__ import annotations

import json
from pathlib import Path

import api_paths

HOUSE = "00"

LIVE_PATH: Path = api_paths.DASHBOARD_WEB / "live.json"
CATALOG_PATH: Path = api_paths.DASHBOARD_WEB / "catalog.json"


def read(path: Path) -> dict | None:
    """Parse a published document, or None if it is absent or mid-write.

    Absent means absent: the desk has never run, and the board says so. A partial read
    cannot happen — the desk writes to a temp file and renames — but a corrupt file must
    still not take the API down with it.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def visible_to(doc: dict | None, account: str, is_admin: bool = False) -> dict:
    """The subset of `doc` this account may see, with the totals re-derived.

    A member sees their own systems and the house's — never another member's.

    **The owner sees everything**, and that is a deliberate widening rather than an
    oversight. This used to refuse it, on the argument that a board an owner reviews
    should be the board a member sees or leak-shaped bugs would only ever appear on the
    member's screen. That argument is still true of the DEFAULT view and is why every row
    still carries its `account`: the page separates mine from members' with a filter, so
    the owner is looking at the same shape a member does, one group at a time, rather than
    at a differently-built page.

    Somebody has to be able to answer "what is running on my desk", and the alternative —
    a second endpoint with a second implementation of the same cut — is the version that
    drifts.
    """
    if not doc:
        return {"generated_at": None, "feed": {"status": "stopped"},
                "venue": {"balance": 0.0, "equity": 0.0}, "strategies": []}

    strategies = [s for s in doc.get("strategies", [])
                  if is_admin or str(s.get("account") or HOUSE) in (HOUSE, account)]

    venue = dict(doc.get("venue") or {})
    # Re-derived, never passed through. The published figure is the whole desk's, and
    # handing it to one member reports the size of everybody else's book.
    books = [s.get("equity") if s.get("equity") is not None else (s.get("capital") or 0.0)
             for s in strategies]
    caps = [s.get("capital") or 0.0 for s in strategies]
    venue["equity"] = round(sum(books), 2)
    venue["balance"] = round(sum(caps), 2)

    out = dict(doc)
    out["strategies"] = strategies
    out["venue"] = venue
    # Who is looking, so the page can split "mine" from "members'" without guessing. The
    # house account is named too, because a promoted book belongs to the desk rather than
    # to any person and the owner reads it as theirs.
    out["account"] = account
    out["is_admin"] = bool(is_admin)
    out["house"] = HOUSE
    return out


def live_for(account: str, is_admin: bool = False) -> dict:
    return visible_to(read(LIVE_PATH), account, is_admin)


def catalog() -> dict | None:
    return read(CATALOG_PATH)
