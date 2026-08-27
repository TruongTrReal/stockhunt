"use client";

import { cloneElement, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError, board, type Gate, type Group, type SheetRef } from "@/lib/api";
import BoardChart, { CLASS_GROUP } from "@/components/BoardChart";
import { useColumnDocs } from "@/components/ColumnDocs";
import {
  BenchRow, LB_COLS, LB_SEL_MAX, LB_SEL_SEED, SERIES_COLORS,
  emptySel, lbCols, lbOrder, robCounts, toggleSel,
  type BoardRow, type BoardSheet, type CellCtx, type DocCtx,
  type EdgeRow, type Rob, type Selection, type Sort,
} from "@/lib/columns";
import {
  fmtDelta, fmtIR, fmtMoney, fmtNum, fmtPct, fmtRatio, grew, pctOr, pnlDelta, pnlRatio,
  sign, stemName,
} from "@/lib/format";

/* THE RESEARCH LEADERBOARD — the vanilla board's `#/backtest`, ported.
 *
 * Same nineteen columns in the same order, the same summary strip, the same log-scale
 * chart over the table, the same hover-to-explain / click-to-rank headers, the same
 * buy-and-hold row with its "not ranked" chip. What is new here is PAGING, and that is
 * the whole reason this app exists:
 *
 * The old board baked its rows into `data.js`, so depth cost bytes on every load — and 94%
 * of a row's bytes are the asset-by-asset table underneath it, which only the detail page
 * ever draws. ~500 rows a sheet carried that way is 319 MB, and capping at 30 rows was the
 * consequence rather than the intent. Paging the API means one page is one request.
 *
 * `n_ranked` is the last page's index and `n_rules` is NOT: `n_rules` counts the whole
 * candidate population including the rows dropped before ranking — never scored by the
 * standard, no book, never opened a position, closet trackers. Paging on `n_rules` would
 * offer pages that are structurally empty.
 */

const PAGE_SIZE = 50;

/* The pill strip's short labels. The group's own DESCRIPTIVE label — "Top 100 US stocks,
 * point-in-time" — is a different string and comes from `/v1/board/meta`'s `groups`; it is
 * what the Universe stat and the Universe section say, exactly as on the vanilla board. A
 * strip of pills wants the short one. */
const CLASS_LABEL: Record<string, string> = {
  us_stocks: "Top 100 US stocks",
  us_etfs: "ETFs",
  crypto: "Crypto",
  commodities: "Commodities",
  cme_futures: "CME futures",
};

/* Must stay in step with the phone breakpoint in `app.css` — that is where the first
 * column is frozen, and the lead columns are only worth moving because they land against
 * a name that stays put. */
const NARROW = "(max-width:760px)";

export default function ResearchPage() {
  const router = useRouter();
  const [sheets, setSheets] = useState<SheetRef[] | null>(null);
  const [cls, setCls] = useState("us_stocks");
  const [tf, setTf] = useState("1d");
  const [page, setPage] = useState(0);
  const [sheet, setSheet] = useState<BoardSheet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  /* EVERY column, by default: the toggle HIDES rather than reveals. The nine `adv`
   * columns are most of the evidence on a row, and hidden they made the first screenful
   * look thinner than the sheet is. */
  const [adv, setAdv] = useState(true);
  const [sort, setSort] = useState<Sort | null>(null);
  const [query, setQuery] = useState("");
  const [narrow, setNarrow] = useState(false);
  const [criteria, setCriteria] = useState<Gate[]>([]);
  const [metaTfs, setMetaTfs] = useState<string[] | null>(null);
  const [groups, setGroups] = useState<Group[]>([]);
  const [rob, setRob] = useState<Record<string, Rob> | null>(null);
  const [sel, setSel] = useState<Selection>(emptySel("", ""));
  const chartRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    api.sheets().then(setSheets).catch((e) => setError(String(e.message ?? e)));
  }, []);

  /* One request for everything configuration-shaped the board needs before it can draw:
   * the six criteria (the Standard column's tooltip), the timeframe list the filter strip
   * offers, and the tab labels. All three come from the document rather than from literals
   * here, which is the standing rule — `config.GATES` decides the letters and their ORDER,
   * `dash_config.TIMEFRAMES` decides which cells are askable, and `GROUPS` decides what a
   * universe is called. A literal copy of any of them is a second vocabulary that drifts.
   *
   * A failure leaves all three empty: the tooltip loses its criterion names, the strip
   * falls back to the timeframes that have sheets, and the pill label stands in for the
   * group's own. None of that is a wrong number on screen. */
  useEffect(() => {
    board.meta()
      .then((m) => {
        setCriteria(m.edge_criteria ?? []);
        setMetaTfs(m.timeframes ?? null);
        setGroups(m.groups ?? []);
      })
      .catch(() => setCriteria([]));
  }, []);

  /* The Robustness column's counts.
   *
   * `board_rank.build_sheet` ranks and does not know about the robustness index — it must
   * not, since importing pandas and `stockhunt.resultsdb` and nothing else is what lets
   * the HTTP layer start without a TA-Lib build — so the vanilla board gets `rob` attached
   * one level up, into the BAKED document, and copies it onto the served board by (class,
   * timeframe, rule). This app has no baked document, so it reduces `/robust.json` itself
   * with the same definition. Fetched AFTER the board renders and never awaited by it: it
   * is ~830 kB and it fills one column, so nothing else may wait on it. A failure leaves
   * the column printing em-dashes, which is the honest answer for a count nobody has.
   */
  useEffect(() => {
    board.robust().then((r) => setRob(robCounts(r))).catch(() => setRob(null));
  }, []);

  useEffect(() => {
    const mq = window.matchMedia(NARROW);
    const on = () => setNarrow(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    api
      .leaderboard(cls, tf, page * PAGE_SIZE, PAGE_SIZE)
      .then((s) => {
        if (live) setSheet(s as unknown as BoardSheet);
      })
      .catch((e: ApiError) => {
        // A 404 is "this sheet was never scored", which is a real and expected state on a
        // timeframe a class has no run for. It is not an error worth a red box.
        if (live) {
          setSheet(null);
          setError(e.status === 404 ? null : e.message);
        }
      })
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [cls, tf, page]);

  /* A SHEET OPENS WITH ITS TOP FIVE ALREADY DRAWN, and they are the DELIVERED five —
   * `Standard`, ties on `book vs B&H` — not the column the reader has since sorted by.
   * "The top five" has to mean the same five whichever way the table is pointing, or the
   * chart quietly re-picks itself when somebody sorts on a test column, which is the
   * selection-on-a-test-column mistake this repo has made once.
   *
   * Seeded once per SHEET KEY, not once per empty list: that is what makes `Clear` mean
   * clear, since emptying the selection leaves `cls`/`tf` pointing here. Guarded on
   * `offset === 0` as well, because under paging the top of page 4 is not the top of the
   * sheet — and a filter change always resets the page, so the first load of a sheet is
   * always its first page. */
  useEffect(() => {
    if (!sheet || sheet.offset !== 0 || !sheet.rows.length) return;
    setSel((s) => {
      if (s.cls === cls && s.tf === tf) return s;
      const next = emptySel(cls, tf);
      sheet.rows.slice(0, LB_SEL_SEED).forEach((r, i) => {
        next.rules.push(r.rule);
        next.slot[r.rule] = i;
      });
      return next;
    });
  }, [sheet, cls, tf]);

  /* The strip describes the SHEET, so its "best strategy" is the sheet's first row and not
   * whatever happens to top the page being read. Captured on the first page and held
   * across page turns; a filter change resets the page to 0, so it is always captured. */
  const [head, setHead] = useState<{ key: string; best: BoardRow } | null>(null);
  useEffect(() => {
    if (sheet && sheet.offset === 0 && sheet.rows.length)
      setHead({ key: `${cls}|${tf}`, best: sheet.rows[0] });
  }, [sheet, cls, tf]);

  const classes = useMemo(
    () => Array.from(new Set((sheets ?? []).map((s) => s.cls))),
    [sheets],
  );
  /* `dash_config.TIMEFRAMES` through `/v1/board/meta` — the list the build ASKED for
   * sheets on, not the list it got. A timeframe this class has no sheet for still gets a
   * button, which is what leaves the empty state under it reachable: "the verdict stage
   * has not run on this cell" is a fact worth being able to check, and a strip built only
   * from scored sheets can never say it. Anything scored but not on that list is appended
   * rather than dropped, so an old sheet still has a home. */
  const timeframes = useMemo(() => {
    const scored = Array.from(
      new Set((sheets ?? []).filter((s) => s.cls === cls).map((s) => s.tf)));
    if (!metaTfs?.length) return scored;
    return [...metaTfs, ...scored.filter((t) => !metaTfs.includes(t))];
  }, [sheets, cls, metaTfs]);

  const pick = useCallback((next: string, which: "cls" | "tf", current: string) => {
    // Clicking the pill that is already on is not a filter change. A `<select>` fired no
    // event for it; a button does, and without this it would blank the sheet and re-fetch
    // it — a cold sheet is seconds of an empty page, so re-selecting what is on screen
    // would look exactly like breaking it.
    if (next === current) return;
    // A filter change invalidates the page number: page 7 of a 500-row sheet is nowhere on
    // a 60-row one, and landing on an empty page reads as "no results" rather than "you
    // were deep in the last one".
    setPage(0);
    // ...and it invalidates the SHEET, which a page turn does not. Holding the old one
    // through the fetch would leave crypto's rows dimmed under a heading that says stocks,
    // and — worse, because it is not dimmed — an unchanged header quoting the old sheet's
    // candidate count, luck threshold and fold count. Page turns keep their rows on screen
    // precisely because those things do not move between pages of one sheet.
    setSheet(null);
    // A sort is a statement about a column of THIS sheet's rows. Carried across, it would
    // silently re-order the next sheet under a note that says it was ranked.
    setSort(null);
    (which === "cls" ? setCls : setTf)(next);
  }, []);

  const onSort = useCallback((i: number) => {
    // First click puts the best value at the top — descending for every figure here,
    // ascending for the two text columns. Second flips it, third gives the page back the
    // order it was delivered in, which is the only one the note can call a ranking.
    const first: 1 | -1 = LB_COLS[i].text ? 1 : -1;
    setSort((s) =>
      !s || s.i !== i ? { i, dir: first } : s.dir === first ? { i, dir: (-first) as 1 | -1 } : null);
  }, []);

  const cols = useMemo(() => lbCols(adv, narrow), [adv, narrow]);
  const label = CLASS_LABEL[cls] ?? cls;
  /* The tab's own row out of `/v1/board/meta`, keyed on the GROUP key — `stocks`, not
   * `us_stocks` — which is the same translation the curve files need, so it reuses
   * `BoardChart`'s table rather than carrying a second one. `label` is what a universe is,
   * and `n` is how many names it holds, which is the denominator the Universe stat prints
   * `n_assets_scored` against: the sheet's `universe` list is the same set, but the group's
   * own count is what the vanilla board rounds against and the two must not drift. */
  const grp = groups.find((g) => g.key === (CLASS_GROUP[cls] ?? cls));
  const grpLabel = grp?.label ?? label;
  const grpN = grp?.n ?? sheet?.universe.length ?? 0;

  /* Buy-and-hold does not depend on the rule, so its row is taken off the first scored row
   * rather than recomputed — one benchmark for the whole sheet. */
  const benchEdge: EdgeRow | null =
    (sheet?.rows.find((r) => r.edge)?.edge as EdgeRow | undefined) ?? null;

  const docCtx: DocCtx | null = sheet
    ? {
        sh: sheet,
        grp: { label: grpLabel, n: grpN },
        bench: benchEdge?.bench_wealth ?? null,
      }
    : null;
  const { hostRef, secRef, boxRef, panel, thProps } = useColumnDocs(cols, docCtx, onSort);

  const selHere = sel.cls === cls && sel.tf === tf;
  const picked = selHere ? sel.rules : [];
  const colorOf = useCallback(
    (rule: string) => SERIES_COLORS[(sel.slot[rule] || 0) % SERIES_COLORS.length],
    [sel],
  );

  const cellCtx: CellCtx = {
    criteria,
    selected: new Set(picked),
    full: picked.length >= LB_SEL_MAX,
    colorOf,
    onToggle: (rule) => setSel((s) => toggleSel(s, cls, tf, rule)),
  };

  const rows = useMemo(() => {
    if (!sheet) return [];
    // The Robustness count is joined by rule name, exactly as `loadLiveBoard` carries it
    // across on the vanilla board. A rule that reached the board after the last build has
    // no count and prints an em-dash, which is the honest answer for it.
    return rob ? sheet.rows.map((r) => ({ ...r, rob: rob[r.rule] ?? null })) : sheet.rows;
  }, [sheet, rob]);

  const entries = useMemo(() => {
    if (!sheet) return [];
    const q = query.trim().toLowerCase();
    let list = lbOrder({ ...sheet, rows }, benchEdge, sort);
    // The search FILTERS the rows on this page; it cannot reach the rules the ranking cut
    // or the pages either side of this one. The benchmark row stays whatever the query,
    // because it is the bar, not a match.
    if (q) list = list.filter((e) => e.bench || stemName(e.row!.rule).toLowerCase().includes(q));
    return list;
  }, [sheet, rows, benchEdge, sort, query]);

  const shown = entries.filter((e) => !e.bench).length;
  const lastPage = sheet ? Math.max(0, Math.ceil(sheet.n_ranked / PAGE_SIZE) - 1) : 0;
  const best = head && head.key === `${cls}|${tf}` ? head.best : (sheet?.rows[0] ?? null);
  const bb = sheet?.book_bench ?? null;

  /* The basis is NAMED rather than assumed, and BOTH keys are named. It has changed three
   * times — ΔSharpe, raw Sharpe, the book's risk-matched excess — and each time a
   * hardcoded caption survived the change and described the previous one. It is now the
   * standard's count with that excess demoted to the tiebreak, which is a distinction a
   * reader cannot recover from the rows: six integer tiers look like no ordering at all
   * until the caption says what is ordering inside them. */
  const tie = sheet?.ranked_tiebreak === "book_cm_excess_cagr" ? "book vs B&H" : "Sharpe";
  const basis =
    sheet?.ranked_on === "edge_passed"
      ? `ranked on Standard, ties on ${tie}`
      : sheet?.ranked_on === "book_cm_excess_cagr"
        ? "ranked on book vs B&H, risk-matched"
        : "ranked on Sharpe";
  const pickedOn =
    sheet?.ranked_on === "edge_passed"
      ? `Standard, then ${tie}`
      : sheet?.ranked_on === "book_cm_excess_cagr"
        ? "book vs B&H"
        : "Sharpe";
  const by = sort ? LB_COLS[sort.i].h : null;

  return (
    <>
      <div className="hero">
        <h1>Research</h1>
        <p className="lede">
          Every strategy run independently on each asset, walk-forward: parameters re-picked
          on each in-sample window and applied to the next. Scored as information ratio
          against buy-and-hold on the same asset — zero means matching it, positive means
          beating it. Single rules, pairs of rules and the strategies converted from outside
          this catalogue are ranked in one list; only the asset class separates them,
          because only the asset class changes the prices, the costs and the benchmark.
        </p>
      </div>

      {/* Both counts report what the TABLE rests on, not what the sheet knows about.
          The universe list is every symbol; `n_assets_scored` is how many names the scored
          columns actually ran on, and `n_scored` how many strategies carry a verdict.
          Advertising only the larger number made the evidence look broader than it is,
          which is the one direction a header must never round. */}
      {sheet && (
        <div className="strip">
          <div className="stat">
            <span className="k">Universe</span>
            <span className="v">{sheet.n_assets_scored ?? grpN}</span>
            <span className="s">
              {sheet.n_assets_scored != null && sheet.n_assets_scored !== grpN
                ? `scored, of ${grpN} in ${grpLabel}`
                : grpLabel}
            </span>
          </div>
          <div className="stat">
            <span className="k">Out-of-sample</span>
            <span className="v">{sheet.years.toFixed(1)}y</span>
            <span className="s">per asset · {sheet.folds} walk-forward folds</span>
          </div>
          {/* The book's span is a DIFFERENT number and it sits here so the two money
              columns are never read against one shared header. The per-asset figure above
              is each name's own out-of-sample bars — a membership spell — while the book
              runs the whole out-of-sample calendar. That is why the same buy-and-hold
              appears twice on the table at very different sizes. */}
          {bb && (
            <div className="stat">
              <span className="k">Book span</span>
              <span className="v">{fmtNum(bb.years, 1)}y</span>
              <span className="s">{bb.n_names} names held as one account</span>
            </div>
          )}
          <div className="stat">
            <span className="k">Strategies</span>
            <span className="v">{sheet.n_scored ?? sheet.n_rules}</span>
            <span className="s">
              {sheet.n_scored != null && sheet.n_scored !== sheet.n_rules
                ? `scored, of ${sheet.n_rules} ranked`
                : ""}
            </span>
          </div>
          {/* ΔSharpe of the BOOK, not IR across assets. IR compares a part-time rule with a
              full-time benchmark and so pays for exposure; it was the one card here still
              quoting a per-asset statistic above a table that is entirely account-level. */}
          <div className="stat">
            <span className="k">Best strategy</span>
            <span className={`v ${sign(best?.book?.dsharpe)}`}>{fmtIR(best?.book?.dsharpe)}</span>
            <span className="s">{best ? stemName(best.rule) : "—"} · ΔSharpe as a book</span>
          </div>
          <div className="stat">
            <span className="k">Time invested</span>
            <span className="v">{pctOr(best?.book ? best.book.exposure : best?.long_frac)}</span>
            <span className="s">of bars, by the book — read this first</span>
          </div>
          {/* The BOOK where there is one, the median asset only as a fallback. This card
              sits directly above the leaderboard and is the figure a reader quotes, so it
              has to be the account-level one: on the median-asset basis it read an order of
              magnitude below what holding the actual universe returned, and a headline that
              disagrees with the column beneath it by that much is worse than no headline.
              The sub-line names the basis either way. */}
          {best?.book ? (
            <div className="stat">
              <span className="k">$10k became</span>
              <span className="v">{fmtMoney(best.book.wealth)}</span>
              <span className="s">
                as a book, vs {fmtMoney(bb?.wealth)} held
                {best.book.cm_excess_cagr == null
                  ? ""
                  : ` · ${fmtPct(best.book.cm_excess_cagr * 100, 2)}/yr at equal risk`}
              </span>
            </div>
          ) : (
            <div className="stat">
              <span className="k">$10k became</span>
              <span className="v">{fmtMoney(grew(best?.net_pct))}</span>
              <span className="s">
                median asset · vs {fmtMoney(grew(best?.bh_pct))} held ·{" "}
                {fmtDelta(pnlDelta(best?.net_pct, best?.bh_pct))}
                {pnlRatio(best?.net_pct, best?.bh_pct) == null
                  ? ""
                  : " · " + fmtRatio(pnlRatio(best?.net_pct, best?.bh_pct)) + " the profit"}
              </span>
            </div>
          )}
          <div className="stat">
            <span className="k">Luck threshold</span>
            <span className="v">+{sheet.noise_ceiling}</span>
            <span className="s">best of {sheet.n_rules} signal-free controls</span>
          </div>
        </div>
      )}

      {/* The filters sit below the summary rather than under the hero, because the thing
          they switch is the table: reaching for another asset class happens while reading
          the ranking. Pills and not selects (2026-08-27) — a select shows one option and
          hides the rest behind a click, so nothing on the page said that a fifth asset
          class exists or that 15m and 5m are scored, on the one strip whose options a
          reader most needs to see. `.f-group` wraps, so ten buttons reflow. */}
      <div className="filters wide">
        <span className="f-group">
          <span className="f-label">Asset class</span>
          {classes.map((c) => (
            <button
              key={c}
              type="button"
              className={`pill${c === cls ? " on" : ""}`}
              onClick={() => pick(c, "cls", cls)}
            >
              {CLASS_LABEL[c] ?? c}
            </button>
          ))}
        </span>
        <span className="f-group">
          <span className="f-label">Timeframe</span>
          {timeframes.map((t) => (
            <button
              key={t}
              type="button"
              className={`pill${t === tf ? " on" : ""}`}
              onClick={() => pick(t, "tf", tf)}
            >
              {t}
            </button>
          ))}
        </span>
      </div>

      {error && <div className="note">{error}</div>}

      {/* The FIRST load has nothing to dim, so it needs a line of its own. Without it the
          page rendered its heading and then nothing at all until the sheet landed, which
          on a cold sheet is several seconds of a screen that looks finished and empty. */}
      {!error && !sheet && loading && (
        <div className="note busy-note">Ranking {label} at {tf}…</div>
      )}

      {!error && !sheet && !loading && (
        <div className="note">
          No scored sheet for <b>{label}</b> at {tf}. The verdict stage has not run on this
          cell.
        </div>
      )}

      {sheet && (
        <div ref={hostRef}>
          {/* The wrapper exists for one reason: the floating bar's "Show me" scrolls back
              to the picture, and the ticking that fills it happens far down the table. */}
          <div ref={chartRef}>
            <BoardChart
              cls={cls}
              tf={tf}
              picked={picked}
              colorOf={colorOf}
              touched={selHere && sel.touched}
            />
          </div>

          <section className="sec" ref={secRef}>
            <div className="sec-head">
              <h2>Leaderboard</h2>
              <span className="sec-note" id="lb-note">
                rows {sheet.offset + 1}–{sheet.offset + sheet.rows.length} of{" "}
                {sheet.n_ranked.toLocaleString()} · {sheet.n_shown_pairs ?? 0} of them pairs ·{" "}
                {by ? (
                  <>
                    picked on {pickedOn}, re-ordered by {by} — <b>not</b> the best{" "}
                    {sheet.rows.length} by {by}, and only the {sheet.rows.length} rows on
                    this page
                  </>
                ) : (
                  basis
                )}
                {query.trim() ? ` · ${shown} match “${query.trim()}”` : ""}
                {sheet.n_flat_dropped
                  ? ` · ${sheet.n_flat_dropped} that never held anything removed`
                  : ""}
                {sheet.n_closet_dropped
                  ? ` · ${sheet.n_closet_dropped} that were buy-and-hold removed`
                  : ""}{" "}
                · tap a row for its detail
              </span>
            </div>

            <div className="lb-tools">
              <input
                className="search"
                type="search"
                placeholder="Search strategies…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              <button
                className={`pill ${adv ? "on" : ""}`}
                title={
                  adv
                    ? "Collapse to the ten ranking columns — hides ΔSharpe, t, Expectancy, Win %, ROE/yr, the two signal-free controls and the cost headroom"
                    : "Show every metric this sheet holds for these rows"
                }
                onClick={() => {
                  // A sort keyed on a column that is about to vanish would keep ordering
                  // the table with no header carrying the arrow — fall back to the
                  // delivered ranking instead.
                  if (adv && sort && LB_COLS[sort.i]?.adv) setSort(null);
                  setAdv(!adv);
                }}
              >
                {adv ? `All ${LB_COLS.length} columns` : "Key columns"}
              </button>
              <span className="sec-note">tick a name to draw it on the chart above</span>
            </div>

            {/* Where a hovered header answers itself. Absolutely positioned over the top of
                the ranking rather than pushed into the flow above it: a block that opens on
                hover and moves the table down moves the header out from under the cursor,
                which closes it again. */}
            {panel}

            <div className={`tbl-wrap${loading ? " is-busy" : ""}`} aria-busy={loading} ref={boxRef}>
              <table>
                <thead>
                  <tr>
                    {cols.map((c) => {
                      // The doc index is into the FULL list, so hiding a column never
                      // renumbers another one's explanation.
                      const i = LB_COLS.indexOf(c);
                      const on = sort && sort.i === i;
                      const thCls = [
                        c.l ? "l" : "",
                        on && sort!.dir < 0 ? "sort-desc" : "",
                        on && sort!.dir > 0 ? "sort-asc" : "",
                      ].filter(Boolean).join(" ");
                      return (
                        // Every column has a `doc` and a sort value — the type says so,
                        // which is the check the vanilla board made at run time with
                        // `c.doc || c.sv`. A column added without a `doc` is the one
                        // column nobody can ask about, so it may not compile.
                        <th key={c.h} {...thProps(i)} className={thCls || undefined}>
                          {c.h}
                        </th>
                      );
                    })}
                  </tr>
                </thead>
                {/* A row the standard has not scored prints em-dashes rather than being
                    dropped. The sweep universe and the scored universe can differ, and
                    hiding the gap would read as "everything here was judged". */}
                <tbody>
                  {entries.map((e, i) =>
                    e.bench ? (
                      <BenchRow key={`bench-${i}`} bench={benchEdge} cols={cols} sh={sheet} />
                    ) : (
                      <tr
                        key={e.row!.rule}
                        // The label goes in a QUERY parameter, not a path segment: a pair
                        // is `LEG_A~LEG_B|op` and an overlay is `ha:chart:ibs@buy=0.3`, and
                        // those survive a query string untouched.
                        onClick={() =>
                          router.push(
                            `/rule/?cls=${encodeURIComponent(cls)}&tf=${encodeURIComponent(tf)}&rule=${encodeURIComponent(e.row!.rule)}`,
                          )
                        }
                      >
                        {cols.map((c, ci) =>
                          cloneElement(c.cell(e.row!, sheet, cellCtx), { key: ci }),
                        )}
                      </tr>
                    ),
                  )}
                </tbody>
              </table>
            </div>

            {/* THE STATUS LINE READS FROM ONE PLACE AT A TIME, and that is the whole point
                of the `loading` branch. `page` is what was asked for and `sheet.offset` is
                what arrived, so while a fetch is in flight the two disagree — the counter
                said "page 2 of 10" beside "rows 1–50", which reads as a broken pager rather
                than a working one mid-request. Waiting says what it is waiting for and
                names no rows; landed says which rows these are.

                Every control is disabled while a page is in flight. Queueing clicks would
                start fetches whose answers arrive out of order, and the guard that drops a
                stale response would then leave the reader on a page they had clicked past. */}
            <div className="lb-tools" style={{ marginTop: 18 }}>
              <button className="pill" disabled={loading || page === 0} onClick={() => setPage(0)}>
                ‹‹ first
              </button>
              <button
                className="pill"
                disabled={loading || page === 0}
                onClick={() => setPage(page - 1)}
              >
                ‹ prev
              </button>
              {loading ? (
                <span className="sec-note busy-note">
                  loading page {page + 1} of {lastPage + 1}…
                </span>
              ) : (
                <span className="sec-note">
                  rows {sheet.offset + 1}–{sheet.offset + sheet.rows.length} of{" "}
                  {sheet.n_ranked.toLocaleString()} · page {page + 1} of {lastPage + 1}
                </span>
              )}
              <button
                className="pill"
                disabled={loading || page >= lastPage}
                onClick={() => setPage(page + 1)}
              >
                next ›
              </button>
              <button
                className="pill"
                disabled={loading || page >= lastPage}
                onClick={() => setPage(lastPage)}
              >
                last ››
              </button>
            </div>
          </section>

          <section className="sec">
            <div className="sec-head">
              <h2>Universe</h2>
              <span className="sec-note">{grpLabel}</span>
            </div>
            <p className="universe">{sheet.universe.join(" · ")}</p>
          </section>

          {/* The floating bar is HIDDEN until the reader has touched the selection: the
              seeded five are the page's opening position, and a bar floating over the
              ranking to announce a choice nobody made is noise on every visit. It stays a
              floating bar rather than sitting inline because the ticking happens far down
              the table and the chart is at the top. */}
          <div className="cmp-bar" hidden={!picked.length || !sel.touched}>
            <span>
              {picked.length} on the chart
              {picked.length >= LB_SEL_MAX ? " — six is the ceiling" : ""}
            </span>
            <button
              onClick={() =>
                chartRef.current?.scrollIntoView({ behavior: "smooth", block: "center" })
              }
            >
              Show me
            </button>
            <button
              className="quiet"
              onClick={() => setSel({ cls, tf, rules: [], slot: {}, touched: true })}
            >
              Clear
            </button>
          </div>
        </div>
      )}
    </>
  );
}
