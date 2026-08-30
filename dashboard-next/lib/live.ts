"use client";

/* THE LIVE DOCUMENT — the desk's own state, and everything the three paper views compute
 * from it.
 *
 * The vanilla board keeps this in one global (`window.DASH`) that `applyLive` mutates and
 * `repaintPaper` re-renders from. There is no global here, so the same job is done by one
 * module-level store with `useSyncExternalStore` on top: the socket and the poller live
 * outside React, exactly as they do over there, and a component subscribes to the result.
 * Keeping them outside React matters for the same reason the vanilla keeps them outside a
 * view — the stream must survive a route change, and a socket re-opened on every
 * navigation would reconnect the desk several times a minute.
 *
 * Three sources, and which one is read is not a style choice:
 *
 *   /live.json          the desk's own document, CUT TO THIS ACCOUNT by `api_live`. The
 *                       source. `account`/`house`/`is_admin` on it are what separate
 *                       "mine" from "everybody else's".
 *   /v1/board/systems   the snapshot the last build froze. THE FALLBACK, for when the
 *                       desk has not published — the same degradation the vanilla board
 *                       gets for free by having the snapshot baked into `data.js`.
 *   /v1/board/meta      `paper_groups` (with the note saying what a universe is worth as
 *                       evidence) and `paper_timeframes`. Configuration, one request.
 *
 * The research figures are deliberately NOT in here. Days of paper fills and years of
 * walk-forward are different measurements and this file must never make them one number.
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type RefObject,
} from "react";
import {
  board,
  liveSocketUrl,
  type BoardMeta,
  type Fill,
  type Live,
  type PaperGroup,
  type System,
} from "@/lib/api";

/* ------------------------------------------------------------------ what a system is */

export interface Holding {
  symbol: string;
  state?: string;
  units?: number | null;
  entry?: number | null;
  mark?: number | null;
  value?: number | null;
  pnl_pct?: number | null;
  trades?: number | null;
  warming?: boolean;
}

/** `System` in `lib/api.ts` is deliberately open (`[k: string]: unknown`) because that
 *  file describes the transport. This names the fields the paper views actually read. */
export interface Sys extends System {
  id: string;
  cls?: string;
  tf?: string;
  rule?: string;
  /** `book` means one account holding a whole asset class — one row, many names. */
  kind?: string;
  symbol?: string;
  state?: string;
  status?: string;
  since?: string;
  days?: number;
  note?: string;
  group?: string;
  account?: string;
  paper_pnl_pct?: number | null;
  paper_trades?: number;
  /** `store.fill_count` — the count in the DATABASE, which survives a restart. */
  lifetime_trades?: number | null;
  position_units?: number | null;
  units?: number | null;
  entry?: number | null;
  mark_price?: number | null;
  capital?: number;
  equity?: number;
  turnover?: number | null;
  held?: number;
  names?: number;
  holdings?: Holding[];
  /** Cumulative P&L in PERCENT since this system's first fill, chained across restarts. */
  paper_curve?: number[];
  bench_curve?: number[];
  /** Indexes where the record LOST A BAR. The line is cut here, never drawn through. */
  curve_breaks?: number[];
}

export type { Fill };

/* --------------------------------------------------------------------- formatters */
/* Copied from the vanilla board rather than shared, because the two boards are separate
 * bundles. Each carries the reason it prints what it prints. */

/** Re-exported, not redefined. This file had its own copy and `lib/format.ts` had another;
 *  a fix to one left the other printing the old thing under the same name. */
import { fmtPct } from "@/lib/format";
export { fmtPct };

export const fmtNum = (v: number | null | undefined, d = 1) =>
  v == null || !Number.isFinite(v) ? "—" : Number(v).toFixed(d);

export const money = (v: number | null | undefined) =>
  v == null ? "—" : "$" + v.toLocaleString(undefined, { maximumFractionDigits: 0 });

/** A signed P&L in dollars, to the cent. `null` prints an em-dash rather than $0.00: on a
 *  fill's realised P&L those are different facts — "closed nothing" and "closed at cost". */
export const cash = (v: number | null | undefined) =>
  v == null
    ? "—"
    : (v >= 0 ? "+" : "−") +
      "$" +
      Math.abs(v).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });

/** Fractional instruments hold positions to 6dp. 2624.767935 units is precision nobody
 *  reads; 4 significant decimals keeps crypto legible without turning shares into 2,625. */
export const fmtUnits = (v: number | null | undefined) =>
  !v
    ? "—"
    : Math.abs(v) >= 100
      ? v.toFixed(2)
      : Math.abs(v) >= 1
        ? v.toFixed(3)
        : v.toFixed(6);

export const price = (v: number | null | undefined) =>
  v == null ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });

/** Colour means one thing on this site: gained or lost. */
/** Re-exported, not redefined — see the note on the definition in `lib/format.ts`. */
import { sign } from "@/lib/format";
export { sign };

/** The same slug the backtest side uses. Nothing reverses it — a page finds its rows by
 *  matching `slug(s.rule)` against the segment, so it never has to be reversible. */
export const slug = (s: string) => String(s).replace(/[^A-Za-z0-9_]/g, "-");

/** Internal class keys leak into free text the desk composed at registration time. This
 *  turns them back into the labels the rest of the page uses, on the way to the screen. */
const CLASS_KEY_LABEL: Record<string, string> = {
  us_stocks: "US stock",
  us_etfs: "ETF",
  crypto: "crypto",
  commodities: "commodity",
  cme_futures: "CME futures",
};
export const prettyNote = (txt: string | null | undefined) =>
  String(txt || "").replace(
    /\b(us_stocks|us_etfs|crypto|commodities|cme_futures)\b/g,
    (k) => CLASS_KEY_LABEL[k] || k,
  );

/* ------------------------------------------------------------------ the two axes */

/* The order is the research's own, so the pills read the same way as the Research
 * section's rather than in whatever order the systems happened to register. */
export const PAPER_CLASS_ORDER = [
  "us_stocks",
  "us_etfs",
  "crypto",
  "commodities",
  "cme_futures",
];

export const PAPER_CLASS_LABEL: Record<string, string> = {
  us_stocks: "Top 100 stocks",
  us_etfs: "ETFs",
  crypto: "Crypto",
  commodities: "Commodities",
  cme_futures: "CME futures",
  /* Pre-2026-08-11 records, and any replay written from one, carry the old two-valued
   * class. Labelled rather than renamed: the sid is what identifies a system, so an old
   * row still belongs to the same record and is still worth showing under its own name. */
  equity: "Equities (legacy)",
};

export const classLabel = (cls: string) => PAPER_CLASS_LABEL[cls] || cls;

/** Every class the desk CAN run, not just the ones something is deployed on today —
 *  "nothing is running on ETFs" is a fact worth being able to check, and the empty state
 *  under the strip already says it. Anything unknown in the rows is APPENDED, never
 *  dropped, so an old record still has a home. */
export const paperClasses = (rows: Sys[]) => {
  const seen = new Set(rows.map((s) => s.cls).filter(Boolean) as string[]);
  return [
    ...PAPER_CLASS_ORDER,
    ...[...seen].filter((c) => !PAPER_CLASS_ORDER.includes(c)).sort(),
  ];
};

/* The same argument, one axis over. This strip was the literal pair `1d / 4h` — the two
 * horizons the HOUSE promotes its own books at — while the desk accepts a registration at
 * any of six (`paper_config.MEMBER_TIMEFRAMES`, which is what `/v1/limits` advertises and
 * what the join wizard offers). A member registering at 1h or 5m got a strategy that ran,
 * filled and published, and a board with no button that could reach it.
 *
 * `paper_timeframes` off `/v1/board/meta` carries the desk's own list, so the two cannot
 * drift again; the constant is only the fallback for a document built before the field
 * existed. Coarse to fine. */
export const PAPER_TF_ORDER = ["1d", "4h", "2h", "1h", "15m", "5m"];

export const paperTimeframes = (rows: Sys[], meta: BoardMeta | null) => {
  const offered =
    meta?.paper_timeframes && meta.paper_timeframes.length
      ? meta.paper_timeframes
      : PAPER_TF_ORDER;
  const seen = new Set(rows.map((s) => s.tf).filter(Boolean) as string[]);
  return [...offered, ...[...seen].filter((t) => !offered.includes(t))];
};

/* --------------------------------------------------- mine vs everybody else's */

/* The rows carry an `account`, the document says who is looking, and `house` names the
 * desk's own — a promoted book belongs to the desk rather than to a person, so the owner
 * reads it as theirs.
 *
 * A member never receives another member's rows at all, so for them this is Mine versus
 * the desk's. The owner does receive everybody's, which is why the split exists: it keeps
 * their page the same SHAPE as a member's, one group at a time, instead of one long mixed
 * list nobody else ever sees. */
export const isMine = (s: Sys, doc: Live | null) => {
  const a = String(s.account || doc?.house || "00");
  return a === String(doc?.account) || (!!doc?.is_admin && a === String(doc?.house || "00"));
};

export const hasAccount = (doc: Live | null) => doc?.account != null;

export const whoPills = (doc: Live | null): [string, string][] =>
  !hasAccount(doc)
    ? []
    : [
        ["mine", doc?.is_admin ? "Mine & the desk" : "Mine"],
        ["others", doc?.is_admin ? "Members" : "The desk"],
      ];

/* ------------------------------------------------------------- is anyone home? */

/* A run replayed from cached bars is NOT paper trading, and the difference is not a
 * technicality: it knows the whole price history, it completes in seconds, and its P&L
 * covers years rather than days. It is shown because it proves the order path — but it
 * has to be labelled every time it appears, or the first person to screenshot this page
 * reports a 283% gain as a live result. */
export const isReplay = (doc: Live | null) =>
  (doc?.feed as { status?: string } | undefined)?.status === "backtest";

/* `feed.status` is whatever the node last published, and a process that dies publishes
 * nothing further — so a dead desk keeps showing the state it was in when it went, which
 * for a node killed during start-up is "starting", forever. `generated_at` is the
 * heartbeat instead: `run_paper.start_marker` rewrites it once a minute for as long as the
 * process lives. Three marks of slack, because the window is one API call wide and a
 * single failed poll must not raise a false alarm. */
export const STALE_AFTER_MS = 180000;

export function feedAgeMs(doc: Live | null): number | null {
  // "2026-08-11 08:57 UTC" -> parseable. Anything unrecognised returns null, which every
  // comparison below treats as "cannot tell" rather than as stale.
  const t = Date.parse(
    String(doc?.generated_at || "").trim().replace(" UTC", "Z").replace(" ", "T"),
  );
  return Number.isFinite(t) ? Date.now() - t : null;
}

/* The vanilla exempts `__SNAPSHOT__` here — the one-file build embeds `live.json` and is
 * MEANT to be read months later, so age says nothing there about whether a desk is up.
 * This app has no such artifact: it cannot render without an API in front of it, so age
 * always means what it says. */
export const feedStale = (doc: Live | null) => {
  const age = feedAgeMs(doc);
  return !isReplay(doc) && age != null && age > STALE_AFTER_MS;
};

export const fmtAge = (ms: number) => {
  const m = Math.round(ms / 60000);
  return m < 90 ? `${m} min` : m < 2880 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} d`;
};

export const feedStatus = (doc: Live | null) =>
  (doc?.feed as { status?: string } | undefined)?.status ?? "";
export const feedSource = (doc: Live | null) =>
  (doc?.feed as { source?: string } | undefined)?.source ?? "";
export const feedPlan = (doc: Live | null) =>
  (doc?.feed as { plan?: string } | undefined)?.plan ?? "";
export const venueName = (doc: Live | null) =>
  (doc?.venue as { name?: string } | undefined)?.name ?? "";
export const venueEquity = (doc: Live | null) =>
  (doc?.venue as { equity?: number } | undefined)?.equity ?? 0;

const STATUS: Record<string, [string, string]> = {
  running: ["run", "live"],
  warming: ["warm", "warming up"],
  halted: ["halt", "halted"],
};

/** `[chip class, label]`. A replayed run is "running" in the state file because the
 *  strategy genuinely ran — but labelling it "live" on screen would be a lie about where
 *  the bars came from. */
export function statusChip(s: Sys, replay: boolean): [string, string] {
  const [c, l] = STATUS[s.status as string] || ["mut", s.status || ""];
  return l === "live" && replay ? ["mut", "replay"] : [c, l];
}

/* --------------------------------------------------------- what a SYSTEM is */

/* One rule at one horizon on one asset class — the thing the research ranked and the thing
 * you would decide to keep or drop. Running it across 20 mega-caps is deployment, not
 * twenty systems: counting instances made the headline read 330 where there are 20 systems
 * on the desk, overstating the breadth of what is being tested by an order of magnitude. */
export const systemKey = (s: Sys) => `${s.cls}|${s.tf}|${s.rule}`;
export const countSystems = (list: Sys[]) => new Set(list.map(systemKey)).size;

/** LIFETIME, not this session. `paper_trades` is a counter the strategy keeps in memory
 *  and resets on every restart — the desk had 1,389 fills behind it and the page said 39. */
export const fillsOf = (s: Sys) =>
  s.lifetime_trades != null ? s.lifetime_trades : s.paper_trades || 0;

export interface Aggregate {
  n: number;
  mean: number;
  live: number;
  fills: number;
  session: number;
  open: number;
}

/** A system's P&L, on the SAME measurement its curve draws.
 *
 * `paper_pnl_pct` is `equity / capital - 1` for the CURRENT SESSION, and `paper_curve` is
 * the chained LIFETIME series `store.lifetime_curve` rebuilds across every session and
 * gap. The desk restarts — for a deploy, for a widened universe — and at each restart the
 * field resets to zero while the curve carries on. So a row drew one measurement and
 * printed the other, and after a restart they disagreed in magnitude and, on
 * `top5-us_stocks-1d`, in SIGN: a rising green line beside a negative figure, on a site
 * where colour means gained or lost.
 *
 * The curve wins, because the lifetime record is the thing this desk exists to produce —
 * `store.py`'s whole argument is that a forward test which resets on restart is a series of
 * unrelated day-one snapshots. The field is the fallback for a system that has published no
 * curve yet, where it is the only answer there is.
 */
export function livePnl(s: Sys): number {
  const curve = s.paper_curve;
  if (Array.isArray(curve) && curve.length) {
    const last = curve[curve.length - 1];
    if (typeof last === "number" && Number.isFinite(last)) return last;
  }
  return s.paper_pnl_pct || 0;
}

export function aggregate(list: Sys[]): Aggregate {
  const n = list.length;
  const mean = n ? list.reduce((a, s) => a + livePnl(s), 0) / n : 0;
  return {
    n,
    mean,
    live: list.filter((s) => s.status === "running").length,
    fills: list.reduce((a, s) => a + fillsOf(s), 0),
    session: list.reduce((a, s) => a + (s.paper_trades || 0), 0),
    open: list.filter((s) => s.state && s.state !== "flat").length,
  };
}

/** Turnover for the SYSTEM, averaged over its deployments. Reported per name per year, the
 *  unit the walk-forward sheets use, so the two figures compare without converting. Absent
 *  rather than zero when nothing has published one. */
export function turnoverOf(rows: Sys[]): number | null {
  const vs = rows.map((s) => s.turnover).filter((v): v is number => typeof v === "number");
  return vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null;
}

/** "1 assets" is a lie about a book. A book is ONE row holding a whole class internally,
 *  so the count the reader wants is the names inside it. */
export function assetCount(rows: Sys[]) {
  const books = rows.filter((s) => s.kind === "book");
  if (!books.length) return `${rows.length} assets`;
  const names = books.reduce((a, s) => a + (s.names || 0), 0);
  const held = books.reduce((a, s) => a + (s.held || 0), 0);
  const rest = rows.length - books.length;
  return `${names} names, ${held} held` + (rest ? ` · ${rest} assets` : "");
}

/** One curve for the SYSTEM, from however many deployments it has. Books are one row and
 *  come through unchanged; a rule spread over twenty names is averaged equal-weight, the
 *  same weighting `aggregate` reports its P&L on, so the line and the number beside it are
 *  the same statistic. Aligned at the TAIL — a system deployed later has a shorter record,
 *  and stretching it to the longest would invent history for it. */
export function systemCurve(rows: Sys[], field: "paper_curve" | "bench_curve"): number[] {
  const cs = rows
    .map((s) => s[field])
    .filter((c): c is number[] => Array.isArray(c) && c.length > 1);
  if (cs.length < 2) return cs[0] || [];
  const n = Math.max(...cs.map((c) => c.length));
  return Array.from({ length: n }, (_, i) => {
    let sum = 0;
    let k = 0;
    for (const c of cs) {
      const j = i - (n - c.length);
      if (j >= 0) {
        sum += c[j];
        k++;
      }
    }
    return k ? sum / k : 0;
  });
}

/** Breaks belong to ONE curve. Averaging several deployments blends their gaps together,
 *  so the marks are kept only where they can still be read literally: a single record. */
export const systemBreaks = (rows: Sys[]) =>
  rows.length === 1 ? rows[0].curve_breaks || [] : [];

/** Every deployment's published fills as one list, newest first, with the symbol carried
 *  onto each row — a book's fills already name theirs, a per-symbol deployment's do not. */
export const systemFills = (rows: Sys[]): Fill[] =>
  rows
    .flatMap((s) =>
      ((s.trades as Fill[] | undefined) || []).map((t) => ({
        ...t,
        symbol: t.symbol || (s.symbol as string),
      })),
    )
    .sort((a, b) => String(b.ts || "").localeCompare(String(a.ts || "")));

/* ------------------------------------------------- the live record, as numbers */

/* Nominal bars a year, for annualising volatility and Sharpe. Crypto prints around the
 * clock; the equity, ETF and commodity legs get a 6.5-hour session, which is why a 4h
 * "day" is two bars and not six. */
const BARS_PER_YEAR: Record<string, { crypto: number; other: number }> = {
  "1d": { crypto: 365, other: 252 },
  "4h": { crypto: 2190, other: 504 },
  "2h": { crypto: 4380, other: 819 },
  "1h": { crypto: 8760, other: 1638 },
  "15m": { crypto: 35040, other: 6552 },
  "5m": { crypto: 105120, other: 19656 },
};

const barsPerYear = (cls: string, tf: string) => {
  const e = BARS_PER_YEAR[tf];
  return e ? (cls === "crypto" ? e.crypto : e.other) : null;
};

/** Under this many bars a standard deviation is a rumour and annualising it is a lie with
 *  three decimal places. Those rows print an em-dash instead. */
export const MIN_METRIC_BARS = 20;

export interface LiveMetrics {
  total: number | null;
  bars: number;
  best: number | null;
  worst: number | null;
  maxdd: number | null;
  vol: number | null;
  sharpe: number | null;
  bpy: number | null;
  lifetime: number;
  /** THE THIRD STATE: a payload published before the desk recorded `realised` has no such
   *  key at all, where "0 closed trades" would be an assertion about it rather than the
   *  absence of one. */
  priced: boolean;
  closed: number | null;
  realised: number | null;
  capped: boolean;
  win_rate: number | null;
  profit_factor: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  turnover: number | null;
}

/* The desk's OWN arithmetic over `paper_curve` and the published fills — computed in the
 * page so it moves with the tick stream instead of freezing at build time, and deliberately
 * NOT the research definitions in `stockhunt/stats.py`. Nothing here is comparable with a
 * sheet, and the caption on the table says so. */
export function liveMetrics(
  rows: Sys[],
  curve: number[],
  cls: string,
  tf: string,
): LiveMetrics {
  const c = (curve || []).filter((v) => Number.isFinite(v));
  // `paper_curve` is cumulative P&L in PERCENT, so the equity index is 1 + pct/100 and a
  // per-bar return is the RATIO of consecutive points — not their difference.
  const eq = c.map((v) => 1 + v / 100);
  const rets = eq.slice(1).map((v, i) => (eq[i] ? v / eq[i] - 1 : 0));
  const n = rets.length;
  const mean = n ? rets.reduce((a, b) => a + b, 0) / n : null;
  const sd =
    n > 1 && mean != null
      ? Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1))
      : null;
  const bpy = barsPerYear(cls, tf);
  const enough = n >= MIN_METRIC_BARS && sd != null && sd > 0 && !!bpy;

  let peak = -Infinity;
  let dd = 0;
  c.forEach((v) => {
    peak = Math.max(peak, v);
    dd = Math.min(dd, v - peak);
  });

  /* A closed trade is one the desk says closed something — `realised` is null on a fill
   * that opened or added, and a float (possibly 0.00, on a scratch) when it closed.
   *
   * This used to filter on `pnl !== 0`, and `pnl` is the whole BOOK's mark at the fill,
   * not the trade's result. So an opening buy counted as a closed trade whenever some
   * unrelated name in the book had moved since its last mark, and two names filling in the
   * same second contributed the same number twice. On the IBS equity book that turned
   * eight round trips — every one a winner, +$188.43 together — into "23 closed trades,
   * 13% win rate, profit factor 0.04". NEVER reintroduce it: the null is the signal, and
   * zero is a real answer. */
  const fills = systemFills(rows);
  const priced = fills.some((t) => "realised" in t);
  const closed = priced ? fills.filter((t) => t.realised != null) : [];
  const wins = closed.filter((t) => (t.realised as number) > 0);
  const losses = closed.filter((t) => (t.realised as number) < 0);
  const sum = (l: Fill[]) => l.reduce((a, t) => a + (t.realised as number), 0);
  const grossLoss = Math.abs(sum(losses));
  const lifetime = rows.reduce((a, s) => a + fillsOf(s), 0);

  return {
    total: c.length ? c[c.length - 1] : null,
    bars: c.length,
    best: n ? Math.max(...rets) * 100 : null,
    worst: n ? Math.min(...rets) * 100 : null,
    maxdd: c.length ? dd : null,
    vol: enough ? (sd as number) * Math.sqrt(bpy as number) * 100 : null,
    sharpe: enough ? ((mean as number) / (sd as number)) * Math.sqrt(bpy as number) : null,
    bpy,
    lifetime,
    priced,
    closed: priced ? closed.length : null,
    realised: closed.length ? sum(closed) : null,
    capped: fills.length < lifetime,
    win_rate: closed.length ? (wins.length / closed.length) * 100 : null,
    profit_factor: grossLoss ? sum(wins) / grossLoss : null,
    avg_win: wins.length ? sum(wins) / wins.length : null,
    avg_loss: losses.length ? sum(losses) / losses.length : null,
    turnover: turnoverOf(rows),
  };
}

/** [label, printed value, what it means]. Flat rather than driven by a key list, because
 *  half of these are counts and dollars that need their own formatter and a shared one
 *  would be a switch statement pretending to be a table. */
export function liveMetricRows(m: LiveMetrics): [string, string, string][] {
  const dash = "—";
  // Not an error and not a gap in the record: these fills predate the desk recording what
  // each one closed, so the closed-trade statistics cannot be derived from them yet. The
  // underlying fills are complete either way, which is what the sentence has to say.
  const stale =
    "not available for this period — these fills were recorded before the " +
    "desk began tracking what each one closed. The fill record itself is complete.";
  const short =
    `not yet — needs ${MIN_METRIC_BARS} bars of record, there ` +
    `${m.bars === 1 ? "is" : "are"} ${m.bars}`;
  return [
    [
      "Total P&L",
      fmtPct(m.total),
      "Cumulative percent since this system's first fill, chained across restarts.",
    ],
    [
      "Max drawdown",
      m.maxdd == null ? dash : fmtPct(m.maxdd, 2),
      "Worst fall from a high-water mark of the live record. Percentage points of P&L, not of equity.",
    ],
    [
      "Volatility",
      m.vol == null ? dash : fmtNum(m.vol, 1) + "%",
      m.vol == null
        ? short
        : `Annualised standard deviation of the bar-to-bar record, on ${m.bpy} bars a year.`,
    ],
    [
      "Sharpe",
      m.sharpe == null ? dash : fmtNum(m.sharpe, 2),
      m.sharpe == null
        ? short
        : "Mean bar return over its standard deviation, annualised, idle cash at 0%. Months of record before this means anything.",
    ],
    ["Best bar", m.best == null ? dash : fmtPct(m.best), "Largest single-bar gain on the record."],
    [
      "Worst bar",
      m.worst == null ? dash : fmtPct(m.worst),
      "Largest single-bar loss on the record.",
    ],
    [
      "Fills",
      m.lifetime.toLocaleString(),
      "Every order that filled, lifetime — the count in the database, not this session's.",
    ],
    [
      "Closed trades",
      m.priced ? (m.closed as number).toLocaleString() : dash,
      m.priced
        ? "Fills that closed part of a position. An opening or adding fill is not one, because it closed nothing."
        : stale,
    ],
    [
      "Realised P&L",
      m.priced ? cash(m.realised) : dash,
      m.priced
        ? "Cash actually booked by the closed trades above, against what the closed part cost. Open positions are not in it — those are in Total P&L."
        : stale,
    ],
    [
      "Win rate",
      m.win_rate == null ? dash : fmtNum(m.win_rate, 1) + "%",
      m.priced
        ? "Share of closed trades that realised a gain. A low rate is fine if the wins are large."
        : stale,
    ],
    [
      "Profit factor",
      m.profit_factor == null ? dash : fmtNum(m.profit_factor, 2),
      !m.priced
        ? stale
        : m.closed && m.profit_factor == null
          ? "No closed trade has lost yet, so there is nothing to divide by."
          : "Gross winnings ÷ gross losses. Above 1 means the wins outweigh the losses.",
    ],
    [
      "Average win",
      m.priced ? cash(m.avg_win) : dash,
      m.priced ? "Mean realised P&L of a winning trade." : stale,
    ],
    [
      "Average loss",
      m.priced ? cash(m.avg_loss) : dash,
      m.priced ? "Mean realised P&L of a losing trade." : stale,
    ],
    [
      "Turnover / yr",
      m.turnover == null ? dash : fmtNum(m.turnover, 1),
      "Round trips per name per year — the unit the walk-forward sheets report, so the two compare.",
    ],
    [
      "Bars recorded",
      m.bars.toLocaleString(),
      "Closed bars behind every figure above. This is the number that says how much to trust them.",
    ],
  ];
}

/* ------------------------------------------------------------------ the CSV export */

const csvCell = (v: unknown) => {
  const s = v == null ? "" : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

/** Both P&L columns, under the names they actually mean. The export used to head the book
 *  snapshot `realised_pnl`, so a spreadsheet built off it inherited the same mistake the
 *  page was making — and kept it after the page was fixed. */
export function fillsCsv(rows: Sys[]) {
  const head = ["time", "symbol", "side", "qty", "price", "realised_pnl", "book_pnl"];
  const body = systemFills(rows).map((t) =>
    [t.ts, t.symbol, t.side, t.qty, t.price, t.realised == null ? "" : t.realised, t.pnl]
      .map(csvCell)
      .join(","),
  );
  return [head.join(","), ...body].join("\n") + "\n";
}

/** A Blob and a synthetic click — the board is static files behind a login and has no
 *  endpoint to ask for a file, and it does not need one: everything in the table is
 *  already in the page. */
export function downloadFills(rows: Sys[], cls: string, tf: string, rule: string) {
  const blob = new Blob([fillsCsv(rows)], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `stockhunt-${slug(cls)}-${slug(tf)}-${slug(rule)}-fills.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

/* ------------------------------------------------------------ the universes */

/** The universes a system can be deployed across, with the note that says what each one is
 *  worth as evidence — "the universe the rule was ranked on" against "a transfer onto
 *  instruments the research never held", which is exactly the question somebody opening a
 *  system's holdings has. Rendered on a system's page and nowhere else. */
export const paperGroupList = (meta: BoardMeta | null): PaperGroup[] =>
  meta?.paper_groups && meta.paper_groups.length
    ? meta.paper_groups
    : [
        { key: "crypto", label: "Crypto" },
        { key: "megacap", label: "Equities" },
        { key: "etf", label: "ETFs" },
      ];

/* ============================== THE STORE ==============================
 *
 * One document, one poller, one socket, outside React. The vanilla keeps the same three
 * things on the module scope of `app.js` for the same reason: a socket re-opened on every
 * navigation would reconnect the desk several times a minute.
 */

export interface LiveState {
  doc: Live | null;
  meta: BoardMeta | null;
  /** True when the document on screen is the LAST BUILD'S SNAPSHOT, not the desk's own. */
  fallback: boolean;
  ready: boolean;
  /** Bumped on every accepted update. The identity of this object is what React watches. */
  rev: number;
}

const EMPTY: LiveState = { doc: null, meta: null, fallback: false, ready: false, rev: 0 };

let state: LiveState = EMPTY;
const listeners = new Set<() => void>();
let started = false;

function publish(next: Partial<LiveState>) {
  state = { ...state, ...next, rev: state.rev + 1 };
  listeners.forEach((l) => l());
}

/* `group` is assigned at BUILD time from the universe lists and the node knows nothing
 * about it, so a live refresh has to put it back or the per-universe sections on a
 * system's page lose their headings.
 *
 * A book holds a whole class and has no symbol to file under, so it takes `book` directly.
 * Anything else needs the baked snapshot's symbol->group map — which is why that snapshot
 * is fetched lazily, below, only when a non-book system is actually on the desk: it is
 * 241 kB, and on this desk almost everything is a book. */
const groupBySymbol: Record<string, string> = {};

function assignGroups(rows: Sys[], meta: BoardMeta | null) {
  rows.forEach((s) => {
    if (s.group) return;
    /* Falls back to the system's own class, which IS a group key for three of the five
     * legs. Before, everything that was not crypto became "etf", so a live-refreshed
     * commodity system moved itself into the SPY/SOXL/TQQQ group between the page load
     * and the first tick. */
    s.group =
      s.kind === "book"
        ? "book"
        : groupBySymbol[s.symbol as string] ||
          ((meta?.paper_groups || []).some((g) => g.key === s.cls) ? (s.cls as string) : "etf");
  });
}

/** The baked snapshot, wanted for two different reasons: as the fallback document when the
 *  desk has not published, and as the symbol->group map above. Fetched at most once. */
let bakedTried = false;
async function loadBaked(): Promise<Live | null> {
  if (bakedTried) return null;
  bakedTried = true;
  try {
    const snap = await board.systems();
    (snap.strategies as Sys[] | undefined)?.forEach((s) => {
      if (s.group && s.symbol) groupBySymbol[s.symbol] = s.group;
    });
    return snap;
  } catch {
    return null;
  }
}

function acceptDoc(doc: Live, fallback: boolean) {
  const rows = (doc.strategies || []) as Sys[];
  assignGroups(rows, state.meta);
  publish({ doc, fallback, ready: true });
  // Only now is it known whether a symbol->group map is needed at all. When it lands the
  // grouping is redone in place, which is why this cannot simply run at start-up.
  if (!fallback && rows.some((s) => s.kind !== "book" && !s.group)) {
    loadBaked().then((snap) => {
      if (!snap) return;
      assignGroups((state.doc?.strategies || []) as Sys[], state.meta);
      publish({});
    });
  }
}

const LIVE_EVERY_MS = 20000;
let liveTimer: ReturnType<typeof setInterval> | null = null;
let liveFailures = 0;

async function pollLive() {
  try {
    acceptDoc(await board.live(), false);
    liveFailures = 0;
  } catch {
    // A missing live.json just means the node is not running; the page keeps whatever
    // numbers it has and stops asking rather than failing every 20 seconds forever.
    if (++liveFailures >= 3 && liveTimer) {
      clearInterval(liveTimer);
      liveTimer = null;
    }
    if (!state.doc) {
      const snap = await loadBaked();
      if (snap) acceptDoc(snap, true);
      else publish({ ready: true });
    }
  }
}

/* ---------- tick stream ----------
 * The node holds a WebSocket open to the vendor and pushes a compact delta whenever an
 * instrument prints. Polling every 20s still runs underneath as the safety net — it is
 * what recovers the page if the socket stalls or the node restarts — but when the stream
 * is up the numbers move on the tick rather than on a timer.
 *
 * The delta carries only what changes (id, P&L, equity, mark, units, state, fill count).
 * The full document is ~300 kB and pushing it twice a second would make the browser
 * re-parse everything to move a few figures.
 */
interface TickRow {
  id: string;
  pnl?: number;
  eq?: number;
  px?: number;
  u?: number;
  st?: string;
  n?: number;
}

let ws: WebSocket | null = null;
let wsRetry = 1;

function applyTicks(msg: { rows: TickRow[] }) {
  const rows = (state.doc?.strategies || []) as Sys[];
  const byId: Record<string, Sys> = {};
  rows.forEach((s) => {
    byId[s.id] = s;
  });
  msg.rows.forEach((r) => {
    const s = byId[r.id];
    if (!s) return;
    if (r.pnl != null) s.paper_pnl_pct = r.pnl;
    if (r.eq != null) s.equity = r.eq;
    if (r.px != null) s.mark_price = r.px;
    if (r.u != null) {
      s.units = r.u;
      s.position_units = r.u;
    }
    if (r.st) s.state = r.st;
    if (r.n != null) s.paper_trades = r.n;
  });
  // Mutated in place, exactly as the vanilla does — the rows are the same objects, so only
  // the wrapper's identity has to change for React to see the tick.
  publish({});
}

function connectTicks() {
  try {
    ws = new WebSocket(liveSocketUrl());
  } catch {
    return;
  }
  ws.onopen = () => {
    wsRetry = 1;
  };
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      // Two shapes reach this socket. The node's local stream sends a compact delta with
      // `rows`; the shared server pushes the whole `live.json`, which is what a remote
      // viewer gets. Both end in the same repaint.
      if (Array.isArray(msg.rows)) applyTicks(msg);
      else if (Array.isArray(msg.strategies)) acceptDoc(msg as Live, false);
    } catch {
      /* a malformed frame is not worth taking the stream down for */
    }
  };
  ws.onclose = () => {
    ws = null;
    // Exponential backoff, capped. A closed socket is normal — the node restarts, the
    // laptop sleeps — and the poller keeps the page correct in the meantime.
    setTimeout(connectTicks, Math.min((wsRetry *= 2), 30) * 1000);
  };
  ws.onerror = () => {
    if (ws) ws.close();
  };
}

function start() {
  if (started || typeof window === "undefined") return;
  started = true;
  board
    .meta()
    .then((m) => {
      publish({ meta: m });
      // The document may already have landed, in which case its rows were grouped against
      // an empty `paper_groups` list and have to be re-read now that it exists.
      assignGroups((state.doc?.strategies || []) as Sys[], m);
      publish({});
    })
    .catch(() => publish({ meta: null }));
  pollLive();
  liveTimer = setInterval(pollLive, LIVE_EVERY_MS);
  connectTicks();
}

const subscribe = (l: () => void) => {
  start();
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
};

/** The desk's document, kept current by the poller and the socket. Never stopped once
 *  started: the stream has to survive a route change, and a page that hangs up on
 *  navigation would reconnect the desk several times a minute. */
export function useLive(): LiveState {
  return useSyncExternalStore(
    subscribe,
    () => state,
    () => EMPTY,
  );
}

export const strategiesOf = (doc: Live | null) => (doc?.strategies || []) as Sys[];

/* ---------------------------------------------------- simulated P&L history */

/* Fetched once and cached. The desk is days old, so its own P&L cannot fill a YTD or
 * 3-month chart; these curves are the same rule run over the same instrument's recent
 * history by `paper_curves.py`. That is a BACKTEST OF A LIVE SYSTEM, and every chart says
 * so — it answers "how would this have done this year", not "how has it done since we
 * started it". */
export interface PcWindow {
  curve: number[];
  bench?: number[];
  dates?: string[];
  pnl_pct: number;
  bench_pnl_pct?: number;
}
export interface PcEntry {
  system?: Record<string, PcWindow>;
  assets?: Record<string, Record<string, PcWindow>>;
}
export type PaperCurves = Record<string, PcEntry>;

/** Both windows are shown side by side rather than behind a toggle: the comparison people
 *  make is "lately versus this year", and a control that hides one of them turns a glance
 *  into two clicks and a memory test. */
export const PC_WINDOWS: [string, string][] = [
  ["3m", "3 months"],
  ["ytd", "Year to date"],
];

let pcurves: PaperCurves | null = null;
let pcurvesTried = false;
const pcListeners = new Set<() => void>();

function loadPaperCurves() {
  if (pcurvesTried) return;
  pcurvesTried = true;
  board
    .paperCurves()
    .then((c) => {
      pcurves = c as PaperCurves;
      pcListeners.forEach((l) => l());
    })
    .catch(() => {
      pcurves = null;
    });
}

/** The simulated windows. A deep link into a system's page never draws the list first, so
 *  every view that wants them asks for them; the fetch happens once. */
export function usePaperCurves(): PaperCurves | null {
  const [, bump] = useState(0);
  useEffect(() => {
    const l = () => bump((n) => n + 1);
    pcListeners.add(l);
    loadPaperCurves();
    return () => {
      pcListeners.delete(l);
    };
  }, []);
  return pcurves;
}

/* ------------------------------------------------ the pointer to the backtest */

/* CHECKED, not assumed. The desk runs promotions whose leaderboard row was cut, and a link
 * that bounces the reader back to the leaderboard is worse than a sentence saying the page
 * is not there.
 *
 * Two differences from the vanilla `backtestHref`. It needs no `CLASS_ARG` map, because
 * `/v1/research` is keyed on the asset class itself rather than on `dash_config.GROUPS`'
 * abbreviations. And it cannot check "is this rule among the sheet's shipped rows" —
 * paging removed the TOP_N cut, which is the whole reason this app exists — so it asks the
 * real question instead: will the rule page have anything to draw? That page renders on
 * EITHER the per-asset rows or the curve, and fails only when both are absent.
 *
 * The per-asset request goes first because it is the small one. A pair records leg
 * diagnostics instead of per-symbol rows, so it 404s there and costs a second request; the
 * alternative is downloading a full equity series to decide whether to print a sentence.
 */
export function useBacktestHref(cls: string, tf: string, rule: string) {
  const [href, setHref] = useState<string | null>(null);
  useEffect(() => {
    if (!cls || !tf || !rule) return;
    let live = true;
    const link = `/rule/?cls=${encodeURIComponent(cls)}&tf=${encodeURIComponent(tf)}&rule=${encodeURIComponent(rule)}`;
    (async () => {
      const ok = await fetch(
        `/v1/research/rule/${cls}/${tf}/${encodeURIComponent(rule)}`,
        { credentials: "same-origin", cache: "no-store" },
      )
        .then((r) => r.ok)
        .catch(() => false);
      if (ok) {
        if (live) setHref(link);
        return;
      }
      const curved = await fetch(
        `/v1/research/curve/${cls}/${tf}/${encodeURIComponent(rule)}`,
        { credentials: "same-origin", cache: "no-store" },
      )
        .then((r) => r.ok)
        .catch(() => false);
      if (live) setHref(curved ? link : null);
    })();
    return () => {
      live = false;
    };
  }, [cls, tf, rule]);
  return href;
}

/* ------------------------------------------------------- keeping the reader's place */

/* A tick lands twice a second. The vanilla rewrites `#sys-body` whole and therefore has to
 * capture and put back the horizontal AND vertical scroll of every `.tbl-wrap`, plus
 * `scrollY`: the holdings table is nine columns and would otherwise snap to column one on
 * every tick, and a reader three hundred fills down the trade history would be thrown back
 * to the newest one.
 *
 * React does not rewrite the DOM, so those offsets survive on their own — but only while
 * the nodes do. A row count that changes, a branch that swaps a table for a paragraph, or
 * a key that moves will remount the container and take its scroll with it, and that is
 * exactly what a tick can cause. So the offsets are remembered as the reader sets them and
 * put back after every commit, which is a no-op in the common case and the whole point in
 * the uncommon one.
 */
export function useKeepScroll(host: RefObject<HTMLElement | null>) {
  const at = useRef(new Map<string, [number, number]>());

  const key = useCallback((el: Element, i: number) => `${i}:${el.className}`, []);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    // `scroll` does not bubble, so it is listened for in the CAPTURE phase on the host
    // rather than bound to each box — the boxes come and go, the host does not.
    const onScroll = (e: Event) => {
      const t = e.target as HTMLElement;
      if (!t.classList || !t.classList.contains("tbl-wrap")) return;
      const boxes = [...el.querySelectorAll<HTMLElement>(".tbl-wrap")];
      const i = boxes.indexOf(t);
      if (i >= 0) at.current.set(key(t, i), [t.scrollLeft, t.scrollTop]);
    };
    el.addEventListener("scroll", onScroll, { capture: true, passive: true });
    return () => el.removeEventListener("scroll", onScroll, { capture: true });
  }, [host, key]);

  // After the commit, never during it: reading layout in render is what makes a repaint
  // visible as a jump.
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    [...el.querySelectorAll<HTMLElement>(".tbl-wrap")].forEach((w, i) => {
      const seen = at.current.get(key(w, i));
      if (!seen) return;
      if (seen[0] && w.scrollLeft !== seen[0]) w.scrollLeft = seen[0];
      if (seen[1] && w.scrollTop !== seen[1]) w.scrollTop = seen[1];
    });
  });
}
