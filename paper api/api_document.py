"""`/v1/board` — the baked board document, served as JSON instead of as a script.

`build_dashboard.py --serve` writes one document to `../Stockhunt Dashboard/web/data.js`,
as `window.DASH = {...};`. The vanilla board reads it by loading that file as a `<script>`
and taking the global. **`dashboard-next` cannot do that and must not want to**: the file
is 3.7 MB, of which 3.2 MB is the `backtest` section that the paged
`/v1/research/leaderboard` exists to stop shipping whole.

So this module serves the SAME document, split by what a reader actually needs:

    GET /v1/board/meta          the small sections: ~48 KB
    GET /v1/board/logic/{rule}  one strategy's plain-English logic, of 505 KB
    GET /v1/board/systems       the baked paper snapshot, when `/live.json` has none

**It parses `data.js` rather than re-deriving anything.** That is the whole point: a
second builder here would be a second answer to every question the board asks, and this
folder's standing rule is that it owns no measurement. `payload.py` builds; this reads.

The one thing it does NOT serve is `backtest`. That section is a QUERY now
(`board_rank.build_sheet` behind `/v1/research`), and handing out a baked copy beside the
live one would put two rankings on one site — the exact drift `tools/test_board_equivalence`
exists to catch.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, status

import api_auth
import api_paths

log = logging.getLogger("stockhunt.api.document")

router = APIRouter(prefix="/v1/board", tags=["board"])

DATA_JS = api_paths.DASHBOARD_WEB / "data.js"
_MARKER = "window.DASH = "

# What `meta` carries. Everything in the document EXCEPT the three heavy sections, each of
# which has its own route or its own endpoint:
#
#   backtest    3.2 MB   -> `/v1/research/leaderboard`, paged, ranked per request
#   logic       505 KB   -> `/v1/board/logic/{rule}`, one strategy at a time
#   strategies  241 KB   -> `/live.json` while the desk is up, `/v1/board/systems` if not
#
# Listing what is INCLUDED rather than what is excluded is deliberate. A new heavy section
# added to `payload.py` would otherwise start riding along on every page load, and the
# thing that made this endpoint necessary is exactly that having happened once.
META_KEYS = ("generated_at", "feed", "venue", "timeframes", "paper_timeframes",
             "paper_groups", "edge_criteria", "summary", "research", "curves", "robust")

# The one thing `meta` takes OUT of `backtest` rather than refusing it whole: each group's
# key, its human label and its universe size. `dash_config.GROUPS` is where those live, the
# tab strip is `Object.keys(D.backtest)`, and a class absent from it is invisible however
# complete its results are -- so a board that cannot see this list cannot draw its own
# filters. It is five short rows; the section it is lifted out of is 3.2 MB.
_GROUP_FIELDS = ("label", "n")


@lru_cache(maxsize=1)
def _parse(mtime: float) -> dict:
    """The document, parsed once per build.

    Keyed on MTIME, the same construction `api_research._curves` uses one file over: a
    rebuild invalidates this without anybody remembering to, and nothing has to be
    restarted for a new board to be served. `maxsize=1` because a superseded document is
    unreachable and holding it would keep several MB alive for the life of the process.
    """
    text = DATA_JS.read_text(encoding="utf-8")
    try:
        body = text[text.index(_MARKER) + len(_MARKER):]
    except ValueError:                                    # not the file we think it is
        raise ValueError(f"{DATA_JS.name} does not contain `{_MARKER.strip()}`")
    return json.loads(body.rstrip().rstrip(";"))


def document() -> dict:
    """The parsed document, or a 503 saying which command produces it.

    A 503 and not a 500: the board not having been built yet is a state of the deployment,
    not a fault in this process, and the caller can do something about it. It is also what
    a fresh clone looks like before `build_dashboard.py` has ever run.
    """
    try:
        return _parse(DATA_JS.stat().st_mtime)
    except FileNotFoundError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The board has not been built. Run `python build_dashboard.py --serve` "
                   "in `Stockhunt Dashboard/`.")
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("data.js is unreadable: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The board document is unreadable. Rebuild it with "
                   "`python build_dashboard.py --serve`.")


@router.get("/meta", summary="The board's small sections, without the heavy ones")
def meta(who: dict = Depends(api_auth.current_principal)) -> dict:
    """Universe notes, gate definitions, timeframe lists, the summary strip's figures.

    Everything here is either configuration or a handful of numbers, and all of it is
    needed before the first view can render — which is why it is one request rather than
    one per section. A missing key is omitted rather than sent as null, so a document built
    by an older `payload.py` still answers.

    `groups` is lifted OUT of `backtest` — the key, the label and the universe size, and
    nothing else. The tab strip is that list, so a board that cannot see it cannot draw its
    own filters; carrying the five rows is not the same as carrying the 3.2 MB they sit in.
    """
    doc = document()
    out = {k: doc[k] for k in META_KEYS if k in doc}
    out["groups"] = [{"key": key,
                      **{f: grp[f] for f in _GROUP_FIELDS if f in grp}}
                     for key, grp in (doc.get("backtest") or {}).items()]
    return out


def _stem(label: str) -> str:
    """The base rule an overlay or variant wraps.

    `ha:chart:ibs@buy=0.3` is `ibs` with three things done to it, and `logic` is recorded
    against the base. Overlays prefix with `:` and a variant suffixes with `@`, so the stem
    is the last colon-segment up to the first `@`.
    """
    return label.rsplit(":", 1)[-1].split("@", 1)[0]


@router.get("/logic/{rule:path}", summary="One strategy's plain-English logic")
def logic(rule: str, who: dict = Depends(api_auth.current_principal)) -> dict:
    """What the rule does, in words, plus its family and source where recorded.

    Per rule and never the whole map: `logic` is 505 KB across every published strategy and
    exactly one detail page's worth of it is ever read. `rule` is a label, not a path — it
    is a dictionary key here and reaches no filesystem — so the pair and overlay grammars
    (`A~B|and`, `ha:chart:ibs@buy=0.3`) pass through untouched.

    **The label is resolved HERE rather than in the browser, and that is the point.** A pair
    is two legs and an operator, and an overlay wraps a base — so a client resolving these
    itself makes up to three round trips to describe one row, and has to carry this grammar
    a second time to do it. `legs` comes back on a pair so the page can say what each half
    does; `matched` says which key actually answered, so a page can be honest about showing
    the base rule's logic for a variant of it.
    """
    book = document().get("logic") or {}

    def look(label: str) -> dict | None:
        return book.get(label) or book.get(_stem(label))

    entry = look(rule)
    if entry is not None:
        matched = rule if rule in book else _stem(rule)
        return dict(entry, matched=matched)

    # A pair, whose legs are recorded individually because `combo_wf.py` composes them
    # rather than defining a third rule. `A~B|op` -- the operator is part of the label.
    if "~" in rule:
        base, _, op = rule.partition("|")
        legs = [leg for leg in base.split("~") if leg]
        found = [{"leg": leg, **(look(leg) or {})} for leg in legs]
        if any(f.get("logic") for f in found):
            return {"op": op or None, "legs": found, "matched": None}

    raise HTTPException(status.HTTP_404_NOT_FOUND,
                        detail=f"No recorded logic for `{rule}`.")


@router.get("/systems", summary="The baked paper snapshot, for when the desk is down")
def systems(who: dict = Depends(api_auth.current_principal)) -> dict:
    """What `/live.json` carries, as the last build froze it.

    **This is the fallback, not the source.** `/live.json` is the desk's own document, cut
    to the caller's account, and is what every paper view should read; this is what the
    board falls back to when the desk has not published — the same degradation the vanilla
    board gets for free by having the snapshot baked into `data.js`.

    It is NOT cut per account, because it is a build artifact rather than the desk's live
    state, and the accounts on it are whatever the desk had published when the board was
    last built. Callers should prefer `/live.json` and treat this as "the shape of things
    when nobody is home".
    """
    doc = document()
    return {"generated_at": doc.get("generated_at"),
            "feed": doc.get("feed"), "venue": doc.get("venue"),
            "strategies": doc.get("strategies") or []}
