"use client";

/* PORTFOLIOS — the ledger's baskets, and the ONE adapter every portfolio component is
 * built against.
 *
 * A portfolio is a named basket of strategy legs with one pot of money ($100,000 split
 * equally, rebalanced to equal weight monthly), one combined equity curve and one on/off
 * toggle. `../paper api/api_portfolios.py` is the contract; `../stockhunt/deskdb.py`'s
 * `portfolios` and `portfolio_changes` tables are the shapes.
 *
 * ------------------------------------------------------------------ THE CHART CONTRACT
 *
 * `stockhunt/blend.py` is still being written, so its exact response is NOT known here.
 * That is handled by putting the whole guess in ONE place: `BlendResponse` below is the
 * assumed wire shape and `adaptBlend` is the ONLY function that reads it. Every component
 * — the chart, the leg table, the correlation panel, the list's sparklines — is built
 * against `Blend`, which this file defines. When the engine lands, `BlendResponse` and
 * `adaptBlend` change and nothing else does.
 *
 * What is assumed, stated so it can be checked rather than discovered:
 *
 *   dates    string[]                       one label per bar, the blend's own span
 *   curve    number[]                       the PORTFOLIO, growth of 100
 *   bench    number[]                       the BLENDED matched benchmark, growth of 100
 *   legs     [{cls, tf, rule, label?, weight?, curve?, contribution?}]
 *   corr     number[][]  or  {labels, matrix}
 *
 * Everything else the pages print is DERIVED here from those arrays — total growth, CAGR,
 * drawdown, each leg's contribution, the correlation reading. That is deliberate: a page
 * that reads `metrics.cagr_pct` off a response nobody has written yet would be wrong in a
 * way that renders, whereas a figure computed from the curve is right as soon as the curve
 * is. The captions say the figures are computed off the blended curve.
 *
 * Growth of 100 is assumed for `curve` and `bench` — the same convention
 * `/v1/research/curve` already uses and `EquityChart` already draws. Nothing here depends
 * on the starting value being exactly 100; every derived figure is a RATIO to the series'
 * own first point, so a curve indexed at 1 reads identically.
 */

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { apiGet, apiSend, ApiError } from "@/lib/api";

/* ============================================================ what the ledger holds */

/** A LEG IS AN ORDINARY BOOK REGISTRATION carrying a `portfolio_id` — the same row a
 *  promotion writes. Which is why every field here is a registration's, and why the paper
 *  desk needs no idea that portfolios exist. */
export interface PortfolioLeg {
  strategy_id?: string;
  name?: string;
  cls: string;
  tf: string;
  rule: string;
  capital?: number | null;
  /** What was ASKED for. Written by the API, cascaded from the portfolio's own `want`. */
  want?: string | null;
  /** What the DESK has done. Written by the desk alone. The two disagree while it catches
   *  up, and that disagreement is information rather than a bug to paper over. */
  state?: string | null;
  reason?: string | null;
  kind?: string | null;
}

export interface Portfolio {
  portfolio_id: string;
  account: string;
  name: string;
  /** `manual` — rules picked by hand. `follow` — tracks one sheet's top `top_n`, daily. */
  kind: string;
  source_cls?: string | null;
  source_tf?: string | null;
  top_n?: number | null;
  capital?: number | null;
  rebalance?: string | null;
  want?: string | null;
  state?: string | null;
  inception?: string | null;
  created_at?: string | null;
  /** Present on `GET /v1/portfolios/{id}`. The LISTING may or may not carry them, so
   *  everything that counts legs goes through `legCount`, which answers "cannot tell"
   *  rather than zero. */
  legs?: PortfolioLeg[];
  n_legs?: number | null;
}

/** One row of `portfolio_changes`. Append-only: a follow-portfolio's holdings move under
 *  it, so its curve is the record of several different baskets and this is the only thing
 *  that says which. */
export interface PortfolioChange {
  id?: number;
  portfolio_id?: string;
  at: string;
  /** `added` | `removed`. */
  action: string;
  strategy_id?: string | null;
  cls?: string | null;
  tf?: string | null;
  rule?: string | null;
  /** Where the rule stood on the sheet when this happened. The sheet is re-ranked nightly
   *  and cannot be asked afterwards, which is why it is written down at the time. */
  rank_at?: number | null;
  source?: string | null;
  n_legs?: number | null;
  leg_capital?: number | null;
  reason?: string | null;
}

/** `/v1/desk` — is anybody reading the ledger, and how far behind are they? Exactly the
 *  fields `DeskOut` in `../paper api/api_strategies.py` declares. */
export interface DeskPulse {
  live: boolean;
  last_pass_at?: string | null;
  seconds_ago?: number | null;
  ticks?: number;
  error?: string | null;
  pending?: number;
  stale_after?: number;
}

/** Every leg the caller wants blended. The API's `Leg`. */
export interface LegRef {
  cls: string;
  tf: string;
  rule: string;
}

/* ================================================== THE CHART CONTRACT: one adapter */

/** The blend engine's response, as `stockhunt/blend.py` writes it. NOTHING OUTSIDE
 *  `adaptBlend` MAY READ THIS TYPE — that is the whole point of it having a name, and it is
 *  what let this front end be built while the engine was still being written.
 *
 *  Every field is optional even where the engine always sends it. A response is the one
 *  thing this app cannot check at compile time, and a page that throws on a missing key is
 *  worse than one that prints an em-dash. */
export interface BlendResponse {
  capital?: number | null;
  rebalance?: string | null;
  n_legs?: number | null;
  axis?: {
    start?: string | null;
    end?: string | null;
    years?: number | null;
    bars?: number | null;
    bars_per_year?: number | null;
    median_bar_days?: number | null;
    /** Whose dates the whole portfolio inherited — the COARSEST leg's. */
    grid_from?: string | null;
    rebalances?: number | null;
  } | null;
  dates?: string[] | null;
  /** The pot's value per bar, IN DOLLARS, starting at `capital`. */
  curve?: number[] | null;
  /** The same blend of the legs' own benchmarks. Null where a leg has none. */
  bench?: number[] | null;
  legs?: BlendResponseLeg[] | null;
  corr?: { labels?: string[] | null; matrix?: number[][] | null } | null;
  metrics?: BlendMetrics | null;
  bench_metrics?: BlendMetrics | null;
  excess?: { cagr?: number | null; sharpe?: number | null; total_return?: number | null } | null;
  /** Things the blend wants said out loud — a leg smoothed onto a coarser grid, a monthly
   *  reset whose spread nobody charged. Surfaced on the page, never swallowed. */
  warnings?: string[] | null;
}

/** FRACTIONS, not percents: `cagr: 0.085` is 8.5%/yr. `max_drawdown` is negative. */
export interface BlendMetrics {
  final_value?: number | null;
  total_return?: number | null;
  cagr?: number | null;
  sharpe?: number | null;
  ann_vol?: number | null;
  max_drawdown?: number | null;
  bars?: number | null;
}

export interface BlendResponseLeg {
  cls?: string | null;
  tf?: string | null;
  rule?: string | null;
  label?: string | null;
  weight_initial?: number | null;
  pnl?: number | null;
  /** THIS LEG'S DOLLAR P&L OVER THE WHOLE POT. Signed and additive: the legs sum to the
   *  book's own `total_return` exactly. Deliberately NOT a share of the profit — dividing
   *  by a negative profit reports a leg that made money as a negative contributor. */
  contribution?: number | null;
  cagr?: number | null;
  sharpe?: number | null;
  ann_vol?: number | null;
  max_drawdown?: number | null;
  own_start?: string | null;
  own_end?: string | null;
  own_years?: number | null;
  /** Native stride over grid stride. Near 1 means this leg is smoothed across a whole
   *  interval and its share of the volatility is understated. */
  interp_ratio?: number | null;
  on_grid_frac?: number | null;
  n_assets?: number | null;
  side?: string | null;
}

/* ------------------------------------------------------- ...and what components see */

export interface BlendLeg {
  /** What the leg is called on screen. */
  label: string;
  /** The engine returns the combined curve and each leg's STATISTICS, not each leg's own
   *  series, so this is empty today. It stays in the type because every component that
   *  could draw a leg already asks for it, and the day the engine sends one they draw it
   *  with no other change. */
  curve: number[];
  cls: string;
  tf: string;
  rule: string;
  /** Share of the pot at inception. Equal across the legs by construction. */
  weight: number | null;
  /** Its dollar P&L as a fraction of the WHOLE POT — signed, and additive across the legs
   *  to the portfolio's own total return. */
  contribution: number | null;
  /** What this leg did on its own over the shared span: annualised, as a fraction. */
  cagr: number | null;
  sharpe: number | null;
  maxDD: number | null;
  pnl: number | null;
  /** Its OWN history, which the shared span may have cut into. */
  ownStart: string | null;
  ownEnd: string | null;
  ownYears: number | null;
  /** Near 1 means this leg was smoothed onto a coarser grid than its own. */
  interpRatio: number | null;
}

/** WHAT EVERY PORTFOLIO COMPONENT IS BUILT AGAINST. The five keys the brief names, plus the
 *  context the pages caption them with. */
export interface Blend {
  dates: string[];
  /** GROWTH OF 100, normalised here from the engine's dollar curve — every chart and every
   *  ratio on this site is written against that convention and `EquityChart` already draws
   *  it. The money figures come off `metrics` and `capital`, which are untouched. */
  portfolio: number[];
  bench: number[];
  legs: BlendLeg[];
  /** Pairwise correlation between the legs, in `legs` order. */
  corr: number[][];
  corrLabels: string[];
  start: string | null;
  end: string | null;
  /** The INTERSECTION of the legs' histories, never the union. */
  years: number | null;
  capital: number | null;
  rebalance: string | null;
  benchLabel: string;
  nLegs: number;
  /** Whose dates the axis is, and how coarse one bar on it is. */
  gridFrom: string | null;
  medianBarDays: number | null;
  rebalances: number | null;
  metrics: BlendMetrics;
  benchMetrics: BlendMetrics | null;
  excess: { cagr?: number | null; sharpe?: number | null; total_return?: number | null } | null;
  warnings: string[];
  /** False when the response carried no portfolio curve at all. */
  ok: boolean;
}

const nums = (v: unknown): number[] =>
  Array.isArray(v) ? v.filter((x): x is number => typeof x === "number" && Number.isFinite(x)) : [];

const strs = (v: unknown): string[] =>
  Array.isArray(v) ? v.map((x) => String(x)) : [];

const numOr = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

/** Rebase a series on its own first point, times 100. The portfolio and its benchmark start
 *  at the same pot, so rebasing each on itself is one scaling applied twice and the reading
 *  between them is untouched. */
const index100 = (curve: number[]): number[] => {
  const c = curve.filter((v) => Number.isFinite(v));
  if (c.length < 2 || !(c[0] > 0)) return c;
  return c.map((v) => (v / c[0]) * 100);
};

/** End over start. Not `last / 100`: a series indexed at 1 is the same curve. */
export function growthOf(curve: number[]): number | null {
  const c = curve.filter((v) => Number.isFinite(v) && v > 0);
  if (c.length < 2) return null;
  return c[c.length - 1] / c[0];
}

/** Annualised, as a percent, from a curve and the span it covers. A FALLBACK ONLY: the
 *  engine sends its own `cagr` and that is the one the pages print. */
export function cagrOf(curve: number[], years: number | null): number | null {
  const g = growthOf(curve);
  if (g == null || !years || years <= 0 || g <= 0) return null;
  return (Math.pow(g, 1 / years) - 1) * 100;
}

/** Worst fall from a high-water mark, as a percent of the peak. Negative. Also a fallback. */
export function maxDDOf(curve: number[]): number | null {
  const c = curve.filter((v) => Number.isFinite(v) && v > 0);
  if (c.length < 2) return null;
  let peak = c[0];
  let dd = 0;
  for (const v of c) {
    if (v > peak) peak = v;
    dd = Math.min(dd, v / peak - 1);
  }
  return dd * 100;
}

/** Years between the first and last label, when the engine did not say. */
function spanYears(dates: string[]): number | null {
  if (dates.length < 2) return null;
  const a = Date.parse(dates[0].replace(" ", "T"));
  const b = Date.parse(dates[dates.length - 1].replace(" ", "T"));
  if (!Number.isFinite(a) || !Number.isFinite(b) || b <= a) return null;
  return (b - a) / (365.2425 * 86400000);
}

/** THE ONE PLACE THE BLEND ENGINE'S RESPONSE IS READ. */
export function adaptBlend(raw: BlendResponse | null | undefined): Blend {
  const r = raw ?? {};
  const axis = r.axis ?? {};
  const dates = strs(r.dates);
  const portfolio = index100(nums(r.curve));
  const bench = index100(nums(r.bench));

  const rawLegs = Array.isArray(r.legs) ? r.legs : [];
  const n = rawLegs.length;
  const legs: BlendLeg[] = rawLegs.map((l) => {
    const cls = String(l?.cls ?? "");
    const tf = String(l?.tf ?? "");
    const rule = String(l?.rule ?? "");
    return {
      label: l?.label ? String(l.label) : legLabel({ cls, tf, rule }),
      // Statistics per leg, not series. See `BlendLeg.curve`.
      curve: [],
      cls,
      tf,
      rule,
      // EQUAL WEIGHT IS THE DEFINITION of this basket, so an absent weight is filled in
      // rather than left null — a blank there would read as "unknown share of the money"
      // when the share is the one thing about it that is fixed.
      weight: numOr(l?.weight_initial) ?? (n ? 1 / n : null),
      contribution: numOr(l?.contribution),
      cagr: numOr(l?.cagr),
      sharpe: numOr(l?.sharpe),
      maxDD: numOr(l?.max_drawdown),
      pnl: numOr(l?.pnl),
      ownStart: l?.own_start ?? null,
      ownEnd: l?.own_end ?? null,
      ownYears: numOr(l?.own_years),
      interpRatio: numOr(l?.interp_ratio),
    };
  });

  const labels = strs(r.corr?.labels);
  return {
    dates,
    portfolio,
    bench,
    legs,
    corr: Array.isArray(r.corr?.matrix) ? r.corr.matrix.map((row) => nums(row)) : [],
    // Not the engine's labels. `blend._label` names a leg `cls/tf/rule` and the
    // correlation panel truncates from the RIGHT inside a narrow column, so five legs off
    // one sheet all render as `us_stock…` and every row names nothing. See `legLabels`.
    corrLabels: legs.length ? legLabels(legs) : labels,
    start: axis.start ?? dates[0] ?? null,
    end: axis.end ?? dates[dates.length - 1] ?? null,
    years: numOr(axis.years) ?? spanYears(dates),
    capital: numOr(r.capital),
    rebalance: r.rebalance ?? null,
    // Named rather than "benchmark": a reader has to be able to tell the blended matched
    // basket from an index, and only one of those is what this line is.
    benchLabel: "the same universes, held",
    nLegs: numOr(r.n_legs) ?? legs.length,
    gridFrom: axis.grid_from ?? null,
    medianBarDays: numOr(axis.median_bar_days),
    rebalances: numOr(axis.rebalances),
    metrics: r.metrics ?? {},
    benchMetrics: r.bench_metrics ?? null,
    excess: r.excess ?? null,
    warnings: strs(r.warnings),
    ok: portfolio.length > 1,
  };
}

export const legLabel = (l: LegRef) => `${l.rule} · ${l.tf} ${l.cls}`;
/** Names for the correlation panel, built so the DISCRIMINATING PART SURVIVES TRUNCATION.
 *
 * `blend._label` puts the class and timeframe in front of the rule, which is the right
 * order for a log line and the wrong one for a 20-character column: the panel clips from
 * the right, so five legs picked off one sheet all read `us_stock…` and the row that
 * exists to name both ends of a correlated pair names neither.
 *
 * When every leg shares a sheet — which a follow-portfolio does by construction — the
 * prefix carries no information at all and is dropped. When they do not, it is the whole
 * point, so it stays; but it goes AFTER the rule, where truncation eats the context rather
 * than the identity.
 */
export function legLabels(legs: BlendLeg[]): string[] {
  const shared = new Set(legs.map((l) => `${l.cls}|${l.tf}`)).size <= 1;
  return legs.map((l) =>
    l.rule ? (shared ? l.rule : `${l.rule} · ${l.tf} ${l.cls}`) : l.label);
}

export const legKey = (l: LegRef) => `${l.cls}|${l.tf}|${l.rule}`;

/* ====================================================== the correlation reading */

export interface CorrPair {
  a: number;
  b: number;
  rho: number;
}

export interface CorrReading {
  n: number;
  /** Mean of the off-diagonal entries. */
  mean: number | null;
  min: number | null;
  max: number | null;
  /** HOW MANY BETS THIS ACTUALLY IS.
   *
   *  `n² / ΣΣρ` — the number of independent, equally-sized bets whose equal-weight blend
   *  would have this basket's variance. At ρ=0 it is `n`; at ρ=1 it is 1. It assumes the
   *  legs carry similar volatility, which equal weighting already assumes, and the caption
   *  says "about" for exactly that reason. */
  effective: number | null;
  /** Every pair, most correlated first. */
  pairs: CorrPair[];
  /** Each leg's mean correlation with the others. */
  perLeg: (number | null)[];
}

export function readCorr(corr: number[][]): CorrReading {
  const n = corr.length;
  const empty: CorrReading = { n, mean: null, min: null, max: null, effective: null, pairs: [], perLeg: [] };
  if (n < 2) return empty;

  const pairs: CorrPair[] = [];
  const perLeg: (number | null)[] = [];
  let sum = 0;
  let k = 0;
  for (let i = 0; i < n; i++) {
    let rowSum = 0;
    let rowN = 0;
    for (let j = 0; j < n; j++) {
      const v = corr[i]?.[j];
      if (i === j || typeof v !== "number" || !Number.isFinite(v)) continue;
      rowSum += v;
      rowN++;
      if (j > i) {
        pairs.push({ a: i, b: j, rho: v });
        sum += v;
        k++;
      }
    }
    perLeg.push(rowN ? rowSum / rowN : null);
  }
  if (!k) return empty;

  const mean = sum / k;
  // ΣΣρ over the WHOLE matrix, diagonal included — the diagonal is the n ones, and leaving
  // it out would make a perfectly uncorrelated basket look infinitely diversified.
  const total = n + 2 * sum;
  const rhos = pairs.map((p) => p.rho);
  return {
    n,
    mean,
    min: Math.min(...rhos),
    max: Math.max(...rhos),
    effective: total > 1e-9 ? (n * n) / total : null,
    pairs: pairs.sort((a, b) => b.rho - a.rho),
    perLeg,
  };
}

/* ====================================================== the requests */

export const portfolioApi = {
  list: () => apiGet<Portfolio[]>("/v1/portfolios"),
  one: (id: string) => apiGet<Portfolio>(`/v1/portfolios/${encodeURIComponent(id)}`),
  changes: (id: string) =>
    apiGet<PortfolioChange[]>(`/v1/portfolios/${encodeURIComponent(id)}/changes`),
  backtest: (id: string) =>
    apiGet<BlendResponse>(`/v1/portfolios/${encodeURIComponent(id)}/backtest`),
  /** BLENDS LEGS THAT NEED NOT EXIST. Writes nothing, needs no admin — choosing what to
   *  put in a basket is exactly the moment somebody needs to know whether the picks are one
   *  bet, and making them create it first to find out would be the wrong order. */
  preview: (legs: LegRef[], capital?: number) =>
    apiSend<BlendResponse>("/v1/portfolios/preview", "POST", {
      legs,
      ...(capital == null ? {} : { capital }),
    }),
  create: (body: {
    name: string;
    kind?: string;
    legs?: LegRef[];
    source_cls?: string;
    source_tf?: string;
    top_n?: number;
    capital?: number;
  }) => apiSend<Portfolio>("/v1/portfolios", "POST", body),
  pause: (id: string) =>
    apiSend<Portfolio>(`/v1/portfolios/${encodeURIComponent(id)}/pause`, "POST"),
  resume: (id: string) =>
    apiSend<Portfolio>(`/v1/portfolios/${encodeURIComponent(id)}/resume`, "POST"),
  desk: () => apiGet<DeskPulse>("/v1/desk"),
};

/** How many legs, or null for CANNOT TELL. The listing is free not to carry them and a
 *  zero would be a claim about an empty basket, which is a different thing. */
export const legCount = (p: Portfolio): number | null =>
  Array.isArray(p.legs) ? p.legs.length : typeof p.n_legs === "number" ? p.n_legs : null;

export const legsOf = (p: Portfolio | null): PortfolioLeg[] =>
  (p?.legs ?? []).filter((l) => l && l.rule);

export const isRetired = (p: Portfolio) => p.want === "retired" || p.state === "retired";

/* ====================================================== the portfolio store
 *
 * Module scope with `useSyncExternalStore` on top, the same shape as `lib/live.ts` and for
 * a weaker version of the same reason: three views read this list, and a fetch per view
 * would mean the paper page and the portfolio list disagreeing about what exists whenever
 * one of them was opened a minute later.
 *
 * It POLLS rather than streams. The ledger changes when somebody acts on it, not on a tick,
 * so twenty seconds is enough — and unlike the desk's socket there is nothing here that has
 * to survive being reconnected.
 */

export interface PortfolioState {
  list: Portfolio[];
  ready: boolean;
  error: string | null;
  rev: number;
}

const EMPTY: PortfolioState = { list: [], ready: false, error: null, rev: 0 };

let state: PortfolioState = EMPTY;
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;

function publish(next: Partial<PortfolioState>) {
  state = { ...state, ...next, rev: state.rev + 1 };
  listeners.forEach((l) => l());
}

/* Legs are what every view needs — the paper page joins them to the desk's systems, the
 * list counts them — and the listing is not guaranteed to carry them. So a row that arrives
 * without legs is filled in from `GET /v1/portfolios/{id}`, ONE AT A TIME.
 *
 * Serially on purpose: the house runs 25 of these, and twenty-five parallel requests at
 * page load on a link with real latency is a worse page than one that fills in over a
 * second or two. Rows render the moment the listing lands; the leg counts arrive after. */
let hydrating = false;
async function hydrate() {
  if (hydrating) return;
  hydrating = true;
  try {
    for (const row of state.list) {
      if (Array.isArray(row.legs)) continue;
      try {
        const full = await portfolioApi.one(row.portfolio_id);
        publish({
          list: state.list.map((p) => (p.portfolio_id === row.portfolio_id ? { ...p, ...full } : p)),
        });
      } catch {
        // One unreadable portfolio must not stop the other twenty-four. Its leg count keeps
        // printing an em-dash, which is the honest answer for a number nobody has.
      }
    }
  } finally {
    hydrating = false;
  }
}

async function poll() {
  try {
    const list = await portfolioApi.list();
    publish({ list, ready: true, error: null });
    hydrate();
  } catch (e) {
    const msg = e instanceof ApiError ? e.message : String(e);
    // Keep whatever list is on screen: a failed poll is not evidence that the portfolios
    // went away, and blanking them would say it was.
    publish({ ready: true, error: msg });
  }
}

const subscribe = (l: () => void) => {
  listeners.add(l);
  if (!timer && typeof window !== "undefined") {
    poll();
    timer = setInterval(poll, 20000);
  }
  return () => {
    listeners.delete(l);
    // Unlike the desk's socket this may stop: it is a poll over a ledger, not a stream
    // whose continuity is the point, and nothing is lost by starting it again on the next
    // page that asks.
    if (!listeners.size && timer) {
      clearInterval(timer);
      timer = null;
    }
  };
};

export function usePortfolios(): PortfolioState & { refresh: () => Promise<void> } {
  const s = useSyncExternalStore(
    subscribe,
    () => state,
    () => EMPTY,
  );
  return { ...s, refresh: poll };
}

/** Drop a fresher copy of one row into the store, so a toggle on the detail page is
 *  reflected on the list without waiting for the next poll. */
export function mergePortfolio(row: Portfolio) {
  publish({
    list: state.list.some((p) => p.portfolio_id === row.portfolio_id)
      ? state.list.map((p) => (p.portfolio_id === row.portfolio_id ? { ...p, ...row } : p))
      : [...state.list, row],
  });
}

/* ====================================================== the desk's pulse
 *
 * `want` and `state` disagreeing is the ONLY thing the ledger says while a request is
 * outstanding, and it says exactly the same thing whether the desk read the row a moment
 * ago or has been down since Tuesday. The heartbeat is what separates those, and it is the
 * reason a page may say "in flight" at all — see `../paper api/web/desk.html`, which solved
 * this first.
 *
 * Ref-counted: it polls only while something is on screen that needs it, at the console's
 * own two seconds, and a hidden tab polls nothing.
 */

let pulse: DeskPulse | null = null;
let pulseErr: string | null = null;
const pulseListeners = new Set<() => void>();
let pulseTimer: ReturnType<typeof setInterval> | null = null;
let pulseRev = 0;

async function pollPulse() {
  try {
    pulse = await portfolioApi.desk();
    pulseErr = null;
  } catch (e) {
    pulseErr = e instanceof ApiError ? e.message : "unreachable";
  }
  pulseRev++;
  pulseListeners.forEach((l) => l());
}

export interface PulseState {
  pulse: DeskPulse | null;
  error: string | null;
}

export function useDeskPulse(): PulseState {
  const [, bump] = useState(0);
  useEffect(() => {
    const l = () => bump((n) => n + 1);
    pulseListeners.add(l);
    if (!pulseTimer) {
      pollPulse();
      pulseTimer = setInterval(() => {
        if (!document.hidden) pollPulse();
      }, 2000);
    }
    return () => {
      pulseListeners.delete(l);
      if (!pulseListeners.size && pulseTimer) {
        clearInterval(pulseTimer);
        pulseTimer = null;
      }
    };
  }, []);
  // `pulseRev` is read so the memo below cannot be hoisted past an update.
  void pulseRev;
  return { pulse, error: pulseErr };
}

/* -------------------------------------------------- what the toggle is allowed to say */

export type SettleKind = "settled" | "inflight" | "stopped" | "never" | "failing" | "unknown";

export interface Settlement {
  kind: SettleKind;
  /** One sentence, and NOT a promise. This page does not run the desk and cannot start it,
   *  so it reports what is true rather than forecasting a pass. */
  text: string;
}

/** THE FOUR STATES the manager console draws, applied to a portfolio's `want` vs `state`.
 *
 * `want === state` is settled and needs no heartbeat. Everything else does, and which
 * sentence it gets is the whole reason `/v1/desk` exists:
 *
 *   pulse failing   up and getting nowhere — a pass that throws still completes and still
 *                   beats, so without the `error` field this reads as healthy
 *   pulse live      in flight: a second from being applied
 *   pulse silent    written down and going nowhere until the desk next starts
 *   never beaten    NOT the same claim as "down", and must not be dressed up as one — a
 *                   desk older than the heartbeat runs perfectly and reports nothing
 */
export function settlementOf(
  want: string | null | undefined,
  deskState: string | null | undefined,
  p: DeskPulse | null,
): Settlement {
  const w = String(want ?? "");
  const s = String(deskState ?? "");
  if (w && s && w === s) {
    return { kind: "settled", text: `The desk has this ${s}.` };
  }
  const asked = w === "live" ? "start trading it" : w === "paused" ? "pause it" : `set it ${w || "—"}`;
  if (!p) {
    return {
      kind: "unknown",
      text: `Asked to ${asked}; the desk says ${s || "nothing yet"}. Whether that is in ` +
            `flight or going nowhere cannot be told without the desk's heartbeat, which ` +
            `this page could not read.`,
    };
  }
  if (p.seconds_ago == null) {
    return {
      kind: "never",
      text: `Asked to ${asked}; the desk says ${s || "nothing yet"}. No desk has reported ` +
            `a pass yet — if one is running it predates the heartbeat and needs a restart ` +
            `to report itself. What you asked for is written down either way.`,
    };
  }
  if (p.error) {
    return {
      kind: "failing",
      text: `Asked to ${asked}. The desk is running but its last pass failed — ${p.error}`,
    };
  }
  if (p.live) {
    return {
      kind: "inflight",
      text: `Asked to ${asked}; the desk is live and has not applied it yet. This line ` +
            `updates on its own.`,
    };
  }
  return {
    kind: "stopped",
    text: `Asked to ${asked}, and it is written down — but the desk is not running (last ` +
          `pass ${fmtAgo(p.seconds_ago)} ago). It will be applied when the desk next starts.`,
  };
}

export const fmtAgo = (s: number | null | undefined) =>
  s == null ? "—" : s < 90 ? `${Math.max(0, Math.round(s))}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`;

/* ====================================================== one blend, fetched */

export interface BlendState {
  blend: Blend | null;
  loading: boolean;
  /** The API's own words. `_blend` answers 503 when a leg has no book curve yet and 409
   *  when the legs do not overlap in time, and both of those are things somebody has to
   *  act on rather than a failure to hide. */
  error: string | null;
}

/** The portfolio's combined backtest. One request, re-run when the id changes.
 *
 *  It is the RESEARCH measurement — what the basket WOULD have done over the walk-forward
 *  history — and never the live record. Days of paper fills and years of walk-forward are
 *  different measurements and this app must never make them one number. */
export function useBlend(id: string | null): BlendState {
  const [s, setS] = useState<BlendState>({ blend: null, loading: !!id, error: null });
  useEffect(() => {
    if (!id) {
      setS({ blend: null, loading: false, error: null });
      return;
    }
    let live = true;
    setS({ blend: null, loading: true, error: null });
    portfolioApi
      .backtest(id)
      .then((raw) => live && setS({ blend: adaptBlend(raw), loading: false, error: null }))
      .catch((e: unknown) =>
        live &&
        setS({
          blend: null,
          loading: false,
          error: e instanceof ApiError ? e.message : String(e),
        }),
      );
    return () => {
      live = false;
    };
  }, [id]);
  return s;
}

/** The same blend, for legs that do not exist yet — the PREVIEW. Fired by a caller rather
 *  than by an effect, because it answers a click. */
export function usePreview() {
  const [s, setS] = useState<BlendState>({ blend: null, loading: false, error: null });
  const seq = useRef(0);
  const run = useCallback(async (legs: LegRef[], capital?: number) => {
    const mine = ++seq.current;
    setS({ blend: null, loading: true, error: null });
    try {
      const raw = await portfolioApi.preview(legs, capital);
      if (seq.current === mine) setS({ blend: adaptBlend(raw), loading: false, error: null });
    } catch (e: unknown) {
      if (seq.current === mine)
        setS({
          blend: null,
          loading: false,
          error: e instanceof ApiError ? e.message : String(e),
        });
    }
  }, []);
  const clear = useCallback(() => {
    seq.current++;
    setS({ blend: null, loading: false, error: null });
  }, []);
  return { ...s, run, clear };
}

/* ====================================================== the list's sparklines

 * A portfolio's combined curve is a BLEND, not a stored series — `/v1/portfolios/{id}/
 * backtest` re-reads every leg's book curve and combines them — so a list of twenty-five
 * cannot ask for all of them at once and should not.
 *
 * So they are fetched ONE AT A TIME, in the order the list draws, and each row renders its
 * spark when its own answer lands. Serially rather than in parallel on purpose: the page is
 * complete and readable the moment the listing arrives, the sparks are the last thing added
 * to it, and twenty-five simultaneous blends is a load spike in exchange for a picture
 * nobody is waiting on.
 *
 * The cache is module-level and never invalidated within a session. These curves are the
 * research history; they move when a stage is re-run, which is not something that happens
 * while somebody has the page open.
 */

export interface Spark {
  portfolio: number[];
  bench: number[];
}

/** `undefined` — not asked yet. `null` — asked, and there is no curve (no book run for a
 *  leg, or the legs do not overlap). Those are different states and the row prints
 *  differently for each. */
const sparks = new Map<string, Spark | null>();
const sparkQueue: string[] = [];
const sparkListeners = new Set<() => void>();
let sparkBusy = false;

async function drainSparks() {
  if (sparkBusy) return;
  sparkBusy = true;
  try {
    while (sparkQueue.length) {
      const id = sparkQueue.shift() as string;
      if (sparks.has(id)) continue;
      try {
        const b = adaptBlend(await portfolioApi.backtest(id));
        sparks.set(id, b.ok ? { portfolio: b.portfolio, bench: b.bench } : null);
      } catch {
        sparks.set(id, null);
      }
      sparkListeners.forEach((l) => l());
    }
  } finally {
    sparkBusy = false;
  }
}

export function useSparks(ids: string[]): (id: string) => Spark | null | undefined {
  const [, bump] = useState(0);
  const key = ids.join(",");
  useEffect(() => {
    const l = () => bump((n) => n + 1);
    sparkListeners.add(l);
    for (const id of ids) {
      if (!sparks.has(id) && !sparkQueue.includes(id)) sparkQueue.push(id);
    }
    drainSparks();
    return () => {
      sparkListeners.delete(l);
    };
    // `key` rather than the array: a new array with the same ids must not re-queue.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return (id: string) => sparks.get(id);
}
