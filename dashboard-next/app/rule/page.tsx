"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  api, board, type ApiError, type BoardRow, type RuleDetail, type RuleLogic,
} from "@/lib/api";
import { EquityChart } from "@/components/EquityChart";
import { MetricsTable } from "@/components/MetricsTable";
import { Robustness } from "@/components/Robustness";
import {
  CLASS_LABEL, DASH, edgeCountText, edgeTitle, ensureMeta, fmtCagr, fmtCagrDelta, fmtDelta,
  fmtIR, fmtMoney, fmtNum, grew, isPairLabel, opOf, pctOr, pnlDelta, sign, splitMatched,
  stemName, xcagrOf, xpnlOf, type AssetRowFull, type AssetStats, type BookBench,
  type BookFull, type CurveDetail, type GateDef,
} from "@/lib/rule";

/** The row's book record, widened to the fields the detail page reads. `BookRec` in
 *  `api.ts` is the subset a leaderboard cell needs; this is the same record. */
const bookOf = (r: BoardRow | null) => (r?.book ?? null) as BookFull | null;

/* THE ROUTE IS A QUERY STRING, NOT A PATH SEGMENT, and that is forced rather than chosen.
 *
 * `output: "export"` pre-renders every route at build time, so a dynamic segment needs
 * `generateStaticParams` — which here would mean enumerating ~500 rules across 20 sheets,
 * from an API that requires a session, at build time on a box that may not be able to
 * reach it. A query string is read in the browser and needs no pre-rendering at all, so
 * one static page serves every rule.
 *
 * The label itself is why this matters more than it looks: a pair is `LEG_A~LEG_B|op` and
 * an overlay is `ha:chart:ibs@buy=0.3`. Those survive a query parameter untouched; as a
 * path they meet URL normalisation, and the dashboard has already had one bug from
 * splitting a pair's name on its own operator.
 */

/* ---------------------------------------------------------------- what the rule does
 *
 * The single most common complaint about this page was that it named a strategy and then
 * showed fifteen numbers about it without ever saying what it did.
 *
 * A pair has no entry of its own and never will: `MAXINDEX~MININDEX|or` is not a strategy
 * anyone wrote down, it is two rules and an operator. So it resolves each leg separately
 * and explains the operator, which is the only honest reading of that row. */
const OP_PROSE: Record<string, string> = {
  and: "Long only when BOTH legs are long — the strictest operator, and the one that spends the least time invested.",
  or: "Long when EITHER leg is long. This is the operator that spends the MOST time invested, which on a rising benchmark is most of why `or` rows top equity leaderboards.",
  vote: "Long on the majority of the two legs, ties resolved to flat.",
  gate: "The first leg decides direction; the second may only veto it.",
};

/** The blocks are "Heading\n    indented body". A heading is a short unindented line;
 *  anything else is body. Guessing wrong only costs a bold, never content. */
function RenderedLogic({ text }: { text: string }) {
  const blocks: { head?: string; body: string[] }[] = [];
  for (const raw of String(text).replace(/\r/g, "").split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const isHead = !/^\s/.test(raw) && line.length < 60 && !/[.:]$/.test(line);
    if (isHead) blocks.push({ head: line, body: [] });
    else if (blocks.length) blocks[blocks.length - 1].body.push(line);
    else blocks.push({ body: [line] });
  }
  return (
    <>
      {blocks.map((b, i) => (
        <div key={i}>
          {b.head && <h4 className="logic-h">{b.head}</h4>}
          {b.body.length > 0 && <p className="logic-p">{b.body.join(" ")}</p>}
        </div>
      ))}
    </>
  );
}

function LogicSection({ rule }: { rule: string }) {
  const [L, setL] = useState<RuleLogic | null>(null);

  useEffect(() => {
    let live = true;
    setL(null);
    /* ONE REQUEST, because the resolution moved to the server. `/v1/board/logic/{rule}`
     * falls an overlay back to its stem and reports which key answered as `matched`, and
     * returns a pair as `{op, legs}` rather than making the browser split the label on its
     * own operator and ask three times — which is the shape of thing this board has had a
     * bug from before. A 404 is a NORMAL state (no logic was recorded for that label), so
     * it renders nothing and is never surfaced as an error. */
    board.logic(rule).then((e) => live && setL(e)).catch(() => {});
    return () => { live = false; };
  }, [rule]);

  if (!L) return null;

  /* A PAIR: two rules and an operator, which is not a strategy anyone wrote down. The
   * operator gets its own paragraph because it is most of what the pair does — `or` spends
   * the MOST time invested, which on a rising benchmark is most of why `or` rows top
   * equity leaderboards. */
  if (L.legs?.length) {
    const op = (L.op ?? opOf(rule)).toLowerCase();
    return (
      <section className="sec">
        <div className="sec-head">
          <h2>How it works</h2>
          <span className="sec-note">two rules joined by <code>{op}</code></span>
        </div>
        <div className="logic">
          {OP_PROSE[op] && (
            <p className="logic-p"><b>The operator.</b> {OP_PROSE[op]}</p>
          )}
          {L.legs.map((l) => (
            <div key={l.leg}>
              <h4 className="logic-h">{l.leg}</h4>
              <div className="logic-leg"><RenderedLogic text={l.logic ?? ""} /></div>
            </div>
          ))}
        </div>
      </section>
    );
  }

  if (!L.logic) return null;
  /* WHICH KEY ANSWERED, said out loud wherever it is not the label that was asked for. An
   * overlay (`ha:chart:ibs@buy=0.3`) has no entry of its own and inherits the base rule's,
   * which describes the SIGNAL and not the wrapper round it. Reading the base rule's prose
   * as the variant's own description is exactly the confusion `ha:` exists to guard
   * against — a Heikin-Ashi result and a plain one are not the same measurement. */
  const isStem = L.matched != null && L.matched !== rule;
  const prov = [isStem ? `from ${L.matched}` : null, L.family].filter(Boolean).join(" · ");
  return (
    <section className="sec">
      <div className="sec-head">
        <h2>How it works</h2>
        {prov && <span className="sec-note">{prov}</span>}
      </div>
      <div className="logic">
        {isStem && (
          <p className="logic-p mut">
            This describes <b>{L.matched}</b>, the base rule. <b>{rule}</b> wraps it, and
            what the wrapper changes is not in this text.
          </p>
        )}
        <RenderedLogic text={L.logic} />
        {L.note && <p className="logic-p mut"><b>Note.</b> {L.note}</p>}
      </div>
    </section>
  );
}

/* -------------------------------------------------- the strip: SEVEN FIGURES, ONE BOOK
 *
 * Every tile here is the BOOK — one account holding the whole universe, equal-weighted,
 * rebalanced every bar, point-in-time membership, IDLE CAPITAL EARNING NOTHING — and it is
 * the same record the leaderboard row prints. That is the whole reason it is read off
 * `/v1/research/row` rather than recomputed from anything on this page: the strip, the
 * chart under it and the row on the board are one measurement. This page used to carry two
 * of them side by side, the book and the median asset, with nothing saying which was which.
 *
 * A missing book prints em-dashes and the note under the strip does not render. It does NOT
 * fall back to the per-asset record: that is the median single asset over its own
 * membership spell — a different portfolio, and a silent substitution on exactly the rows
 * where the book run is missing is the mixing this page removed.
 */
function HeroStrip({ row, criteria }: { row: BoardRow; criteria: GateDef[] }) {
  const bk = (row.book ?? {}) as BookFull;
  const bench = (row.book_bench ?? {}) as BookBench;
  const std = bk.standard ?? null;

  return (
    <>
      <div className="strip">
        <div className="stat">
          <span className="k">&Delta;Sharpe</span>
          <span className={`v ${sign(bk.dsharpe)}`}>{fmtIR(bk.dsharpe)}</span>
          <span className="s">book vs the same universe held</span>
        </div>
        <div className="stat">
          <span className="k">Time invested</span>
          <span className="v">{pctOr(bk.exposure)}</span>
          {/* EXPOSURE BEFORE MONEY. A rule in the market 95% of the time is buy-and-hold
              with a rounding error, and every return figure beside it has to be read
              against that first. */}
          <span className="s">
            of bars, by the book
            {bk.exposure != null && bk.exposure > 0.9 && " — this is nearly buy-and-hold"}
          </span>
        </div>
        <div className="stat">
          <span className="k">t-statistic</span>
          <span className={`v ${sign(bk.t)}`}>{bk.t == null ? DASH : bk.t.toFixed(2)}</span>
          {/* ACROSS FOLDS, never across assets, and against the sheet's OWN bar — searching
              ~400 candidates raises it above the 2.0 a single pre-specified test needs, and
              "T failed" beside a printed target of 2.0 and a t of 2.81 is unreadable. */}
          <span className="s">
            across {bk.n_folds || "the"} folds
            {std?.t_bar != null
              ? ` · needs ${fmtNum(std.t_bar, 1)} after multiplicity`
              : " · needs 2.0"}
          </span>
        </div>
        <div className="stat">
          <span className="k">$10k became</span>
          <span className="v">{fmtMoney(bk.wealth)}</span>
          <span className="s">
            {bk.wealth == null
              ? "no book run covers this sheet"
              : `the book · vs ${fmtMoney(bk.bench_wealth)} held · ` +
                `${fmtDelta(bk.wealth - (bk.bench_wealth ?? 0))}`}
          </span>
        </div>
        <div className="stat">
          <span className="k">Return / yr</span>
          <span className="v">{bk.cagr == null ? DASH : fmtCagr(bk.cagr * 100)}</span>
          <span className="s">
            {bk.cagr == null
              ? DASH
              : `the book · holding made ${
                  bench.cagr != null ? fmtCagr(bench.cagr * 100) : DASH}`}
          </span>
        </div>
        <div className="stat">
          <span className="k">Max drawdown</span>
          <span className={`v ${bk.dd == null ? "" : "loss"}`}>
            {bk.dd == null ? DASH : `${fmtNum(bk.dd, 1)}%`}
          </span>
          {/* The benchmark's own fall, which is a SHEET-level figure and not a per-row one:
              189 names fall on different days, so the account falls about half as far as
              its typical member and nobody ever held the median stock. */}
          <span className="s">
            {bench.dd != null
              ? `holding fell ${fmtNum(bench.dd, 1)}%`
              : "worst fall of the account"}
          </span>
        </div>
        <div className="stat">
          <span className="k">Standard</span>
          {/* A COUNT, not a monogram. The six-letter STRCWH version encoded which criteria
              passed in six characters whose only legend was the column header; the count is
              what a reader wants and WHICH six is the tooltip. `underpowered` reads "cannot
              tell", not a fail, and the tooltip leads with that. */}
          {/* `.gates` NESTED inside `.v`, exactly as the vanilla board nests it: `.v` owns
              the 26px mono figure and `.gates` owns the colour, and merging the two classes
              onto one span would have `.gates`'s muted grey win the cascade over the tile's
              own ink for every row that passed. */}
          <span className="v">
            <span
              className={`gates${std?.verdict === "PASS" ? "" : " none"}`}
              title={edgeTitle(std, criteria)}
            >
              {edgeCountText(std)}
            </span>
          </span>
          <span className="s">criteria cleared on the book — hover for which</span>
        </div>
      </div>

      {bk.wealth != null && (
        /* The book/per-asset distinction decides how every figure above is read, so it
         * stays — as a line, with the reasoning on hover. It used to be a paragraph. */
        <p className="sec-note">
          <span
            className="explains"
            title={"One account holding every name at once, idle capital earning nothing — " +
                   "the same account the chart draws and the same numbers as this rule's " +
                   "row on the leaderboard. Breadth and the per-name table below are per " +
                   "asset by construction: a book has no breadth, it has one equity curve."}
          >
            every figure above is <b>the book</b>
          </span>
          {" "}· {bk.n_names ?? row.asset_n ?? DASH} names · {fmtNum(bk.years, 1)}y
          out-of-sample
          {row.asset_pos != null && row.asset_n
            ? ` · ${row.asset_pos} of ${row.asset_n} names positive`
            : ""}
        </p>
      )}
    </>
  );
}

/* ------------------------------------------------------------------------- the page */

function RuleView() {
  const q = useSearchParams();
  const cls = q.get("cls") ?? "";
  const tf = q.get("tf") ?? "";
  const rule = q.get("rule") ?? "";

  const [curve, setCurve] = useState<CurveDetail | null>(null);
  const [detail, setDetail] = useState<RuleDetail | null>(null);
  const [row, setRow] = useState<BoardRow | null>(null);
  const [curveErr, setCurveErr] = useState<string | null>(null);
  const [detailErr, setDetailErr] = useState<string | null>(null);
  const [criteria, setCriteria] = useState<GateDef[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // The six gate definitions, for the `Standard` tooltip. Shared with the robustness
    // matrix through one cached fetch, because both halves of this page want the document.
    let live = true;
    ensureMeta().then((m) =>
      live && setCriteria(((m?.edge_criteria ?? []) as unknown) as GateDef[]));
    return () => { live = false; };
  }, []);

  useEffect(() => {
    if (!cls || !tf || !rule) return;
    let live = true;
    setLoading(true);
    setCurve(null); setDetail(null); setRow(null);
    setCurveErr(null); setDetailErr(null);
    /* Three fetches, in parallel, failing INDEPENDENTLY — which is the whole reason this is
     * `allSettled` and not `all`. Each absence is a real and different state:
     *
     *   - curve, no rows: a PAIR records leg-correlation diagnostics rather than per-symbol
     *     backtests, so `combo_wf.py` never wrote a per-name table for it.
     *   - rows, no curve: the sheet's book run was done without `--curves`, or the JSON is
     *     older than the CSV beside it.
     *   - a 404 on the ROW alone: the label is not RANKED on this sheet, which is not the
     *     same as unknown. The book stage scored ~400 rules per sheet and a leaderboard
     *     ships thirty, so most cells a robustness matrix links to land here — with a book,
     *     a curve and often a per-name table, but no six-criteria verdict and no strip.
     *
     * Treating any one of those as a failed page would hide the parts that did arrive.
     *
     * The per-asset table is read off `api.rule()` even though `/row` now carries
     * `per_asset` too: that route answers for an OFF-BOARD rule where `/row` 404s, so it is
     * the one source that covers both cases with one code path. */
    Promise.allSettled([
      api.curve(cls, tf, rule),
      api.rule(cls, tf, rule),
      api.row(cls, tf, rule),
    ]).then(([c, d, r]) => {
      if (!live) return;
      if (c.status === "fulfilled") setCurve(c.value as CurveDetail);
      else setCurveErr((c.reason as ApiError)?.message ?? "no curve");
      if (d.status === "fulfilled") setDetail(d.value);
      else setDetailErr((d.reason as ApiError)?.message ?? "no per-asset rows");
      // A rejected row needs no message of its own: "not ranked here" is already the
      // sentence the off-board reading below prints, and a second copy of it beside the
      // strip would be the same fact stated twice.
      if (r.status === "fulfilled") setRow(r.value);
      setLoading(false);
    });
    return () => { live = false; };
  }, [cls, tf, rule]);

  if (!cls || !tf || !rule) {
    return <div className="note">No rule named. Open one from the leaderboard.</div>;
  }

  /* THE ROW'S OWN `kind` where there is a row, and the LABEL where there is not. An
   * off-board rule has no row to ask, and a pair reached through a robustness matrix is
   * exactly that case, so the label rule still has to work: `~` is the `combo_wf.py`
   * grammar and appears in no single, overlay or published-strategy name. Reading it off
   * the label rather than off a query parameter is also what stops a pasted URL lying
   * about which kind of page this is. */
  const isPair = row ? row.kind === "pair" : isPairLabel(rule);
  const op = row?.op && row.op !== "nan" && row.op !== "None" ? row.op : opOf(rule);
  const rows = (detail?.rows ?? []) as AssetRowFull[];
  const stats = (detail?.stats ?? {}) as AssetStats;
  const hasAssetTable = rows.length > 0;

  const { mm, drawn, all } = splitMatched(curve);
  const side = curve?.side ?? "long";

  /* How far apart the out-of-sample spans are, MEASURED rather than asserted. The caption
   * warns that a money ranking is partly a ranking of holding period, and on a sheet where
   * every name ran the same length that warning is simply untrue. */
  const spans = rows.map((p) => p.years).filter((v): v is number => v != null && v > 0);
  const spanRatio = spans.length ? Math.max(...spans) / Math.min(...spans) : 1;

  const wins = stats.pos ?? null;
  const nAssets = stats.n ?? null;

  /* THE SENTENCE THAT POINTS AT WHAT COMES NEXT, and there are three of them because there
   * are three different absences. A pair and an off-board single both reach the chart with
   * no per-asset rows, and the pair's reason — leg diagnostics instead of per-symbol
   * backtests — is untrue of the other. Two absences, two reasons, two sentences. */
  /* The per-name pointer, as a clause rather than a paragraph. Both ABSENCES still get
   * named, because "no table below" is a fact about what this sheet ran and not an
   * omission — but each is one phrase now, with the reason on hover. */
  const tail = hasAssetTable ? (
    <> · per name below</>
  ) : isPair ? (
    <>
      {" "}·{" "}
      <span
        className="explains"
        title={"A pair has no per-name table — `combo_wf.py` records leg-correlation " +
               "diagnostics instead of per-symbol rows — so breadth is all this sheet " +
               "knows about where it worked."}
      >
        no per-name table
      </span>
    </>
  ) : (
    <>
      {" "}·{" "}
      <span
        className="explains"
        title={"This rule is not on this sheet's ranked board. The book stage records the " +
               "account and its curve; the per-symbol backtests come from a stage that was " +
               "never run here."}
      >
        not on the ranked board
      </span>
    </>
  );

  return (
    <>
      <Link className="back" href="/">← research</Link>

      <div className="hero">
        <div className="d-head">
          <span className="d-name">{stemName(rule)}</span>
          {op && <span className="chip mut">{op}</span>}
          <span className="chip mut">{tf}</span>
          <span className="chip mut">{CLASS_LABEL[cls] ?? cls}</span>
        </div>
        {/* The lede names the FOLD COUNT and the book's span, and both are facts about the
            sheet rather than about the rule — they arrive with the row. Without a row they
            drop out rather than being guessed: an off-board rule was never scored by the
            stage that counts folds. */}
        <p className="lede">
          {isPair
            ? <>Two rules joined by <code>{op}</code>, walked forward</>
            : "Walk-forward"}{" "}
          out-of-sample
          {row?.folds != null && `, ${row.folds} folds`}, held as one book of{" "}
          {bookOf(row)?.n_names ?? curve?.n_assets ?? nAssets ?? DASH} names
          {bookOf(row)?.years != null && (
            <> over <b>{fmtNum(bookOf(row)?.years, 1)} years</b></>
          )}
          , equal-weighted and rebalanced every bar, idle capital earning nothing.
          {hasAssetTable && row?.years != null
            ? ` Each name is also scored on its own below, over its own ${
                fmtNum(row.years, 1)}-year median spell.`
            : hasAssetTable && " Each name is also scored on its own below."}
        </p>
      </div>

      {/* THE SEVEN-TILE STRIP, and only where the rule is RANKED on this sheet. An
          off-board rule has no six-criteria verdict and no fold statistics — none of it is
          computed for a rule the standard never scored — and rendering an empty strip would
          imply the measurement exists somewhere. The note further down says so in words. */}
      {row && <HeroStrip row={row} criteria={criteria} />}

      {loading && <div className="note busy-note">Loading {stemName(rule)}…</div>}

      {/* Moved DOWN, out of the position between the strip and the chart. What a rule does
          is worth reading, and it is prose — several hundred words of it — which is the
          worst possible thing to put between a reader and the picture they came for. It
          sits beside the robustness matrix instead, where the two questions it belongs
          with are: what does this do, and where else does it hold. */}

      {/* TWO COLUMNS THAT FLOW INDEPENDENTLY, not two rows of two.
       *
       * Paired in rows, the taller half of each pair sets the row height and the shorter
       * one leaves a hole under it: the metrics table is twelve rows and the chart is
       * fixed-height, so pairing them put a screen of dead space beside the matrix. As
       * columns, each side packs on its own.
       *
       * Left is the PICTURE half — the curve, then where it holds up. Right is the NUMBERS
       * half — the same curve's metrics, then what the rule actually does. Each row of the
       * reading is beside its own evidence. See `.d-split` in `app/busy.css`. */}
      <div className="d-split">
      <div className="d-col">
      {curve && (
        <section className="sec">
          <div className="sec-head">
            <h2>Cumulative P&amp;L</h2>
            <span className="sec-note">
              the book &mdash; one account, {curve.n_assets ?? DASH} names, equal-weight
              {curve.pit ? ", point-in-time members only" : ""}
              {drawn.length > 0 && (
                <> &middot; vs {drawn.map((l) => l.label).join(" and ")} <b>at equal risk</b></>
              )}
              {" "}&middot; <b>{side === "short" ? "long/short" : "long/flat"}</b>
            </span>
          </div>

          {side === "short" && (
            <div className="note">
              <b>This is the long/short version of the rule.</b> &quot;Stay out&quot; is
              turned into &quot;sell it&quot; (<code>2p−1</code>), so it is in the market on
              every bar and pays borrow on the short leg. That is the side scored for this
              rule, and the chart has to show the same strategy the verdict was computed on.
              The long/flat version is a different strategy with a different exposure and is
              not what the leaderboard row reports.
            </div>
          )}

          {/* `ruleLabel` is the FULL label, not the stem: the legend has to name the
              strategy the chart is of, and a pair's operator is part of which strategy
              that is. The hero prints the stem because the operator is a chip there. */}
          <EquityChart
            curve={curve.curve}
            drawn={drawn}
            dates={curve.dates}
            ruleLabel={rule}
            mm={mm}
            tail={tail}
          />
        </section>
      )}

      {curveErr && !curve && (
        /* Named separately from the per-asset absence, because they are different facts.
           The curve file is written by the same run that scores the book, so a rule with a
           book column and no curve means the JSON is older than the CSV beside it. */
        <div className="note">
          No equity curve is stored for <b>{stemName(rule)}</b> on {CLASS_LABEL[cls] ?? cls}{" "}
          at {tf}. The book run for this sheet was done without <code>--curves</code>, or is
          older than the sheet beside it. ({curveErr})
        </div>
      )}

      <Robustness rule={rule} cls={cls} tf={tf} />
      </div>

      <div className="d-col">
      {curve && (
        <MetricsTable
          metrics={curve.metrics}
          lines={all}
          assetN={nAssets}
          hasAssetTable={hasAssetTable}
        />
      )}
      <LogicSection rule={rule} />
      </div>
      </div>

      {/* ROBUSTNESS SITS ABOVE THE PER-NAME TABLE, and the order is the argument. See the
          header comment in `components/Robustness.tsx`. */}


      {hasAssetTable ? (
        <AssetTable
          rows={rows}
          stats={stats}
          spanRatio={spanRatio}
          wins={wins}
          nAssets={nAssets}
        />
      ) : isPair ? (
        <div className="note">
          <b>Pairs have no asset-by-asset page.</b> The pair sweep records leg-correlation
          diagnostics rather than per-symbol rows, so breadth is the whole of what this
          sheet knows about where it worked and where it did not. Its two legs each have
          their own page and were each ranked on this same leaderboard.
        </div>
      ) : (
        <div className="note">
          {/* A DIFFERENT ABSENCE FROM THE PAIR'S, and it must not borrow that sentence:
              nothing about this rule prevents a per-symbol table, the stage that builds one
              was simply never run on this cell. */}
          <b>No asset-by-asset table for this cell.</b> This rule is not on{" "}
          {CLASS_LABEL[cls] ?? cls} at {tf}&apos;s ranked board: the book stage scored the
          account and its curve, but the per-symbol backtests come from a stage that was
          never run here. There is no six-criteria verdict for it either, and rendering an
          empty one would imply the measurement exists somewhere.
          {detailErr && <> ({detailErr})</>}
        </div>
      )}
    </>
  );
}

/* THE WHOLE UNIVERSE, sorted on `P&L vs B&H`, and NOTHING IS CUT.
 *
 * It used to ship the best 10 and worst 5, which made the table a selection: the middle
 * invisible, the ends needing a span floor to stop a two-month name winning on a rate, and
 * a caption that had to keep saying "not a sample you can average". No selection, no floor,
 * no such caption.
 *
 * THE SORT KEY IS A VISIBLE COLUMN. This ranked on `net_pct` once — the strategy's own
 * terminal wealth — under a header that said "by P&L" beside a `P&L vs B&H` column that was
 * a different number. `xpnl` is the money gap in points and `$10k became − buy & hold` is a
 * strictly increasing function of it, so the column the header names is the column the
 * order is on. Change the key and the header in the same commit.
 */
function AssetTable({ rows, stats, spanRatio, wins, nAssets }: {
  rows: AssetRowFull[];
  stats: AssetStats;
  spanRatio: number;
  wins: number | null;
  nAssets: number | null;
}) {
  // Nulls sink, so a name with nothing to compare cannot win the sort by being blank. The
  // API already ranks them this way; re-applying it here keeps the page's order true to
  // its own header rather than to whatever order the rows arrived in.
  const sorted = [...rows].sort((a, b) => {
    const xa = xpnlOf(a);
    const xb = xpnlOf(b);
    return Number(xa == null) - Number(xb == null) || (xb ?? 0) - (xa ?? 0);
  });
  const unranked = stats.unranked ?? 0;

  return (
    <section className="sec">
      <div className="sec-head">
        <h2>Asset by asset</h2>
        <span className="sec-note">
          all {sorted.length} name{sorted.length === 1 ? "" : "s"}, ranked by P&amp;L vs buy
          &amp; hold
        </span>
      </div>
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th className="l">Asset</th>
              <th>Years</th>
              <th>IR vs buy &amp; hold</th>
              <th>$10k became</th>
              <th>Buy &amp; hold</th>
              <th>P&amp;L vs B&amp;H</th>
              <th>CAGR</th>
              <th>B&amp;H CAGR</th>
              <th>vs B&amp;H / yr</th>
              <th className="l">Verdict</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((p) => {
              const delta = pnlDelta(p.net_pct, p.bh_pct);
              return (
                <tr key={p.symbol}>
                  <td className="l">{p.symbol}</td>
                  <td>{p.years == null ? DASH : p.years.toFixed(1)}</td>
                  <td className={sign(p.ir)}>{fmtIR(p.ir)}</td>
                  <td>{fmtMoney(grew(p.net_pct))}</td>
                  <td>{fmtMoney(grew(p.bh_pct))}</td>
                  <td className={sign(delta)}>{fmtDelta(delta)}</td>
                  <td>{fmtCagr(p.net_cagr)}</td>
                  <td>{fmtCagr(p.bh_cagr)}</td>
                  <td className={sign(xcagrOf(p))}>{fmtCagrDelta(xcagrOf(p))}</td>
                  <td className="l">
                    <span className={`chip ${p.ir != null && p.ir > 0 ? "run" : "halt"}`}>
                      {p.ir != null && p.ir > 0 ? "beat" : "lost"}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
          <caption>
            Sorted by <b>P&amp;L vs B&amp;H</b> — what $10,000 under this rule finished
            with, minus what the same $10,000 held finished with — best to worst.{" "}
            <b>Every name the rule was run on is here</b>, so this is the whole population
            and not a selection: what you count in it is the rule&apos;s own, and nothing has
            been set aside.
            {unranked > 0 && (
              <>
                {" "}{unranked} of them {unranked === 1 ? "has" : "have"} no return on one
                side of the comparison and {unranked === 1 ? "sits" : "sit"} at the bottom
                printing em-dashes.
              </>
            )}
            {/* ORDERING ON MONEY IS PARTLY ORDERING ON HOLDING PERIOD, and this is where the
                page says so. `years` differs by asset by a factor of twenty on some sheets,
                so a name held four decades outranks a recent one at a far worse annual rate.
                That is why `vs B&H / yr` stays on the row and why the caption points at
                `Years` — and why the warning is gated on the measured ratio, since on a
                sheet where every name ran the same length it would be untrue. */}
            {spanRatio > 1.5 && (
              <>
                {" "}Read the order with <b>Years</b> in view: span differs by asset — by a
                factor of {spanRatio.toFixed(0)} on this sheet — so a long-held name can
                out-earn a short-held one at a far worse annual rate. That is why{" "}
                <b>vs B&amp;H / yr</b> is on the row, and why it will not agree with this
                ranking.
              </>
            )}
            {wins != null && nAssets ? (
              <>
                {" "}The breadth gate asks for 70% of assets positive; this rule manages{" "}
                {pctOr(wins / nAssets)}.
              </>
            ) : null}
            {" "}Verdict follows the IR, so it can disagree with the money in <b>both</b>{" "}
            directions: an asset can out-earn buy-and-hold and still read &ldquo;lost&rdquo;
            for the risk it took, and one can earn <i>less</i> and read &ldquo;beat&rdquo;
            for taking much less risk to get there. Positions are unlevered — 1x, cash when
            flat — so the money columns are what the capital itself earned.
          </caption>
        </table>
      </div>
    </section>
  );
}

export default function RulePage() {
  // `useSearchParams` suspends during prerender, and a static export prerenders every
  // route. Without this boundary the build fails outright rather than at runtime.
  return (
    <Suspense fallback={<div className="note">Loading…</div>}>
      <RuleView />
    </Suspense>
  );
}
