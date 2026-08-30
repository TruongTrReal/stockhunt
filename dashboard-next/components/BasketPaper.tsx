"use client";

/* WHAT THE BASKET HAS ACTUALLY DONE — the desk's record, on the portfolio's own page.
 *
 * Three sections, and they answer three different questions that were all previously
 * answerable only one leg at a time on `/paper/sys/`:
 *
 *   BasketCurve    the cumulative return of the whole pot, over the record, moving with
 *                  the feed. The one picture somebody arrives for.
 *   BasketLegs     each leg's live figures side by side — not the backtest's, the desk's.
 *                  Which leg is carrying the basket and which is bleeding.
 *   BasketFills    every fill, newest first, with the leg that made it.
 *
 * NONE OF THESE IS THE RESEARCH. The blended walk-forward curve is a separate section on
 * the same page and the two are never added, never drawn together and never compared in a
 * caption. A basket's four weeks of fills say nothing about whether the rules work; they
 * say what the desk did. That distinction is the reason the sections are visibly split and
 * separately titled rather than interleaved.
 *
 * REALTIME MEANS "AS FAST AS THE DESK KNOWS". `useLive` holds a socket and a poller, so
 * these repaint whenever `live.json` is republished — which is once per closed bar per
 * leg, because a book marks its holdings on a bar and nothing prices it between them. A
 * daily leg therefore moves once a day, and the caption says so rather than implying a
 * tick that does not exist.
 */

import Link from "next/link";
import { useMemo, useState } from "react";
import { PnlFigure } from "@/components/PnlChart";
import {
  cash,
  fmtNum,
  fmtPct,
  fmtUnits,
  liveMetricRows,
  liveMetrics,
  livePnl,
  money,
  price,
  sign,
  statusChip,
  type Sys,
} from "@/lib/live";
import { fmtMoney } from "@/lib/format";
import {
  contributionOf,
  downloadBasketFills,
  type PaperLeg,
  type PaperRecord,
} from "@/lib/paperbasket";

/* ------------------------------------------------------------------ the curve */

export function BasketCurve({
  rec,
  replay,
}: {
  rec: PaperRecord;
  replay: boolean;
}) {
  const pnl = rec.pnlPct;
  const dollars = rec.equity - rec.capital;

  if (!rec.reporting) {
    return (
      <p className="sec-note">
        The desk has published nothing for any of these legs yet. A basket that was created
        moments ago sits here until the next control tick attaches it, and one whose legs
        were refused shows the reason against each leg in the table below.
      </p>
    );
  }

  return (
    <div className="sys-live">
      <div className="sys-headline">
        <span className={`pnl-val num ${sign(pnl)}`}>{fmtPct(pnl)}</span>
        <span className="pnl-lbl">
          cumulative {replay ? "replay" : "paper"} P&amp;L on the whole pot
          {rec.since ? ` since ${rec.since}` : ""}
        </span>
      </div>

      {rec.curve.length > 1 ? (
        <PnlFigure
          curve={rec.curve}
          breaks={rec.breaks}
          from={rec.since || "start"}
          to="now"
        />
      ) : (
        <p className="pnl-young">
          The record is {rec.curve.length} closed bar{rec.curve.length === 1 ? "" : "s"} old
          — a line needs two. The figure above it is live either way.
        </p>
      )}

      <p className="sec-note" style={{ maxWidth: "80ch" }}>
        <span
          className="explains"
          title={
            "Equal capital per leg and NO rebalancing between them, because that is what " +
            "the desk does: each leg is its own funded book and nothing moves money from " +
            "one to another. Under a fixed equal split the pot's cumulative return is the " +
            "plain mean of the legs' — a leg the desk has not attached yet contributes 0, " +
            "which is its money sitting in cash rather than an absence.\n\n" +
            "The research blend further up this page rebalances monthly. These are " +
            "different numbers on purpose and must not be compared."
          }
        >
          {rec.legs.length} leg{rec.legs.length === 1 ? "" : "s"}, equal capital, no
          rebalancing between them
        </span>{" "}
        · {fmtMoney(rec.capital)} in, {fmtMoney(rec.equity)} marked{" "}
        <span className={`num ${sign(dollars)}`}>
          ({dollars >= 0 ? "+" : ""}
          {money(dollars)})
        </span>
        {rec.reporting < rec.legs.length && (
          <>
            {" "}· {rec.legs.length - rec.reporting} leg
            {rec.legs.length - rec.reporting === 1 ? "" : "s"} not reporting, held as cash
            in this figure
          </>
        )}
        {rec.breaks.length > 0 && (
          <>
            {" "}· cut at {rec.breaks.length} outage
            {rec.breaks.length === 1 ? "" : "s"}
          </>
        )}
      </p>

      {!rec.aligned && (
        <div className="note">
          <b>This basket mixes timeframes, so the line is not a time axis.</b> The legs are
          aligned by bar index at the tail, which is only the same thing as aligning them by
          date while every leg sits on one grid. The end point — the figure above — is
          correct regardless; the shape in between is not, and the leg table below is the
          honest reading of it.
        </div>
      )}

      <p className="sec-note">
        <span
          className="explains"
          title={
            "The desk republishes when a bar closes and marks its holdings then. A daily " +
            "leg therefore moves once a day and a 4h leg six times; nothing prices the " +
            "book between bars, so there is no intraday tick to show and none is drawn."
          }
        >
          repaints when the desk republishes — once per closed bar
        </span>{" "}
        · {rec.running} of {rec.legs.length} legs running, {rec.open} holding a position ·{" "}
        <Link href="/paper/">the whole desk</Link>
      </p>
    </div>
  );
}

/* ------------------------------------------------------- the legs, live */

/* NOT THE BACKTEST'S FIGURES. `LegTable` on this same page prints what each leg did over
 * the walk-forward years; every column here is the desk's own record since the basket was
 * picked up. They will disagree, often by a lot, and the reason is that four weeks is not
 * a measurement of a rule — which is the sentence in the caption rather than a footnote.
 *
 * Contribution is a share of the WHOLE POT, not of the profit, so the column adds up to
 * the basket's own return. Dividing by a profit that happens to be negative reports a leg
 * that made money as a negative contributor. */
interface LegCol {
  h: string;
  l?: boolean;
  doc: string;
  cell: (l: PaperLeg, n: number, replay: boolean) => React.ReactNode;
}

const LEG_COLS: LegCol[] = [
  {
    h: "Leg",
    l: true,
    doc: "The rule this leg trades. Its research evidence — the decades, not these weeks — "
       + "is on its own strategy page.",
    cell: (l) => (
      <>
        <Link
          href={`/rule/?cls=${encodeURIComponent(l.leg.cls)}&tf=${encodeURIComponent(l.leg.tf)}&rule=${encodeURIComponent(l.leg.rule)}`}
        >
          {l.label}
        </Link>
        <span className="grp-meta">
          {" "}· {l.leg.tf} {l.leg.cls}
        </span>
      </>
    ),
  },
  {
    h: "Status",
    l: true,
    doc: "What the desk is doing with this leg right now. `want` is what was asked for and "
       + "`state` is what the desk has done; while they disagree the leg is being attached "
       + "or retired, and that disagreement is information rather than a bug.",
    cell: (l, _n, replay) => {
      if (!l.sys)
        return (
          <span className="chip mut" title="the desk has published no row for this leg">
            {l.leg.state || l.leg.want || "pending"}
          </span>
        );
      const [c, label] = statusChip(l.sys, replay);
      return <span className={`chip ${c}`}>{label}</span>;
    },
  },
  {
    h: "Position",
    l: true,
    doc: "Whether the book is in the market, and how many of its names it is holding. A "
       + "book holds a whole asset class, so `flat` means every name in it is flat.",
    cell: (l) =>
      l.sys ? (
        <>
          <span className="chip mut">{l.sys.state || "—"}</span>
          {l.sys.names ? (
            <span className="grp-meta">
              {" "}
              {l.sys.held ?? 0}/{l.sys.names} held
            </span>
          ) : null}
        </>
      ) : (
        <span className="flat">—</span>
      ),
  },
  {
    h: "Money in it",
    doc: "What this leg was funded with out of the pot. Equal shares — that is what a "
       + "portfolio here is — and it is re-split whenever the membership changes.",
    cell: (l) => <>{fmtMoney(l.sys?.capital ?? (Number(l.leg.capital) || 0))}</>,
  },
  {
    h: "Marked at",
    doc: "What the leg's book is worth at the desk's latest marks: cash plus every holding "
       + "at its last bar close. This is the number that moves.",
    cell: (l) => <>{l.sys?.equity == null ? "—" : fmtMoney(l.sys.equity)}</>,
  },
  {
    h: "P&L",
    doc: "This leg's own cumulative percent since its first fill, chained across every "
       + "restart — `store.lifetime_curve`, not the current session's figure, which resets "
       + "to zero every time the desk is redeployed.",
    cell: (l) => {
      const v = l.sys ? livePnl(l.sys) : null;
      return <span className={v == null ? "" : sign(v)}>{fmtPct(v)}</span>;
    },
  },
  {
    h: "Contributed",
    doc: "This leg's share of what the BASKET returned, in percentage points of the whole "
       + "pot. These add up to the figure at the top of this section. It is a share of the "
       + "pot and not of the profit: dividing by a profit that happens to be negative "
       + "reports a leg that made money as a negative contributor.",
    cell: (l, n) => {
      const v = contributionOf(l, n);
      return <span className={sign(v)}>{fmtPct(v)}</span>;
    },
  },
  {
    h: "Fills",
    doc: "Every fill this leg has recorded, across all sessions — `store.fill_count`, the "
       + "count in the database, which survives a restart. The table below shows the most "
       + "recent of them.",
    cell: (l) =>
      l.sys ? (
        <>{(l.sys.lifetime_trades ?? l.sys.paper_trades ?? 0).toLocaleString()}</>
      ) : (
        <span className="flat">—</span>
      ),
  },
  {
    h: "Since",
    l: true,
    doc: "When this leg's record starts. A leg swapped in last week has a shorter record "
       + "than the basket does, and its P&L is over that shorter window.",
    cell: (l) => <>{l.sys?.since || "—"}</>,
  },
];

export function BasketLegs({ rec, replay }: { rec: PaperRecord; replay: boolean }) {
  if (!rec.legs.length)
    return <p className="sec-note">This basket holds no legs yet.</p>;

  const n = rec.legs.length;
  // Biggest contributor first, so the leg carrying the basket is the first row read. Legs
  // the desk has published nothing for sort last rather than at 0 among the losers.
  const sorted = [...rec.legs].sort((a, b) => {
    if (!a.sys !== !b.sys) return a.sys ? -1 : 1;
    return contributionOf(b, n) - contributionOf(a, n);
  });

  return (
    <>
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              {LEG_COLS.map((c) => (
                <th key={c.h} className={c.l ? "l" : undefined}>
                  <span className="explains" title={c.doc}>
                    {c.h}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((l) => (
              <tr key={l.leg.strategy_id || `${l.leg.cls}|${l.leg.tf}|${l.leg.rule}`}>
                {LEG_COLS.map((c) => (
                  <td key={c.h} className={c.l ? "l" : "num"}>
                    {c.cell(l, n, replay)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
          <caption>
            The desk&apos;s own record, not the research. Weeks of fills describe the
            execution path — that the orders went out, filled, and were marked — and say
            nothing about whether the rules work. That question is the risk-matched one over
            decades and it is answered on <Link href="/">Research</Link>.
          </caption>
        </table>
      </div>
    </>
  );
}

/* ---------------------------------------------- the basket's numbers, live */

/* The same arithmetic `/paper/sys/` runs on one system, over the blended basket curve.
 * `liveMetrics` needs a class and a timeframe to annualise on, and a basket has one only
 * when every leg shares it — so a mixed basket gets the counts and the drawdown and an
 * em-dash where the annualised figures would be, rather than a Sharpe computed on a bar
 * count that means two different things. */
export function BasketMetrics({ rec }: { rec: PaperRecord }) {
  const cls = rec.legs[0]?.leg.cls ?? "";
  const tf = rec.aligned ? (rec.legs[0]?.leg.tf ?? "") : "";
  const rows = rec.legs.map((l) => l.sys).filter((s): s is Sys => !!s);
  const m = useMemo(
    () => liveMetrics(rows, rec.curve, cls, tf),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rec.curve.length, rec.curve[rec.curve.length - 1], rec.shown, cls, tf],
  );

  if (!rec.reporting)
    return <p className="sec-note">Nothing has been recorded for this basket yet.</p>;

  return (
    <>
      {!rec.aligned && (
        <p className="sec-note">
          Volatility and Sharpe are withheld on this basket. Annualising needs a bars-a-year
          figure and the legs are on different timeframes, so there is no single one — a
          number computed on the wrong grid would be wrong by the ratio between them, which
          is a factor of six between daily and 4h. The counts, the drawdown and the trade
          statistics below do not depend on it and are exact.
        </p>
      )}
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
              <td className="l">
                <span className="explains" title={help}>
                  {name}
                </span>
              </td>
              <td className="num">{val}</td>
            </tr>
          ))}
        </tbody>
        <caption>
          Measured over the closed bars of THIS record and over the fills below, on the
          blended pot. There is deliberately no benchmark column: the comparison that
          decides whether a basket is worth running is the risk-matched one over decades,
          further up this page.
        </caption>
      </table>
    </div>
    </>
  );
}

/* ------------------------------------------------------------------ the fills */

/* WHAT THE DESK PUBLISHES, WHICH IS NOT THE WHOLE RECORD. `paper_state.MAX_TRADES` caps
 * `trades` at 200 PER LEG while `lifetime_trades` counts the database, so the header says
 * which of the two is on screen instead of quietly printing the shorter one.
 *
 * Both P&L columns, labelled. `realised` is what that ONE fill closed against what the
 * closed part cost — null when it opened or added — and `pnl` is the whole book's mark at
 * that instant. Never summed together; they answer different questions. */
export function BasketFills({ rec, name }: { rec: PaperRecord; name: string }) {
  const [leg, setLeg] = useState<string>("");
  const fills = useMemo(
    () => (leg ? rec.fills.filter((t) => t.leg === leg) : rec.fills),
    [rec.fills, leg],
  );

  const legNames = rec.legs.map((l) => l.label);

  if (!rec.lifetime && !fills.length)
    return (
      <p className="sec-note">
        No fills yet — none of these legs has opened a position. A book waits out its
        warm-up before it trades, and a rule that is flat has nothing to report.
      </p>
    );

  return (
    <>
      <div className="tbl-tools">
        <button className="btn" onClick={() => downloadBasketFills(name, fills)}>
          Export CSV
        </button>
        <button
          type="button"
          className={`pill${leg === "" ? " on" : ""}`}
          onClick={() => setLeg("")}
        >
          All legs
        </button>
        {legNames.map((l) => (
          <button
            key={l}
            type="button"
            className={`pill${leg === l ? " on" : ""}`}
            onClick={() => setLeg(l)}
          >
            {l}
          </button>
        ))}
        <span className="sec-note">
          {rec.shown < rec.lifetime
            ? `the last ${rec.shown.toLocaleString()} of ${rec.lifetime.toLocaleString()} fills`
            : `${rec.shown.toLocaleString()} fill${rec.shown === 1 ? "" : "s"}`}
          , newest first
          {rec.shown < rec.lifetime
            ? " — the desk publishes its most recent 200 per leg; the full record is retained"
            : ""}
        </span>
      </div>

      <div className="tbl-wrap fills-box">
        <table>
          <thead>
            <tr>
              <th className="l">Time</th>
              <th className="l">
                <span
                  className="explains"
                  title="Which leg of this basket made the fill. Five books trade side by
                         side under one pot and their fills are not interchangeable."
                >
                  Leg
                </span>
              </th>
              <th className="l">Asset</th>
              <th className="l">Side</th>
              <th>Qty</th>
              <th>Price</th>
              <th>
                <span
                  className="explains"
                  title={
                    "What this ONE FILL closed, against what the closed part cost. Blank " +
                    "on a fill that opened or added, because it closed nothing. The trade " +
                    "statistics count only this column."
                  }
                >
                  Realised P&amp;L
                </span>
              </th>
              <th>
                <span
                  className="explains"
                  title={
                    "That LEG's whole book mark at that moment, so every name filling in " +
                    "the same second carries the same value — and two different legs' " +
                    "rows carry two different books' marks. Never sum this column."
                  }
                >
                  Book P&amp;L
                </span>
              </th>
            </tr>
          </thead>
          <tbody>
            {fills.map((t, i) => (
              <tr key={`${t.leg}|${t.ts}|${t.symbol}|${i}`}>
                <td className="l">{t.ts || ""}</td>
                <td className="l">{t.leg}</td>
                <td className="l">{t.symbol || ""}</td>
                <td className={`l ${t.side === "BUY" ? "gain" : "loss"}`}>{t.side || ""}</td>
                <td className="num">{fmtUnits(t.qty)}</td>
                <td className="num">{price(t.price)}</td>
                <td
                  className={`num ${t.realised == null ? "" : sign(t.realised)}`}
                  title={
                    t.realised == null
                      ? "this fill opened or added — it closed nothing"
                      : undefined
                  }
                >
                  {cash(t.realised)}
                </td>
                <td className={`num book-pnl ${sign(t.pnl)}`}>{cash(t.pnl)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!fills.length && (
        <p className="sec-note">
          That leg has published no fills. {fmtNum(rec.lifetime, 0)} are recorded across the
          basket.
        </p>
      )}
    </>
  );
}
