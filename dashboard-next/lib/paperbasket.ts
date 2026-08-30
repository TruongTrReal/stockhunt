"use client";

/* THE PAPER RECORD OF A BASKET — what the desk HAS done with it, moving with the feed.
 *
 * `lib/portfolio.ts` is the other measurement: the legs' book curves blended over the
 * years the walk-forward covers, which is what the basket WOULD have done. This file is
 * the desk's own record out of `live.json`. They are separate modules for the same reason
 * `api_portfolios.backtest` says it in a docstring — days of paper fills and years of
 * walk-forward are different measurements, and the one thing neither page may ever do is
 * add them or draw them on one pair of axes.
 *
 * THE LEGS ARE MATCHED BY `sid`, NEVER BY (cls, tf, rule). A rule can be promoted by hand
 * AND held by a basket at the same time — two registrations, two funded books, two
 * records of the same rule — so matching on the rule would pull the hand-promoted book's
 * fills into the basket's record and count its money twice. `store.sid_for` is
 * `account:name` and `portfolios._leg_name` puts the portfolio inside the name, which is
 * exactly the discriminator this needs.
 */

import type { Fill } from "@/lib/api";
import { fillsOf, livePnl, type Sys } from "@/lib/live";
import { legsOf, type Portfolio, type PortfolioLeg } from "@/lib/portfolio";

/** The record's identity for one leg: `00:pf-top5-us_stocks-1d-us_stocks-1d-ibs`. */
export const sidOf = (p: Portfolio, l: PortfolioLeg): string | null =>
  l.name ? `${p.account}:${l.name}` : null;

export interface PaperLeg {
  leg: PortfolioLeg;
  /** The desk's row for this leg, or null when it has published none — a leg the desk has
   *  not attached yet, or one whose account this reader cannot see. */
  sys: Sys | null;
  /** What to call it in a column. The rule alone, because every leg of a follow-portfolio
   *  shares its class and timeframe and repeating them turns five labels into one. */
  label: string;
}

/** This basket's legs, each paired with the desk's row for it, in the ledger's order. */
export function paperLegs(p: Portfolio | null, rows: Sys[]): PaperLeg[] {
  if (!p) return [];
  const bySid = new Map(rows.map((s) => [String(s.id), s]));
  return legsOf(p).map((leg) => {
    const sid = sidOf(p, leg);
    return { leg, sys: sid ? (bySid.get(sid) ?? null) : null, label: leg.rule };
  });
}

/** Do all the legs trade one sheet? A follow-portfolio does by construction; a hand-picked
 *  basket need not, and that decides whether the curve below is a time series at all —
 *  see `paperCurve`. */
export const oneSheet = (legs: PaperLeg[]) =>
  new Set(legs.map((l) => `${l.leg.cls}|${l.leg.tf}`)).size <= 1;

/** THE BASKET'S OWN CUMULATIVE RETURN, in percent, from the desk's record.
 *
 * Equal capital per leg and NO rebalancing between legs, because that is what the desk
 * actually does: each leg is its own funded book and nothing ever moves money from one to
 * another. Under a fixed equal split the portfolio's cumulative return is the plain MEAN
 * of the legs' cumulative returns — `sum((C/n)(1 + r_i)) / C - 1` — so there is no
 * compounding arithmetic to get wrong and none is invented.
 *
 * The research blend rebalances monthly and this does not. They are different numbers on
 * purpose and the captions say which is which.
 *
 * A LEG WITH NO RECORD CONTRIBUTES 0, NOT NOTHING. Its share of the pot is real money
 * sitting in cash, so the sum is divided by the FULL leg count rather than by however many
 * legs happened to publish a curve. Dividing by the present ones reports a half-funded
 * basket at the return of its funded half.
 *
 * Aligned at the TAIL, because a leg added later has a shorter record and stretching it to
 * the longest would invent history for it. That is INDEX alignment, not time alignment,
 * and it is exact only while every leg sits on one bar grid and under
 * `store.MAX_CURVE_POINTS` — both true of a follow-portfolio, which is what `oneSheet`
 * gates the caption on.
 */
export function paperCurve(
  legs: PaperLeg[],
  field: "paper_curve" | "bench_curve" = "paper_curve",
): number[] {
  const cs = legs
    .map((l) => l.sys?.[field])
    .filter((c): c is number[] => Array.isArray(c) && c.length > 1);
  if (!cs.length) return [];
  const n = Math.max(...cs.map((c) => c.length));
  const denom = Math.max(legs.length, cs.length);
  return Array.from({ length: n }, (_, i) => {
    let sum = 0;
    for (const c of cs) {
      const j = i - (n - c.length);
      if (j >= 0) sum += c[j];
    }
    return sum / denom;
  });
}

/** Where the chained record LOST A BAR, mapped onto the blended curve.
 *
 * Only from a single reporting leg. Averaging several legs blends their outages together,
 * and a cut drawn at a point where four of five legs were trading normally is a claim
 * about the record that is not true. `systemBreaks` makes the same call one level down.
 */
export const paperBreaks = (legs: PaperLeg[]): number[] => {
  const present = legs.map((l) => l.sys).filter((s): s is Sys => !!s);
  return present.length === 1 ? present[0].curve_breaks || [] : [];
};

/** A fill, with the leg that made it. `systemFills` answers the same question for one
 *  rule's deployments, where naming the leg would name the rule the page is already about;
 *  here five legs share one table and the column is the whole point. */
export interface PaperFill extends Fill {
  leg: string;
  cls?: string;
  tf?: string;
}

export function paperFills(legs: PaperLeg[]): PaperFill[] {
  const out: PaperFill[] = [];
  for (const { sys, label, leg } of legs) {
    for (const t of ((sys?.trades as Fill[] | undefined) || [])) {
      out.push({
        ...t,
        symbol: t.symbol || String(sys?.symbol ?? ""),
        leg: label,
        cls: leg.cls,
        tf: leg.tf,
      });
    }
  }
  return out.sort((a, b) => String(b.ts || "").localeCompare(String(a.ts || "")));
}

/** What this leg was funded with out of the pot. The desk's figure when it has published
 *  one, the ledger's while it is still being attached — so the pot does not appear to
 *  shrink between the two. */
export const capitalOf = (l: PaperLeg) => l.sys?.capital ?? (Number(l.leg.capital) || 0);

/** What a leg's share of the pot is worth ON THE RECORD.
 *
 * NOT the desk's `equity` field, and that distinction cost a wrong figure on this page.
 * The sandbox re-funds every book at its configured capital when the desk restarts, so
 * `equity` is the CURRENT SESSION's mark and drops back to the pot on every deploy. The
 * page printed "+18.32%" from the chained lifetime curve beside "$20k marked" from the
 * session — two different measurements in adjacent columns, and the money one silently
 * said the basket had made nothing.
 *
 * The dollars therefore come off the SAME series as the percent: the lifetime curve,
 * applied to the leg's funding. `livePnl` already makes the same call one level down and
 * for the same reason.
 */
export const valueOf = (l: PaperLeg) =>
  capitalOf(l) * (1 + (l.sys ? livePnl(l.sys) / 100 : 0));

export interface PaperRecord {
  legs: PaperLeg[];
  /** Legs the desk has actually published a row for. */
  reporting: number;
  curve: number[];
  breaks: number[];
  /** Every published fill across the legs, newest first, each carrying its leg. */
  fills: PaperFill[];
  /** Cumulative percent now — the last point of the curve, 0 before there is one. */
  pnlPct: number;
  /** The pot, and what it is worth on the record. Both off the lifetime curve — see
   *  `valueOf` for why the desk's own `equity` field is the wrong number here. */
  capital: number;
  value: number;
  /** Fills the desk has PUBLISHED (capped per leg) against fills it has RECORDED. */
  shown: number;
  lifetime: number;
  since: string | null;
  running: number;
  open: number;
  /** True while every leg trades one sheet, so the curve reads as a time series. */
  aligned: boolean;
}

export function paperRecord(p: Portfolio | null, rows: Sys[]): PaperRecord {
  const legs = paperLegs(p, rows);
  const present = legs.map((l) => l.sys).filter((s): s is Sys => !!s);
  const curve = paperCurve(legs);
  const fills = paperFills(legs);
  // The EARLIEST leg's start, never the newest. The basket's record begins when its first
  // leg did; taking the last would date the whole record from its most recent swap.
  const sinces = present.map((s) => s.since).filter((v): v is string => !!v).sort();
  return {
    legs,
    reporting: present.length,
    curve,
    breaks: paperBreaks(legs),
    fills,
    pnlPct: curve.length ? curve[curve.length - 1] : 0,
    // The LEDGER's capital for a leg the desk has not published yet, so the pot does not
    // appear to shrink while a leg is being attached.
    capital: legs.reduce((a, l) => a + capitalOf(l), 0),
    value: legs.reduce((a, l) => a + valueOf(l), 0),
    shown: fills.length,
    lifetime: present.reduce((a, s) => a + fillsOf(s), 0),
    since: sinces[0] ?? null,
    running: present.filter((s) => s.status === "running").length,
    open: present.filter((s) => s.state && s.state !== "flat").length,
    aligned: oneSheet(legs),
  };
}

/** One leg's share of what the BASKET made, in percentage points of the whole pot.
 *
 * Shares of the pot and not of the profit — the same convention `LegTable` prints for the
 * research blend, and for the same reason: dividing by a profit that happens to be
 * negative reports a leg that made money as a negative contributor. These add up to the
 * basket's own return, which is what makes the column readable.
 */
export const contributionOf = (l: PaperLeg, nLegs: number) =>
  l.sys ? livePnl(l.sys) / Math.max(nLegs, 1) : 0;

/** Both P&L columns plus the leg, under the names they actually mean.
 *
 * `downloadFills` in `lib/live.ts` exports one system's; a basket's export has to carry
 * which leg each fill came from or the file is five books shuffled together.
 */
export function basketFillsCsv(fills: PaperFill[]): string {
  const head = "time,leg,symbol,side,qty,price,realised_pnl,book_pnl";
  const cell = (v: unknown) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [
    head,
    ...fills.map((t) =>
      [t.ts, t.leg, t.symbol, t.side, t.qty, t.price, t.realised, t.pnl]
        .map(cell)
        .join(","),
    ),
  ].join("\n");
}

export function downloadBasketFills(name: string, fills: PaperFill[]) {
  const blob = new Blob([basketFillsCsv(fills)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${name.replace(/[^A-Za-z0-9_-]/g, "-")}-fills.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
