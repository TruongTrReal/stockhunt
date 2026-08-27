"""`/v1/research` — the leaderboard as a query, and the queue that puts rows on it.

The research board used to be a snapshot. `payload.py` read 131 CSVs, joined and ranked
them, and wrote `web/data.js`; the page read that constant. A new strategy could not land
on the board — somebody had to re-run a stage and then re-run the builder — and the reason
was not the builder. **A CSV cannot take an insert.** Every stage rewrites its sheet whole,
which is why a scoped `strat_wf.py --rules` run has to land as `*.partial.csv`.

So the two halves were separated. Scoring a rule is genuinely slow (~32s for one strategy
on us_stocks 1d) and becomes a job. *Ranking* one is a join and a sort, and becomes this.

**This is the same seam `/v1/strategies` already draws.** That endpoint writes a row to
`stockhunt.deskdb` and the desk acts on its next tick; this one writes a row to
`stockhunt.resultsdb` and `research_worker.py` scores it on its next drain. Neither process
calls the other, and if the web layer is down the worker keeps scoring.

Two things this module deliberately does not do
------------------------------------------------
**It does not rank anything itself.** `board_rank.build_sheet` is the one implementation of
that join, shared with the dashboard builder, and it is reached through
`api_paths.use_dashboard()` — the same seam `api_board` uses for `web_files`. A second
ranking here would be a second answer to "which rule is better", on a page whose whole
argument is that there is one measurement.

**It does not decide whether a label is real.** Importing `strategies.registry` to check
loads TA-Lib, and this process must start and test without a compiled TA-Lib or the
trading stack present — that is what `api_paths` exists to protect. So the check here is
the label's SHAPE, which catches a typo in a millisecond, and the authoritative check is
the worker's, against the catalog, before it will score anything. Same doctrine as the
order path: the API's checks give a caller a fast useful error, the far side's checks bind.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

import api_auth
import api_config
import api_paths                                                        # noqa: F401

api_paths.use_dashboard()
import board_rank                                                       # noqa: E402

# The most rows one request may build. Each row carries its asset-by-asset table, so
# this is a response-size bound rather than a taste in page lengths: uncapped on
# us_stocks 1d it would join 190 symbols per rule across ~500 rules for one request.
MAX_PAGE = 200
from stockhunt import resultsdb                                         # noqa: E402

log = logging.getLogger("stockhunt.api.research")

router = APIRouter(prefix="/v1/research", tags=["research"])

# The label grammar, loosely. `strategies/registry.py` owns the real one:
#
#     ibs                      the published parameter set
#     ibs@buy=0.3              a variant, written as a diff against it
#     volregime:hi:0.5:ibs     an overlay wrapping any of the above
#     MININDEX~SAREXT|and      a pair, legs joined by an operator
#
# This pattern admits all four and rejects whitespace, path separators and anything that
# could reach a shell or a filename. It is a shape check, not a membership test.
LABEL_RE = re.compile(r"^[A-Za-z0-9_.,:=@~|+-]{1,120}$")

# A submitted module's name, which becomes a file in `strategies/published/`. Far stricter
# than the label: this one is used to build a path.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,48}$")

MAX_CODE_BYTES = 64 * 1024


class TrialRequest(BaseModel):
    label: str = Field(..., examples=["ibs@buy=0.3"],
                       description="A variant in the existing grammar. No new code runs.")
    cls: str = Field(..., examples=["crypto"])
    tf: str = Field(..., examples=["1d"])
    why: str = Field("", max_length=500,
                     description="What you are testing. Goes into the trial ledger, "
                                 "which is what the deflation count is read from.")


class StrategyRequest(BaseModel):
    name: str = Field(..., examples=["my_reversion"],
                      description="Becomes strategies/published/<name>.py")
    code: str = Field(..., description="A module defining `position` and `GRID`.")
    cls: str = Field(..., examples=["crypto"])
    tf: str = Field(..., examples=["1d"])
    why: str = Field("", max_length=500)


class JobOut(BaseModel):
    job_id: str
    kind: str
    label: str
    cls: str
    tf: str
    state: str
    stage: str | None = None
    reason: str | None = None
    submitted_at: str
    started_at: str | None = None
    finished_at: str | None = None


def _job_out(row: dict) -> JobOut:
    """The job, without the submitted source.

    `code` is deliberately not echoed back. It is the one field on the row that can be
    large, and a job listing is polled.
    """
    return JobOut(**{k: row.get(k) for k in JobOut.model_fields})


def _rate_limit(account: str) -> None:
    """Counted from the store, not from an in-process window.

    Same construction as `api_orders.rate_limit` and the same reason: restarting the API
    must not hand a looping bot a fresh allowance, which is exactly when it would be
    hammering. A scoring job costs minutes of CPU, so the ceiling here is far lower than
    the order path's.
    """
    since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
        timespec="seconds")
    if resultsdb.jobs_since(account, since) >= api_config.MAX_TRIALS_PER_MINUTE:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"More than {api_config.MAX_TRIALS_PER_MINUTE} submissions in a "
                   f"minute. Each one is a walk-forward run over the whole sheet; the "
                   f"queue is drained in order and there is nothing to gain by filling it.",
            headers={"Retry-After": "60"})


def _check_label(label: str) -> str:
    label = label.strip()
    if not LABEL_RE.match(label):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Not a strategy label. Expected something like `ibs`, `ibs@buy=0.3`, "
                   "`volregime:hi:0.5:ibs` or `MININDEX~SAREXT|and` — no spaces, no "
                   "slashes, 120 characters at most.")
    return label


def _check_sheet(cls: str, tf: str) -> tuple[str, str]:
    """Refuse a sheet the store has never scored anything on.

    Queuing against one would burn a walk-forward run to discover there are no bars, and
    the caller would learn it minutes later from a `failed` job instead of immediately.
    """
    have = {(s["cls"], s["tf"]) for s in resultsdb.sheets()}
    if (cls, tf) not in have:
        known = ", ".join(sorted(f"{c}/{t}" for c, t in have)) or "none"
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"No sheet for {cls}/{tf}. Scored sheets: {known}.")
    return cls, tf


# ============================================================ reading the board

@router.get("/sheets", summary="Which (class, timeframe) sheets have been scored")
def sheets(who: dict = Depends(api_auth.current_principal)) -> list[dict]:
    return resultsdb.sheets()


@router.get("/leaderboard", summary="One ranked sheet, computed now")
def leaderboard(cls: str, tf: str,
                offset: int = Query(0, ge=0, description="rows to skip"),
                limit: int = Query(None, ge=0, le=MAX_PAGE,
                                   description="rows to return; omit for the board's own "
                                               "depth, 0 for the header alone"),
                who: dict = Depends(api_auth.current_principal)) -> dict:
    """The sheet as the board renders it, ranked at the moment you ask.

    Every population statistic on it — the noise ceiling, the trial count, the correlation
    between IR and time in the market — is recomputed here over whatever is in the store,
    rather than read back from something a build froze. That is not a nicety: those figures
    are defined over the whole candidate population, so a single new rule changes them for
    every existing row, and a baked payload goes stale the moment one lands.

    PAGED, because a sheet is ~500 rows and 94% of a row's bytes are the per-asset table
    under it. `offset`/`limit` window the ROWS and leave every population statistic alone,
    so paging forward never changes what the header says the ranking was drawn from. The
    last page's index is `n_ranked` -- NOT `n_rules`, which also counts the candidates
    dropped before ranking (unscored, no book, never traded, closet trackers).
    """
    _check_sheet(cls, tf)
    sheet = board_rank.build_sheet(cls, tf, board_rank.universes().get(cls, []),
                                   offset=offset, limit=limit)
    if sheet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"No rankable rows for {cls}/{tf}.")
    return sheet


@router.get("/board", summary="Every sheet, shaped like the dashboard's `backtest` section")
def board(who: dict = Depends(api_auth.current_principal)) -> dict:
    """What `data.js` used to carry, built per request.

    Shaped like `payload["backtest"]` rather than as something new, which is what lets the
    browser keep one code path for the baked snapshot and the live board.

    `build_board` memoises on the store's revision: tens of seconds to build every sheet,
    then a dictionary lookup until something is written. Nobody reading the board should
    meet the slow case — `api_app`'s lifespan starts `board_rank.start_warmer`, which does
    that build on a thread this process owns and repeats it whenever the store moves.

    The warmer is a latency measure and nothing else. It calls exactly this function, so
    turning it off (`API_BOARD_WARM_SECONDS=0`) changes who waits and never what they
    are handed.
    """
    return board_rank.build_board()


@router.get("/rule/{cls}/{tf}/{rule:path}", summary="One rule, asset by asset")
def rule(cls: str, tf: str, rule: str,
         who: dict = Depends(api_auth.current_principal)) -> dict:
    """The detail page's table.

    `{rule:path}` because a pair's label contains no slash but does contain `|`, and
    FastAPI's default converter would still be fine — the path converter is here so a
    future label containing a separator cannot silently truncate, which is the failure the
    dashboard already had once when it split a pair's name on its operator.
    """
    _check_sheet(cls, tf)
    per = board_rank._per_asset_from_riskmatch(cls, tf)
    rows = per.get(rule)
    if rows is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"No per-asset rows for {rule!r} on {cls}/{tf}.")
    ranked, meta = board_rank._rank_assets(list(rows))
    return {"cls": cls, "tf": tf, "rule": rule,
            "stats": board_rank._asset_stats(rows) | meta, "rows": ranked}


# ============================================================ submitting

@router.post("/trials", response_model=JobOut,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Score a label variant. 202 means queued, not ranked.")
def submit_trial(body: TrialRequest, request: Request,
                 who: dict = Depends(api_auth.current_principal)) -> JobOut:
    """Queue a variant of an existing strategy for scoring.

    A `202` says the job is written down and sequenced. The worker drains in `seq` order
    and a walk-forward run takes minutes; poll `/v1/research/jobs/{id}` for `scored`, at
    which point the rule is on the board with no rebuild of anything.

    **The submission is the trial registration.** `research_worker` writes it into
    `data/reference/trials.csv` before it scores, which is the append-only ledger the
    deflation count comes from. That matters more here than anywhere else in the repo: an
    open leaderboard *is* a search, and a search whose size is not counted is how the best
    of N worthless candidates gets published as a finding. Two results here have already
    been retracted; both would have been caught by an honest N.
    """
    account = who["account_id"]
    label = _check_label(body.label)
    _check_sheet(body.cls, body.tf)
    _rate_limit(account)
    row = resultsdb.submit_job(account, "label", label, body.cls, body.tf)
    resultsdb.set_meta(f"why:{row['job_id']}", {"why": body.why, "account": account})
    log.info("research trial queued: %s %s/%s by %s", label, body.cls, body.tf, account)
    return _job_out(row)


@router.post("/strategies", response_model=JobOut,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Submit a strategy module. It is gated on causality before scoring.")
def submit_strategy(body: StrategyRequest, request: Request,
                    who: dict = Depends(api_auth.current_principal)) -> JobOut:
    """Queue a new `strategies/published/` module for scoring.

    **This runs code you wrote, in the worker's process, on the desk's box.** There is no
    sandbox and that is a deliberate choice, not an oversight: this API is invitation-only
    and the allowlist is the trust boundary, exactly as it is for a member's strategy
    placing real orders. The author is recorded on the rule and stays there across every
    later re-ingest.

    What is NOT taken on trust is whether the strategy is causal. `research_worker` runs
    `strategies/tests/test_causality.py --rules <name>` before it will score anything, and
    a nonzero exit rejects the job with the reason attached — no row, no rank. That gate
    tests by TRUNCATION rather than by reading the code, which is how a whole-series
    `nanmedian` survived review here long enough to contaminate two published stages. An
    agent submitting a rule that peeks at the future would otherwise top this board
    instantly, and it would look like the best result the repo has ever produced.
    """
    account = who["account_id"]
    # NOT `.lower()`. Normalising here would accept `My_Rule` and store the result under
    # `my_rule`, so the submitter's label and the board's label would differ — and a label
    # is the key every result in this repo is stored under, going back three studies.
    # Refusing is the only answer that cannot silently rename somebody's strategy.
    name = body.name.strip()
    if not NAME_RE.match(name):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A strategy name is lower-case letters, digits and underscores, 3-49 "
                   "characters, starting with a letter. It becomes a filename.")
    if len(body.code.encode("utf-8")) > MAX_CODE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"Module over {MAX_CODE_BYTES // 1024} kB. A published "
                                   f"strategy in this repo is one file and one rule.")
    _check_sheet(body.cls, body.tf)
    _rate_limit(account)
    row = resultsdb.submit_job(account, "code", name, body.cls, body.tf, code=body.code)
    resultsdb.set_meta(f"why:{row['job_id']}", {"why": body.why, "account": account})
    log.warning("research CODE submission queued: %s %s/%s by %s",
                name, body.cls, body.tf, account)
    return _job_out(row)


@router.get("/jobs", response_model=list[JobOut], summary="Your submissions")
def my_jobs(who: dict = Depends(api_auth.current_principal)) -> list[JobOut]:
    return [_job_out(r) for r in resultsdb.jobs(account=who["account_id"])]


@router.get("/jobs/{job_id}", response_model=JobOut, summary="One submission")
def one_job(job_id: str,
            who: dict = Depends(api_auth.current_principal)) -> JobOut:
    row = resultsdb.job(job_id)
    # Not-yours and not-found answer identically. Otherwise the 403 confirms a job id
    # exists, which is the one bit an enumeration needs.
    if row is None or (row["account"] != who["account_id"] and not who["is_admin"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such job.")
    return _job_out(row)
