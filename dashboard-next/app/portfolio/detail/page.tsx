"use client";

/* /portfolio/detail/?id=… — ONE basket, and the point of the whole feature.
 *
 * A combined curve on its own is a number in a nicer shape. What a reader has to be able to
 * do here is take the basket apart: which legs did the work, whether the legs are actually
 * different bets, what the basket held at the time the curve was earning it, and whether it
 * is trading right now. Those are the four sections, and none of them is an appendix to the
 * chart.
 *
 * A QUERY STRING, not a path segment. `output: "export"` pre-renders every route, so a
 * dynamic segment would need `generateStaticParams` — enumerating every portfolio at build
 * time, from an API that requires a session, on a box that may not be able to reach it. One
 * static page serves every id. `app/rule/page.tsx` is here for the same reason.
 *
 * TWO MEASUREMENTS, NEVER ADDED — and BOTH are on this page now, which makes the rule
 * stricter rather than looser. "On the desk" is the record: the blended paper curve, each
 * leg's live figures, and every fill. "In the research" is the walk-forward over the legs'
 * whole history — what the basket WOULD have done. Two separately titled halves with their
 * own captions; never summed, never drawn on one pair of axes, and neither offered as
 * evidence for the other. Weeks of fills describe the execution path; decades of
 * walk-forward describe the rules.
 */

import { Suspense, useEffect, useMemo, useState, type ReactElement } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  BasketCurve,
  BasketFills,
  BasketLegs,
  BasketMetrics,
} from "@/components/BasketPaper";
import { LegCorrelation } from "@/components/LegCorrelation";
import { LegTable } from "@/components/LegTable";
import { PortfolioChart } from "@/components/PortfolioChart";
import { PortfolioToggle } from "@/components/PortfolioToggle";
import { fmtMoney, fmtNum, fmtDate } from "@/lib/format";
import { isReplay, strategiesOf, useLive } from "@/lib/live";
import { paperRecord } from "@/lib/paperbasket";
import {
  growthOf,
  legKey,
  legsOf,
  portfolioApi,
  readCorr,
  useBlend,
  usePortfolios,
  type Blend,
  type BlendLeg,
  type Portfolio,
  type PortfolioChange,
  type PortfolioLeg,
} from "@/lib/portfolio";

/** The engine reports FRACTIONS — `cagr: 0.085` is 8.5%/yr. One place converts. */
const pct = (v: number | null | undefined, d = 1) =>
  v == null ? "—" : `${fmtNum(v * 100, d)}%`;

/* ------------------------------------------------------------------ the membership log */

/** Same contract as the leaderboard's columns and the leg table's: a column without a `doc`
 *  is the one column nobody can ask about, so the type will not let one be added. */
interface ChangeCol {
  h: string;
  l?: boolean;
  doc: string;
  cell: (c: PortfolioChange) => ReactElement;
}

const CHANGE_COLS: ChangeCol[] = [
  {
    h: "When",
    l: true,
    doc: "When the basket changed. Append-only: nothing here is ever updated or deleted, "
       + "because a composition that can be rewritten afterwards is not a record of what "
       + "was held.",
    cell: (c) => <td className="l">{fmtDate(c.at)}</td>,
  },
  {
    h: "What",
    l: true,
    doc: "Added or removed. A follow-portfolio is re-checked daily against its sheet, so a "
       + "rule that dropped out is retired and its replacement started — one row each.",
    cell: (c) => (
      <td className="l">
        <span className={`chip ${c.action === "added" ? "run" : "mut"}`}>{c.action}</span>
      </td>
    ),
  },
  {
    h: "Leg",
    l: true,
    doc: "The rule this row is about, and where it came from. Its own evidence is on its "
       + "strategy page.",
    cell: (c) =>
      c.rule ? (
        <td className="l">
          <Link
            href={`/rule/?cls=${encodeURIComponent(c.cls ?? "")}&tf=${encodeURIComponent(c.tf ?? "")}&rule=${encodeURIComponent(c.rule)}`}
          >
            {c.rule}
          </Link>
          <span className="grp-meta">
            {c.tf ? ` · ${c.tf}` : ""}
            {c.cls ? ` ${c.cls}` : ""}
          </span>
        </td>
      ) : (
        <td className="l flat">—</td>
      ),
  },
  {
    h: "Rank then",
    doc: "Where the rule stood on its sheet at that moment. Written down at the time on "
       + "purpose: the sheet is re-ranked nightly and cannot be asked afterwards what it "
       + "said last March.",
    cell: (c) => (c.rank_at == null ? <td className="flat">—</td> : <td>{c.rank_at}</td>),
  },
  {
    h: "Legs after",
    doc: "How many legs the basket held once the change was applied, and what each was "
       + "resized to. Without them, reconstructing what a dollar was doing on a given day "
       + "means replaying every row from inception and hoping none is missing.",
    cell: (c) =>
      c.n_legs == null ? (
        <td className="flat">—</td>
      ) : (
        <td>
          {c.n_legs}
          {c.leg_capital == null ? "" : ` × ${fmtMoney(c.leg_capital)}`}
        </td>
      ),
  },
  {
    h: "Why",
    l: true,
    doc: "The reason recorded with the change — what the selector decided, in its own "
       + "words. This is what separates a rule that earned its place from one that arrived "
       + "last week.",
    cell: (c) => <td className="l">{c.reason || c.source || "—"}</td>,
  },
];

const stripTags = (s: string) => s.replace(/<[^>]+>/g, "");

function MembershipLog({ id }: { id: string }) {
  const [rows, setRows] = useState<PortfolioChange[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setRows(null);
    setErr(null);
    portfolioApi
      .changes(id)
      .then((r) => live && setRows(r))
      .catch((e) => live && setErr(String((e as Error).message ?? e)));
    return () => {
      live = false;
    };
  }, [id]);

  if (err) return <p className="sec-note">The membership log could not be read — {err}.</p>;
  if (rows === null) return <p className="sec-note busy-note">Reading the log…</p>;
  if (!rows.length)
    return (
      <p className="sec-note">
        Nothing has changed since this basket was created, so the curve above is one
        composition throughout.
      </p>
    );

  // Newest first: what it holds NOW is the question somebody arrives with.
  const sorted = [...rows].sort((a, b) => String(b.at).localeCompare(String(a.at)));

  return (
    <div className="tbl-wrap">
      <table>
        <thead>
          <tr>
            {CHANGE_COLS.map((c) => (
              <th key={c.h} className={c.l ? "l" : undefined}>
                <span className="explains" title={stripTags(c.doc)}>
                  {c.h}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((c, i) => (
            <tr key={c.id ?? `${c.at}-${i}`}>
              {CHANGE_COLS.map((col, ci) => (
                <ChangeCell key={ci} col={col} row={c} />
              ))}
            </tr>
          ))}
        </tbody>
        <caption>
          Every swap, with the reason. A follow-portfolio&apos;s holdings move under it as
          its sheet moves, so the equity curve above is the record of several different
          baskets — this is the only thing that says which one was earning what.
        </caption>
      </table>
    </div>
  );
}

function ChangeCell({ col, row }: { col: ChangeCol; row: PortfolioChange }) {
  return col.cell(row);
}

/* ------------------------------------------------------------------ the page */

function DetailView() {
  const q = useSearchParams();
  const id = q.get("id") ?? "";
  const { list, ready } = usePortfolios();
  const { doc } = useLive();
  const { blend, loading, error } = useBlend(id || null);
  const [showLegs, setShowLegs] = useState(false);

  const p: Portfolio | null = list.find((x) => x.portfolio_id === id) ?? null;

  const ledgerLegs = legsOf(p);
  const byKey = useMemo(() => {
    const m = new Map<string, PortfolioLeg>();
    ledgerLegs.forEach((l) => m.set(legKey(l), l));
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ledgerLegs.length, p?.portfolio_id]);

  const corr = useMemo(() => readCorr(blend?.corr ?? []), [blend]);

  /* THE DESK'S RECORD for this basket, out of the live document. `useLive` holds the
     socket and the poller, so everything below repaints when the desk republishes —
     which is once per closed bar, because a book marks its holdings on a bar and nothing
     prices it in between. Keyed on the document's own stamp rather than on the array:
     `strategiesOf` returns a new array on every tick and an unkeyed memo would rebuild
     five legs, their fills and a blended curve on each one. */
  const rows = strategiesOf(doc);
  const replay = isReplay(doc);
  const rec = useMemo(
    () => paperRecord(p, rows),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [p?.portfolio_id, ledgerLegs.length, doc?.generated_at, rows.length],
  );

  if (!ready && !p) return <div className="note busy-note">Reading the ledger…</div>;
  if (!p)
    return (
      <div className="note">
        No portfolio with id <b>{id}</b> is visible to you. It may have been retired, or it
        may belong to somebody else — a stranger&apos;s id answers the same way as one that
        does not exist. <Link href="/portfolio/">Back to portfolios</Link>.
      </div>
    );

  /* Reading the house's portfolios is open; changing one is the owner's decision, for the
     same reason promotion is. Null while the live document has not landed — the control is
     offered and the API's own refusal answers, which is better than hiding a button on a
     guess. */
  const canWrite =
    doc?.account == null
      ? null
      : p.account === String(doc.house ?? "00")
        ? !!doc.is_admin
        : p.account === String(doc.account);

  const source = p.source_cls && p.source_tf ? `${p.source_cls} ${p.source_tf}` : null;

  return (
    <>
      <Link className="back" href="/portfolio/">
        ← Portfolios
      </Link>

      <div className="hero-row">
        <div className="hero-left">
          <div className="d-head">
            <span className="d-name">{p.name}</span>
            <span className="chip mut">
              {p.kind === "follow" ? (source ? `follows ${source}` : "follows a sheet") : "hand-picked"}
            </span>
            {p.kind === "follow" && p.top_n ? (
              <span className="chip mut">top {p.top_n}, re-checked daily</span>
            ) : null}
          </div>
          <p className="lede">
            {fmtMoney(p.capital)} in one pot, split equally across{" "}
            {ledgerLegs.length || blend?.legs.length || "its"} legs and rebalanced back to
            equal weight {p.rebalance ?? "monthly"}.{" "}
            {p.kind === "follow"
              ? "It tracks the top of one leaderboard sheet, re-checked once a day, so what it holds moves as the sheet does."
              : "Its legs were chosen by hand and nothing re-checks them."}
          </p>
        </div>

        <PortfolioToggle p={p} canWrite={canWrite} />
      </div>

      {/* ============================================== ON THE DESK — the record.
          First, and above the research, because it is the only half of this page that
          reports something that happened. The research below is the larger and more
          decision-relevant measurement, but it is a projection; a reader arriving at a
          basket that is trading wants to know what it is doing before what it might have
          done. The two are separated by a divider and their own headings so that no
          figure from one can be read as belonging to the other. */}
      <div className="sec-band">
        <h2 className="band-h">On the desk</h2>
        <span className="band-note">
          what this basket has actually done since it was picked up — the desk&apos;s own
          record, repainting as it publishes
        </span>
      </div>

      <section className="sec">
        <div className="sec-head">
          <h2>Cumulative return</h2>
          <span className="sec-note">the whole pot, from the fills the desk has recorded</span>
        </div>
        <BasketCurve rec={rec} replay={replay} />
      </section>

      <div className="d-split">
        <div className="d-col">
          <section className="sec">
            <div className="sec-head">
              <h2>Each leg, live</h2>
              <span className="sec-note">
                biggest contributor first · these are the desk&apos;s figures, not the
                backtest&apos;s
              </span>
            </div>
            <BasketLegs rec={rec} replay={replay} />
          </section>
        </div>

        <div className="d-col">
          <section className="sec">
            <div className="sec-head">
              <h2>The record, as numbers</h2>
              <span className="sec-note">
                over the closed bars of this record only — no benchmark column
              </span>
            </div>
            <BasketMetrics rec={rec} />
          </section>
        </div>
      </div>

      <section className="sec">
        <div className="sec-head">
          <h2>Trade history</h2>
          <span className="sec-note">every fill, newest first, with the leg that made it</span>
        </div>
        <BasketFills rec={rec} name={p.name} />
      </section>

      {/* ============================================== IN THE RESEARCH — the projection. */}
      <div className="sec-band">
        <h2 className="band-h">In the research</h2>
        <span className="band-note">
          what these legs would have done held together over the walk-forward years. A
          different measurement from everything above, and never added to it
        </span>
      </div>

      {/* RANKING IS NOT PASSING. It belongs on this page more than on the list, because this
          is where the combined curve is, and a combined curve is the most persuasive thing
          this feature draws. */}
      <div className="note">
        <b>Combining rules that each fail the standard does not produce one that passes.</b>{" "}
        Nothing on any sheet here clears the six acceptance criteria, so these legs are the
        least-bad candidates rather than five that work — see each leg&apos;s own row on{" "}
        <Link href="/">Research</Link> for what it was actually scored at.
      </div>

      {loading && <div className="note busy-note">Blending the legs…</div>}

      {error && !loading && (
        <div className="note">
          <b>No combined curve.</b> {error}
        </div>
      )}

      {blend && blend.ok && (
        <Strip blend={blend} nLegs={ledgerLegs.length || blend.legs.length} p={p} />
      )}

      <div className="d-split">
        <div className="d-col">
          <section className="sec">
            <div className="sec-head">
              <h2>Combined curve</h2>
              <span className="sec-note">
                the legs blended at fixed weights, against the benchmark blended the same way
              </span>
            </div>
            {blend && blend.ok ? (
              <PortfolioChart
                blend={blend}
                label={p.name}
                showLegs={showLegs}
                onShowLegs={setShowLegs}
              />
            ) : !loading && !error ? (
              <p className="sec-note">
                This basket holds no legs yet, so there is nothing to combine.
              </p>
            ) : null}
          </section>

          <section className="sec">
            <div className="sec-head">
              <h2>The legs, and what each contributed</h2>
              <span className="sec-note">
                biggest contributor first · each links out to its own evidence
              </span>
            </div>
            {blend && blend.legs.length ? (
              <>
                <LegTable
                  legs={blend.legs}
                  capital={blend.capital ?? p.capital ?? null}
                  years={blend.years}
                  corrOf={(i) => corr.perLeg[i] ?? null}
                  rowOf={
                    ledgerLegs.length
                      ? (l: BlendLeg) => byKey.get(legKey(l)) ?? null
                      : undefined
                  }
                />
                <p className="sec-note" style={{ maxWidth: "78ch" }}>
                  Contributions are shares of the <b>whole pot</b> and add up to what the
                  portfolio itself returned. They are not shares of the profit: dividing by a
                  profit that happens to be negative reports a leg that made money as a
                  negative contributor.
                </p>
              </>
            ) : (
              <LedgerLegs legs={ledgerLegs} />
            )}
          </section>
        </div>

        <div className="d-col">
          <section className="sec">
            <div className="sec-head">
              <h2>Five bets, or one?</h2>
              <span className="sec-note">how alike the legs are</span>
            </div>
            {blend ? (
              <LegCorrelation blend={blend} />
            ) : (
              <p className="sec-note">
                Needs the combined backtest, which has not landed.
              </p>
            )}
          </section>

          <section className="sec">
            <div className="sec-head">
              <h2>Membership</h2>
              <span className="sec-note">when the basket changed, and why</span>
            </div>
            <MembershipLog id={id} />
          </section>
        </div>
      </div>

      <p className="sec-note" style={{ maxWidth: "72ch" }}>
        Everything above is the walk-forward research: what these legs would have done held
        together over their shared history. What this basket has done since the desk picked
        it up is on <Link href="/paper/">Paper trading</Link>, and the two are reported
        separately and never summed.
      </p>
    </>
  );
}

/** The headline figures, and they are the BLEND ENGINE'S OWN rather than re-derived here.
 *  `stockhunt/blend.py` computes them with `stockhunt.stats` — the one definition of Sharpe,
 *  CAGR and drawdown in this repo — so a second arithmetic in the browser would be a second
 *  opinion about the same curve. Only the two growth figures are read off the drawn series,
 *  because those ARE the drawn series.
 *
 *  `Worst fall` is a LOWER BOUND and is labelled as one: the stored curves are decimated to
 *  ~320 points, so a trough that opened and closed inside one of those bars is invisible. */
function Strip({ blend, nLegs, p }: { blend: Blend; nLegs: number; p: Portfolio }) {
  const growth = growthOf(blend.portfolio);
  const bench = growthOf(blend.bench);
  const c = readCorr(blend.corr);
  const m = blend.metrics;
  return (
    <div className="strip">
      <div className="stat">
        <span className="k">Capital</span>
        <span className="v">{fmtMoney(blend.capital ?? p.capital)}</span>
        <span className="s">
          one pot · {nLegs} leg{nLegs === 1 ? "" : "s"}, equal weight
        </span>
      </div>
      <div className="stat">
        <span className="k">Span</span>
        <span className="v">{blend.years == null ? "—" : `${fmtNum(blend.years, 1)}y`}</span>
        <span className="s">
          <span
            className="explains"
            title={
              "The INTERSECTION of the legs' histories, not the union. Legs can come from "
              + "classes whose data begins decades apart, and a statistic computed on the "
              + "overlap must not be labelled with the longest leg's history."
            }
          >
            where every leg has data
          </span>
          {blend.start ? ` · from ${blend.start}` : ""}
        </span>
      </div>
      <div className="stat">
        <span className="k">$100 became</span>
        <span className="v">{growth == null ? "—" : fmtMoney(growth * 100)}</span>
        <span className="s">
          {bench == null
            ? "no benchmark returned"
            : `against ${fmtMoney(bench * 100)} for the same universes held`}
        </span>
      </div>
      <div className="stat">
        <span className="k">Per year</span>
        <span className="v">{pct(m.cagr)}</span>
        <span className="s">
          annualised over the span above
          {blend.benchMetrics?.cagr != null && <> · {pct(blend.benchMetrics.cagr)} held</>}
        </span>
      </div>
      <div className="stat">
        <span className="k">Worst fall</span>
        <span className="v">{pct(m.max_drawdown)}</span>
        <span className="s">
          <span
            className="explains"
            title={
              "A LOWER BOUND, not the figure on a rule's own page. The stored curves keep " +
              "roughly 320 points per rule whatever the bar count, so one point stands for " +
              "several weeks on a daily sheet and a trough that opened and closed inside " +
              "one of them cannot be seen here."
            }
          >
            at least this, on a coarse grid
          </span>
        </span>
      </div>
      <div className="stat">
        <span className="k">Independent bets</span>
        <span className="v">{c.effective == null ? "—" : `≈ ${fmtNum(c.effective, 1)}`}</span>
        <span className="s">
          out of {c.n || nLegs} legs · read this before the money
        </span>
      </div>
    </div>
  );
}

/** What the ledger says it holds, for a basket whose blend could not be produced. Without
 *  this, a portfolio with no book curves would show a page that never names its own legs. */
function LedgerLegs({ legs }: { legs: PortfolioLeg[] }) {
  if (!legs.length) return <p className="sec-note">This basket holds no legs yet.</p>;
  return (
    <>
      <p className="sec-note">
        The blend could not be produced, so what each leg contributed is unknown. These are
        the legs the ledger says it holds:
      </p>
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th className="l">
                <span className="explains" title="The rule, its timeframe and its asset class.">
                  Leg
                </span>
              </th>
              <th className="l">
                <span
                  className="explains"
                  title="What was asked for and what the desk has done — two different fields, written by two different processes."
                >
                  On the desk
                </span>
              </th>
              <th>
                <span className="explains" title="What this leg was funded with out of the pot.">
                  Money in it
                </span>
              </th>
            </tr>
          </thead>
          <tbody>
            {legs.map((l) => (
              <tr key={legKey(l)}>
                <td className="l">
                  <Link
                    href={`/rule/?cls=${encodeURIComponent(l.cls)}&tf=${encodeURIComponent(l.tf)}&rule=${encodeURIComponent(l.rule)}`}
                  >
                    {l.rule}
                  </Link>
                  <span className="grp-meta">
                    {" "}
                    · {l.tf} {l.cls}
                  </span>
                </td>
                <td className="l">
                  <span className="chip mut">{l.state || "—"}</span>
                  {l.want && l.want !== l.state && (
                    <span className="grp-meta"> · asked {l.want}</span>
                  )}
                </td>
                <td>{fmtMoney(l.capital)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export default function PortfolioDetailPage() {
  // `useSearchParams` suspends during prerender, and a static export prerenders every route.
  return (
    <Suspense fallback={<div className="note busy-note">Loading…</div>}>
      <DetailView />
    </Suspense>
  );
}
