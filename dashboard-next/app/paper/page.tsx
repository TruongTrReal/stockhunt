"use client";

/* #/paper — every system on the desk, ranked.
 *
 * THE DESK IS NOT A FUND, and this page is built around that. There used to be a strip of
 * five tiles at the top, two of which added the desk up: a mean P&L across every system,
 * and a dollar total on the capital deployed. Both are gone on purpose. Its systems are
 * independent forward tests that happen to share a process, so their sum tracks how many
 * are switched on and their mean reads "flat" when half are up and half are down. Neither
 * decided anything, and both drew the eye first. What is left is one muted `.deskline` of
 * housekeeping — systems live, the class split, the fill count, the feed — and everything
 * that means something is per system, in the row.
 *
 * The route is a query string for the same reason the rule page's is: `output: "export"`
 * pre-renders every route, and a rule label carries `|` and `~` which a path segment does
 * not survive intact.
 */

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { PnlLive } from "@/components/PnlChart";
import {
  aggregate,
  assetCount,
  classLabel,
  countSystems,
  feedAgeMs,
  feedPlan,
  feedSource,
  feedStale,
  feedStatus,
  fillsOf,
  fmtAge,
  fmtPct,
  hasAccount,
  isMine,
  isReplay,
  money,
  paperClasses,
  paperTimeframes,
  sign,
  slug,
  strategiesOf,
  systemBreaks,
  systemCurve,
  systemKey,
  useLive,
  usePaperCurves,
  venueEquity,
  venueName,
  whoPills,
  type Sys,
} from "@/lib/live";
import type { Live } from "@/lib/api";

/* MODULE SCOPE, exactly as `pf` is in the vanilla board: leaving a system's page and
 * coming back must not throw the reader onto us_stocks 1d again. A component's state
 * would, because this page unmounts on the way out. */
const pf = { cls: "us_stocks", tf: "1d", who: "mine" };

export const systemHref = (cls: string, tf: string, rule: string) =>
  `/paper/sys/?cls=${encodeURIComponent(cls)}&tf=${encodeURIComponent(tf)}&rule=${encodeURIComponent(slug(rule))}`;

/* ------------------------------------------------------- is anyone home? */

function FeedValue({ doc }: { doc: Live | null }) {
  if (feedStale(doc)) return <span className="v loss">stale</span>;
  return (
    <span className={`v ${feedStatus(doc) === "ok" ? "gain" : ""}`}>{feedStatus(doc)}</span>
  );
}

function FeedNote({ doc, sub }: { doc: Live | null; sub: string }) {
  const age = feedAgeMs(doc);
  if (feedStale(doc) && age != null)
    return <span className="s loss">no update in {fmtAge(age)}</span>;
  return <span className="s">{sub}</span>;
}

export function StaleBanner({ doc }: { doc: Live | null }) {
  const age = feedAgeMs(doc);
  if (!feedStale(doc) || age == null) return null;
  return (
    <div className="note">
      <b>Figures as of {doc?.generated_at}.</b> The desk has not published for{" "}
      {fmtAge(age)}, so everything below is that snapshot rather than a live quote.
    </div>
  );
}

/* A run replayed from cached bars is NOT paper trading: it knows the whole price history,
 * it completes in seconds, and its P&L covers years rather than days. It is shown because
 * it proves the order path — but it has to be labelled every time it appears, or the first
 * person to screenshot this page reports a 283% gain as a live result. */
export function ReplayBanner({ doc }: { doc: Live | null }) {
  if (!isReplay(doc)) return null;
  return (
    /* STILL A BANNER, not a tooltip. Everything else on these pages moved to hover, and
       this one may not: it is the label that stops a screenshot of a 283% replay being read
       as a live result, and a warning nobody can see is not a warning. What it does lose is
       three of its four lines. */
    <div className="note">
      <b>Replay, not live.</b>{" "}
      <span
        className="explains"
        title={"Run over cached historical bars inside Nautilus — the run that proves bars, "
               + "signals, orders and fills all connect. Nothing is trading against a live "
               + "feed yet, so read the P&L as a test of the execution path and nothing else."}
      >
        cached bars, not a live feed — the P&amp;L tests the execution path
      </span>
    </div>
  );
}

/* ----------------------------------------------------------- the empty desk */

/* Nothing is running until the Nautilus node has filled an order and written
 * `results/paper_state.json`. That is the honest state, and it gets its own screen rather
 * than a dashboard of zeroes and em-dashes pretending to be a live desk. */
function PaperEmpty({ doc }: { doc: Live | null }) {
  return (
    <>
      <div className="hero">
        <h1>Paper trading</h1>
        <p className="lede">
          Nothing is trading yet. The sandbox writes its state only once it has filled an
          order, and it has not run long enough to do that.
        </p>
      </div>

      <div className="strip">
        <div className="stat">
          <span className="k">Systems live</span>
          <span className="v">0</span>
          <span className="s">not started</span>
        </div>
        <div className="stat">
          <span className="k">Data feed</span>
          <FeedValue doc={doc} />
          <FeedNote doc={doc} sub={`${feedSource(doc)} · ${feedPlan(doc)}`} />
        </div>
        <div className="stat">
          <span className="k">Sandbox equity</span>
          <span className="v">{money(venueEquity(doc))}</span>
          <span className="s">{venueName(doc)}</span>
        </div>
      </div>

      <div className="note">
        The forward test has not yet accumulated a record worth reading. The multi-year
        evidence is the research — see <Link href="/">Backtest</Link>.
      </div>
    </>
  );
}

/* ------------------------------------------------------------- the hero */

/* Paper and research are separate sections rather than two panels on one screen. They are
 * different periods and different sample sizes — weeks of simulated fills against years of
 * walk-forward out-of-sample — and putting them side by side invites the conclusion that a
 * good paper fortnight validates a rule the research scores negative. */
function Hero({ replay }: { replay: boolean }) {
  return (
    <div className="hero">
      <h1>{replay ? "Strategy replay" : "Paper trading"}</h1>
      <p className="lede">
        {replay
          ? `The live strategy class, the live signal layer and the live order path — run over cached bars in a Nautilus backtest engine. Same code, historical clock.`
          : /* Two vendors, not one. The desk fed four classes from Twelve Data until
               `cme_futures` joined it, and that class cannot come from there — every CME
               root resolves to an equity wearing the same letters — so it is fed from
               Databento on its own venue. */
            `Live simulated fills from the Nautilus sandbox on real vendor bars — Twelve Data for equities, ETFs, crypto and spot commodities, Databento for the CME futures.`}{" "}
        This section is about whether the <em>execution path</em> works. Whether the rules
        work is a different question, answered in <Link href="/">Backtest</Link>.
      </p>
    </div>
  );
}

/* ------------------------------------------------------------- one row */

function SystemRow({ rows }: { rows: Sys[] }) {
  const router = useRouter();
  /* Off the ROW, never off the key. `systemKey` joins on "|" and a pair's rule name
   * contains one — `MININDEX~SAREXT|and` — so splitting the key back apart truncated every
   * pair at its operator, and the list printed `MININDEX~SAREXT` for two systems that
   * differ only in whether the legs vote or agree. */
  const { cls = "", tf = "", rule = "" } = rows[0];
  const a = aggregate(rows);

  /* The system's OWN record. `paper_curve` is cumulative paper P&L in percent since its
   * first fill; `bench_curve` is the same basket held over the same bars. This is the ONE
   * place on the paper side the market line is drawn — 34px, where it is context rather
   * than a verdict. */
  const live = systemCurve(rows, "paper_curve");
  const bench = systemCurve(rows, "bench_curve");
  const breaks = systemBreaks(rows);
  const href = systemHref(cls, tf, rule);

  /* `role="link"` on a div rather than an `<a>`, which is what the vanilla does and is not
   * a shortcut: `app.css` gives every anchor a hairline bottom border, so a real link here
   * would draw a rule under the whole row. The keyboard path is bound explicitly for the
   * same reason `bindGo` does — a focusable thing that only answers the mouse is worse
   * than one that cannot be focused at all. */
  return (
    <div className="grp">
      <div
        className="grp-row"
        role="link"
        tabIndex={0}
        aria-label={`${rule}, ${tf} ${classLabel(cls)} — open this system`}
        onClick={() => router.push(href)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            router.push(href);
          }
        }}
      >
        <span className="grp-id">
          <span className="grp-name">{rule}</span>
          <span className="grp-meta">
            {tf} · {classLabel(cls)} · {assetCount(rows)} · {a.fills} fill
            {a.fills === 1 ? "" : "s"}
          </span>
        </span>
        <span className="grp-live">
          {live.length > 1 ? (
            <PnlLive curve={live} bench={bench} breaks={breaks} w={300} h={34} />
          ) : (
            <span className="hist-lbl">no curve yet</span>
          )}
        </span>
        <span className={`grp-pnl num ${sign(a.mean)}`}>{fmtPct(a.mean)}</span>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------- the page */

export default function PaperPage() {
  const { doc, meta, ready } = useLive();
  // Fetched here as well as on a system's page: a reader who never opens one still pays
  // nothing, and the first system they open finds it already cached.
  usePaperCurves();

  const [, force] = useState(0);
  const repaint = () => force((n) => n + 1);

  const all = strategiesOf(doc);
  const replay = isReplay(doc);

  /* ---------- the order of the list ----------
   * These are independent forward tests and the only question a reader brings to them is
   * "which of mine is working", so they are ranked on their own P&L rather than by name.
   *
   * The catch is that the numbers move several times a second on the tick stream, and a
   * list that re-sorts under the cursor cannot be read: a row you are reaching for slides
   * away. So THE RANKING IS FROZEN. It is recomputed when the reader does something —
   * opens the view, clicks a filter — and every tick repaint in between reuses the same
   * order while the figures inside the rows keep moving. A system that appears mid-session
   * (a promotion) has no frozen rank and goes to the bottom until the next re-rank, which
   * is visible rather than surprising.
   */
  const rankRef = useRef<Map<string, number>>(new Map());
  const frozenAt = useRef<string>("");

  const rows = all.filter(
    (s) =>
      s.cls === pf.cls &&
      s.tf === pf.tf &&
      (!hasAccount(doc) || (pf.who === "mine" ? isMine(s, doc) : !isMine(s, doc))),
  );

  /* The token is everything a REFREEZE depends on: the filter, and the arrival of the
   * document. A tick changes neither, which is exactly why it reuses the order.
   *
   * `ready` is in it because this app, unlike the vanilla board, opens with NO rows: the
   * desk's document is fetched rather than baked in. Without it the first ranking would be
   * frozen over an empty list and every system would land at the bottom in key order. */
  const token = `${pf.cls}|${pf.tf}|${pf.who}|${ready ? 1 : 0}`;
  if (frozenAt.current !== token) {
    const keys = [...new Set(rows.map(systemKey))];
    const next = new Map<string, number>();
    keys
      .map((k) => [k, aggregate(rows.filter((s) => systemKey(s) === k)).mean] as const)
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .forEach(([k], i) => next.set(k, i));
    rankRef.current = next;
    frozenAt.current = token;
  }
  const at = (k: string) =>
    rankRef.current.has(k) ? (rankRef.current.get(k) as number) : Number.MAX_SAFE_INTEGER;
  const ordered = [...new Set(rows.map(systemKey))].sort(
    (a, b) => at(a) - at(b) || a.localeCompare(b),
  );

  const pick = (which: "cls" | "tf" | "who", value: string) => {
    pf[which] = value;
    // A filter click is the reader acting, which is the one thing that re-ranks.
    frozenAt.current = "";
    repaint();
  };

  /* Two absences, two screens. Nothing has ARRIVED yet is a line under the hero; nothing
   * is RUNNING is `PaperEmpty`, which says so and means it. Drawing the ranking's heading
   * over an empty list while the fetch is in flight would say the second when the truth is
   * the first. */
  if (!ready) {
    return (
      <>
        <Hero replay={false} />
        <div className="note busy-note">Reading the desk…</div>
      </>
    );
  }
  if (!all.length) return <PaperEmpty doc={doc} />;

  /* ---------- the housekeeping line ----------
   * Not a summary of performance. How many are live, how many fills exist, and whether the
   * feed is up — the three things a reader needs before trusting the rows below. */
  const running = all.filter((s) => replay || s.status === "running");
  const fills = all.reduce((x, s) => x + fillsOf(s), 0);
  const since = all.map((s) => s.since).filter(Boolean).sort()[0];

  const classes = paperClasses(all);
  const timeframes = paperTimeframes(all, meta);
  const who = whoPills(doc);

  return (
    <>
      <Hero replay={replay} />

      <ReplayBanner doc={doc} />
      <StaleBanner doc={doc} />

      {/* ONE MUTED LINE, and what is NOT on it is the point. There were five stat tiles
          here, two of which added the desk up — a mean P&L across every system, and a
          dollar total on the capital deployed. Nobody decided anything with either: the
          desk is not a fund, its systems are independent forward tests that happen to
          share a process, so their sum tracks how many are switched on and their mean
          reads "flat" when half are up and half are down. What is left is the
          housekeeping a reader needs before trusting the rows below. */}
      <p className="deskline">
        <span>
          <b>{countSystems(running)}</b> of {countSystems(all)}{" "}
          {replay ? "systems" : "systems live"}
        </span>
        <span>
          {classes
            .map((c) => `${countSystems(all.filter((s) => s.cls === c))} ${classLabel(c)}`)
            .join(" · ")}
        </span>
        <span>
          <b>{fills}</b> fills{since ? ` since ${since}` : ""}
        </span>
        <span>
          feed <FeedValue doc={doc} />
          {feedStale(doc) ? "" : ` · ${feedSource(doc)}`}
        </span>
      </p>

      {/* BOTH STRIPS READ THEIR OPTIONS FROM THE DATA, never from a literal. The timeframe
          row was the pair `1d / 4h` — the two horizons the HOUSE promotes its own books at
          — while the desk accepts a registration at any of six, so a member registering at
          1h got a strategy that ran, filled and published and a board with no button that
          could reach it. The list is what the desk CAN run, not what it happens to be
          running: "nothing is deployed at 1h" is a fact worth being able to check. */}
      <div className="filters">
        {who.length ? (
          <span className="f-group">
            <span className="f-label">Whose</span>
            {who.map(([v, label]) => (
              <button
                key={v}
                type="button"
                className={`pill${pf.who === v ? " on" : ""}`}
                onClick={() => pick("who", v)}
              >
                {label}
              </button>
            ))}
          </span>
        ) : null}
        <span className="f-group">
          <span className="f-label">Asset</span>
          {classes.map((c) => (
            <button
              key={c}
              type="button"
              className={`pill${pf.cls === c ? " on" : ""}`}
              onClick={() => pick("cls", c)}
            >
              {classLabel(c)}
            </button>
          ))}
        </span>
        <span className="f-group">
          <span className="f-label">Timeframe</span>
          {timeframes.map((t) => (
            <button
              key={t}
              type="button"
              className={`pill${pf.tf === t ? " on" : ""}`}
              onClick={() => pick("tf", t)}
            >
              {t}
            </button>
          ))}
        </span>
      </div>

      <section className="sec">
        <div className="sec-head">
          <h2>{replay ? "Replayed systems" : "Running systems"}</h2>
          <span className="sec-note">
            {countSystems(rows)} systems, best first · each one is its own record · tap a
            system for its record and every name it holds
          </span>
        </div>

        {ordered.length ? (
          ordered.map((key) => (
            <SystemRow key={key} rows={rows.filter((s) => systemKey(s) === key)} />
          ))
        ) : (
          <p className="sec-note">Nothing matches this filter.</p>
        )}
      </section>

      <p className="sec-note" style={{ maxWidth: "62ch" }}>
        {replay ? (
          <>
            Simulated fills over historical bars — the proof that orders reach positions.
            Not a forward result and not evidence about a rule; see{" "}
            <Link href="/">Backtest</Link> for the walk-forward answer.
          </>
        ) : (
          <>
            Paper P&amp;L is days old and is not evidence about a rule — see{" "}
            <Link href="/">Backtest</Link> for the multi-year result. Simulated fills only,
            no real money.
          </>
        )}
      </p>
    </>
  );
}
