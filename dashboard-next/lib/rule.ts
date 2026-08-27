/* Everything the STRATEGY DETAIL PAGE needs and the leaderboard did not.
 *
 * `lib/api.ts` is the one door to the API and is shared with the board, so nothing
 * view-specific belongs in it. What is here is of three kinds:
 *
 *   - the formatters `web/app.js` uses, ported verbatim in behaviour. They are not
 *     cosmetic: the typographic minus, the money abbreviation and the unsigned CAGR each
 *     exist because the ASCII/percent version of them was misread on this page once.
 *   - the SHAPES the API returns that `lib/api.ts` does not spell out. `/v1/research/curve`
 *     hands back the whole `book_curves_*.json` entry — `matched`, `indexes`, `pit`,
 *     `side` — and `robust.json` carries a second per-rule map (`open`) beside `rules`.
 *     Declared here rather than edited into `api.ts`, which the board also imports.
 *   - the two vocabularies the vanilla board keeps: the metric rows of the performance
 *     table, and the robustness matrix's metric axis.
 */

import { board, type AssetRow, type BoardMeta, type BookBench, type BookRec, type Curve,
         type CurveIndex, type Gate, type Matched, type MatchedLine, type Robust,
         type StandardRec } from "@/lib/api";

/* ------------------------------------------------------------------ formatting
 *
 * `−` rather than `-` throughout. `toFixed` emits an ASCII hyphen, which sits at a
 * different height and width from the typographic minus, and a column mixing the two
 * reads as misaligned. */

/* ONE declaration per payload, and it lives in `api.ts`.
 *
 * These were declared here first, because `api.ts` did not yet describe what
 * `/v1/research/curve` actually returns. It does now, so restating them would be two
 * answers to the shape of one document — the drift this repo removes wherever it finds it.
 * The aliases keep the names this module's own code reads by.
 */
export type { BookBench, CurveIndex, Matched, MatchedLine, StandardRec };
export type CurveDetail = Curve;
/* `GateDef` read either shape while `api.ts` described `edge_criteria` wrongly. It does not
 * any more -- `Gate` is `{k, name, target, ask}`, which is what the document ships -- so the
 * tolerant reader collapses to the real type rather than staying a second opinion about one
 * payload. Same for the book: `BookRec` now carries every field the hero strip reads. */
export type GateDef = Gate;
export type BookFull = BookRec;
export type RobustIndex = Robust;

export const DASH = "—";

export const fmtNum = (v: number | null | undefined, d = 1) =>
  v == null || Number.isNaN(v) ? DASH : Number(v).toFixed(d);

export const fmtPct = (v: number | null | undefined, d = 2) =>
  v == null || Number.isNaN(v) ? DASH : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d) + "%";

export const fmtIR = (v: number | null | undefined) =>
  v == null || Number.isNaN(v) ? DASH : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(3);

/** Annualised growth, printed UNSIGNED: 17.5% reads as a rate, +17.5% reads as a gain. */
export const fmtCagr = (v: number | null | undefined, d = 1) =>
  v == null || Number.isNaN(v) ? DASH : v.toFixed(d) + "%";

/** A CAGR *difference*, always signed, so the two comparison columns line up. */
export const fmtCagrDelta = (v: number | null | undefined, d = 1) =>
  v == null || Number.isNaN(v) ? DASH : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d) + "%";

/* A percentage return over 23 years is unreadable (+74,735%) and a percentage-point gap
 * against the benchmark is worse (−89,644 points, which is not a quantity that means
 * anything). The same result as money — $10k became $7.5M against $16.4M holding — is
 * immediately legible. */
export const STAKE = 10000;
export const grew = (pct: number | null | undefined) =>
  pct == null || Number.isNaN(pct) ? null : STAKE * (1 + pct / 100);

export const fmtMoney = (v: number | null | undefined) => {
  if (v == null || Number.isNaN(v)) return DASH;
  const a = Math.abs(v);
  if (a >= 1e9) return "$" + (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return "$" + (v / 1e3).toFixed(0) + "k";
  return "$" + v.toFixed(0);
};

/* The money gap against the benchmark, as a DIFFERENCE and never as a ratio. A ratio to a
 * negative base carries no meaning and flips its own sign: AVAX/USD buy-and-hold turned
 * $10k into $1k, so a rule that made $14k and scores IR +0.354 rendered as "−0.47x" in red
 * beside a verdict of "beat". The difference is signed correctly whether the benchmark rose
 * or fell. */
export const pnlDelta = (net: number | null | undefined, bh: number | null | undefined) =>
  net == null || bh == null ? null : (grew(net) as number) - (grew(bh) as number);

export const fmtDelta = (v: number | null | undefined) =>
  v == null || Number.isNaN(v) ? DASH : (v >= 0 ? "+" : "−") + fmtMoney(Math.abs(v));

/** Colour means exactly one thing on this site: gained or lost. Nothing else gets it. */
export const sign = (v: number | null | undefined) =>
  v == null || Number.isNaN(v) ? "flat" : v > 0 ? "gain" : v < 0 ? "loss" : "flat";

/** A fraction as a whole percent, the leaderboard's `Long %` convention. */
export const pctOr = (v: number | null | undefined) =>
  v == null || Number.isNaN(v) ? DASH : (v * 100).toFixed(0) + "%";

/* A pair is two rules joined by an operator and carries that operator inside its own
 * name — `HT_TRENDMODE~MAXINDEX|or`. The page prints the stem and shows the operator as a
 * chip, which is also what marks the row as a pair. */
export const stemName = (r: string) => String(r).split("|")[0];
export const opOf = (r: string) => {
  const i = String(r).indexOf("|");
  return i < 0 ? "" : String(r).slice(i + 1);
};
export const legsOf = (r: string) =>
  String(r).split("~").map((s) => s.split("|")[0].trim());

/* IS THIS A PAIR? Derived from the label rather than carried in the URL, and that is safe
 * because `~` is the pair grammar and nothing else: `combo_wf.py` writes `A~B|op` and no
 * single rule, overlay (`ha:chart:ibs@buy=0.3`) or published strategy name contains it.
 * The alternative — passing `kind` as a query parameter — would make a hand-typed or
 * pasted URL able to lie about what the page is showing. */
export const isPairLabel = (r: string) => String(r).includes("~");

/* ------------------------------------------------- the two names for one asset class
 *
 * `dash_config.GROUPS` keys the board by GROUP (`stocks`, `etf`, `futures`) while the
 * stages and every API route take the CLASS ARG (`us_stocks`, `us_etfs`, `cme_futures`).
 * `robust.json`'s envs carry the group key; `/v1/research/*` wants the class arg. Both
 * directions are needed, so both are here — and adding a class means adding it to both,
 * exactly as `CLASS_LABEL`/`CLASS_ARG` in `web/app.js` do. */
export const CLASS_ARG: Record<string, string> = {
  stocks: "us_stocks", crypto: "crypto", etf: "us_etfs",
  commodities: "commodities", futures: "cme_futures",
};
export const GROUP_KEY: Record<string, string> = Object.fromEntries(
  Object.entries(CLASS_ARG).map(([g, c]) => [c, g]),
);
export const CLASS_LABEL: Record<string, string> = {
  stocks: "Top 100 US Stocks", crypto: "Crypto", etf: "ETFs",
  commodities: "Commodities", futures: "CME Futures",
  us_stocks: "Top 100 US Stocks", us_etfs: "ETFs", cme_futures: "CME Futures",
};

/** The detail page's own URL. A QUERY STRING, so `A~B|op` and `ha:chart:ibs@buy=0.3`
 *  survive the round trip; see the comment at the top of `app/rule/page.tsx`. */
export const ruleHref = (cls: string, tf: string, rule: string) =>
  `/rule/?cls=${encodeURIComponent(cls)}&tf=${encodeURIComponent(tf)}` +
  `&rule=${encodeURIComponent(rule)}`;

/* ------------------------------------------------------------- what a curve carries
 *
 * `/v1/research/curve` returns `{cls, tf, rule} | <the whole book_curves entry>`. The half
 * beyond the raw series is the RISK-MATCHED comparison, and it is the half this page is
 * built on. `lib/api.ts` declares all of it; what lives here is the reading of it. */

/** The comparison instruments stored with a curve, whatever shape the file is in.
 *  Absent means no index is cached for the class, which is a real state — crypto and
 *  commodities hit it. */
export const curveIndexes = (c: CurveDetail | null): CurveIndex[] =>
  Array.isArray(c?.indexes) ? c.indexes
    : c?.index ? [{ symbol: c.index_symbol, curve: c.index, metrics: c.index_metrics }]
    : [];

/** Two is what the chart has distinct strokes for, and three lines is already the most a
 *  reader can follow on a log axis at this width. Extras are dropped from the CHART only —
 *  the metrics table below still carries every one of them as a column. */
export const CHART_INDEXES = 2;

/** Which matched lines are DRAWN and which are merely TABULATED.
 *
 *  The chart takes the index lines only. The table has no such limit, so it gets every
 *  matched line including the sheet's own basket — which is what the leaderboard's verdict
 *  is scored against and therefore has to stay a column. */
export function splitMatched(c: CurveDetail | null) {
  const mm = c?.matched ?? {};
  const byLabel: Record<string, MatchedLine> = {};
  for (const l of mm.lines ?? []) if (l?.curve?.length) byLabel[l.label] = l;
  const drawn = curveIndexes(c)
    .filter((i) => i?.curve?.length)
    .slice(0, CHART_INDEXES)
    .map((i) => byLabel[i.symbol ?? ""])
    .filter(Boolean);
  const all = (mm.lines ?? []).filter((l) => l && l.metrics);
  return { mm, drawn, all };
}

/* ------------------------------------------------------- the performance metrics table
 *
 * `[key, name, what it means, decimal places, suffix]`, in the order `web/app.js` prints
 * them: the risk-adjusted figures first, the trade-level ones after, and time in market
 * last because it is what the reader is told to read BEFORE any return figure. */
export const METRIC_ROWS: [string, string, string, number, string?][] = [
  ["sharpe", "Sharpe", "Return per unit of total volatility. Above 1 is good; it says nothing about beating the benchmark.", 2],
  ["sortino", "Sortino", "Sharpe counting only downside volatility, so upside swings are not penalised.", 2],
  ["calmar", "Calmar", "Annual return divided by the worst peak-to-trough fall. How much pain each unit of return cost.", 2],
  ["max_dd_pct", "Max drawdown", "Worst fall from a high-water mark. The number that decides whether a strategy is actually holdable.", 1, "%"],
  ["vol_pct", "Volatility", "Annualised standard deviation of returns.", 1, "%"],
  ["cagr_pct", "CAGR", "Compounded annual growth.", 2, "%"],
  ["profit_factor", "Profit factor", "Gross winnings ÷ gross losses across trades. Above 1 means the wins outweigh the losses.", 2],
  ["win_rate_pct", "Win rate", "Share of trades that closed positive. A low win rate is fine if the wins are large.", 1, "%"],
  ["trades", "Trades", "A trade is one held position, entry to exit.", 0],
  ["avg_win_pct", "Average win", "Mean return of a winning trade.", 2, "%"],
  ["avg_loss_pct", "Average loss", "Mean return of a losing trade.", 2, "%"],
  ["exposure_pct", "Time in market", "Share of bars holding any position. Read this before any return figure.", 1, "%"],
];

export const mval = (
  m: Record<string, number | null> | null | undefined,
  key: string, dp: number, suffix?: string,
) => {
  const v = m?.[key];
  return v == null ? DASH : Number(v).toFixed(dp) + (suffix ?? "");
};

/* ------------------------------------------------------------------- the robustness index
 *
 * `robust.json` carries a SECOND per-rule map beside `rules`: the same ~400 rules scored at
 * the next bar's open. `lib/api.ts` declares `rules` alone, so the fill axis is added here.
 * The per-cell arrays are POSITIONAL and `fields` names the axis — the two must move
 * together, which is why every read goes through `fields.indexOf`. */
/** The metric axis of the matrix. Keys are `fields` entries; the label and formatter are
 *  the vanilla board's, so a cell prints the same string in both. */
export const ROB_METRICS: Record<string, [string, (v: number | null | undefined) => string]> = {
  sharpe: ["Sharpe", (v) => fmtNum(v, 2)],
  cm_excess_cagr: ["book vs B&H", (v) => (v == null ? DASH : fmtPct(v * 100, 2))],
  cagr: ["ROI/yr", (v) => (v == null ? DASH : fmtCagr(v * 100))],
  dd: ["Max DD", (v) => (v == null ? DASH : fmtNum(v, 1) + "%")],
  exposure: ["Long %", (v) => pctOr(v)],
  n_trades: ["Trades", (v) => (v == null ? DASH : String(v))],
  profit_factor: ["Profit factor", (v) => fmtNum(v, 2)],
  win_rate: ["Win %", (v) => (v == null ? DASH : fmtNum(v * 100, 1) + "%")],
};

/* Fetched ONCE per tab and kept, which is the port of `ensureRobust()`. It is ~830 kB and
 * every strategy page draws from it, so re-downloading it per rule would make walking the
 * matrix — the one thing the section is for — the most expensive thing on the board. */
let robustPending: Promise<RobustIndex | null> | null = null;
export function ensureRobust(): Promise<RobustIndex | null> {
  if (!robustPending) {
    robustPending = board
      .robust()
      .then((j) => j as RobustIndex)
      // Swallowed to null rather than thrown: the section says "could not be loaded",
      // and nothing else on the detail page waits on this fetch.
      .catch(() => null);
  }
  return robustPending;
}

/* The board document's small half, fetched ONCE per tab for the same reason. Two views on
 * this page need it — the robustness matrix wants the timeframe axis and the hero strip
 * wants the six gate definitions — and neither should pay for the other's fetch. */
let metaPending: Promise<BoardMeta | null> | null = null;
export function ensureMeta(): Promise<BoardMeta | null> {
  if (!metaPending) metaPending = board.meta().catch(() => null);
  return metaPending;
}

/* -------------------------------------------------------------- the six-criteria verdict
 *
 * WHAT `edge_criteria` ACTUALLY SHIPS. The board document carries `{k, name, target, ask}`
 * per gate, which is not what `Gate` in `api.ts` names (`{key, letter, label, target}`).
 * Until those two agree, this reads either — the alternates are optional and the fallback
 * chain picks whichever arrived. It is a tolerant read of ONE payload, not a second
 * declaration of it, and it should collapse to `Gate` the moment `api.ts` is corrected. */
/** `n/6`, or an em-dash. A `null` record is NOT `0/6`: the first says the standard has no
 *  answer here, the second says it was measured and failed. */
export const edgeCountText = (e: StandardRec | null | undefined) =>
  e == null ? DASH : `${e.passed ?? DASH}/${e.n ?? DASH}`;

/* THE TOOLTIP THAT MAKES THE COUNT READABLE, and most of it is one sentence about `T`.
 *
 * The T criterion's PRINTED target is ">= 2.0", which is the bar for a single pre-specified
 * test and not for this one: searching ~400 candidates raises it. A reader seeing T failed
 * beside "target >= 2.0" and a t of 2.81 is owed the number it actually had to clear and
 * where that number came from — and that bar is MEASURED, by sign-flip permutation of the
 * panel's own per-fold edges, rather than assumed by Bonferroni.
 *
 * `underpowered` leads the tooltip, because it means CANNOT TELL and not "no". */
export function edgeTitle(e: StandardRec | null | undefined, criteria: GateDef[]): string {
  if (e == null) return "the standard has no verdict for this rule on this sheet";
  const named = (criteria ?? [])
    // POSITIONAL against `config.EDGE_STANDARD` order, which is why nothing between the
    // document and here may reorder or re-letter the list: doing so would tick the wrong
    // criterion's name on every row and raise nothing.
    .map((c, i) => `${e.gates?.[i] ? "✓" : "✗"} ${c.k ?? ""}  ` +
                   `${c.target ?? ""}  ${c.name ?? ""}`)
    .join("\n");
  const bar = e.t_bar == null ? ""
    : `\n\nT is scored against ${e.t_bar.toFixed(2)}, not 2.0: `
      + `${e.n_candidates ? `${e.n_candidates} candidates were` : "the panel was"} searched`
      + (e.t_bar_source === "maxT"
          ? ", and that bar is measured by sign-flip permutation of this sheet's own "
            + "per-fold edges"
            + (e.t_bar_bonferroni
                ? ` — Bonferroni would have assumed ${e.t_bar_bonferroni.toFixed(2)}` : "")
          : "")
      + ".";
  const head = e.verdict === "underpowered"
    ? `too few folds to resolve — cannot tell, not "no"\n\n` : "";
  return head + named + bar;
}

/* ------------------------------------------------------------------- the per-asset rows
 *
 * `_rank_assets` writes `xcagr` and `xpnl` onto every row before ordering on the second.
 * Both are declared so the table can print the rate beside the money it is sorted on. */
export interface AssetRowFull extends AssetRow {
  /** Excess CAGR: the annual rate. A column, never the sort key. */
  xcagr?: number | null;
  /** Excess total return in points. THE SORT KEY, and `P&L vs B&H` is a strictly
   *  increasing function of it, so the header names a column the reader can see. */
  xpnl?: number | null;
}

/** Both fall back to recomputing the difference, for a store written before the keys. */
export const xcagrOf = (p: AssetRowFull) =>
  p.xcagr != null ? p.xcagr
    : p.net_cagr != null && p.bh_cagr != null ? p.net_cagr - p.bh_cagr : null;
export const xpnlOf = (p: AssetRowFull) =>
  p.xpnl != null ? p.xpnl
    : p.net_pct != null && p.bh_pct != null ? p.net_pct - p.bh_pct : null;

/** What `_asset_stats` and `_rank_assets` return alongside the rows. */
export interface AssetStats {
  n?: number | null;
  /** Names with a positive IR — the breadth numerator. */
  pos?: number | null;
  /** Names with an IR at all — the honest denominator. */
  scored?: number | null;
  net_pct?: number | null;
  bh_pct?: number | null;
  years?: number | null;
  /** Names with no return on one side of the comparison; they sink to the bottom. */
  unranked?: number | null;
}
