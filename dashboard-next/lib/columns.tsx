/* ---------- the leaderboard's columns ----------
 *
 * A direct port of `LB_COLS` in `../Stockhunt Dashboard/web/app.js`, in the same order,
 * with the same headers, the same cell formatting and the same `doc` prose. Only the
 * rendering changed — HTML strings became JSX — because the two boards must print the
 * same table off the same `board_rank.build_sheet` document.
 *
 * Declared as a list rather than written inline for the reason it always was: the phone
 * and the desktop want them in a different order, and a table cannot reorder its own
 * columns in CSS. `lead` marks the three that decide whether a row is worth opening at
 * all — how much Sharpe it added over holding, how much money that was, and how much of
 * the standard it cleared — which on a narrow screen move up beside the frozen name.
 *
 * Every column from `Long %` down is the BOOK: one account holding the whole universe,
 * equal-weighted, rebalanced every bar, point-in-time membership, idle capital earning
 * nothing. Until 2026-08-13 most of them were the MEDIAN SINGLE ASSET, which is a
 * different portfolio over a different span, so a row was two measurements side by side.
 * `bookNum` is the shared renderer and it BLANKS on a row with no book run — never falls
 * back to the per-asset figure, because a column holding two measurements is the bug
 * that was fixed.
 *
 * Each column carries three things beyond how it draws a cell:
 *
 *   `doc`  what it means, shown after a dwell on the header. It used to be one caption
 *          under the table — eight hundred words a reader either read before they had a
 *          question or scrolled past forever. Same text, asked for a column at a time. A
 *          function where the answer depends on the sheet, a plain string where it does
 *          not. A COLUMN ADDED WITHOUT A `doc` IS THE ONE COLUMN NOBODY CAN ASK ABOUT.
 *   `sv`   the value a header click sorts on, and `bsv` the benchmark row's value for the
 *          same column — null wherever buy-and-hold has no comparable figure, which is
 *          the same set of columns that print an em-dash on its row.
 *   `text` marks the columns that sort alphabetically and so ascend on the first click.
 */

import { type ReactElement } from "react";
import type { Gate, Row, Sheet } from "@/lib/api";
import {
  esc, fmtDD, fmtIR, fmtMoney, fmtNum, fmtPct, fmtSharpe, fmtSigned,
  opLabel, pctOr, sign, stemName,
} from "@/lib/format";

/* ------------------------------------------------------------------ what a row carries
 *
 * `lib/api.ts` types the handful of fields its own leaderboard drew. The full board reads
 * most of `board_rank.leaderboard_entry`, so the extra fields are declared here rather
 * than by widening that file — the API module is shared with two other views and this is
 * the one that needs the whole record. Shapes are `board_rank._book_record`,
 * `_book_bench` and `_book_standard`; if a field moves there it moves here.
 */

export interface BookStandard {
  passed: number;
  n: number;
  /** Positional, in `config.EDGE_STANDARD` order — the same order `edge_criteria` names. */
  gates: boolean[];
  verdict: string | null;
  powered: boolean;
  rankable: boolean;
  /** The bar `t` was actually scored against, which is not the printed 2.0. */
  t_bar: number | null;
  t_bar_source: string | null;
  t_bar_bonferroni: number | null;
  n_candidates: number;
}

export interface Book {
  wealth: number | null;
  cagr: number | null;
  bench_wealth: number | null;
  cm_excess_cagr: number | null;
  cm_bench_cagr: number | null;
  cm_ratio: number | null;
  sharpe: number | null;
  sharpe_bench: number | null;
  dsharpe: number | null;
  t: number | null;
  n_folds: number | null;
  dd: number | null;
  exposure: number | null;
  win_rate: number | null;
  profit_factor: number | null;
  n_trades: number | null;
  trades_per_asset: number | null;
  expectancy: number | null;
  n_names: number | null;
  years: number | null;
  roe_ann: number | null;
  vs_random: number | null;
  vs_constant: number | null;
  headroom: number | null;
  standard: BookStandard | null;
}

/** The sheet's buy-and-hold: one universe held passively, so it is the same figure on
 *  every row and is carried at sheet level rather than per row. */
export interface BookBench {
  wealth: number | null;
  cagr: number | null;
  sharpe: number | null;
  dd: number | null;
  years: number | null;
  n_names: number | null;
  start: string | null;
  end: string | null;
  index_wealth: number | null;
  index_symbol: string | null;
}

/** Only the fields the benchmark row and the doc context read. The per-asset standard is
 *  otherwise off this page since 2026-08-13. */
export interface EdgeRow {
  sharpe?: number | null;
  bench_sharpe?: number | null;
  bench_wealth?: number | null;
}

/** In how many scored environments the book's Sharpe cleared the same universe held
 *  passively. A raw count, never a composite score. */
export interface Rob {
  n: number;
  total: number;
}

export type BoardRow = Omit<Row, "book" | "edge"> & {
  book?: Book | null;
  edge?: EdgeRow | null;
  rob?: Rob | null;
  /** The MEDIAN ASSET's total return, and its benchmark's. Nothing in the table reads
   *  them any more — the money columns are the book since 2026-08-13 — but the summary
   *  strip still falls back to them on a sheet with no book run at all, where they are the
   *  only money the sheet has. */
  net_pct?: number | null;
  bh_pct?: number | null;
};

export type BoardSheet = Omit<Sheet, "rows" | "book_bench"> & {
  rows: BoardRow[];
  book_bench?: BookBench | null;
  n_shown_pairs?: number;
  n_flat_dropped?: number;
  n_closet_dropped?: number;
  n_nobook_dropped?: number;
  n_unscored_dropped?: number;
  /** A statement about the BOOK's fold count, which is the stricter one. `false` says
   *  CANNOT TELL, and is not a fail. */
  powered?: boolean | null;
  book_folds?: number | null;
};

/** What the sheet-dependent `doc`s are given. Same three keys the vanilla board passes. */
export interface DocCtx {
  sh: BoardSheet;
  grp: { label: string; n: number };
  bench: number | null;
}

/** What a cell needs that is not on the row: the gate names for the Standard tooltip, and
 *  the chart selection, which lives in the Strategy cell's checkbox. The vanilla board
 *  read both off module state; a React tree passes them down instead. */
export interface CellCtx {
  /** `config.GATES` through `/v1/board/meta`, in its own order: the letters and their
   *  order are the standard's, and re-lettering or reordering them here would tick the
   *  wrong criterion's name in every tooltip on the site and raise nothing. */
  criteria: Gate[];
  selected: Set<string>;
  /** Six lines is the ceiling, so an unticked box on a full chart says so rather than
   *  silently doing nothing. */
  full: boolean;
  colorOf: (rule: string) => string;
  onToggle: (rule: string) => void;
}

export interface LbCol {
  /** Plain text, not HTML — it is a React child here and is escaped where a `doc` or the
   *  ranking note interpolates it. */
  h: string;
  l?: boolean;
  lead?: boolean;
  adv?: boolean;
  text?: boolean;
  cell: (r: BoardRow, sh: BoardSheet, cx: CellCtx) => ReactElement;
  bh?: (b: EdgeRow | null, sh: BoardSheet) => ReactElement;
  doc: string | ((c: DocCtx) => string);
  sv: (r: BoardRow) => number | string | null | undefined;
  bsv?: (b: EdgeRow | null, sh: BoardSheet) => number | null | undefined;
}

/* ------------------------------------------------------------------- the cell renderers */

const Dash = () => <td className="flat">—</td>;

/** Every book column blanks where there is no book record, and never falls back. */
const bookNum = (v: number | null | undefined, fmt: (x: number) => string) =>
  v == null ? <Dash /> : <td>{fmt(v)}</td>;

/* A figure shown against the benchmark's own value rather than alone, and coloured by that
 * comparison. Raw Sharpe especially: on a rising market it largely measures how much of the
 * time a rule was invested, so a bare 0.66 looks like skill until you see buy-and-hold
 * scored 0.63 over the same bars. The comparison is the number; the level is context. */
const vsCell = (
  v: number | null | undefined,
  bench: number | null | undefined,
  fmt: (x: number) => string,
  better: (a: number, b: number) => boolean,
  tip: string,
) => {
  if (v == null) return <Dash />;
  const cls = bench == null ? "" : better(v, bench) ? "gain" : "loss";
  const title = bench == null ? undefined : `buy & hold: ${fmt(bench)} — ${tip}`;
  return (
    <td className={cls} title={title}>
      {fmt(v)}
    </td>
  );
};

const numCell = (
  present: unknown,
  v: number | null,
  f: (x: number | null) => string,
) => (present == null ? <Dash /> : <td className={sign(v)}>{f(v)}</td>);

const bookExposure = (r: BoardRow) =>
  r.book && r.book.exposure != null ? r.book.exposure : null;

/* `n of 6`, not the STRCWH letter strip. The strip encoded which criteria passed in a
 * six-character monogram nobody could read without the legend, and the legend was the
 * column header — so the header stopped naming the column and started being a key. The
 * count is the number a reader actually wants; *which* six is a tooltip. */
const edgeCount = (e: BookStandard | null | undefined, criteria: Gate[]) => {
  if (e == null) return <span className="gates none">—</span>;
  const named = criteria
    .map((c, i) => `${e.gates[i] ? "✓" : "✗"} ${c.k}  ${c.target}  ${c.name}`)
    .join("\n");
  /* The T criterion's printed target is ">= 2.0", which is the bar for a single
   * pre-specified test and not for this one: searching ~400 candidates raises it. A
   * reader seeing T failed beside "target >= 2.0" and a t of 2.81 is owed the number it
   * actually had to clear, and where that number came from. */
  const bar =
    e.t_bar == null
      ? ""
      : `\n\nT is scored against ${e.t_bar.toFixed(2)}, not 2.0: ${
          e.n_candidates ? e.n_candidates + " candidates were" : "the panel was"
        } searched${
          e.t_bar_source === "maxT"
            ? `, and that bar is measured by sign-flip permutation of this sheet's own per-fold edges${
                e.t_bar_bonferroni
                  ? ` — Bonferroni would have assumed ${e.t_bar_bonferroni.toFixed(2)}`
                  : ""
              }`
            : ""
        }.`;
  const why =
    (e.verdict === "underpowered"
      ? `too few folds to resolve — cannot tell, not "no"\n\n${named}`
      : named) + bar;
  return (
    <span className={`gates${e.verdict === "PASS" ? "" : " none"}`} title={why}>
      {e.passed}/{e.n}
    </span>
  );
};

/* How many positions the rule opened on a typical asset. Not coloured — trading a lot is
 * neither good nor bad on its own — but it is what makes the profit factor beside it
 * readable: 1,283 trades is a distribution, 3 is an anecdote. */
const tradesCell = (v: number | null | undefined) => {
  if (v == null) return <Dash />;
  const why =
    v < 30
      ? `${v} trades on a typical asset — too few to read the profit factor as a rate`
      : `${v} positions opened on a typical asset, out-of-sample`;
  return (
    <td className="flat" title={why}>
      {Math.round(v).toLocaleString()}
    </td>
  );
};

/* The book's terminal wealth, coloured against the book's OWN buy-and-hold.
 *
 * The colour is the RAW money question — did this account end up with more than holding —
 * and deliberately not the risk-matched one, even though the risk-matched figure is what
 * the table is ranked on. The two genuinely disagree and the split is the point: a rule
 * can clear holding per unit of risk while ending with far less money, because it was
 * only ever exposed to half the market. Colour that cell green and the reader is told
 * they made money they did not make; put the risk-matched verdict in its own column, in
 * its own colour, and both facts survive. Money here, skill next door. */
const bookWealthCell = (b: Book | null | undefined) => {
  if (b == null) return <Dash />;
  const bw = b.bench_wealth;
  const cls = bw == null ? "" : (b.wealth ?? 0) > bw ? "gain" : "loss";
  const mult = bw != null && bw > 0 && b.wealth != null ? b.wealth / bw : null;
  const title =
    (bw == null
      ? ""
      : `holding the same universe over the same bars returns ${fmtMoney(bw)}${
          mult ? ` — this is ${fmtNum(mult, 2)}x that` : ""
        }`) +
    (b.exposure == null
      ? ""
      : `, and it was invested ${fmtNum(b.exposure * 100, 0)}% of the time`) +
    (b.cm_excess_cagr == null
      ? ""
      : `. At equal risk: ${fmtPct(b.cm_excess_cagr * 100, 2)}/yr` +
        (b.cm_ratio ? `, ${fmtNum(b.cm_ratio, 1)}x the money` : ""));
  return (
    <td className={cls} title={title || undefined}>
      {fmtMoney(b.wealth)}
    </td>
  );
};

const pfCell = (b: Book | null | undefined, longFrac: number | null) => {
  const v = b && b.profit_factor;
  if (v == null) return <Dash />;
  // A rule that is in the market ~always closes almost nothing, so its profit factor is a
  // couple of trades rather than a distribution — the same reason the benchmark has none.
  // Greyed rather than hidden: the number is real, it is just not comparable with the
  // 1,283-trade rule above it, and the Long % flag on the same row says why.
  const uncountable = longFrac != null && longFrac > 0.9;
  const cls = uncountable ? "flat" : v > 1 ? "gain" : "loss";
  const why = uncountable
    ? "barely closes a trade at this exposure — not comparable with a rule that turns over"
    : "gross winnings ÷ gross losses, per closed trade — 1.00 is break-even. " +
      "Buy-and-hold never closes a trade, so it has none to compare.";
  return (
    <td className={cls} title={why}>
      {fmtNum(v, 2)}
    </td>
  );
};

/* Rendered inside the Strategy cell rather than as a column of its own, so the phone's
 * frozen first column stays the name and the table gains no track.
 *
 * A ticked box wears its line's colour, which is the only thing tying a row to a line on
 * the chart above it once the names at the ends are clipped. */
function Cbx({ rule, cx }: { rule: string; cx: CellCtx }) {
  const on = cx.selected.has(rule);
  const full = !on && cx.full;
  return (
    <span
      className={`cbx${on ? " on" : ""}${full ? " full" : ""}`}
      role="checkbox"
      aria-checked={on}
      style={on ? { background: cx.colorOf(rule), borderColor: cx.colorOf(rule) } : undefined}
      title={
        on
          ? "Drawn on the chart above — click to remove"
          : full
            ? "Six lines is the ceiling; remove one first"
            : "Draw this book on the chart above"
      }
      // Ticking must not navigate: the row's own click opens the detail page, so the
      // checkbox stops the event before it reaches the row.
      onClick={(e) => {
        e.stopPropagation();
        e.preventDefault();
        cx.onToggle(rule);
      }}
    />
  );
}

/* ------------------------------------------------------------------------ the columns */

export const LB_COLS: LbCol[] = [
  {
    h: "Strategy",
    l: true,
    cell: (r, _sh, cx) => {
      const op = opLabel(r.op);
      return (
        <td className="l">
          <Cbx rule={r.rule} cx={cx} />
          {stemName(r.rule)}
          {/* `.chip` carries no margin — the gap is the space in the markup, which JSX
              would otherwise swallow across a line break. */}
          {op ? <> <span className="chip mut">{op}</span></> : null}
        </td>
      );
    },
    doc: `The rule, and what it is made of. A chip after the name marks a <b>pair</b> and
      gives its operator: <code>or</code> takes a position if either leg does (the most
      exposed), <code>and</code> only when both agree, <code>vote</code> by majority,
      <code>gate</code> uses one leg as a filter on the other. Single rules and pairs are
      ranked in one list because they are the same kind of object — same folds, same
      benchmark, same six criteria. Everything here is <b>walk-forward</b>: parameters are
      re-picked on each in-sample window and applied to the next, so what you are reading
      is out-of-sample.`,
    text: true,
    sv: (r) => stemName(r.rule).toLowerCase(),
  },
  /* `Side` used to sit here and is gone (2026-08-13). It named the side the per-asset
   * standard picked. Now that the verdict is computed on the book and the book is built
   * long/flat only, every row would read "long/flat" and the column would be a constant
   * that still implied a choice had been made. */
  {
    h: "Long %",
    cell: (r) => (
      <td className={bookExposure(r) != null && bookExposure(r)! > 0.9 ? "loss" : ""}>
        {pctOr(bookExposure(r))}
      </td>
    ),
    bh: () => <td className="flat">100%</td>,
    doc: ({ sh }) => `Share of bars <b>the book</b> holds a position, weighted across every
      name it holds — how much of the time its capital was at work. <b>Read it before any
      money column.</b> Anything above 90% is flagged: at that point the rule is
      approximately buy-and-hold, and it scores near the benchmark for that reason rather
      than through skill.${
        sh.exposure_corr == null ? "" : ` On this sheet exposure and IR correlate at
      <b>${fmtSigned(sh.exposure_corr, 2)}</b>${sh.exposure_corr > 0.5
        ? " — so the old IR ranking was largely a ranking of time invested, which is why"
          + " this table is ranked on the Standard column instead, broken by a"
          + " risk-matched figure, with ROE/yr beside ROI/yr to show"
          + " what the capital earned while it was actually deployed."
        : ", so the ranking here is not simply a ranking of exposure — unlike the equity"
          + " sheets, where it is."}`}`,
    sv: (r) => bookExposure(r),
    bsv: () => 1,
  },
  /* `adv: true` marks the columns behind the "More columns" toggle. Nothing here changes
   * what a column means, only when it is drawn — the doc index is into the FULL list, so
   * hiding a column never renumbers another one's explanation. */
  {
    h: "ΔSharpe",
    adv: true,
    cell: (r) => bookNum(r.book?.dsharpe, fmtIR),
    // Zero by construction — the benchmark measured against itself. This is the number
    // that places the row, and the line every rule above it has cleared.
    bh: () => <td className="flat">0.000</td>,
    doc: `<b>The book's</b> Sharpe minus the same universe held passively — one account
      against one account, computed <b>per fold and then averaged</b>, which is how the
      acceptance standard defines the criterion the <b>Standard</b> column scores. The
      pooled version (one Sharpe over all bars, minus one) is stored beside it
      and runs a little higher; a per-fold mean weights every fold equally where pooling
      weights every bar equally. Sharpe is used rather than information ratio throughout
      because IR compares a part-time rule against a full-time one and scores capital
      deployment as much as skill.
      <br><br><b>Idle capital earns nothing here.</b> A rule that sits out half the time
      is credited with no interest for those bars, and neither is the passive book it is
      measured against — the two lose the credit together, so what goes is a return that
      was never the signal's. The per-asset version of this number is what the
      <b>Standard</b> column still counts; it is a median across names rather than an
      account, so the two do not have to agree.`,
    sv: (r) => r.book?.dsharpe,
    bsv: () => 0,
  },
  {
    h: "t",
    adv: true,
    cell: (r) => bookNum(r.book?.t, (v) => fmtSigned(v, 2)),
    doc: `How reliable the ΔSharpe beside it is: its mean divided by its own
      standard error <b>across the book's walk-forward folds</b>. <b>t ≥ 2 is the bar</b>,
      and the <b>Standard</b> column raises it for multiplicity — with ~400 candidates
      searched the bar lands near 3.8, so a row can clear 2.0 here and still fail that
      criterion. Hover <b>Standard</b> for the bar this sheet measured.
      <br><br><b>The bar is measured, not assumed.</b> Correcting for a search means
      asking how high the BEST of ~400 candidates would score if none of them had an edge,
      and that depends on how alike the candidates are — dozens of near-identical candle
      patterns are not dozens of separate chances. So the sheet's own per-fold edges are
      sign-flipped at random, all rules at once, ten of thousands of times: the edge is
      destroyed, the correlation between rules is preserved, and the 95th percentile of
      the best rule's t under that null is the bar. It is exact for any correlation
      structure and needs no assumption about it.
      <br><br>It can move the bar either way. On us_stocks 1d it lands at <b>3.76</b>
      where Bonferroni asked <b>3.84</b> — nearly the same number for two cancelling
      reasons: Bonferroni is too strict about independence and too lenient about the fat
      tails of a 21-fold average. Measured against simulated independent panels, those 387
      candidates behave like about <b>85</b> separate tests, not 387.
      <br><br>Measured across <b>time</b>, never across assets. The account IS every name
      at once, so breadth cannot inflate it — which is the failure mode a per-asset t has
      to be defended against, since twenty stocks that move together are not twenty
      independent tests.
      <br><br>A block bootstrap over the same book is also stored and is <b>looser</b>:
      <code>ibs</code> on us_stocks 1d bootstraps to +3.87 and scores +2.81 across its 21
      folds. The threshold was calibrated on fold-to-fold spread, so this is the number the
      verdict reads; the bootstrap is a second opinion, not a better one.
      <br><br><b>Clearing the bar is not the same as clearing luck.</b> This asks whether
      one rule beat its benchmark reliably; the deflated Sharpe prices how many rules were
      looked at before it was picked, and lives in
      <code>portfolio_wf.py --n-trials --trial-dispersion</code>.`,
    sv: (r) => r.book?.t,
  },
  {
    h: "Expectancy",
    adv: true,
    cell: (r) => bookNum(r.book?.expectancy, (v) => fmtSigned(v * 100, 2) + "%"),
    doc: `What one <b>trade</b> is worth on average, as a percentage:
      <code>win% × avg win − loss% × avg loss</code>, pooled across every name the book
      holds. A trade is one position held from open to close, not one bar.
      <b>Only readable beside Trades.</b> It rewards trading rarely, so it is a column and
      never the sort key: buy-and-hold shows a huge expectancy on the single position it
      holds for the whole window, and a coin-flip rule that opens 72 positions beats a real
      one that opens 642. High expectancy with a handful of trades is a small sample, not an
      edge.`,
    sv: (r) => r.book?.expectancy,
  },
  {
    h: "Win %",
    adv: true,
    cell: (r) => bookNum(r.book?.win_rate, (v) => fmtNum(v * 100, 1) + "%"),
    doc: `Share of the book's trades that closed profitably, pooled across its names.
      Deliberately <b>not</b> a ranking metric and not a virtue on its own — it is one half
      of expectancy, and the half that can be pushed arbitrarily high by cutting winners
      early and holding losers. Read it against the average win and loss it is paired
      with.`,
    sv: (r) => r.book?.win_rate,
  },
  {
    h: "ROI/yr",
    cell: (r) => bookNum(r.book?.cagr, (v) => fmtSigned(v * 100, 1) + "%"),
    // The passive book's own annual rate, from the sheet rather than the row: holding is
    // one portfolio, so it is the same figure opposite every rule.
    bh: (_b, sh) => (
      <td className="flat">
        {sh.book_bench?.cagr != null ? fmtSigned(sh.book_bench.cagr * 100, 1) + "%" : "—"}
      </td>
    ),
    doc: `Annualised return of <b>the book</b> — what the whole account earned, including
      the time its capital sat in cash earning <b>nothing</b>. This is the honest "what
      did I make" number, and it is the one that penalises a rule for being out of the
      market — the more so since 2026-08-13, when the T-bill credit came off both
      sides. It is the same series the equity chart on the detail page draws, so the two
      cannot disagree.`,
    sv: (r) => r.book?.cagr,
    bsv: (_b, sh) => sh.book_bench?.cagr,
  },
  {
    h: "ROE/yr",
    adv: true,
    cell: (r) => bookNum(r.book?.roe_ann, (v) => fmtSigned(v * 100, 1) + "%"),
    // Identical to ROI on this row and that is the point: buy-and-hold is never idle, so
    // it has no gap between what the account earned and what the deployed money earned.
    bh: (_b, sh) => (
      <td className="flat">
        {sh.book_bench?.cagr != null ? fmtSigned(sh.book_bench.cagr * 100, 1) + "%" : "—"}
      </td>
    ),
    doc: `The book's return on capital <b>while it was actually deployed</b>: the interest
      earned on the idle fraction is stripped out, and what is left is annualised over
      <i>deployed</i> years — calendar years × time invested — rather than calendar years.
      ROI asks what the account earned; this asks what the money earned when it was at
      work, and the two differ by exactly the idle time. It is here because exposure and IR
      correlate at 0.881 on daily equities, so an account-level ranking is substantially a
      ranking of who stayed invested longest. Buy-and-hold's ROI and ROE are identical
      because it is never idle; a rule holding 46% of the time can earn far more per
      deployed dollar and still show a smaller ROI.`,
    sv: (r) => r.book?.roe_ann,
    bsv: (_b, sh) => sh.book_bench?.cagr,
  },
  {
    h: "Sharpe",
    lead: true,
    cell: (r) =>
      vsCell(r.book?.sharpe, r.book?.sharpe_bench, fmtSharpe, (a, b) => a > b,
        "the same universe held passively, over the same bars"),
    bh: (_b, sh) => <td className="flat">{fmtNum(sh.book_bench?.sharpe, 3)}</td>,
    doc: `The book's return per unit of volatility. Idle capital earns nothing, so a rule
      is not paid for the bars it sat out. Coloured against <b>the same
      universe held passively over the same bars</b> rather than against nothing, and
      hovering the cell gives that value: raw Sharpe largely rewards time in the market, so
      0.66 reads like skill until you see the benchmark scored 0.63. The level is context;
      the comparison is the number.
      <br><br><b>Measured at the optimistic fill.</b> Every figure on this row is computed
      buying at the same close whose high, low and close produced the signal — a price
      nobody knew when the decision was made. Removing that costs real performance, and how
      much depends on whether you can trade the closing auction: with a market-on-close
      order and the signal computed minutes early it is a small haircut, and if you have to
      wait for the next open it is a large one. Treat this column as the <b>top</b> of a
      range, not a result. <code>portfolio_wf.py --fill</code> prices both ends.`,
    sv: (r) => r.book?.sharpe,
    bsv: (_b, sh) => sh.book_bench?.sharpe,
  },
  /* The BOOK's drawdown, not the median asset's, since 2026-08-13. They are wildly
   * different numbers and the page was showing the less useful one: 189 names falling on
   * different days is the whole point of holding 189 names, and nobody ever lived through
   * the median asset's drawdown. */
  {
    h: "Max DD",
    cell: (r, sh) =>
      vsCell(r.book?.dd, sh?.book_bench?.dd, fmtDD, (a, b) => a > b,
        "worst peak-to-trough fall of the passive book over the same bars"),
    bh: (_b, sh) => (
      <td className="flat">
        {sh.book_bench?.dd != null ? fmtNum(sh.book_bench.dd, 1) + "%" : "—"}
      </td>
    ),
    doc: ({ sh }) => `The worst peak-to-trough fall of <b>the book</b> — one account
      holding every name at once — against the same universe held passively${
        sh.book_bench && sh.book_bench.dd != null
          ? `, which fell <b>${fmtNum(sh.book_bench.dd, 1)}%</b> over these bars` : ""}.
      Hover a cell for the comparison.
      <br><br><b>This is not the drawdown of a typical single stock</b>, which is the
      figure this column used to carry and is roughly twice as deep: individual names fall
      on different days, so a book of them falls far less than any of its parts. The
      per-asset figure is still measured; it is just not what anyone would have
      experienced. A rule that sits out part of the time ought to fall
      less than one that never does; many here do not, and that is worth knowing before
      the money columns are believed.`,
    // Drawdowns are negative, so the largest number is the shallowest fall — sorting
    // descending puts the least painful first, like every other column here.
    sv: (r) => r.book?.dd,
    bsv: (_b, sh) => sh.book_bench?.dd,
  },
  {
    h: "Trades/asset",
    cell: (r) => tradesCell(r.book?.trades_per_asset),
    bh: () => (
      <td className="flat" title="one position, opened at the start and never closed">
        1
      </td>
    ),
    doc: `The book's trades divided by the names it holds — positions opened on a
      typical name over the whole out-of-sample window. Not good or bad on its own; it is
      <b>what makes the profit factor beside it readable</b>. 1,283 trades is a
      distribution; 3 is an anecdote, and a rule that trades three times can post a profit
      factor of 32 without having found anything. Buy-and-hold shows 1: opened at the
      start, never closed.`,
    sv: (r) => r.book?.trades_per_asset,
    bsv: () => 1,
  },
  {
    h: "Profit factor",
    cell: (r) => pfCell(r.book, bookExposure(r)),
    doc: `Gross winnings ÷ gross losses across the book's closed trades, pooled over its
      names. Scored against <b>1.00</b>, not against the benchmark, because buy-and-hold
      holds one position throughout and never closes a trade — it has no profit factor to
      compare with, and inventing one would be worse than leaving the cell blank. Greyed
      above 90% invested, where a rule barely closes anything. Read it with
      Trades/asset.`,
    sv: (r) => r.book?.profit_factor,
  },
  {
    h: "vs random",
    adv: true,
    cell: (r) => bookNum(r.book?.vs_random, fmtIR),
    doc: `The book's Sharpe above a <b>signal-free control invested exactly as often</b>,
      at random. Being in the market pays in a rising market whether or not you were right,
      so this prices that handicap and leaves what the signal itself did. A rule that
      cannot beat its own coin-flip has found exposure, not an edge.
      <br><br>The controls are not a model: <code>RANDOM_25/50/75/90</code> are backtested
      as books by the same run, on the same bars and fees, and the curve through their
      measured Sharpes is read at this rule's own exposure. Each of them therefore scores
      exactly +0.000 here, which is the check that the curve is honest.`,
    sv: (r) => r.book?.vs_random,
  },
  {
    h: "vs constant",
    adv: true,
    cell: (r) => bookNum(r.book?.vs_constant, fmtIR),
    doc: `The same question asked a second way: return per unit of drawdown (CAGR ÷ max
      drawdown) against simply <b>owning less of the same basket, all the time</b>, at the
      book's own average weight and the rest in cash. Anyone can hold 47% of a basket
      and keep the rest in cash — a rule has to beat that before its timing is worth
      anything.`,
    sv: (r) => r.book?.vs_constant,
  },
  /* `$10k / asset` and a per-asset `vs B&H` used to sit here, and they are gone
   * (2026-08-13). Both were the MEDIAN SINGLE NAME over a ~12-year membership spell
   * rather than the sheet's out-of-sample span, and the two columns below ask the
   * identical questions of the account a reader would actually have owned. Two money
   * columns on two different measurements invited exactly one mistake, which is reading
   * them as a bigger and a smaller version of one number. */
  {
    h: "$10k / book",
    lead: true,
    cell: (r) => bookWealthCell(r.book),
    bh: (_b, sh) => <td className="flat">{fmtMoney(sh.book_bench?.wealth)}</td>,
    doc: ({ sh }) => {
      const bb = sh.book_bench;
      if (!bb) return `No book run covers this sheet, so every row prints an em-dash.
        The columns to the left are per-asset medians and remain the sheet's only money.`;
      return `What $10,000 became in <b>one account holding the whole universe</b> —
        ${bb.n_names} names, equal-weighted, rebalanced every bar, held only on the dates
        each was actually a member — over <b>${fmtNum(bb.years, 1)} years</b>
        (${esc(bb.start)} to ${esc(bb.end)}).
        <br><br>This is the number a reader means by "what would I have made". It is
        <b>not</b> a bigger version of <i>$10k / asset</i>: that one is the median single
        stock over its own membership spell, this is the portfolio over the sheet's whole
        out-of-sample span, and diversification, rebalancing and universe churn all live in
        the gap. Holding this universe passively over these bars returned
        <b>${fmtMoney(bb.wealth)}</b>${bb.index_wealth
          ? `, against ${fmtMoney(bb.index_wealth)} for ${esc(bb.index_symbol)} — the
        purchasable index — over the same bars` : ""}.
        <br><br><b>Green here and green in <i>book vs B&amp;H</i> are different claims,
        and rows routinely have one without the other.</b> This column is coloured on raw
        money — did the account end with more than holding. The next one is coloured on
        the risk-matched comparison. A rule invested half the time can clear holding
        comfortably per unit of risk and still finish with far less money, because it was
        only ever exposed to half the market. Neither number is the trick; read them
        together.
        <br><br>Scored on the same walk-forward out-of-sample bars as everything else on
        the table: the run starts at the first bar that was ever out-of-sample, so no rule
        is credited with the history it was selected on.`;
    },
    sv: (r) => r.book?.wealth,
    bsv: (_b, sh) => sh.book_bench?.wealth,
  },
  {
    h: "book vs B&H",
    lead: true,
    cell: (r) =>
      numCell(r.book, r.book?.cm_excess_cagr != null ? r.book.cm_excess_cagr * 100 : null,
        (v) => fmtPct(v, 2)),
    bh: () => <td className="flat">+0.00%</td>,
    doc: `<b>The tiebreak this table is ordered by</b>, inside each tier of the Standard
      column. Annual return of the book above the
      same universe held passively, after the passive side has been scaled <i>down</i> with
      cash to the rule's own volatility — never levered up, so no margin and no borrow.
      <br><br>Matching the risk first is what makes it a measure of skill rather than of
      nerve. <code>corr(IR, long_frac)</code> is 0.881 on daily equities, so ranking on
      plain return is largely ranking on who stayed invested longest; a rule in the market
      47% of the time is compared here with holding 47% of the market and the rest in
      cash. A rule that beats holding only by taking more risk scores +0.00% here, which
      is the honest answer.`,
    sv: (r) => r.book?.cm_excess_cagr,
    bsv: () => 0,
  },
  {
    h: "fees",
    adv: true,
    cell: (r) => bookNum(r.book?.headroom, (v) => fmtNum(v, 1) + "x"),
    doc: `Cost headroom: how many times the modelled commission and spread could rise
      before <b>the book</b> stops beating the same basket held at its own volatility.
      <b>0.0x means it already does not</b>, at the real cost. A high-turnover rule with a
      thin edge dies here first, which is why the number sits on the row rather than in an
      appendix.
      <br><br>Measured, not extrapolated: every term in the cost model is linear in its
      own rate, so the account at 5x the schedule is its zero-cost book minus five times
      its measured drag — exact arithmetic on the same series, not a re-fit. The ladder is
      0.5, 1, 2, 3, 5, 10, 20 and it stops at the first multiple that fails.`,
    sv: (r) => r.book?.headroom,
  },
  {
    h: "Standard",
    l: true,
    lead: true,
    cell: (r, _sh, cx) => <td className="l">{edgeCount(r.book?.standard, cx.criteria)}</td>,
    doc: ({ sh }) => `<b>The column this table is ranked on.</b> How many of the <b>six
      acceptance criteria</b> the row cleared — hover the cell for which ones, with each
      target. All six or it is not an edge; nothing here has cleared them. This is the only
      verdict on the page, and it is computed on the book with idle capital earning
      nothing. A row the standard has not scored prints em-dashes rather than
      being dropped, and a sheet with too few folds says <i>cannot tell</i> rather than
      <i>no</i>.${sh.powered === false ? `
      <br><br><b>This sheet is one of those</b>: the book spans ${sh.book_folds
        ? `<b>${sh.book_folds} fold${sh.book_folds === 1 ? "" : "s"}</b>` : "too few folds"}
      against the 20 the threshold was calibrated on, so neither a pass nor a fail in this
      column means anything here. The money columns are unaffected — what the account did
      is a measurement, and only this one needs statistical power it does not have.` : ""}
      <br><br><b>Computed on the BOOK</b>, like every other column here — the six criteria
      are the same and so are their thresholds (<code>metrics.apply_edge_standard</code> is
      shared with the per-asset stage), but they are fed the account's numbers. Four of
      them are columns you can read on this row: <i>ΔSharpe</i>, <i>vs random</i>,
      <i>vs constant</i> and <i>fees</i>. The fifth is the money above the
      volatility-matched basket, and the sixth is the <i>t</i> beside them.
      <br><br><b>Two of the six are not the same statistic they are per asset.</b> ΔSharpe
      here is the difference of two pooled Sharpes, not the mean of per-fold differences;
      and <i>t</i> is a block bootstrap over the book's bars, not a t across folds. Both
      are still measured across <b>time</b> — a book cannot borrow significance from
      breadth, since it is every name at once — but a bootstrap over 5,900 bars is a
      looser test than 54 folds, and rows pass here that do not pass per asset.
      <br><br>It is a <b>coarse</b> key — six integer tiers, and nothing reaches the top
      one — so most of the visible order comes from the tiebreak inside each tier, which is
      <b>${sh.ranked_tiebreak === "book_cm_excess_cagr" ? "book vs B&amp;H"
        : "Sharpe"}</b> on this sheet. Read the tiers as how much evidence a row has and
      the order within one as how much money it made at equal risk; a rule cannot climb a
      tier by earning more.
      <br><br>Buy-and-hold clears none of the six and is drawn where that puts it. It is
      not competing: the six are measured <i>against</i> it, so it cannot pass its own
      test, and the rows above it are the ones with something the standard could score.`,
    sv: (r) => r.book?.standard?.passed,
  },
  /* Reduced from the FULL book sheets, not from the shipped rows — a count derived from
   * the ranked page would show a rule only where it did well, which inverts the question.
   * A raw count on purpose: no composite score. */
  {
    h: "Robustness",
    cell: (r) =>
      r.rob ? (
        <td
          title={`book Sharpe above the same universe held passively in ${r.rob.n} of the ${r.rob.total} (class × timeframe) environments the book stage scored`}
        >
          {r.rob.n}/{r.rob.total}
          <span className="rb">
            <i style={{ width: `${Math.round((100 * r.rob.n) / Math.max(r.rob.total, 1))}%` }} />
          </span>
        </td>
      ) : (
        <Dash />
      ),
    bh: () => <Dash />,
    doc: `In how many of the (class × timeframe) environments the book stage scored this
      rule its Sharpe cleared <b>the same universe held passively</b> — out of the
      environments it was scored on at all, currently up to nine. A raw count,
      deliberately not a composite score: the formula for a "robustness score" would be
      one more free parameter. A rule high on this sheet with a low count here usually
      means one flattering environment; the <b>Robustness</b> section at the bottom of the
      rule's own page draws the full matrix, weak cells included, and every square in it
      opens that environment.`,
    sv: (r) => (r.rob ? r.rob.n : null),
  },
];

/* ------------------------------------------------------------- which columns are drawn
 *
 * EVERY column, by default. The toggle stays, and it HIDES rather than reveals.
 *
 * It defaulted the other way for a while — ten columns, the diagnostics one click behind
 * `adv: true` — on the argument that the full table reads as a spreadsheet. It does read
 * as a spreadsheet, and a spreadsheet is what somebody comparing thirty strategies came
 * here for: the hidden nine are ΔSharpe, t, Expectancy, Win %, ROE/yr, the two
 * signal-free controls and the cost headroom, which is most of the evidence. Collapsing
 * to the key ten is still one click, for a phone or a screenshot.
 *
 * Hiding a column changes nothing about what it MEANS: the doc index is into the full
 * list, so the explanations never renumber.
 */
export const lbCols = (adv: boolean, narrow: boolean): LbCol[] => {
  const cols = adv ? LB_COLS : LB_COLS.filter((c) => !c.adv);
  if (!narrow) return cols;
  const [name, ...rest] = cols;
  return [name, ...rest.filter((c) => c.lead), ...rest.filter((c) => !c.lead)];
};

/* ---------------------------------------------------------- what a click on a header does
 *
 * Re-orders THE ROWS ON THIS PAGE. It does not go back to the sheet and fetch the best
 * rows by the clicked column — the ranking cut and paged the list long before the page
 * saw it. That distinction is the difference between reading a leaderboard and selecting
 * on a test column, which this repo has done once and had to retract, so the note above
 * the table says which of the two you are looking at whenever the order is not the
 * default.
 *
 * Missing is not small. An unscored row, and the benchmark in the columns where it has no
 * comparable value, sink to the bottom in BOTH directions rather than winning an ascending
 * sort with a blank. Ties keep the delivered order they arrived in.
 */
export interface Sort {
  /** Index into the FULL `LB_COLS`, so hiding a column cannot renumber a sort. */
  i: number;
  dir: 1 | -1;
}

export type Entry = { row: BoardRow; bench?: false } | { bench: true; row?: undefined };

export function lbOrder(sh: BoardSheet, benchEdge: EdgeRow | null, sort: Sort | null): Entry[] {
  const rows: Entry[] = sh.rows.map((row) => ({ row }));
  if (!sort) {
    /* The delivered order, with the benchmark spliced in at the rank its own record earns.
     * Where that line falls in the list IS the result.
     *
     * The key is the standard's own count, so the benchmark is placed by that count and
     * not by the tiebreak beneath it. It clears NONE of the six — not because it is bad
     * but because it is the bar the six are measured against — so it sits below every rule
     * that cleared at least one criterion and above every rule that cleared none. That
     * keeps the line monotone with the column the table is sorted on.
     *
     * Under paging this is evaluated per page, which is the honest reading of it: the row
     * marks where holding falls among the rows on screen, and on a page where nothing has
     * dropped to zero criteria yet it sits at the bottom. */
    const below =
      sh.ranked_on === "edge_passed"
        ? sh.rows.findIndex((r) => r.book?.standard != null && r.book.standard.passed <= 0)
        : sh.ranked_on === "book_cm_excess_cagr"
          ? sh.rows.findIndex((r) => r.book?.cm_excess_cagr != null && r.book.cm_excess_cagr < 0)
          : sh.rows.findIndex(
              (r) => r.edge?.sharpe != null && r.edge.sharpe < (benchEdge?.bench_sharpe ?? 0));
    if (benchEdge) rows.splice(below < 0 ? rows.length : below, 0, { bench: true });
    return rows;
  }
  const c = LB_COLS[sort.i];
  if (benchEdge) rows.push({ bench: true });
  const val = (e: Entry) => {
    const v = e.bench ? (c.bsv ? c.bsv(benchEdge, sh) : null) : c.sv(e.row!);
    return v == null || (typeof v === "number" && !isFinite(v)) ? null : v;
  };
  return rows
    .map((e, i) => ({ e, i, v: val(e) }))
    .sort(
      (a, b) =>
        Number(a.v == null) - Number(b.v == null) ||
        (a.v == null
          ? a.i - b.i
          : (typeof a.v === "string"
              ? sort.dir * a.v.localeCompare(b.v as string)
              : sort.dir * ((a.v as number) - (b.v as number))) || a.i - b.i),
    )
    .map((x) => x.e);
}

/* Buy-and-hold, rendered into the ranking at the position the sort key gives it.
 *
 * It is not a candidate and is deliberately absent from `edge_standard.csv` — scoring the
 * benchmark as one of the things being selected would add it to the trial count and let it
 * win its own comparison. But leaving it off the page entirely made the reader hold the
 * benchmark in their head while scanning the rows, which is exactly the arithmetic people
 * get wrong. So it is drawn, from the `bench_*` figures every scored row already carries,
 * and marked as the bar rather than a competitor: no verdict, no link, muted throughout.
 *
 * Only the columns that are genuinely the benchmark's own are filled. `t`, `vs random` and
 * `vs constant` are left blank rather than set to zero: an exposure-matched control at 100%
 * long IS buy-and-hold, so those comparisons are degenerate, not passed. Same for the
 * standard — it is the bar, so it does not clear it.
 */
export function BenchRow({
  bench, cols, sh,
}: {
  bench: EdgeRow | null;
  cols: LbCol[];
  sh: BoardSheet;
}) {
  if (bench == null) return null;
  /* Under the Standard ranking the benchmark has no count to be placed by, so it falls to
   * the bottom of every sheet — nothing has ever cleared zero criteria. Last place on a
   * leaderboard reads as "worst", and on this repo that is the one conclusion the page
   * must not imply by accident: the whole finding is that nothing beats holding. So the
   * row says it is not in the ranking rather than leaving its position to speak. */
  const unranked = sh.ranked_on === "edge_passed";
  return (
    <tr className="bench-row">
      {cols.map((c, i) =>
        i === 0 ? (
          <td className="l" key={i}>
            Buy &amp; hold <span className="chip mut">benchmark</span>{" "}
            {unranked ? (
              <span
                className="chip mut"
                title="The six criteria measure a rule AGAINST buy-and-hold, so the benchmark cannot clear them and has no count to be ranked by. Its position here is not a score."
              >
                not ranked
              </span>
            ) : null}
          </td>
        ) : c.bh ? (
          <BhCell key={i} col={c} bench={bench} sh={sh} />
        ) : (
          <td className="flat" key={i}>
            —
          </td>
        ),
      )}
    </tr>
  );
}

/** A wrapper so each `bh` renderer's `<td>` can carry a key without cloning it. */
function BhCell({ col, bench, sh }: { col: LbCol; bench: EdgeRow | null; sh: BoardSheet }) {
  return col.bh!(bench, sh);
}

/* -------------------------------------------------- the Robustness column's raw material
 *
 * `board_rank.build_sheet` ranks and does not know about the robustness index — it must
 * not, since importing pandas and `stockhunt.resultsdb` and nothing else is what lets the
 * HTTP layer start without a TA-Lib build. The vanilla board gets `rob` attached one level
 * up, by `payload.robustness_index` into the BAKED document, and copies it onto the served
 * board by (class, timeframe, rule). This app has no baked document to copy from, so it
 * reduces `/robust.json` itself — the same definition, from the same file the matrix is
 * drawn from, computed once per load and never per row.
 *
 * The definition is `payload.robustness_index`'s and must not drift from it: environments
 * where the book's Sharpe cleared the same universe held passively, out of the
 * environments the rule was scored on at all. `fields[0]` is `sharpe`; the per-cell arrays
 * are positional, which is why `fields` is read rather than assumed.
 */
export function robCounts(robust: {
  fields?: string[];
  envs?: { key: string; bench?: { sharpe?: number | null } | null }[];
  rules?: Record<string, Record<string, (number | null)[]>>;
}): Record<string, Rob> {
  const si = (robust.fields ?? []).indexOf("sharpe");
  if (si < 0) return {};
  const benchSharpe = new Map<string, number | null | undefined>(
    (robust.envs ?? []).map((e) => [e.key, e.bench?.sharpe]),
  );
  const out: Record<string, Rob> = {};
  for (const [rule, cells] of Object.entries(robust.rules ?? {})) {
    let beat = 0;
    for (const [ek, v] of Object.entries(cells)) {
      const b = benchSharpe.get(ek);
      if (v[si] != null && b != null && v[si]! > b) beat++;
    }
    out[rule] = { n: beat, total: Object.keys(cells).length };
  }
  return out;
}

/* ------------------------------------------------------- the chart selection, and its six
 *
 * Six line colours, and they carry IDENTITY and nothing else — which line is which
 * strategy. Worth stating, because everywhere else on this page colour is a verdict: green
 * cleared the benchmark, red trailed it. So the series palette deliberately contains no
 * green and no red (`--s1`..`--s6` in `app.css`), and buy-and-hold is not in it at all.
 *
 * Six is also `LB_SEL_MAX`, and the two are one number on purpose: the palette was
 * validated for six adjacent slots against both themes, so a seventh line would have to
 * repeat a hue, which is worse than not drawing it.
 */
export const SERIES_COLORS = ["var(--s1)", "var(--s2)", "var(--s3)",
                              "var(--s4)", "var(--s5)", "var(--s6)"];
export const LB_SEL_MAX = 6;
/** Five, not six: buy-and-hold is a line too, and six plus the benchmark is the tangle
 *  `LB_SEL_MAX` exists to prevent. */
export const LB_SEL_SEED = 5;

/** The selection is pinned to ONE sheet. Rows from two sheets sit on different bars,
 *  benchmarks and cost grids, and one chart across them would be the mixed-measurement
 *  bug this page already removed once. `slot` is which of the six colours a rule owns,
 *  held separately from the order it was picked in: keyed on the index instead,
 *  un-ticking the second of four lines would repaint the other three, and a reader who
 *  removed one thing would watch the whole chart change identity. */
export interface Selection {
  cls: string | null;
  tf: string | null;
  rules: string[];
  slot: Record<string, number>;
  /** The seeded five are the page's opening position, not a choice: the floating bar
   *  stays hidden until the reader has actually touched the selection. */
  touched: boolean;
}

export const emptySel = (cls: string, tf: string): Selection => ({
  cls, tf, rules: [], slot: {}, touched: false,
});

export function toggleSel(sel: Selection, cls: string, tf: string, rule: string): Selection {
  const base = sel.cls === cls && sel.tf === tf ? sel : emptySel(cls, tf);
  const rules = [...base.rules];
  const slot = { ...base.slot };
  const i = rules.indexOf(rule);
  if (i >= 0) {
    rules.splice(i, 1);
    delete slot[rule];
  } else if (rules.length < LB_SEL_MAX) {
    // A freed slot is reused by the next pick, which is what keeps six colours enough.
    const taken = new Set(Object.values(slot));
    let k = 0;
    while (taken.has(k)) k++;
    slot[rule] = k;
    rules.push(rule);
  }
  return { cls, tf, rules, slot, touched: true };
}
