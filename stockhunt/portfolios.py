"""A portfolio: a basket of strategies with ONE pot of money, one curve and one switch.

It sits on top of `deskdb` and owns no trading of its own. A leg is an ORDINARY row in
`registrations` — `kind='book'`, exactly the row a promotion writes — that additionally
carries a `portfolio_id`. Everything downstream therefore keeps working untouched: the
desk warms it up, fills it, prices it and retires it without knowing a portfolio exists.
That is the whole reason a leg is not its own registration kind. A parallel type would be
a second lifecycle to keep in step with the first forever, and the first one is the one
that trades.

What this module owns is the arithmetic and the record around that:

* **Equal weight, and where the money is.** The pot is split across the live legs and
  re-split whenever the membership changes, so a leg's `capital` is derived and the
  portfolio's is the one to edit.
* **The toggle.** One `want` on the portfolio, cascaded to every leg in one transaction.
  A basket half switched off is a position nobody chose to hold.
* **What changed and when.** `portfolio_changes` is append-only. A 'follow' portfolio's
  membership is decided by a sheet that moves underneath it, so a step in its curve is
  explained by a row there or by nothing at all.

Stdlib only, like `deskdb` itself: `paper api/` imports this to answer a request, and
importing it must not drag the trading stack in behind it.

**`want` is the owner's; `state` is the desk's.** Nothing here writes a leg's `state`, and
nothing writes a portfolio's except `mark_state`, which only the desk calls.
"""

from __future__ import annotations

import sqlite3

from stockhunt import deskdb

# 'manual' — somebody picked the rules. 'follow' — it tracks the top `top_n` of one
# leaderboard sheet, re-checked daily.
KINDS = ("manual", "follow")
WANTS = ("live", "paused", "retired")
# Only the schedule that is actually implemented. A portfolio carrying `rebalance='weekly'`
# that nothing rebalances weekly is a lie the row goes on telling about itself.
REBALANCES = ("monthly",)
DEFAULT_CAPITAL = 100_000.0


# ------------------------------------------------------------------ helpers, internal

def _pid(account: str, name: str) -> str:
    """`pf_00_core`. Derived and readable, for the same reason `strategy_id` is."""
    return f"pf_{account}_{name}"


def _ascii(text: str) -> str:
    """Fold a name into something a Nautilus identifier can actually carry.

    `StrategyId`'s Rust constructor PANICS on a non-ASCII character — a process abort, not
    an exception any Python `try` can catch — so a name with an em-dash in it does not
    produce a bad registration, it takes the whole desk down in a restart loop. That is
    what happened on 2026-08-28: nine baskets named with an em-dash, eleven restarts, and
    every other book on the desk down with them.

    So the fold is here, at the one place a leg is named, rather than at the caller. Any
    non-ASCII run becomes a hyphen and whitespace collapses, which keeps a hand-typed
    portfolio name — a member may choose anything — from ever reaching the node intact.
    """
    out = []
    for ch in str(text):
        out.append(ch if (ch.isascii() and (ch.isalnum() or ch in "-_.")) else "-")
    folded = "".join(out).strip("-").lower()
    while "--" in folded:
        folded = folded.replace("--", "-")
    return folded or "portfolio"


def _leg_name(pf: dict, cls: str, tf: str, rule: str) -> str:
    """The registration name for one leg, and it is the diff's key.

    `deskdb.register` is idempotent on `(account, name)`, so this string decides whether a
    nightly reconcile re-uses a leg or creates a second one. It carries the portfolio so a
    basket cannot adopt a hand-promoted book that happens to hold the same rule.
    """
    return _ascii(f"pf-{pf['name']}-{cls}-{tf}-{rule}")


def _must(portfolio_id: str, account: str | None = None) -> dict:
    pf = get(portfolio_id, account)
    if pf is None:
        raise LookupError(f"no such portfolio: {portfolio_id}")
    return pf


def _source_of(pf: dict) -> str | None:
    """The sheet a 'follow' portfolio tracks, named as the board names it."""
    if pf["kind"] != "follow" or not pf["source_cls"]:
        return None
    return f"{pf['source_cls']}_{pf['source_tf']}"


def _log(portfolio_id: str, action: str, *, strategy_id: str | None = None,
         cls: str | None = None, tf: str | None = None, rule: str | None = None,
         rank_at: int | None = None, source: str | None = None,
         n_legs: int | None = None, leg_capital: float | None = None,
         reason: str | None = None) -> None:
    """Append one row to the record. Nothing updates it and nothing deletes it."""
    conn = deskdb.connect()
    # The ledger's own write lock, taken here for the reason its helpers take it: one
    # writer at a time inside this process, whatever thread the caller arrived on.
    with deskdb._lock:
        conn.execute("""
            INSERT INTO portfolio_changes
                (portfolio_id, at, action, strategy_id, cls, tf, rule, rank_at, source,
                 n_legs, leg_capital, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (portfolio_id, deskdb.utcnow(), action, strategy_id, cls, tf, rule,
              None if rank_at is None else int(rank_at), source,
              None if n_legs is None else int(n_legs),
              None if leg_capital is None else float(leg_capital), reason))


# ---------------------------------------------------------------------------- writes

def create(account: str, name: str, kind: str, *, capital: float = DEFAULT_CAPITAL,
           source_cls: str | None = None, source_tf: str | None = None,
           top_n: int | None = 5, rebalance: str = "monthly",
           inception: str | None = None) -> dict:
    """Open an empty portfolio. It holds nothing until legs are added.

    **Not idempotent, deliberately.** `register` returns the existing row for a repeated
    name because a deploy script running twice must not double a book; a portfolio's name
    is typed by a person into a form, so the same name arriving with a different `capital`
    or a different `top_n` is a mistake, and answering it with the row already there hands
    back settings nobody asked for.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("a portfolio needs a name")
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}; one of {KINDS}")
    if rebalance not in REBALANCES:
        raise ValueError(f"unknown rebalance schedule: {rebalance!r}; "
                         f"one of {REBALANCES}")
    if float(capital) <= 0:
        raise ValueError("a portfolio with no money is not a portfolio")
    if kind == "follow":
        if not (source_cls and source_tf):
            raise ValueError("a 'follow' portfolio has to name the sheet it follows "
                             "— source_cls and source_tf")
        if top_n is None or int(top_n) < 1:
            raise ValueError("a 'follow' portfolio has to say how deep it follows")
        top_n = int(top_n)
    else:
        # A manual basket carrying a source would be reconciled against that sheet by the
        # daily pass and quietly rewritten under the person who chose its rules.
        source_cls = source_tf = top_n = None

    portfolio_id = _pid(account, name)
    conn = deskdb.connect()
    with deskdb._lock:
        try:
            conn.execute("""
                INSERT INTO portfolios
                    (portfolio_id, account, name, kind, source_cls, source_tf, top_n,
                     capital, rebalance, want, state, inception, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,'live','pending',?,?)
            """, (portfolio_id, account, name, kind, source_cls, source_tf, top_n,
                  float(capital), rebalance, inception or deskdb.utcnow(),
                  deskdb.utcnow()))
        except sqlite3.IntegrityError:
            raise ValueError(
                f"{account} already has a portfolio named {name!r}") from None
    return _must(portfolio_id)


def set_want(portfolio_id: str, want: str, account: str | None = None) -> bool:
    """THE TOGGLE: 'live', 'paused' or 'retired', cascaded to every leg.

    One transaction, because a basket half switched off is a position nobody chose to
    hold — and the window between two separate statements is exactly where the desk's next
    tick lands.

    Legs already retired are left alone. They were dropped by a membership change and are
    not part of the basket any more; resuming a portfolio must not resurrect a rule that
    fell off the sheet three months ago.

    Only `want` moves. `state` is what the desk did, and only the desk writes it.
    """
    if want not in WANTS:
        raise ValueError(f"unknown want: {want}")
    conn = deskdb.connect()
    with deskdb._lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            sql = "UPDATE portfolios SET want = ? WHERE portfolio_id = ?"
            args: list = [want, portfolio_id]
            if account is not None:
                # Scoped in the WHERE rather than checked beforehand: a check-then-write
                # leaves a window, and one account's statement has no business naming
                # another's portfolio at all.
                sql += " AND account = ?"
                args.append(account)
            hit = conn.execute(sql, tuple(args)).rowcount > 0
            if hit:
                conn.execute("""UPDATE registrations SET want = ?
                                WHERE portfolio_id = ? AND want <> 'retired'""",
                             (want, portfolio_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return hit


def mark_state(portfolio_id: str, state: str) -> None:
    """What the desk has actually done with the basket. Only the desk calls this."""
    conn = deskdb.connect()
    with deskdb._lock:
        conn.execute("UPDATE portfolios SET state = ? WHERE portfolio_id = ?",
                     (state, portfolio_id))


def _attach(pf: dict, cls: str, tf: str, rule: str, *, symbols: list[str] | None = None,
            benchmark: str | None = None, allow_short: bool = False,
            signal_tf: str | None = None) -> dict:
    """Register one leg. No log row and no resize — the batch owns both.

    Goes through `deskdb.register`, so a leg is indistinguishable from a promoted book
    except for its `portfolio_id`. A rule that left the basket and came back is REVIVED
    under the same `strategy_id`: its record continues with a measured gap in it, rather
    than restarting as a shorter, newer track record that happens to begin at a good time.
    """
    return deskdb.register(
        pf["account"], _leg_name(pf, cls, tf, rule), cls, list(symbols or []), tf,
        # A placeholder until `resize` divides the pot. It never reaches the desk ahead of
        # that: every path through this module resizes before it returns.
        float(pf["capital"]), kind="book", rule=rule, benchmark=benchmark,
        allow_short=allow_short, signal_tf=signal_tf, portfolio_id=pf["portfolio_id"])


def _detach(portfolio_id: str, strategy_id: str) -> bool:
    """Retire one leg. Never delete it: it may have traded, and then it is a record.

    Scoped by `portfolio_id` rather than by account — two portfolios on one account must
    not be able to retire each other's legs.
    """
    conn = deskdb.connect()
    with deskdb._lock:
        cur = conn.execute("""UPDATE registrations SET want = 'retired'
                              WHERE strategy_id = ? AND portfolio_id = ?""",
                           (strategy_id, portfolio_id))
    return cur.rowcount > 0


def add_leg(portfolio_id: str, cls: str, tf: str, rule: str, *,
            symbols: list[str] | None = None, benchmark: str | None = None,
            allow_short: bool = False, signal_tf: str | None = None,
            reason: str | None = None, rank_at: int | None = None,
            account: str | None = None) -> dict:
    """Add one rule to the basket by hand, re-split the money, and record it."""
    pf = _must(portfolio_id, account)
    row = _attach(pf, cls, tf, rule, symbols=symbols, benchmark=benchmark,
                  allow_short=allow_short, signal_tf=signal_tf)
    shape = resize(portfolio_id)
    _log(portfolio_id, "added", strategy_id=row["strategy_id"], cls=cls, tf=tf,
         rule=rule, rank_at=rank_at, source=_source_of(pf),
         n_legs=shape["n_legs"], leg_capital=shape["leg_capital"],
         reason=reason or "added by hand")
    return deskdb.registration(row["strategy_id"])            # type: ignore[return-value]


def remove_leg(portfolio_id: str, strategy_id: str, reason: str,
               account: str | None = None) -> bool:
    """Drop one leg. Retired, never deleted — the same principle as
    `deskdb.delete_registration`'s refusal: a forward test somebody can erase is not a
    record.

    Returns False when that leg is not in this portfolio, so nothing can be discovered by
    guessing an id.
    """
    pf = _must(portfolio_id, account)
    leg = deskdb._row("SELECT * FROM registrations "
                      "WHERE strategy_id = ? AND portfolio_id = ?",
                      (strategy_id, portfolio_id))
    if leg is None or not _detach(portfolio_id, strategy_id):
        return False
    shape = resize(portfolio_id)
    _log(portfolio_id, "removed", strategy_id=strategy_id, cls=leg["cls"], tf=leg["tf"],
         rule=leg["rule"], source=_source_of(pf), n_legs=shape["n_legs"],
         leg_capital=shape["leg_capital"], reason=reason)
    return True


def resize(portfolio_id: str, account: str | None = None) -> dict:
    """Split the pot equally across the live legs. Returns the shape it wrote.

    Not rounded. Five ways into $100,000 leaves a remainder that has to be handed to
    somebody, and choosing which leg gets it is a weighting decision made by an accident
    of arithmetic — the desk sizes in shares off this number anyway.

    A leg the desk REFUSED keeps its slot. Reallocating its money to the legs that did
    attach would make the basket's weights depend on desk health; leaving it means a
    rejected leg reads as a fifth of the portfolio sitting in cash, which is what it is.
    """
    pf = _must(portfolio_id, account)
    live = legs(portfolio_id)
    if not live:
        return {"n_legs": 0, "leg_capital": 0.0}
    each = float(pf["capital"]) / len(live)
    conn = deskdb.connect()
    with deskdb._lock:
        conn.execute("""UPDATE registrations SET capital = ?
                        WHERE portfolio_id = ? AND want <> 'retired'""",
                     (each, portfolio_id))
    return {"n_legs": len(live), "leg_capital": each}


def apply_membership(portfolio_id: str, target: list[tuple[str, str, str]], reason: str,
                     account: str | None = None,
                     benchmarks: dict[str, str] | None = None) -> dict:
    """Make the basket hold exactly `target`, given as `(cls, tf, rule)` cells.

    The diff the daily 'follow' reconcile calls. It adds what is missing, retires what is
    no longer wanted, re-splits the money and writes one `portfolio_changes` row per
    change.

    **Idempotent.** Called twice with the same target, the second call writes nothing —
    which is what makes it safe to run every night, safe to retry after a crash halfway
    through, and safe to run by hand while the nightly pass is due.

    The order of `target` is the sheet's own ranking, and it is recorded on every row that
    is added: a change reads as "it came in 4th" only if the rank is written down at the
    time, because the sheet is re-ranked nightly and cannot be asked afterwards. A removal
    carries no rank — falling off the sheet is the whole event.

    `benchmarks` maps an asset class to the baseline a leg in it is scored against, and it
    is passed in rather than derived because this module may not know what a class's index
    is: the desk declares that per class in `catalog.json`, and `/v1/house/strategies`
    already reads it from there. A book has no single instrument to hold against, so
    leaving the baseline to be inferred from the holdings is how it ends up differing from
    the strategy in more than the signal — the one comparison error this repo cares about
    most. Absent, a leg carries no baseline, which prints as nothing rather than as a
    flattering something.
    """
    pf = _must(portfolio_id, account)
    # Keyed by the LEG NAME, not by the (cls, tf, rule) triple, because the name is what
    # `register` is idempotent on. Keying on the triple lets the diff disagree with the
    # ledger — `IBS` and `ibs` are two targets and one registration, so every run would
    # "add" the second one, get the first back, and log a change that never happened.
    held = {leg["name"]: leg for leg in legs(portfolio_id)}

    wanted: list[tuple[str, tuple[str, str, str]]] = []
    keep: set[str] = set()
    for cell in target:
        key = (cell[0], cell[1], cell[2])
        name = _leg_name(pf, *key)
        # A sheet that names the same rule twice must not buy it twice, and must not make
        # the diff flap between two runs that saw exactly the same thing.
        if name not in keep:
            keep.add(name)
            wanted.append((name, key))

    dropped = [leg for name, leg in held.items() if name not in keep]
    for leg in dropped:
        _detach(portfolio_id, leg["strategy_id"])

    added = []
    for rank, (name, key) in enumerate(wanted, start=1):
        if name in held:
            continue
        added.append((_attach(pf, *key,
                              benchmark=(benchmarks or {}).get(key[0])), key, rank))

    # Resize once, after the whole diff, and only then log. A row written mid-batch would
    # record a `leg_capital` the next add immediately invalidated — and this log is the
    # only place a reader can find out what a leg was actually running on that day.
    shape = resize(portfolio_id)
    source = _source_of(pf)
    for leg in dropped:
        _log(portfolio_id, "removed", strategy_id=leg["strategy_id"], cls=leg["cls"],
             tf=leg["tf"], rule=leg["rule"], source=source, n_legs=shape["n_legs"],
             leg_capital=shape["leg_capital"], reason=reason)
    for row, (cls, tf, rule), rank in added:
        _log(portfolio_id, "added", strategy_id=row["strategy_id"], cls=cls, tf=tf,
             rule=rule, rank_at=rank, source=source, n_legs=shape["n_legs"],
             leg_capital=shape["leg_capital"], reason=reason)

    return {"portfolio_id": portfolio_id,
            "added": [row["strategy_id"] for row, _key, _rank in added],
            "removed": [leg["strategy_id"] for leg in dropped],
            "unchanged": len(wanted) - len(added),
            "n_legs": shape["n_legs"], "leg_capital": shape["leg_capital"]}


# ----------------------------------------------------------------------------- reads

def state_of(legs: list[dict]) -> str:
    """What the DESK has actually done with this basket, derived from its legs.

    Not read from the `portfolios.state` column, and the column is not written either.
    Nothing in this process may write a desk state, and the desk cannot write this one
    without learning what a portfolio is — which is exactly the coupling a leg being an
    ordinary registration exists to avoid. A stored copy would therefore be a second
    answer that nobody updates, and it was: every basket on the board read
    `live -> pending` for as long as it existed while all forty-five of its legs were
    running, because the column was written once at creation and never again.

    A basket is only `live` when ALL of it is. `partial` is a real and different state — a
    leg the desk refused leaves the pot split across holdings that are not all there — and
    reporting it as `live` would be the half-switched-off basket this module's toggle
    exists to prevent, arriving by the other door.
    """
    states = [str(l.get("state") or "pending") for l in legs]
    if not states:
        return "pending"
    if all(s == "live" for s in states):
        return "live"
    if any(s == "live" for s in states):
        return "partial"
    if all(s == "retired" for s in states):
        return "retired"
    if any(s == "rejected" for s in states):
        return "rejected"
    return "pending"


def get(portfolio_id: str, account: str | None = None) -> dict | None:
    """One portfolio, optionally constrained to an account.

    Passing the account is how a member-facing read stays a member-facing read: without
    it, a caller who guesses a `portfolio_id` reads somebody else's basket.
    """
    sql = "SELECT * FROM portfolios WHERE portfolio_id = ?"
    args: tuple = (portfolio_id,)
    if account is not None:
        sql += " AND account = ?"
        args = (portfolio_id, account)
    return deskdb._row(sql, args)


def legs(portfolio_id: str, include_retired: bool = False) -> list[dict]:
    """The registrations carrying this `portfolio_id`.

    "Live" here is `want <> 'retired'` — the OWNER's intent, not the desk's `state`. A leg
    the desk has not attached yet is still one of the things this portfolio holds, and its
    share of the money is already spoken for.

    `include_retired` is how the record is read back: a dropped leg keeps its
    `portfolio_id` forever, so what the basket held in March is still answerable in June.
    """
    sql = "SELECT * FROM registrations WHERE portfolio_id = ?"
    if not include_retired:
        sql += " AND want <> 'retired'"
    return [deskdb._shape(r)                                       # type: ignore[misc]
            for r in deskdb._rows(sql + " ORDER BY created_at, strategy_id",
                                  (portfolio_id,))]


def listing(account: str | None = None) -> list[dict]:
    """Every portfolio, each with its live legs attached under `legs`."""
    sql = "SELECT * FROM portfolios"
    args: tuple = ()
    if account is not None:
        sql += " WHERE account = ?"
        args = (account,)
    out = []
    for row in deskdb._rows(sql + " ORDER BY created_at, portfolio_id", args):
        row["legs"] = legs(row["portfolio_id"])
        # Derived from the legs, never read from the column. See `state_of`.
        row["state"] = state_of(row["legs"])
        out.append(row)
    return out


def changes(portfolio_id: str, limit: int = 200) -> list[dict]:
    """The log, newest first."""
    return deskdb._rows("SELECT * FROM portfolio_changes WHERE portfolio_id = ? "
                        "ORDER BY id DESC LIMIT ?", (portfolio_id, int(limit)))
