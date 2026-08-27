/* The one door to `paper api`.
 *
 * Same-origin by design: in production the FastAPI process serves this export, so a bare
 * `/v1/...` carries the session cookie with no CORS and no token in JavaScript. In `next
 * dev` the rewrite in `next.config.ts` makes :3000 look like that same origin.
 *
 * `credentials: "same-origin"` is explicit rather than left to the default, because the
 * default differs between fetch implementations and the failure it produces — every
 * request 401ing on a page that looks signed in — reads as an auth bug rather than a
 * fetch-option one.
 */

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

async function get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined && v !== null) qs.set(k, String(v));
  }
  const url = `/v1/research/${path}${qs.toString() ? `?${qs}` : ""}`;
  const res = await fetch(url, { credentials: "same-origin", cache: "no-store" });

  if (!res.ok) {
    // A 401 here is a lapsed session, not a bad request. The API answers an unauthenticated
    // ASSET fetch with 401 rather than the login HTML precisely so this branch can tell the
    // difference; sending the browser to /login is the only useful thing to do with it.
    if (res.status === 401 && typeof window !== "undefined") {
      window.location.href = "/login";
    }
    let detail = res.statusText;
    try {
      detail = (await res.json())?.detail ?? detail;
    } catch {
      /* a non-JSON error body is still an error; keep the status text */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export interface SheetRef {
  cls: string;
  tf: string;
  [k: string]: unknown;
}

export interface EdgeRec {
  passed?: number | null;
  verdict?: string | null;
  side?: string | null;
}

export interface BookRec {
  cm_excess_cagr?: number | null;
  n_trades?: number | null;
}

export interface Row {
  rule: string;
  kind: string;
  op?: string | null;
  ir_net?: number | null;
  t_stat?: number | null;
  long_frac?: number | null;
  exposure?: number | null;
  turnover?: number | null;
  net_cagr?: number | null;
  bh_cagr?: number | null;
  edge?: EdgeRec | null;
  book?: BookRec | null;
}

export interface Sheet {
  timeframe: string;
  years: number;
  rows: Row[];
  /** Rows that survived every drop and were ordered — the last page's index. NOT n_rules. */
  n_ranked: number;
  offset: number;
  limit: number;
  n_rules: number;
  n_singles: number;
  n_pairs: number;
  n_catalog: number;
  n_scored: number;
  noise_ceiling: number | null;
  exposure_corr: number | null;
  folds: number | null;
  ranked_on: string;
  ranked_tiebreak: string;
  universe: string[];
  n_assets_scored?: number | null;
  book_bench?: { n_names?: number; years?: number } | null;
}

export interface Curve {
  cls: string;
  tf: string;
  rule: string;
  curve: number[];
  bench: number[];
  dates: string[];
  metrics?: Record<string, number | null>;
  bench_metrics?: Record<string, number | null>;
  n_assets?: number | null;
}

export interface AssetRow {
  symbol: string;
  ir?: number | null;
  years?: number | null;
  net_cagr?: number | null;
  bh_cagr?: number | null;
  net_pct?: number | null;
  bh_pct?: number | null;
}

export interface RuleDetail {
  cls: string;
  tf: string;
  rule: string;
  stats: Record<string, number | null>;
  rows: AssetRow[];
}

export const api = {
  sheets: () => get<SheetRef[]>("sheets"),
  /** One page of one sheet. `limit: 0` returns the header alone, which is how a caller
   *  learns `n_ranked` before deciding what to ask for. */
  leaderboard: (cls: string, tf: string, offset = 0, limit?: number) =>
    get<Sheet>("leaderboard", { cls, tf, offset, limit }),
  /** One rule's book curve and its risk-matched benchmark. The label is a PATH segment
   *  and can contain `|` and `~`, so it is encoded rather than interpolated raw. */
  curve: (cls: string, tf: string, rule: string) =>
    get<Curve>(`curve/${cls}/${tf}/${encodeURIComponent(rule)}`),
  rule: (cls: string, tf: string, rule: string) =>
    get<RuleDetail>(`rule/${cls}/${tf}/${encodeURIComponent(rule)}`),
};

/** The API caps a page at 200 rows because each one carries its asset-by-asset table. */
export const MAX_PAGE = 200;
