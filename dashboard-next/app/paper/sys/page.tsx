"use client";

/* /paper/sys/?cls=&tf=&rule= — ONE SYSTEM: its live record, and every name it holds.
 *
 * The list used to be an accordion, and the accordion had become a detail view wearing a
 * list item's clothes. Every system on this desk is a book holding a whole asset class, so
 * opening one unfolded a hundred-name table INSIDE the ranked list, and opening two made
 * the ranking unreadable — which is the only thing the list is for. None of it had a URL
 * either: a disclosure triangle cannot be bookmarked, linked in a message, or sent to
 * somebody.
 *
 * THE RULE COMES OFF THE ROW, NEVER OFF THE KEY, and the URL slugs it and never reverses
 * it: this page finds its rows by matching `slug(s.rule)` against the `rule` parameter,
 * exactly as the backtest detail page does. `systemKey` joins on `|` and a pair's name
 * contains one, so anything that splits a key truncates every pair at its operator.
 */

import { Fragment, Suspense, useMemo, useRef } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { PnlFigure, PnlSpark } from "@/components/PnlChart";
import { ReplayBanner, StaleBanner } from "@/app/paper/page";
import {
  aggregate,
  assetCount,
  cash,
  classLabel,
  downloadFills,
  fmtPct,
  fmtUnits,
  isReplay,
  liveMetricRows,
  liveMetrics,
  money,
  paperGroupList,
  PC_WINDOWS,
  prettyNote,
  price,
  sign,
  slug,
  statusChip,
  strategiesOf,
  systemBreaks,
  systemCurve,
  systemFills,
  systemKey,
  turnoverOf,
  useBacktestHref,
  useKeepScroll,
  useLive,
  usePaperCurves,
  type Fill,
  type Holding,
  type PaperCurves,
  type Sys,
} from "@/lib/live";

/** Nine, and it is a constant because three places have to agree on it: the header, the
 *  book's "nothing published yet" colspan, and the per-symbol row for a non-book system. */
const ASSET_COLS = 9;

function AssetHead({ replay }: { replay: boolean }) {
  return (
    <thead>
      <tr>
        <th className="l">Asset</th>
        <th className="l">State</th>
        <th>Units</th>
        <th title="what the units currently held cost, averaged over every fill that built the position">
          Avg cost
        </th>
        <th>Mark</th>
        <th>Value</th>
        <th>{replay ? "Replay P&L" : "Paper P&L"}</th>
        <th>Trades</th>
        <th className="l">Status</th>
      </tr>
    </thead>
  );
}

/* A book is one strategy holding a whole class, so it expands into one row PER NAME rather
 * than the single row every other system gets.
 *
 * EVERY NAME IS LISTED, held or not. "46 of 100 held" only reads if the other 54 are
 * visible as waiting — a name the rule is out of is holding its slice in cash, which is a
 * state and not an absence. Held names sort to the top because they are the ones doing
 * something; the rest stay alphabetical so a reader can find one. */
function bookRows(s: Sys) {
  const rows = s.holdings || [];
  if (!rows.length) {
    return [
      <tr key="none">
        <td className="l" colSpan={ASSET_COLS}>
          {s.symbol || "the book"} — no holdings published yet
        </td>
      </tr>,
    ];
  }
  const rank = (h: Holding) => (Math.abs(h.units || 0) > 0 ? 0 : 1);
  const sorted = [...rows].sort(
    (a, b) => rank(a) - rank(b) || a.symbol.localeCompare(b.symbol),
  );
  const num = (v: number | null | undefined) =>
    v == null ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return sorted.map((h) => (
    <tr key={`${s.id}:${h.symbol}`}>
      <td className="l">{h.symbol}</td>
      <td className="l">{h.warming ? "warming" : h.state}</td>
      <td>{fmtUnits(h.units)}</td>
      <td>{num(h.entry)}</td>
      <td>{num(h.mark)}</td>
      <td>{h.value ? money(h.value) : "—"}</td>
      <td className={h.pnl_pct == null ? "" : sign(h.pnl_pct)}>
        {h.pnl_pct == null ? "—" : fmtPct(h.pnl_pct)}
      </td>
      <td>{h.trades || 0}</td>
      <td className="l">
        {h.warming ? "waiting for bars" : Math.abs(h.units || 0) > 0 ? "holding" : "in cash"}
      </td>
    </tr>
  ));
}

/** One row for a system deployed on a single instrument. A book expands into one row per
 *  name instead, and the two shapes share a header, so they print the same columns in the
 *  same order. */
function AssetRow({ s, replay }: { s: Sys; replay: boolean }) {
  const router = useRouter();
  const [chip, label] = statusChip(s, replay);
  // `router.push`, never a bare href: it is what applies `basePath`, and this export is
  // mounted at /next/ rather than at the root.
  return (
    <tr
      style={{ cursor: "pointer" }}
      onClick={() => router.push(`/paper/strategy/?id=${encodeURIComponent(s.id)}`)}
    >
      <td className="l">{s.symbol}</td>
      <td className="l">
        <span className={`pos-${s.state}`}>{s.state}</span>
      </td>
      <td>{fmtUnits(s.position_units)}</td>
      <td>{price(s.entry)}</td>
      <td>{price(s.mark_price)}</td>
      <td>
        {s.position_units && s.mark_price
          ? money(Math.abs(s.position_units * s.mark_price))
          : "—"}
      </td>
      <td className={sign(s.paper_pnl_pct)}>{fmtPct(s.paper_pnl_pct)}</td>
      <td>{s.paper_trades}</td>
      <td className="l">
        <span className={`chip ${chip}`}>{label}</span>
      </td>
    </tr>
  );
}

/* ---------------------------------------------- the live record, as numbers */

/* NO BENCHMARK COLUMN, and that is not the same decision as the backtest page's. There, a
 * strategy is scored against the same basket held AT THE STRATEGY'S OWN VOLATILITY OVER
 * DECADES, which is a comparison that decides something. Days of paper fills against days
 * of holding is not that comparison, and printed beside these figures it was being read as
 * one. The verdict lives on the Research page; this page reports the record. */
function MetricsSection({
  rows,
  curve,
  cls,
  tf,
  replay,
}: {
  rows: Sys[];
  curve: number[];
  cls: string;
  tf: string;
  replay: boolean;
}) {
  const m = liveMetrics(rows, curve, cls, tf);
  const shown = systemFills(rows).length;
  return (
    <section className="sec">
      <div className="sec-head">
        <h2>Performance metrics</h2>
        <span
          className="sec-note explains"
          title={
            "Measured over the closed bars of this record only — a record this short " +
            "describes the execution path, not the rule.\n\n" +
            "There is deliberately no buy-and-hold column: the comparison that decides " +
            "whether a strategy is worth running is the risk-matched one over decades, on " +
            "the backtest page. These are the desk's own live arithmetic and are not " +
            "directly comparable with the research figures."
          }
        >
          the live record itself — no benchmark column
        </span>
      </div>
      <div className="tbl-wrap metrics-box">
        <table>
          <thead>
            <tr>
              <th className="l">Metric</th>
              <th>Value</th>
            </tr>
          </thead>
          <tbody>
            {liveMetricRows(m).map(([name, val, help]) => (
              <tr key={name}>
                {/* The explanation is on the name, not standing beside it in a column of
                    its own. Same move as the research metrics table: a reader who does not
                    recognise a row asks, and one who does should not read past the answer
                    on every visit. */}
                <td className="l">
                  <span className="explains" title={help}>{name}</span>
                </td>
                <td className="num">{val}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------- the fills */

/* WHAT THE DESK PUBLISHES, which is not the whole record. `paper_state.MAX_TRADES` caps it
 * at 200 per strategy while `lifetime_trades` counts the database, so the header and the
 * caption say which of the two is on screen rather than quietly printing the shorter one.
 *
 * Both P&L columns are here, labelled. `realised` is what that ONE FILL closed against
 * what the closed part cost — null when it opened or added — and `pnl` is the whole book's
 * mark at that instant. The trade statistics above count only the first. */
function FillsSection({
  rows,
  cls,
  tf,
  rule,
}: {
  rows: Sys[];
  cls: string;
  tf: string;
  rule: string;
}) {
  const fills = systemFills(rows);
  const lifetime = rows.reduce(
    (a, s) => a + (s.lifetime_trades != null ? s.lifetime_trades : s.paper_trades || 0),
    0,
  );
  return (
    <section className="sec">
      <div className="sec-head">
        <h2>Trade history</h2>
        <span className="sec-note">
          {fills.length < lifetime
            ? `the last ${fills.length.toLocaleString()} of ${lifetime.toLocaleString()} fills`
            : `${fills.length.toLocaleString()} fill${fills.length === 1 ? "" : "s"}`}
          , newest first
        </span>
      </div>
      {fills.length ? (
        <>
          <div className="tbl-tools">
            {/* The export is a Blob built from the rows already on the page: the board is
                static files behind a login and has no endpoint to ask for a file. The
                vanilla has to re-bind this handler on every tick because it rewrites the
                container whole; React keeps the node, so the binding survives. */}
            <button className="btn" onClick={() => downloadFills(rows, cls, tf, rule)}>
              Export CSV
            </button>
            {fills.length < lifetime ? (
              <span className="sec-note">
                The desk publishes its most recent {fills.length.toLocaleString()} fills per
                system; the full record is retained.
              </span>
            ) : null}
          </div>
          <div className="tbl-wrap fills-box">
            <table>
              <thead>
                <tr>
                  <th className="l">Time</th>
                  <th className="l">Asset</th>
                  <th className="l">Side</th>
                  <th>Qty</th>
                  <th>Price</th>
                  <th>
                    <span
                      className="explains"
                      title={"What this ONE FILL closed, against what the closed part cost. "
                             + "Blank on a fill that opened or added, because it closed "
                             + "nothing. The trade statistics above count only this column."}
                    >
                      Realised P&amp;L
                    </span>
                  </th>
                  <th>
                    <span
                      className="explains"
                      title={"The whole book's mark at that moment, so every name filling in "
                             + "the same second carries the same value. Never sum it with "
                             + "the column beside it: they answer different questions."}
                    >
                      Book P&amp;L
                    </span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {fills.map((t: Fill, i: number) => (
                  <tr key={`${t.ts}|${t.symbol}|${i}`}>
                    <td className="l">{t.ts || ""}</td>
                    <td className="l">{t.symbol || ""}</td>
                    <td className={`l ${t.side === "BUY" ? "gain" : "loss"}`}>
                      {t.side || ""}
                    </td>
                    <td>{fmtUnits(t.qty)}</td>
                    <td>{price(t.price)}</td>
                    <td
                      className={t.realised == null ? "" : sign(t.realised)}
                      title={
                        t.realised == null
                          ? "this fill opened or added — it closed nothing"
                          : undefined
                      }
                    >
                      {cash(t.realised)}
                    </td>
                    <td className={`book-pnl ${sign(t.pnl)}`}>{cash(t.pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <p className="sec-note">No fills yet — this system has not opened a position.</p>
      )}
    </section>
  );
}

/* -------------------------------------------------------- one simulated window */

function PnlPanel({
  entry,
  label,
}: {
  entry: { curve: number[]; dates?: string[]; pnl_pct: number } | undefined;
  label: string;
}) {
  if (!entry) return <p className="sec-note">No simulated history for this window.</p>;
  const d = entry.dates || [];
  return (
    <div className="pnl-wrap">
      <div className="pnl-head">
        {/* The rule's own line and nothing else — no dashed basket and no "buy & hold x%"
            beside the label. Every full-size chart on the paper side draws one series. */}
        <span className={`pnl-val num ${sign(entry.pnl_pct)}`}>{fmtPct(entry.pnl_pct)}</span>
        <span className="pnl-lbl">{label}</span>
      </div>
      <PnlSpark curve={entry.curve} w={600} h={150} />
      <div className="pnl-axis">
        <span>{d[0] || ""}</span>
        <span>{d[d.length - 1] || ""}</span>
      </div>
    </div>
  );
}

/* ============================== the page ============================== */

function SystemView() {
  const q = useSearchParams();
  const cls = q.get("cls") ?? "";
  const tf = q.get("tf") ?? "";
  const ruleSlug = q.get("rule") ?? "";

  const { doc, meta, ready } = useLive();
  const pcurves = usePaperCurves();
  const body = useRef<HTMLDivElement>(null);
  // The volatile half is rebuilt on every tick. React keeps the nodes, but a row count
  // that moves can still remount a table and take its scroll with it — see the note on
  // `useKeepScroll`. The holdings table is nine columns wide and the fills list scrolls
  // inside its own box, so both axes are remembered.
  useKeepScroll(body);

  const rows = strategiesOf(doc).filter(
    (s) => s.cls === cls && s.tf === tf && slug(s.rule || "") === ruleSlug,
  );
  const replay = isReplay(doc);
  const rule = rows[0]?.rule || "";

  /* THE HERO IS FROZEN, the way the vanilla freezes it by putting it outside `#sys-body`.
   * The name, the class, the note and the status chip are settled when the system is first
   * found, so a tick cannot re-measure the row a reader is looking at. Everything that
   * genuinely moves is downstairs. */
  const hero = useRef<{ rule: string; note: string; chip: [string, string] | null } | null>(
    null,
  );
  if (rows.length && (!hero.current || hero.current.rule !== rule)) {
    hero.current = {
      rule,
      note: prettyNote(rows[0].note),
      chip: rows.length === 1 ? statusChip(rows[0], replay) : null,
    };
  }

  const href = useBacktestHref(cls, tf, rule);

  const groups = paperGroupList(meta);
  const sim = useMemo(() => {
    if (!rows.length || !pcurves) return null;
    const entry = (pcurves as PaperCurves)[systemKey(rows[0])];
    return entry?.system ?? null;
  }, [pcurves, rows]);

  if (!ready) return <div className="note busy-note">Reading the desk…</div>;
  if (!rows.length) {
    return (
      <div className="note">
        No system named <b>{ruleSlug}</b> on the {classLabel(cls)} desk at {tf}. It may have
        been retired — <Link href="/paper/">back to paper trading</Link>.
      </div>
    );
  }

  const a = aggregate(rows);
  const live = systemCurve(rows, "paper_curve");
  const breaks = systemBreaks(rows);
  const since = rows[0].since;
  const days = Math.max(...rows.map((s) => s.days || 0), 0);
  const turn = turnoverOf(rows);

  /* A book is one row holding a whole class, so "with a position" would be a single yes/no
   * about the account. Names held out of names carried is the same question asked of the
   * thing that has an answer. */
  const books = rows.filter((s) => s.kind === "book");
  const held = books.reduce((x, s) => x + (s.held || 0), 0);
  const names = books.reduce((x, s) => x + (s.names || 0), 0);
  const equity = rows.reduce((x, s) => x + (s.equity || 0), 0);
  const capital = rows.reduce((x, s) => x + (s.capital || 0), 0);

  return (
    <>
      <Link className="back" href="/paper/">
        ← {replay ? "strategy replay" : "paper trading"}
      </Link>
      <div className="hero">
        <div className="d-head">
          <span className="d-name">{hero.current?.rule}</span>
          <span className="chip mut">{tf}</span>
          <span className="chip mut">{classLabel(cls)}</span>
          {hero.current?.chip ? (
            <span className={`chip ${hero.current.chip[0]}`}>{hero.current.chip[1]}</span>
          ) : null}
        </div>
        <p className="lede">{hero.current?.note}</p>
      </div>

      <ReplayBanner doc={doc} />
      <StaleBanner doc={doc} />

      <div ref={body}>
        <div className="strip">
          <div className="stat">
            <span className="k">{replay ? "Replay P&L" : "Paper P&L"}</span>
            <span className={`v ${sign(a.mean)}`}>{fmtPct(a.mean)}</span>
            <span className="s">cumulative{since ? `, since ${since}` : ""}</span>
          </div>
          <div className="stat">
            <span className="k">Fills</span>
            <span className="v">{a.fills.toLocaleString()}</span>
            <span className="s">lifetime, carried across restarts</span>
          </div>
          <div className="stat">
            <span className="k">{books.length ? "Holding" : "With a position"}</span>
            <span className="v">
              {books.length ? `${held} / ${names}` : `${a.open} / ${rows.length}`}
            </span>
            <span className="s">
              {books.length ? "names held right now" : "deployments with exposure"}
            </span>
          </div>
          <div className="stat">
            <span className="k">Turnover / yr</span>
            <span className="v">{turn == null ? "—" : turn.toFixed(1)}</span>
            <span className="s">
              per name — the unit the backtest reports, so the two compare
            </span>
          </div>
          <div className="stat">
            <span className="k">Equity</span>
            <span className="v">{equity ? money(equity) : "—"}</span>
            <span className="s">{capital ? `of ${money(capital)} staked` : "paper only"}</span>
          </div>
          <div className="stat">
            <span className="k">Running</span>
            <span className="v">
              {rows.length === 1 ? (
                (() => {
                  const [c, l] = statusChip(rows[0], replay);
                  return <span className={`chip ${c}`}>{l}</span>;
                })()
              ) : (
                `${a.live} / ${rows.length}`
              )}
            </span>
            <span className="s">
              {rows.length === 1 ? "one deployment" : `${rows.length} deployments of one rule`}
            </span>
          </div>
        </div>

        {/* TWO COLUMNS THAT FLOW INDEPENDENTLY. The record and the numbers OF that record
            are two views of one thing and a reader moves between them; stacked, the second
            was a screen below the first. `.d-col` is the same pair the research detail page
            uses -- see `app/busy.css` -- so the two pages scan the same way. */}
        <div className="d-split">
        <div className="d-col">
        <section className="sec">
          <div className="sec-head">
            <h2>{replay ? "Replayed record" : "Live record"}</h2>
            <span className="sec-note">
              {days} day{days === 1 ? "" : "s"} of {replay ? "replayed" : "simulated"} fills
              {breaks.length
                ? ` · cut at ${breaks.length} outage${breaks.length === 1 ? "" : "s"}`
                : ""}
            </span>
          </div>
          <div className="sys-live">
            <div className="sys-headline">
              <span className={`pnl-val num ${sign(a.mean)}`}>{fmtPct(a.mean)}</span>
              <span className="pnl-lbl">
                cumulative {replay ? "replay" : "paper"} P&amp;L
                {since ? ` since ${since}` : ""}
              </span>
            </div>
            {live.length > 1 ? (
              <PnlFigure
                curve={live}
                breaks={breaks}
                from={since || "start"}
                to={days ? `${days} day${days === 1 ? "" : "s"} in` : "today"}
              />
            ) : (
              <p className="pnl-young">
                The live record is {live.length} closed bar{live.length === 1 ? "" : "s"} old
                — a line needs two. The figure above it is live either way, and the simulated
                windows below are what this rule did over the same instruments&apos; recent
                history.
              </p>
            )}
          </div>
        </section>

        </div>

        <div className="d-col">
        <MetricsSection rows={rows} curve={live} cls={cls} tf={tf} replay={replay} />

        <section className="sec">
          <div className="sec-head">
            <h2>Simulated history</h2>
            <span className="sec-note">not traded — the same rule over recent bars</span>
          </div>
          {sim ? (
            <>
              <div className="sim-wins">
                {PC_WINDOWS.map(([w, label]) => (
                  <PnlPanel key={w} entry={sim[w]} label={label} />
                ))}
              </div>
              <p className="sec-note pnl-caveat">
                <span
                  className="explains"
                  title={
                    "This rule over the same instruments' recent history. They say how it " +
                    "WOULD have gone; the record above is what it did. Whether it beats " +
                    "holding is the multi-year question, answered on the backtest page and " +
                    "not by three months of either line."
                  }
                >
                  simulated, not traded
                </span>
              </p>
            </>
          ) : (
            <p className="sec-note pnl-caveat">
              No simulated history for this system yet; only the live record below is
              available.
            </p>
          )}
        </section>

        </div>
        </div>

        {/* Full width, both of them: nine columns of holdings and seven of fills do not fit
            half a rail, and squeezing a table is how it starts scrolling sideways again. */}
        <FillsSection rows={rows} cls={cls} tf={tf} rule={rule} />

        {/* ONE SECTION PER UNIVERSE, and the note above each table is why this page carries
            them: `paper_groups[].note` says what that universe is worth as evidence — the
            one the rule was ranked on, or a transfer onto instruments the research never
            held — which is the first thing somebody about to read a table of names needs.
            It is rendered here and nowhere else. */}
        {(() => {
          const sections = groups
            .map((g) => {
              const gs = rows
                .filter((s) => (s.group || "") === g.key)
                .sort((x, y) => String(x.symbol).localeCompare(String(y.symbol)));
              if (!gs.length) return null;
              const ga = aggregate(gs);
              const gBooks = gs.filter((s) => s.kind === "book");
              /* Per-name sparklines, where `paper_curves.py` published any. It drops them
                 for a book on purpose (6.3 MB against 0.2 MB), so in practice this renders
                 for the older per-symbol deployments and nothing else. */
              const c = pcurves ? (pcurves as PaperCurves)[systemKey(gs[0])] : null;
              const cards = c?.assets
                ? gs
                    .map((s) => {
                      const av = c.assets?.[s.symbol as string];
                      if (!av) return null;
                      return (
                        <figure className="mini-card" key={s.id}>
                          <figcaption>
                            <span className="mini-sym">{s.symbol}</span>
                          </figcaption>
                          {PC_WINDOWS.map(([w, label]) => {
                            const e = av[w];
                            if (!e) return null;
                            return (
                              <div className="mini-win" key={w}>
                                <span className="hist-lbl">{label}</span>
                                <PnlSpark curve={e.curve} w={300} h={46} />
                                <span className="hist-nums">
                                  <b className={sign(e.pnl_pct)}>{fmtPct(e.pnl_pct)}</b>
                                </span>
                              </div>
                            );
                          })}
                        </figure>
                      );
                    })
                    .filter(Boolean)
                : [];

              return (
                <section className="sec" key={g.key}>
                  <div className="sec-head">
                    <h2>{g.label || g.key}</h2>
                    <span className="sec-note">
                      {assetCount(gs)}
                      {gBooks.length ? "" : ` · ${ga.open} with a position`} · {ga.fills} fills
                    </span>
                  </div>
                  {g.note ? <p className="grp-note">{g.note}</p> : null}
                  <div className="tbl-wrap">
                    <table>
                      <AssetHead replay={replay} />
                      <tbody>
                        {gs.map((s) =>
                          s.kind === "book" ? (
                            // A book is many rows from one system, so they are grouped
                            // under the system's key rather than each carrying one.
                            <Fragment key={s.id}>{bookRows(s)}</Fragment>
                          ) : (
                            <AssetRow key={s.id} s={s} replay={replay} />
                          ),
                        )}
                      </tbody>
                    </table>
                  </div>
                  {cards.length ? <div className="minis">{cards}</div> : null}
                </section>
              );
            })
            .filter(Boolean);
          return sections.length ? (
            sections
          ) : (
            <p className="sec-note">No holdings published for this system yet.</p>
          );
        })()}
      </div>

      {/* CHECKED, NOT ASSUMED. The desk runs promotions whose leaderboard row was cut, and
          a link that bounces the reader back to the leaderboard is worse than a sentence
          saying the page is not there. `useBacktestHref` asks the API. */}
      {/* CHECKED, NOT ASSUMED -- the desk runs promotions whose leaderboard row was cut,
          and a link that bounces the reader back to the board is worse than a sentence
          saying the page is not there. `useBacktestHref` asks the API. One line: the
          caveat is true and was three lines of it. */}
      <p className="sec-note">
        {href ? (
          <>
            <Link href={href}>the walk-forward result for {rule}</Link> is the multi-year
            question · this page is{" "}
            <span
              className="explains"
              title={"Evidence about the EXECUTION PATH and about nothing else. Whether the "
                     + "rule works is answered by the walk-forward run over decades, not by "
                     + "days of simulated fills."}
            >
              {replay ? "a replay over cached bars" : "days of simulated fills"}
            </span>
          </>
        ) : (
          <>
            No walk-forward row for this rule on the sheet it trades ·{" "}
            <Link href="/">the board</Link> is where that question is answered
          </>
        )}
      </p>
    </>
  );
}

export default function SystemPage() {
  // `useSearchParams` suspends during the static prerender, which `output: "export"` runs
  // at build time. The boundary is what lets one exported page serve every system.
  return (
    <Suspense fallback={<div className="note busy-note">Reading the desk…</div>}>
      <SystemView />
    </Suspense>
  );
}
