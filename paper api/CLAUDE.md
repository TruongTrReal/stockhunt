# CLAUDE.md

Guidance for Claude Code working in this directory. Read `../CLAUDE.md` first.
**No results here** — this folder serves the desk to other people; it measures nothing.

## What this is

The invitation-only HTTP layer in front of the paper desk. Two things live behind one
login: the **dashboard**, served from `../Stockhunt Dashboard/web/`, and the **API** that
will carry the strategy endpoints — register, list, remove — which hang off
`current_session` and are not written yet.

```
public caller --HTTPS--> paper api/  --reads--> Stockhunt Dashboard/web/   the board
                              |
                              |  (not yet, and never a direct call)
                              v
                     paper trading engine/     run_paper.py, the live desk
```

**This process owns no trading.** It cannot place an order today, and when the strategy
endpoints arrive they must write a *request* the desk picks up rather than reaching into
it. Same separation `Stockhunt Dashboard/serve.py` keeps, for the same reason: if this is
wedged, taken down or compromised, `run_paper.py` keeps trading.

## Files

```
api_paths.py    paths, .env.local, the server secret. Import first; imports no trading code
api_config.py   every tunable, with what it trades against
authdb.py       state/auth.db - users, api_keys, webhook_secrets, otp_challenges,
                sessions, audit
mailer.py       the code, over Gmail SMTP. `python mailer.py --check` tests the credentials
api_auth.py     /auth/*, `current_session`, `current_principal`, and the API-key endpoints
api_strategies.py  /v1/strategies - a manager registers, pauses, retires
api_orders.py   /v1/orders - the hot path. 202 means written down, never filled
api_webhook.py  /v1/webhook/tradingview - the ONE route whose credential is in the BODY,
                because a TradingView alert can send no header
api_house.py    /v1/house - the catalog, and promoting a backtested rule to the desk
api_live.py     the per-account cut of live.json. ONE implementation of that cut
api_board.py    the dashboard behind that session, plus /ws
api_app.py      the ASGI app: CORS, lifespan, /healthz, the exception handler
web/login.html  the sign-in screen. Self-contained on purpose
web/desk.html   the manager console. Registering is a WIZARD; see below
web/agent.md    THE integration contract, and the only copy of it
web/docs.html   the reference. A renderer for `agent.md` — it holds no prose of its own
run_api.py      start it
run.ps1         start it and, with -Tunnel, publish it. This is how the desk is shared now
admin_users.py  the allowlist, from the shell. The ONLY way an account is created
test_auth.py    you cannot get a session you were not given
test_board.py   the board is actually behind one
test_accounts.py    account ids are permanent and unique
test_strategies.py  keys, and the strategy control plane
test_orders.py      idempotency, ordering, scoping, honest status codes
test_webhook.py     the body-credential door: scope, rotation, derived idempotency
test_house_and_board.py  promotion, and the per-account cut
```

## The manager desk

Two audiences reach this process and they cannot share one credential. A browser signs in
with an emailed code and carries a cookie. A **manager's strategy runs unattended on their
own machine** and can complete no email flow, so it carries an API key.

`current_principal` is the seam every `/v1` endpoint hangs off. It routes by prefix — a key
is `sk_live_...` and a session token is not — so one lookup answers instead of two, and a
mistyped key can never be probed against the sessions table. It returns `account_id`, which
is the identity that leaves this process. **The email never does.**

```
GET    /v1/limits                  the terms: capital, classes, timeframes, the caps
GET    /v1/desk                    the desk's heartbeat, and your own pending count
POST   /v1/strategies              register. Comes back `pending`, never `live`
GET    /v1/strategies              yours
POST   /v1/strategies/{id}/pause   stop taking orders, keep the positions
POST   /v1/strategies/{id}/resume
DELETE /v1/strategies/{id}         retire. The record is kept, always
DELETE /v1/strategies/{id}?purge=true   remove one that NEVER traded -> 204

POST   /v1/strategies/{id}/webhook mint/rotate the TradingView secret. SESSION ONLY
GET    /v1/strategies/{id}/webhook does one exist, and has TradingView ever called
DELETE /v1/strategies/{id}/webhook revoke it

POST   /v1/orders                  202 = written down and sequenced. NOT filled
DELETE /v1/orders/{coid}           request a cancel; it may already have filled
GET    /v1/orders                  ?strategy_id= &state= &since_seq= to poll
GET    /v1/orders/{coid}

POST   /v1/webhook/tradingview     an alert. NO header; the secret is in the body

GET    /v1/house/catalog           promotable rules. Any member may read it
GET    /v1/house/strategies        what the house desk runs. Any member may read it
POST   /v1/house/strategies        promote a backtested rule. OWNER ONLY
DELETE /v1/house/strategies/{id}

POST   /auth/keys                  mint a key. SESSION ONLY - a key cannot mint a key
GET    /auth/keys                  yours, identifiable and useless to a reader
DELETE /auth/keys/{id}
```

**`client_order_id` is required and it is the idempotency key.** Sending one twice returns
the first order with `200` instead of `202`. Without it every network timeout on a
manager's side is a doubled position, invisible until somebody reconciles a book by hand.
`seq` is monotonic and the desk drains in that order, so a cancel cannot overtake its order.

## TradingView is the one caller that cannot send a header

An alert posts a JSON body to a URL and TradingView offers **no way to add one**, so an
alert cannot reach `/v1/orders` at all. `POST /v1/webhook/tradingview` exists for that
constraint and no other: same ledger, same `strategy_of`, same `rate_limit`, same `202`.

**The credential is a per-strategy secret, never the account key.** An alert message sits
in plain text in TradingView's UI, travels in exports and gets pasted into chat — it is
where a secret *leaks*, so the one that lives there is the weakest the desk can issue.
`sk_live_…` trades every strategy on the account, reads the book and retires
registrations; `whk_…` submits orders for **one** strategy and does nothing else. The
prefix is load-bearing twice over: `current_principal` routes a bearer credential by it, so
a `whk_` in an `Authorization` header falls through to the sessions table and answers 401.

Minted behind the browser login and never by a key or by itself, which is `/auth/keys`'
containment for a sharper reason — this is the credential most likely to leak, so
revocation must be an ending rather than a race. Minting again revokes the previous one, so
there is one live secret per strategy and rotating is a single act. It is keyed on
`account_id`, not `email`, and stays alive while **any** address on the account is active:
a webhook belongs to a strategy, a strategy belongs to an account, and killing a live alert
because one of two linked mailboxes was retired would be the wrong granularity.

**`client_order_id` is derived, because TradingView has no such concept.** That field is
the property the whole order API rests on, and an alert re-firing on one bar is exactly
what it defends against. So the id is built from strategy, symbol, side and the bar:

    "bar_time": "{{time}}"      the BAR's timestamp   -> dedupe = "bar"     stable
    "bar_time": "{{timenow}}"   when the alert FIRED  -> differs per firing, defeats it
    omitted                     -> dedupe = "minute", a one-minute bucket

The middle row is the trap and it has no error attached: `{{timenow}}` looks like the right
placeholder and silently turns two copies of one signal into two orders. The console's
generated message therefore uses `{{time}}` and says so in words, and the reply carries
`dedupe` so which promise applied is never a guess. The minute fallback is a knowing
trade-off in the other direction — two *deliberate* same-side orders on one symbol inside
one minute collapse into one — and it is taken because this desk trades `1d` and `4h` bar
closes, where a doubled position is the far likelier accident.

**The size comes from the alert and is required.** `{{strategy.order.contracts}}`. This
process cannot see the book, and a quantity invented here would be the API's opinion inside
somebody's track record.

**A chart ticker is not a desk symbol.** `{{ticker}}` is whatever the chart says —
`BINANCE:BTCUSDT`, `BTCUSDT.P`, `NASDAQ:AAPL`, `ES1!` — for a book holding `BTC/USD`.
`normalize_symbol` reduces both sides (venue prefix, perpetual and continuous suffixes,
punctuation, stablecoin quote folded to USD) and the match is against the registration's
own list. It is a comparison key and is **never stored**: the order carries the symbol as
the desk spells it, because that is the instrument the desk holds.

**Every refusal is a 4xx, deliberately.** TradingView never reads the `202`; its alert log
shows the status and nothing else. A soft "accepted, but I ignored it" would be a green
tick over an alert that traded nothing.

**The body is parsed by hand rather than by a declared pydantic parameter.** TradingView
labels the request `text/plain` unless it decides the message is JSON, and FastAPI hands a
model raw bytes in that case — a 422 reading "input should be a valid dictionary" for a
body that is perfectly good JSON, about a header the caller cannot set. So `_parse` reads
the body and never consults the content type. That is why the endpoint is `async` and the
database work goes through `run_in_threadpool`.

Field names are generous on input (`strategyId`/`strategy_id`, `password`/`secret`,
`contracts`/`qty`, `ticker`/`symbol`, `time`/`bar_time`) and extra fields are ignored,
because the body is typed by hand into an alert box with no client library to get it right.
An unsubstituted `{{…}}` is caught before any field parser sees it and named as what it
actually is: a strategy placeholder on an *indicator* alert.

**TradingView will only call port 80 or 443, on a publicly trusted certificate.** That is
its rule, not this desk's; `https://srv1903626.hstgr.cloud` already satisfies it and needs
no change. There is no such thing as HTTPS on port 80.

## Retiring keeps everything; the one exception is a row that recorded nothing

Deleting is refused because a forward test somebody can erase is not a record — a manager
who can remove a losing run can remove the evidence of it, which is survivorship bias
committed by hand on one's own track record.

That protects **evidence**, and `?purge=true` is the case with none: a registration that
never placed an order has no fill, no curve point and no row in the desk's `strategies`
table. `deskdb.delete_registration` re-checks all three conditions and returns the reason
rather than trusting the flag.

**`kind='member'` is the load-bearing guard, not the terminal state.** "No orders" proves
"never traded" only for a strategy that trades ON INSTRUCTION. A house rule or a book
trades itself, fills continuously and submits nothing through this ledger — so the same
emptiness that means "litter" for a member means "months of record" for those, and the
check would delete exactly the thing the rule exists to keep.

## Promoting is a table, and the catalog is not what breaks

Three things on the owner's half of `/desk`, each of which was misreporting itself:

**`symbols` arrives decoded.** `deskdb.registrations` parses the stored JSON, so the row
carries an array. The house table called `JSON.parse` on it anyway, and `JSON.parse([])`
stringifies to `""` and throws — so every house row threw, and the ONE `try` wrapped around
the whole admin block reported it as **"No catalog yet — run `python catalog.py`"** and
disabled Promote. The catalog was intact the entire time. Two fetches now get two catches:
*a catch can only name the cause of a failure it was narrow enough to see.*

**The rule picker reads the catalog's CURRENT fields.** The old `<select>` label was
`ir ${c.ir_net}`, and `ir_net` stopped being what a cell carries when the research moved to
the book — cells hold `edge_passed`, `book_cm_excess_cagr`, `long_frac` and `t_stat`. All 25
options printed "ir undefined", which reads as missing data rather than as a stale page. It
is a five-column table now, sorted the way the leaderboard sorts (criteria cleared, then
money at equal risk), with the columns NAMED in the markup — a header with nothing under it
fails visibly.

**One register button.** The heading had one and the empty state had another, so the first
screen a manager sees offered the same action twice.

## The console is a reader of a ledger, so it reads continuously

`want <> state` is the only thing the registrations table says while a request is
outstanding, and it says exactly the same thing whether the desk read the row a moment ago
or has not been running since Tuesday. The page printed one sentence for both — *the desk
applies it on its next pass* — which is a forecast this process has no standing to make:
it does not run the desk and cannot start it. Worse, the sentence was printed on every
click, including the ones where the desk had already finished.

Three things fixed it and all three are needed:

* **`/v1/desk` reads `deskdb.pulse()`.** The desk stamps a heartbeat when each pass ends,
  so "in flight" and "nobody is home" stop looking alike. It is a display signal only —
  nothing here waits, retries or refuses on it, because an API that needed the desk up to
  accept a write would be the coupling this folder exists to avoid.
* **The page polls.** It used to fetch at load and once more immediately after an action —
  the one moment the desk cannot possibly have acted yet — and then hold that snapshot
  until somebody reloaded. It now refreshes every 2s while the tab is visible, and skips
  the DOM write when nothing changed so a selection is not dropped every pass.
* **An action waits and then reports.** `settle()` watches until `state` reaches what
  `want` asked for and says what happened; if it does not converge, the sentence names the
  reason — refused with the desk's own words, or the desk is down.

`DESK_STALE_SECONDS` is the tolerance and it is twenty missed passes, wide enough that a
slow drain never reads as an outage.

**Keys are minted behind the browser login and never by a key.** That is the containment: a
stolen key can trade its owner's paper book, but it cannot issue a replacement that
survives revoking the first, so revocation is final rather than a race. `revoke` kills the
account's keys as well as its sessions, and `api_key` joins against `users` on every
request — two independent reasons a revoked account stops working.

## Registering is a wizard, and the reason is what it puts in front of you

`/desk` used to be a five-field form dropped in the middle of the page, with key
management a separate section further down and the order format reachable only through the
OpenAPI dump at `/docs`. All three are needed, in that order, and nothing said so — a
member could register, see `pending`, and never learn that it would not trade because they
had not minted a key or read what a `202` means.

So it is one path with an end: **name it → read the terms → take the key → copy working
code → watch the first call arrive.** Five screens in a full-bleed overlay, content
centred, one decision per screen.

**The terms screen is the point of the redesign, not decoration.** Everything that
surprises somebody about this desk — a `202` is not a fill, the desk re-checks and can
still refuse, retire keeps the record forever — is true *before* the first order and was
previously discoverable only after it. It is stated once, up front, where a decision is
still being made.

Three things it must keep doing:

* **It states no number it does not fetch.** Capital, the class list, the timeframes and
  both caps come from `GET /v1/limits`, which reads `api_config` and `api_strategies`. All
  of them are settable from the environment; written into the markup they would go on
  saying `$10,000` and offering `commodities` the day either changed, and a page that
  misstates the terms is worse than one that omits them. `test_strategies.py` asserts the
  endpoint and the config agree.
* **Symbols are picked, never typed.** The desk subscribes to a fixed universe and refuses
  everything else — a new symbol costs an instrument, a subscription and a full warm-up —
  so a free-text field could only ever produce a registration accepted here and rejected
  there, minutes later, in a `reason` nobody is still watching. `/v1/limits` carries
  `universe` per class, read from what the desk **published** (`catalog.json`), never
  computed here. When the desk has not published one the field falls back to free text:
  a research artifact that has not been rebuilt must not be able to stop somebody
  registering, and the desk checks anyway.
* **Timeframes come from the desk's list, not this one.** `api_config.TIMEFRAMES` must stay
  a subset of `paper_config.MEMBER_TIMEFRAMES`; `test_strategies.py` reads the desk's file
  off disk and asserts it, because this process cannot import it. Widen the desk first.
* **The wizard mints, it does not manage.** The keys table on the page behind it is still
  the place to revoke, and both call the same `/auth/keys`. Closing the wizard with a key
  minted and uncopied is the one exit that interrupts, because that secret is genuinely
  unrecoverable.
* **Each secret appears in exactly one tab, and neither is in the agent brief.** The API
  key is in `.env`; the webhook secret is in `TradingView`. The brief exists to be pasted
  into a model's context, and a credential pasted into a chat log is one that has to be
  treated as revoked — which is also why Python and cURL read the key from
  `$STOCKHUNT_API_KEY` rather than carrying it.

  **The TradingView tab is a different integration, not a fifth language.** The three code
  tabs all send a *header*, which is the one thing an alert cannot do, so this one hands
  over a different credential and a message to paste. Two secrets are therefore on one
  screen and must never be swapped — the key trades everything the account owns, the
  webhook secret places orders for one strategy — so the note under the block names which
  one is in it. It is minted on a **press**, never on opening the tab: clicking a tab to
  see what is behind it must not leave a live credential on a strategy nobody is
  connecting. And the message is the server's `_alert_body`, fetched rather than composed
  here, because a second copy on this page is the one that would still say `{{timenow}}`
  the day the first one stopped.

  The block holds **nothing but the JSON**, and the webhook URL is a separate copy button
  rather than a comment line above it — a comment would be copied straight into
  TradingView's Message box with everything else, and the alert would fail on a body that
  is no longer JSON.

The last screen polls two things a copied snippet cannot promise: whether the **credential**
has been used since the screen opened, and whether an order for *this* strategy reached the
ledger. That is the integration proven end to end rather than asserted.

Credential, not *key*, and the distinction is load-bearing now: a TradingView integration
never sends the API key, so a check that only watched `/auth/keys` would sit on
"Watching…" through a working alert — the exact outcome the panel exists to rule out. When
a webhook secret was minted in the run, its `last_used_at` ticks the same line. The webhook
is asked about only when one exists, so an ordinary run makes no extra request.

## There is one integration document, and the page renders it

`web/agent.md` is the contract. `GET /desk/agent.md` serves it as markdown with `{{BASE}}`
substituted for the origin the caller actually reached, and `web/docs.html` fetches that
same URL and renders it — the page contains no prose of its own.

**Because the alternative is two copies that drift, with the machine-readable one being
the copy nobody proofreads.** The same words are read by a person scrolling a reference and
by a model being handed a brief; the moment those are separate files, the human page gets
the correction and the agent gets last month's contract.

`/desk/agent.md` is the **one board route that takes either credential** — it hangs off
`current_principal`, not `optional_session`. That is the feature: a manager can point their
agent straight at the URL with the same key it will trade under. It answers `401` rather
than redirecting, because it is fetched rather than navigated to, and a `fetch` handed a
login page renders HTML into the docs body. A key still opens nothing else: `/desk` and
`/desk/docs` redirect, as `test_board.py` asserts.

The base URL is substituted per request rather than baked in because the document goes
straight into code somebody runs, and behind the tunnel the scheme is knowable only from
the forwarded header — the same per-request reading the session cookie's `Secure` flag
does, sharing `api_auth.public_base_url`.

## `/live.json` is generated per reader, not served off disk

The desk publishes ONE document describing every system it runs, each tagged with the
account that owns it. `api_live.visible_to` cuts it down to the caller's own systems plus
the house (`00`), and that is the only place the cut is made.

**Filtering the list is not enough.** The document also carries venue totals summed over
every system on the desk. Passing those through would report one manager the size of
everybody else's book — a leak with no strategy names attached, which is the kind that
survives review. They are re-derived from the rows that survive.

**The socket needs the same cut**, and it is the easier one to forget because it is a file
watcher rather than a handler. Forgetting it streams the whole desk to every reader for as
long as their tab is open.

**`PER_ACCOUNT` in `api_board` is load-bearing.** `_register_static()` generates a GET route
for every path in `web_files.ALLOWED`, and `/live.json` is on that list. FastAPI matches in
registration order, so a generated static route would shadow the filtered handler and serve
the raw file. `test_house_and_board.py` asserts the path is registered exactly once.

An admin gets **no wider view**. Seeing everything is a different feature with a different
endpoint; quietly widening this one would mean the board an owner reviews is not the board a
member sees, and leak-shaped bugs would only ever appear where nobody is looking.

## Commands

```powershell
python admin_users.py allow you@example.com --label "MK" --admin
python admin_users.py list
python admin_users.py revoke someone@example.com     # deactivate + sign out
python admin_users.py audit --limit 30               # why no code arrived
python mailer.py --check                             # do the SMTP credentials work
python run_api.py                                    # 127.0.0.1:8080, board at /
.\run.ps1                                            # ...the same, as a background job
.\run.ps1 -Tunnel                                    # + a public URL. THE way to share
.\run.ps1 -Stop
..\.venv\Scripts\python -m pytest test_auth.py test_board.py -q
```

Run from **this** directory: `run_api.py` starts uvicorn on the import string
`"api_app:app"`, so the modules must resolve by bare name from the process cwd. The venv
is `..\.venv` — same one as the desk, because the strategy endpoints will import
`paper_config` and a second environment would mean keeping two copies of
`nautilus_trader` in step.

The suite is **not** in the root `testpaths`. It would make `pytest` at the repo root
require `fastapi` to be installed to collect, and the unit suite deliberately depends on
numpy and pandas only.

## Signing in is two calls

```
POST /auth/otp      {"email": "..."}            -> 202, always
POST /auth/session  {"email": "...", "code": "123456"}  -> {"token": ..., "expires_at": ...}
POST /auth/browser  the same, delivered as an HttpOnly cookie. What /login calls
GET  /auth/me       Authorization: Bearer <token>
GET  /auth/sessions                             every live token on the account
DELETE /auth/session                            sign out    (204; also clears the cookie)
DELETE /auth/sessions                           sign out everywhere
```

## The board is behind that session, and it had to move here to be

`GET /` is the dashboard. `GET /login` is the only page an unauthenticated caller can
fetch; everything else under `web_files.ALLOWED` needs a session, and so does `/ws`.

**Why it moved into this process.** A page cannot put an `Authorization` header on
`<script src="app.js">`, on a stylesheet link, or on a WebSocket handshake — the browser
issues those, not the application. So gating the board needs a **cookie**, a cookie is
scoped to an origin, and a quick tunnel exposes exactly one port. Serving the board from
any other process means the login page and the board cannot both be reachable from
outside this machine.

**So `serve.py` is loopback-only now, and refuses to be anything else.** It authenticates
nobody, and `run.ps1 -Tunnel` in the dashboard folder used to point a public URL at it —
so the URL was the entire security model. Both now refuse and say where to go. The board
is the same directory and the same files either way; only the gate differs.

**One session, two carriers.** `POST /auth/session` returns the token in the body for
scripts; `POST /auth/browser` sets it as a cookie for the page. Same `sessions` table,
same expiry, same revocation. Two endpoints rather than a flag, because they hand out the
same secret under materially different terms and a boolean in a request body is too quiet
a place to record that.

The cookie is **HttpOnly**, so the board's own JavaScript cannot read it — a script
injected into the page has no way to lift the session and carry it off. It is
**SameSite=Lax**, and that is what stands in for a CSRF token: the cookie does not ride
along on a POST or DELETE issued by somebody else's page, while still being sent on a
top-level navigation to the board. Its `Secure` flag is decided **per response** from the
request scheme, because behind the tunnel the hop to uvicorn is plain HTTP and only
`X-Forwarded-Proto` says otherwise — a cookie hardcoded `Secure` would never be set
locally, and one that never was would travel in clear on the LAN.

**Revocation reaches an open socket.** `/ws` re-checks the session every ~30 polls, so a
reader who is signed out or revoked stops receiving the live desk instead of streaming
until they close the tab. Everything else is per request and is checked anyway.

**Responses behind the login are `private`**, never `public`: this is one reader's view,
and a shared cache between here and them must not keep a copy for the next person.

**The routes are generated from `web_files.ALLOWED`**, not written out. A catch-all
`/{path:path}` would work and would also silently shadow any API route added later.

## `demo_data.js` is still not served, from either process

`web_files.py` in the dashboard folder is the single definition of what may be served and
of how a URL becomes a file. Two servers read that directory now; two copies of a
traversal check is one implementation and one liability, and the allowlist is what keeps
`demo_data.js` — the layout fixture full of invented numbers — off the wire.

## There is no registration endpoint, and that is the feature

An address works because the owner ran `admin_users.py allow`. **The allowlist is the user
table** — a row in `users` is the invitation. Adding a user is the one action with no
recovery path if the wrong person does it, so it needs shell access to the box, which puts
the blast radius of any bug in the web layer short of the allowlist.

`revoke` deactivates **and** sweeps the sessions, and `authdb.session()` joins against
`users` on every request. Both halves are needed: without them a revoked user keeps working
until their token expires, which is up to thirty days of access granted by the command
whose entire purpose was to take it away.

## Four properties, each of which fails silently if broken

**1. `POST /auth/otp` answers identically for every address.** Same status, same body,
whether the address is on the list, was revoked yesterday, or has never been seen. The
endpoint is unauthenticated and reachable by anyone with the URL, so *any* difference in
its reply answers "is this person on the desk".

That is why the per-email cooldown and hourly cap are enforced by **not sending the mail**
rather than by returning 429: a 429 only real users can trigger is the same oracle wearing
a different status code. The one limit that does answer back is scoped to the **caller's
IP**, not to the mailbox they asked about, so tripping it reveals nothing about who is
registered.

**2. Mail is sent off the request path**, as a `BackgroundTasks` job. Latency is the small
half. An SMTP round trip takes about a second, and doing it inline would make the
allowlisted branch measurably slower than the other one — restoring by stopwatch exactly
the leak the identical body was there to close. The residual difference is a few SQLite
writes, orders of magnitude below what is measurable across a network.

The cost is that a delivery failure cannot be reported in the response. It lands in the
audit table as `otp.send_failed`; `admin_users.py audit` is where to look when somebody
says no code arrived.

**3. Every verify failure is one 401 with one wording** — unknown address, wrong code,
expired code, attempts exhausted. `authdb.verify` returns a reason and it goes to the audit
table, never to the caller.

**4. A new code retires the old one.** Six digits is a million possibilities and the whole
margin comes from `OTP_MAX_ATTEMPTS = 5` against **one** live challenge. Leaving previous
challenges live would let an attacker request twenty codes and take twenty times the
guesses. The attempt is also counted *before* the comparison, so cutting the connection
mid-request does not buy free tries.

## What is hashed, and what is not

| stored as | why |
|---|---|
| OTP → **HMAC-SHA256** under `api_paths.server_secret()` | six digits is a lookup table under a plain hash; keying it means a stolen `auth.db` is useless without the secret, which is a different file |
| session token → **plain SHA-256** | 256 bits of `secrets` output, so there is no dictionary to run and the slow-hash argument does not apply — while a *fast* hash does, since it runs on every authenticated request |
| email → **clear text** | the point of the table is telling the owner who is on it |

`state/server_secret` is generated on first use, not defaulted. A hardcoded fallback would
make every stolen database brute-forceable against a known key. Rotating it invalidates
outstanding codes and nothing else.

`state/` is **gitignored**. Unlike the desk's `paper.db`, which is tracked because it is
the forward-test record, this is a credential store: committing it publishes the allowlist
and every live session hash.

## Configuration

Environment, or `../.env.local` — same precedence as the rest of the repo. Everything is
in `api_config.py` with its reasoning; the ones that must be set:

| | |
|---|---|
| `GMAIL_USER`, `GMAIL_APP_PASSWORD` | an **app password**, not the account password. Google has refused plain-password SMTP since 2022, and an app password is separately revocable |
| `API_OWNER_EMAIL` | seeded as the first admin, and **only when the table is empty** — a seed that ran every start would resurrect an account just revoked |
| `API_TRUST_PROXY` | off by default. `X-Forwarded-For` is a request header, so honouring it without a proxy in front lets a caller choose their own rate-limit bucket |
| `API_CORS_ORIGINS` | empty by default, and never `*`: these calls carry credentials. A script caller is unaffected by CORS, so empty means "no browser origin is expected yet", not "locked". The board is same-origin and needs none of it |
| `API_SERVE_BOARD` | on. Off makes this a pure API and leaves the dashboard reachable only on loopback |

**`API_DEV_ECHO_OTP` returns the sign-in code in the HTTP response.** It exists so the flow
can be exercised before SMTP credentials are in place, and it is a complete bypass of
authentication for anyone who can reach the port. `run_api.py` **refuses to start** if it
is set and the bind host is not loopback, and the startup banner shouts about it. A warning
would be read once and scrolled past.

## Gotchas

- **`api_paths` imports no trading code**, deliberately. `use_paper_engine()` is the seam
  for when the strategy endpoints need `paper_config`, and it is called at the point of
  use. Importing it at module scope would put the whole backtest engine — universes,
  membership tables, `strategies` — inside a process whose job is to answer HTTP, and it
  would make this folder untestable without it.
- **Named `api_paths.py`, not `config.py`.** Same reason as the other three bootstraps; see
  the table in `../CLAUDE.md`. Module basenames must stay globally unique across every
  folder that can land on `sys.path` together, which is why everything here is `api_*`
  except the three names that are already unique (`authdb`, `mailer`, `admin_users`).
- **`authdb` runs in autocommit** (`isolation_level = None`) so `verify` can issue its own
  `BEGIN IMMEDIATE`. Python's implicit transaction handling cannot be nested inside that.
- **Times are ISO-8601 UTC strings**, as in the desk's `store.py`. Fixed width, always
  `+00:00`, so lexicographic comparison is chronological and expiry is a `WHERE` clause.
- **`normalize_email` case-folds and trims, and does nothing else.** Gmail's dot- and
  plus-aliasing is deliberately not collapsed: that rule is true at one provider and false
  at others, and applying it would silently widen an allowlist written by hand.
- **A token issued over plain HTTP is a token on the wire.** Bind loopback and put a
  tunnel or a reverse proxy in front for anything else. `run.ps1 -Tunnel` in **this**
  folder is the worked example: cloudflared terminates TLS, and the launcher sets
  `API_TRUST_PROXY` only in that case, because the forwarded headers it turns on are
  forgeable by any caller when there is no proxy in front.
- **`run.ps1 -Tunnel` refuses to publish an empty allowlist.** A login nobody can pass
  looks exactly like a login that is working.
- **The single-file build is still ungated.** `dist/dashboard.html` is a file you hand
  someone: no server, no session, every number inlined. Nothing here changes that, and
  sending it is still the act of sharing the whole board with whoever receives it.
- **`web/` here is this folder's own page**, and `../Stockhunt Dashboard/web/` is the
  generated board. Only the second is ever written by a build, and this process writes
  neither.
