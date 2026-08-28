"use client";

/* THE LEGS, AND WHAT EACH ONE CONTRIBUTED — the reason the portfolio page exists.
 *
 * A basket's combined curve says what happened; this says who did it. Without it a reader
 * cannot tell a portfolio carried by one leg from one where five pulled together, and those
 * are different things to own — the first is a single position wearing a basket's name.
 *
 * CONTRIBUTION IS A SHARE OF THE POT, NOT A SHARE OF THE PROFIT, and that is the blend
 * engine's decision rather than this table's. A leg's dollar P&L over the whole capital is
 * signed and additive: the legs sum to the book's own total return exactly. A share of the
 * profit does not — when the book loses money, dividing by a negative profit reports a leg
 * that MADE money as a negative contributor.
 *
 * Every column carries a `doc`, and that is a TYPE requirement here exactly as it is on the
 * leaderboard: a column added without one is the one column nobody can ask about, so it
 * does not compile. They are `title`s rather than the leaderboard's `.coldoc` popover
 * because this is a short fixed table of one-sentence explanations, and a native tooltip
 * needs no timer, no placement pass and no dismiss handler.
 *
 * Each leg links OUT to its own strategy page rather than being reproduced here. One rule's
 * evidence is nineteen columns, a risk-matched chart, a robustness matrix and a per-name
 * table; a second copy of any of it would be a second opinion to keep in step.
 */

import type { ReactElement } from "react";
import Link from "next/link";
import { SERIES_COLORS } from "@/lib/columns";
import { fmtMoney, fmtNum, fmtPct } from "@/lib/format";
import { legKey, type BlendLeg, type PortfolioLeg } from "@/lib/portfolio";

export interface LegCtx {
  capital: number | null;
  /** This leg's mean correlation with the others, or null where there is no matrix. */
  corrOf: (i: number) => number | null;
  /** The ledger row for this leg, where there is one. Absent on a PREVIEW, whose legs do
   *  not exist yet — which is why the desk column is offered rather than assumed. */
  rowOf: (l: BlendLeg) => PortfolioLeg | null;
  /** The leg's index, for the colour it has in the correlation panel. */
  index: number;
  /** The largest |contribution| on the table, so the bars are comparable down the column
   *  rather than each drawn against an absolute scale nothing reaches. */
  peak: number;
  /** The span the blend covers, so a leg whose own history is longer can say so. */
  years: number | null;
}

export interface LegCol {
  h: string;
  /** Left-aligned, for the text columns. */
  l?: boolean;
  /** WHAT IT MEANS. Required: a column nobody can ask about may not be added. */
  doc: string;
  cell: (leg: BlendLeg, cx: LegCtx) => ReactElement;
}

const Dash = () => <td className="flat">—</td>;

const ruleHref = (l: BlendLeg) =>
  `/rule/?cls=${encodeURIComponent(l.cls)}&tf=${encodeURIComponent(l.tf)}&rule=${encodeURIComponent(l.rule)}`;

const sysHref = (l: BlendLeg) =>
  `/paper/sys/?cls=${encodeURIComponent(l.cls)}&tf=${encodeURIComponent(l.tf)}&rule=${encodeURIComponent(l.rule.replace(/[^A-Za-z0-9_]/g, "-"))}`;

/** A contribution, drawn as well as printed. Neutral ink and never a gain/loss hue: on this
 *  site those two colours mean gained and lost, and the SIGN is already carried by the
 *  printed number and by which side of the centre line the bar sits on. A leg that lost
 *  money inside a basket that made some is a real and important row. */
function ShareBar({ v, peak }: { v: number; peak: number }) {
  const w = peak > 0 ? Math.min(Math.abs(v) / peak, 1) * 50 : 0;
  return (
    <span className="corr-track" aria-hidden="true">
      <span className="corr-mid" />
      <span
        className="corr-fill"
        style={v >= 0 ? { left: "50%", width: `${w}%` } : { right: "50%", width: `${w}%` }}
      />
    </span>
  );
}

export const LEG_COLS: LegCol[] = [
  {
    h: "Leg",
    l: true,
    doc: "The rule, its timeframe and its asset class. Its own evidence — the walk-forward "
       + "ranking, the risk-matched chart, the per-name table — is on its strategy page; "
       + "this table is only about what it did inside this basket.",
    cell: (l, cx) => (
      <td className="l">
        <i
          className="corr-dot"
          style={{ background: SERIES_COLORS[cx.index] ?? "var(--muted)" }}
          aria-hidden="true"
        />
        <Link href={ruleHref(l)} onClick={(e) => e.stopPropagation()}>
          {l.rule}
        </Link>
        <span className="grp-meta"> · {l.tf} {l.cls}</span>
      </td>
    ),
  },
  {
    h: "Share of pot",
    doc: "How much of the money this leg was given at inception. Equal across the legs by "
       + "construction, and reset back to equal every month — so a leg that grew is trimmed "
       + "and one that fell is topped up.",
    cell: (l) => (l.weight == null ? <Dash /> : <td>{fmtNum(l.weight * 100, 1)}%</td>),
  },
  {
    h: "Money in it",
    doc: "The pot times the share above. What the leg started with, not what it is worth "
       + "now — this table is the research blend, not the desk's record.",
    cell: (l, cx) =>
      cx.capital == null || l.weight == null ? <Dash /> : <td>{fmtMoney(cx.capital * l.weight)}</td>,
  },
  {
    h: "Alone / yr",
    doc: "What this leg compounded at <b>on its own</b> over the span the basket covers. "
       + "Not what it earned inside the basket — that is the next column — and not the "
       + "figure on its own dashboard row, which is measured over that rule's whole history "
       + "rather than the part it shares with the other legs.",
    cell: (l) => (l.cagr == null ? <Dash /> : <td>{fmtNum(l.cagr * 100, 1)}%</td>),
  },
  {
    h: "Contributed",
    doc: "This leg's dollar profit as a share of the <b>whole pot</b> — signed, and additive: "
       + "the legs add up to the portfolio's own total return exactly. It is deliberately "
       + "not a share of the profit, because dividing by a negative profit reports a leg "
       + "that made money as a negative contributor. Bars are scaled to the largest leg on "
       + "this table so they can be read against each other.",
    cell: (l, cx) =>
      l.contribution == null ? (
        <Dash />
      ) : (
        <td>
          <span className="leg-share">
            <ShareBar v={l.contribution} peak={cx.peak} />
            <b className="num">{fmtPct(l.contribution * 100, 0)}</b>
          </span>
        </td>
      ),
  },
  {
    h: "Alike the rest",
    doc: "Mean correlation of this leg with the other legs. High is the warning: a leg that "
       + "moves with everything else is adding exposure rather than diversification, "
       + "whatever it contributed. Measured over the span the legs share.",
    cell: (l, cx) => {
      const v = cx.corrOf(cx.index);
      return v == null ? <Dash /> : <td>{fmtNum(v, 2)}</td>;
    },
  },
  {
    h: "Own history",
    doc: "How long this leg's own record is. The basket is measured over the "
       + "<b>intersection</b> of its legs' histories, never the union, so a leg with more "
       + "years than the span above has had the rest of them discarded here — they are not "
       + "in any figure on this page.",
    cell: (l, cx) =>
      l.ownYears == null ? (
        <Dash />
      ) : (
        <td
          title={
            l.ownStart && l.ownEnd
              ? `its own record runs ${l.ownStart} to ${l.ownEnd}`
              : undefined
          }
          className={cx.years != null && l.ownYears > cx.years * 1.05 ? "" : "flat"}
        >
          {fmtNum(l.ownYears, 1)}y
        </td>
      ),
  },
];

/** The desk column is APPENDED rather than always present: a preview's legs do not exist on
 *  any ledger, and a column of em-dashes there would read as "registered but not running". */
export const LEG_DESK_COL: LegCol = {
  h: "On the desk",
  l: true,
  doc: "What was asked for, and what the desk has done — <b>two different fields</b>, "
     + "written by two different processes. They disagree while the desk catches up. A leg "
     + "with no row here is not registered at all.",
  cell: (l, cx) => {
    const row = cx.rowOf(l);
    if (!row) return <td className="l flat">not registered</td>;
    const settled = row.want && row.state && row.want === row.state;
    return (
      <td className="l">
        <span className={`chip ${row.state === "live" ? "run" : row.state === "warming" ? "warm" : "mut"}`}>
          {row.state || "—"}
        </span>
        {!settled && <span className="grp-meta"> · asked {row.want || "—"}</span>}{" "}
        <Link href={sysHref(l)} onClick={(e) => e.stopPropagation()} className="grp-meta">
          record
        </Link>
      </td>
    );
  },
};

/** The `doc`s carry <b> for emphasis; a `title` attribute renders no markup, so it is
 *  stripped rather than shown as literal angle brackets. */
const stripTags = (s: string) => s.replace(/<[^>]+>/g, "");

export interface LegTableProps {
  legs: BlendLeg[];
  capital: number | null;
  /** The blend's span, for the "own history" column's comparison. */
  years?: number | null;
  corrOf: (i: number) => number | null;
  /** Provided only where the legs are real registrations. */
  rowOf?: (l: BlendLeg) => PortfolioLeg | null;
}

export function LegTable({ legs, capital, years = null, corrOf, rowOf }: LegTableProps) {
  const cols = rowOf ? [...LEG_COLS, LEG_DESK_COL] : LEG_COLS;
  const peak = Math.max(0, ...legs.map((l) => Math.abs(l.contribution ?? 0)));
  const rows = legs.map((l, i) => ({ l, i }));
  // Biggest contributor first, so the leg that did the work is at the top. Legs with no
  // contribution to report sink rather than being dropped — the row still says what the leg
  // is and whether it is running.
  rows.sort((a, b) => (b.l.contribution ?? -Infinity) - (a.l.contribution ?? -Infinity));

  return (
    <div className="tbl-wrap">
      <table>
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c.h} className={c.l ? "l" : undefined}>
                {/* Every column explains itself. The type makes that unskippable. */}
                <span className="explains" title={stripTags(c.doc)}>{c.h}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(({ l, i }) => (
            <tr key={legKey(l) + i}>
              {cols.map((c, ci) => (
                <ColCell
                  key={ci}
                  col={c}
                  leg={l}
                  ctx={{
                    capital,
                    corrOf,
                    rowOf: rowOf ?? (() => null),
                    index: i,
                    peak,
                    years,
                  }}
                />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ColCell({ col, leg, ctx }: { col: LegCol; leg: BlendLeg; ctx: LegCtx }) {
  return col.cell(leg, ctx);
}
