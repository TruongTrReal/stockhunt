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

async function get<T>(
  path: string,
  params?: Record<string, string | number | boolean | undefined>,
): Promise<T> {
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

/** The BOOK: one account holding the whole universe, equal-weighted, rebalanced every bar,
 *  point-in-time membership, idle capital earning nothing.
 *
 *  Every money column on the leaderboard and every tile on a detail page's hero strip is
 *  this record, and that is the point — the page used to mix the book with the MEDIAN ASSET
 *  and nothing said which was which. A book has one equity curve and no breadth; breadth and
 *  the per-name table are per asset by construction and are read off `asset_*` instead. */
export interface BookRec {
  cm_excess_cagr?: number | null;
  n_trades?: number | null;
  wealth?: number | null;
  bench_wealth?: number | null;
  cagr?: number | null;
  dsharpe?: number | null;
  t?: number | null;
  n_folds?: number | null;
  dd?: number | null;
  exposure?: number | null;
  n_names?: number | null;
  years?: number | null;
  standard?: StandardRec | null;
}

/** The six acceptance criteria, scored ON THE BOOK by `portfolio_wf._standard`.
 *
 *  `gates` is positional against `edge_criteria` — `config.EDGE_STANDARD` order — so the two
 *  must never be reordered independently: re-lettering one would tick the wrong criterion's
 *  name in every tooltip and raise nothing.
 *
 *  `t_bar` is MEASURED, not Bonferroni: a sign-flip permutation of the panel's own per-fold
 *  edges, which reads the redundancy between rules off the data instead of assuming there is
 *  none. `powered: false` says CANNOT TELL, never fail. */
export interface StandardRec {
  passed?: number | null;
  n?: number | null;
  verdict?: string | null;
  powered?: boolean | null;
  gates?: (boolean | number | null)[];
  t_bar?: number | null;
  t_bar_source?: string | null;
  t_bar_bonferroni?: number | null;
  n_trials?: number | null;
  [k: string]: unknown;
}

/** The book's own benchmark: that universe held passively, over the book's own span.
 *
 *  One figure for the whole sheet rather than a per-row value, which is why the leaderboard
 *  reads it off `sheet.book_bench` and a cell function takes `(row, sheet)`. */
export interface BookBench {
  wealth?: number | null;
  cagr?: number | null;
  sharpe?: number | null;
  dd?: number | null;
  years?: number | null;
  n_names?: number | null;
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
  book_bench?: BookBench | null;
}

/** One benchmark index, scaled to the strategy's own volatility.
 *
 *  `weight` is the share of the index the blend holds, with the rest in bills; `raw_wealth`
 *  is what that index did UNSCALED. The second is caption prose and never a line on the
 *  chart — ranking lines by where they end pays for volatility, and one sizing on the page
 *  is what stops that reading being offered as an equal option.
 */
export interface MatchedLine {
  /** The instrument, or `"this universe, held"` for the sheet's own basket. */
  label: string;
  /** How much of it is held; the rest is cash. Scaled DOWN, never levered up. */
  weight: number | null;
  curve: number[];
  metrics?: Record<string, number | null>;
  wealth?: number | null;
  cagr_pct?: number | null;
  max_dd_pct?: number | null;
  raw_wealth?: number | null;
}

export interface Matched {
  /** The strategy's own annualised volatility: what every line above is held at. */
  vol_pct?: number | null;
  strategy?: {
    label?: string;
    weight?: number;
    wealth?: number | null;
    cagr_pct?: number | null;
    max_dd_pct?: number | null;
  };
  lines?: MatchedLine[];
}

export interface CurveIndex {
  /** Optional because the retired single-index shape carried the symbol in a sibling key,
   *  and a cached file written before that change still has to render. */
  symbol?: string;
  curve?: number[];
  metrics?: Record<string, number | null>;
  [k: string]: unknown;
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
  /** The one sizing this site draws: every benchmark at the strategy's own volatility. */
  matched?: Matched;
  indexes?: CurveIndex[];
  /** The single-index shape the retired `curves.py` wrote. Read both, or old files break. */
  index?: number[];
  index_symbol?: string;
  index_metrics?: Record<string, number | null>;
  /** Whether the book held point-in-time members only. */
  pit?: boolean;
  /** `long` for everything the book stage writes; kept because the file carries it. */
  side?: string;
}

/** A leaderboard row with its sheet's context attached — what `api.row()` returns.
 *
 *  `rank`, `folds`, `book_bench` and `noise_ceiling` are facts about the SHEET, not about
 *  the rule, and they are here because a reader who arrived from a link has no other way to
 *  know whether they are looking at 3rd of 493 or 300th. */
export interface BoardRow extends Row {
  rank: number;
  n_ranked: number;
  n_rules: number;
  folds?: number | null;
  years?: number | null;
  noise_ceiling?: number | null;
  book_bench?: BookBench | null;
  per_asset?: AssetRow[];
  asset_n?: number | null;
  asset_pos?: number | null;
  asset_unranked?: number | null;
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
   *  learns `n_ranked` before deciding what to ask for.
   *
   *  `assets: false` is not optional here in practice. Each row's per-asset table is ~94%
   *  of its bytes and this app never renders one off a leaderboard — the detail page asks
   *  for the single rule it is showing. Sending them anyway made a page of 50 rows 987 KB
   *  instead of 84 KB, which on a link with real latency is most of the wait. Everything
   *  those tables are summarised into (median asset, breadth, `asset_n`) is computed
   *  server-side and still arrives. Pass `assets: true` only for a caller that draws the
   *  tables themselves. */
  leaderboard: (cls: string, tf: string, offset = 0, limit?: number, assets = false) =>
    get<Sheet>("leaderboard", { cls, tf, offset, limit, assets }),
  /** One rule's book curve and its risk-matched benchmark. The label is a PATH segment
   *  and can contain `|` and `~`, so it is encoded rather than interpolated raw. */
  curve: (cls: string, tf: string, rule: string) =>
    get<Curve>(`curve/${cls}/${tf}/${encodeURIComponent(rule)}`),
  rule: (cls: string, tf: string, rule: string) =>
    get<RuleDetail>(`rule/${cls}/${tf}/${encodeURIComponent(rule)}`),
  /** ONE ranked row, plus the sheet context printed beside it.
   *
   *  A detail page shows the same figures its leaderboard row does, and without this the
   *  only way to reach them is to page the sheet looking for the label — ten requests and
   *  ~500 rows to render seven numbers. A 404 means "not RANKED here", which is not the
   *  same as unknown: an off-board rule still answers on `rule()` and `curve()`, and that
   *  is what the page's off-board reading is built from. */
  row: (cls: string, tf: string, rule: string) =>
    get<BoardRow>(`row/${cls}/${tf}/${encodeURIComponent(rule)}`),
};

/** The API caps a page at 200 rows because each one carries its asset-by-asset table. */
export const MAX_PAGE = 200;

/* ---------------------------------------------------------------- the rest of the board
 *
 * Everything below is what the PORT needs and the leaderboard alone did not. Three sources,
 * and which one a view reads is not a style choice:
 *
 *   /v1/board/meta        the baked document's small sections — universe notes, gate
 *                         definitions, timeframe lists, the summary strip. Configuration
 *                         and a handful of numbers, so it is one request at start-up.
 *   /live.json            the DESK's own document, cut to this account by `api_live`.
 *                         Fresh, and what every paper view reads.
 *   /robust.json, /curves the big per-view files, fetched by the one view that draws them.
 *
 * `data.js` is deliberately not among them. It is the same document, but it is 3.7 MB and
 * 87% of that is the `backtest` section the paged leaderboard exists to stop shipping.
 */

/** Anything outside `/v1/research/`: the board's own files and the document endpoint. */
async function root<T>(path: string): Promise<T> {
  const res = await fetch(path, { credentials: "same-origin", cache: "no-store" });
  if (!res.ok) {
    if (res.status === 401 && typeof window !== "undefined") window.location.href = "/login";
    throw new ApiError(res.status, res.statusText);
  }
  return res.json() as Promise<T>;
}

export interface PaperGroup {
  key: string;
  label?: string;
  /** What this universe is worth as evidence. Rendered on a system's page, nowhere else. */
  note?: string;
  symbols?: string[];
}

/** One of the six acceptance criteria, as `config.GATES` defines it.
 *
 *  The field names are the document's, not a tidied version of them: `k` is the letter the
 *  `Standard` column's monogram used, `name` is the full definition, `target` is the bar and
 *  `ask` restates it in money. Renaming them here would be a second vocabulary for one
 *  document, and the letters and their ORDER come from `config.GATES` — this list must not
 *  be reordered or re-lettered on the way through. */
export interface Gate {
  k: string;
  name: string;
  target: string;
  ask?: string;
}

/** A leaderboard tab: the key, its human label, and how many names its universe holds.
 *
 *  Lifted out of the baked `backtest` section by `/v1/board/meta`, because the tab strip IS
 *  this list — a class absent from it is invisible however complete its results are. The
 *  `key` is the GROUP key (`stocks`, `etf`, `futures`), which is not the class name the
 *  `/v1/research/*` routes take (`us_stocks`, `us_etfs`, `cme_futures`); the curve files are
 *  keyed on the group too. */
export interface Group {
  key: string;
  label?: string;
  n?: number;
}

export interface BoardMeta {
  generated_at?: string;
  feed?: Record<string, unknown>;
  venue?: Record<string, unknown>;
  timeframes?: string[];
  paper_timeframes?: string[];
  paper_groups?: PaperGroup[];
  edge_criteria?: Gate[];
  groups?: Group[];
  summary?: Record<string, unknown>;
  research?: Record<string, unknown>;
  curves?: Record<string, { rules?: string[]; n_scored?: number }>;
}

export interface Fill {
  ts: string;
  side: string;
  qty: number;
  price: number;
  symbol: string;
  /** What this fill CLOSED against what the closed part cost. Null when it opened or added. */
  realised?: number | null;
  /** The whole book's mark at that instant. A different question; never sum it with above. */
  pnl?: number | null;
}

export interface System {
  id: string;
  rule?: string;
  asset_class?: string;
  timeframe?: string;
  account?: string;
  trades?: Fill[];
  [k: string]: unknown;
}

/** What the desk publishes, cut to this account. `account`/`house`/`is_admin` say who is
 *  looking, which is what separates "mine" from "everybody else's" on the paper views. */
export interface Live {
  generated_at?: string;
  feed?: Record<string, unknown>;
  venue?: Record<string, unknown>;
  strategies?: System[];
  account?: string | null;
  house?: string | null;
  is_admin?: boolean;
}

export interface RobustEnv {
  key: string;
  cls: string;
  tf: string;
  years?: number | null;
  n_names?: number | null;
  bench?: Record<string, number | null>;
  bench_open?: Record<string, number | null>;
}

/** Positional per-cell arrays: `fields` names the axis. The two MUST move together.
 *
 *  `rules` is the CLOSE fill and `open` is the other one, and both are needed: `5m` was run
 *  at `open` only, on purpose, because a close-fill number on 78 bars a day is the
 *  look-ahead rather than the rule. A matrix reading only `rules` drops every 5m cell
 *  without a word. */
export interface Robust {
  fields: string[];
  envs: RobustEnv[];
  rules: Record<string, Record<string, (number | null)[]>>;
  open?: Record<string, Record<string, (number | null)[]>>;
}

/** What a rule does, in words. The LABEL IS RESOLVED SERVER-SIDE: an overlay falls back to
 *  its base rule (`matched` says which key answered, so a page can be honest that it is
 *  showing the base's description) and a pair comes back as its `legs` and its operator in
 *  one request rather than three. */
export interface RuleLogic {
  logic?: string;
  family?: string;
  note?: string;
  /** Which key answered — the label itself, or the stem it fell back to. Null for a pair. */
  matched?: string | null;
  op?: string | null;
  legs?: { leg: string; logic?: string; family?: string }[];
}

export const board = {
  meta: () => root<BoardMeta>("/v1/board/meta"),
  /** One rule's plain-English logic. 404 where none was recorded, which is not an error. */
  logic: (rule: string) => root<RuleLogic>(`/v1/board/logic/${encodeURIComponent(rule)}`),
  /** The desk's live document. Prefer this over the baked snapshot below. */
  live: () => root<Live>("/live.json"),
  /** The snapshot the last build froze, for when the desk has not published. */
  systems: () => root<Live>("/v1/board/systems"),
  robust: () => root<Robust>("/robust.json"),
  paperCurves: () => root<Record<string, unknown>>("/paper_curves.json"),
  /** Full-resolution detail curves for one sheet. Several hundred kB — fetch on demand. */
  curves: (cls: string, tf: string) =>
    root<Record<string, unknown>>(`/curves/${cls}_${tf}.json`),
  /** The downsampled set the BOARD chart draws: `dates`, `bench`, `rules`. Tens of kB. */
  boardCurves: (cls: string, tf: string) =>
    root<{ dates: string[]; bench: number[]; rules: Record<string, number[]> }>(
      `/curves/board_${cls}_${tf}.json`),
};

/** The live desk pushes here every ~2s. Same origin, so `wss:` follows from `https:`. */
export const liveSocketUrl = () =>
  `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
