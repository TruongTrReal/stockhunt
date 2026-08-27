"use client";

/* DOES IT GENERALISE — the same rule on every sheet the book stage scored, including the
 * ones it lost on.
 *
 * It is a SECTION on the strategy's own page, and it sits ABOVE the per-name table. That
 * order is the argument: both sections answer "where else does this hold up" on different
 * axes — the matrix across asset classes and timeframes, the table across the names inside
 * this one sheet — and the matrix is the wider question and by far the cheaper read,
 * twenty-five squares against several hundred rows. A reader who stops after one section
 * should have stopped after that one. It is also the only section on the page that
 * navigates anywhere, and underneath the longest table on the page the exits were the
 * hardest thing on it to find.
 *
 * It was a third tab on the research page for a while, and as a tab it asked the reader to
 * pick a strategy twice — once on the leaderboard to find it, again from a dropdown over
 * there — and drew a matrix about one rule on a page that was about none.
 *
 * The index is FETCHED, not baked: `payload.robustness_index` cuts it from the FULL
 * `book_*.csv` sheets — ~400 rules across the environments — because a matrix built from
 * the shipped rows would show a rule only where it ranked well, and its weak environments
 * would vanish, which inverts the question. At ~830 kB it is read by exactly one view, so
 * `ensureRobust()` fetches it the first time a detail page needs it and holds it after.
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, type RobustEnv } from "@/lib/api";
import {
  CLASS_ARG, CLASS_LABEL, DASH, GROUP_KEY, ROB_METRICS, ensureMeta, ensureRobust, fmtNum,
  ruleHref, stemName, type RobustIndex,
} from "@/lib/rule";

/* WHICH FILL THE MATRIX IS DRAWN ON, and it is the one control here that changes what the
 * numbers MEAN rather than which of them is shown.
 *
 * `close` is the published convention: the signal is computed from a bar's own high, low
 * and close and then transacted at that same close — a price nobody knew when the decision
 * was made. Every other page here treats that as an optimistic bound. In a matrix it is
 * worse than optimistic, because the bias is per-bar and comparing timeframes is the whole
 * point of the view: `ibs` on commodities reads 5.5%/yr at 1d over 6,511 bars and
 * 1,970%/yr at 15m over 75,909, same instruments and same period. A matrix on close fill
 * alone says every reversion rule is more robust the finer you slice, which is an artifact
 * of counting, not a property of the rule.
 *
 * `open` fills at the next bar's open instead. It is not the truth either — it charges a
 * full session of delay a market-on-close order would not pay — so the honest reading is
 * the RANGE, which is why the summary prints both counts side by side whatever this is set
 * to, and why the default is `open`: on a question about generalisation, the bound that
 * cannot be inflated by slicing is the safer one to land on.
 *
 * Both selectors live at MODULE scope, so a reader who switched fill and then walked into
 * another environment is still looking at the fill they chose. That is the same contract
 * `robFill` has in the vanilla board, where `offBoardDetail` reads it explicitly. */
let robFill: "open" | "close" = "open";
let robMetric = "sharpe";

/** The three things a single square needs out of the index: its row, and how to read the
 *  two positional fields the tint is computed from. */
interface CellView {
  cells: Record<string, (number | null)[]>;
  iS: number;
  iT: number;
  benchOf: (e: RobustEnv) => number | null;
}

export interface RobustnessProps {
  rule: string;
  /** The CLASS ARG (`us_stocks`), which is what the routes take. The index's envs carry
   *  the group key (`stocks`), so one of the two maps is applied on every comparison. */
  cls: string;
  tf: string;
}

export function Robustness({ rule, cls, tf }: RobustnessProps) {
  const [R, setR] = useState<RobustIndex | null>(null);
  const [ready, setReady] = useState(false);
  const [timeframes, setTimeframes] = useState<string[] | null>(null);
  /** Which (class, timeframe) cells have a ranked sheet at all — see `Cell`. */
  const [ranked, setRanked] = useState<Set<string> | null>(null);
  const [fill, setFill] = useState(robFill);
  const [metric, setMetric] = useState(robMetric);

  useEffect(() => {
    let live = true;
    ensureRobust().then((j) => {
      if (!live) return;
      setR(j);
      setReady(true);
    });
    // The timeframe AXIS is `dash_config.TIMEFRAMES` through the board document, never a
    // literal: a timeframe the research gains appears here the moment its sheets do.
    // Both of these are small and both degrade to a fallback, so neither is awaited.
    ensureMeta().then((m) => live && setTimeframes(m?.timeframes ?? null));
    api.sheets()
      .then((s) => live && setRanked(new Set(s.map((x) => `${x.cls}_${x.tf}`))))
      .catch(() => {});
    return () => { live = false; };
  }, []);

  const markKey = `${GROUP_KEY[cls] ?? cls}_${tf}`;

  const view = useMemo(() => {
    if (!R?.envs?.length) return null;
    /* THE SECTION IS DRAWN IF EITHER FILL KNOWS THE RULE, and the selected fill may then
     * be empty. That is not the same as "the index does not cover this rule": `5m` was run
     * at `open` ONLY, on purpose, so a rule scored there is absent from `rules` entirely
     * and gating on the selected map alone would tell the reader it was never scored
     * anywhere. Instead every square prints an em-dash whose tooltip says to switch the
     * fill selector — an absence the reader can act on. */
    if (!R.rules?.[rule] && !R.open?.[rule]) return null;
    const cells = ((fill === "open" && R.open ? R.open : R.rules) ?? {})[rule] ?? {};

    const iS = R.fields.indexOf("sharpe");
    const iT = R.fields.indexOf("n_trades");
    const benchOf = (e: RobustEnv) =>
      fill === "open" && e.bench_open?.sharpe != null
        ? e.bench_open.sharpe : e.bench?.sharpe ?? null;

    const scored = R.envs.filter((e) => cells[e.key]);
    // "Never traded" is not "never scored", and the strip distinguishes them: a book that
    // opened no position has nothing to score, which is a different fact from a cell the
    // stage never reached.
    const active = scored.filter((e) => cells[e.key][iT] !== 0);

    const sharpes = active
      .map((e) => ({ e, s: cells[e.key][iS] }))
      .filter((x) => x.s != null)
      .sort((a, b) => (a.s as number) - (b.s as number));
    const beat = active.filter((e) => {
      const v = cells[e.key][iS];
      const bs = benchOf(e);
      return v != null && bs != null && v > bs;
    });

    /* Counted on BOTH fills whatever the matrix is showing, because the gap between them
     * is the finding on several rules rather than a footnote to it: a rule can clear
     * holding in 8 of 20 environments at the published fill and 5 of 20 once it can no
     * longer transact at a close it peeked at. Printing one would mean printing it
     * without the other. */
    const countOn = (src: Record<string, Record<string, (number | null)[]>> | undefined,
                     isOpen: boolean) => {
      const c = (src ?? {})[rule] ?? {};
      const envs = R.envs.filter((e) => c[e.key] && c[e.key][iT] !== 0);
      const n = envs.filter((e) => {
        const v = c[e.key][iS];
        const bs = isOpen && e.bench_open?.sharpe != null
          ? e.bench_open.sharpe : e.bench?.sharpe ?? null;
        return v != null && bs != null && v > bs;
      }).length;
      return { n, total: envs.length };
    };
    const cClose = countOn(R.rules, false);
    const cOpen = countOn(R.open, true);

    const med = sharpes.length
      ? sharpes.length % 2
        ? (sharpes[(sharpes.length - 1) / 2].s as number)
        : ((sharpes[sharpes.length / 2 - 1].s as number)
           + (sharpes[sharpes.length / 2].s as number)) / 2
      : null;

    // Row order and column order both come off the index, so a class or a timeframe the
    // book stage gains appears without being named anywhere in this file.
    const classes = Array.from(new Set(R.envs.map((e) => e.cls)));
    const tfs = timeframes?.length
      ? timeframes : Array.from(new Set(R.envs.map((e) => e.tf)));

    return { cells, iS, iT, benchOf, scored, active, beat, cClose, cOpen, med,
             worst: sharpes[0], best: sharpes[sharpes.length - 1], classes, tfs };
  }, [R, rule, fill, timeframes]);

  const envName = (e: RobustEnv) => `${CLASS_LABEL[e.cls] ?? e.cls} ${e.tf}`;

  return (
    <section className="sec">
      <div className="sec-head">
        <h2>Robustness</h2>
        <span className="sec-note">
          does it generalise — the same rule on every sheet the book stage scored,
          including the ones it lost on
        </span>
      </div>

      {!ready && <p className="sec-note">Loading the robustness index…</p>}

      {ready && !view && (
        <p className="sec-note">
          The robustness index does not cover {stemName(rule)}
          {R ? "" : " — it could not be loaded"}.
        </p>
      )}

      {ready && view && R && (
        <>
          <div className="filters">
            <span className="f-group">
              <span className="f-label">Fill</span>
              {/* `.fsel` and not pills: the two genuinely are dropdowns, and they are the
                  only two on the board that stayed selects when the filter strips went
                  back to pills. */}
              <select
                className="fsel"
                value={fill}
                onChange={(e) => {
                  const v = e.target.value as "open" | "close";
                  robFill = v;
                  setFill(v);
                }}
              >
                <option value="open">Open (pessimistic)</option>
                <option value="close">Close (published)</option>
              </select>
            </span>
            <span className="f-group">
              <span className="f-label">Metric</span>
              <select
                className="fsel"
                value={metric}
                onChange={(e) => {
                  robMetric = e.target.value;
                  setMetric(e.target.value);
                }}
              >
                {Object.entries(ROB_METRICS).map(([k, [label]]) => (
                  <option key={k} value={k}>{label}</option>
                ))}
              </select>
            </span>
          </div>

          <div className="strip">
            <div className="stat">
              <span className="k">Environments</span>
              <span className="v">{view.scored.length}</span>
              <span className="s">
                of {R.envs.length} scored · {view.scored.length - view.active.length} never
                traded
              </span>
            </div>
            <div className="stat">
              <span className="k">Beat holding</span>
              <span className={`v ${view.beat.length ? "" : "loss"}`}>
                {view.beat.length} / {view.active.length}
              </span>
              {/* A RAW COUNT and never a composite score — environments where the book's
                  Sharpe cleared the same universe held passively. The matrix tint carries
                  that same single meaning whatever metric is displayed. */}
              <span className="s">
                book Sharpe above the passive universe — a raw count, not a score
                {view.cOpen.total > 0 && view.cClose.total > 0 && (
                  <>
                    {" · "}
                    <b>{view.cOpen.n}/{view.cOpen.total}</b> at the pessimistic fill,{" "}
                    {view.cClose.n}/{view.cClose.total} at the published one
                  </>
                )}
              </span>
            </div>
            <div className="stat">
              <span className="k">Median Sharpe</span>
              <span className="v">{fmtNum(view.med, 2)}</span>
              <span className="s">across the traded environments</span>
            </div>
            <div className="stat">
              <span className="k">Worst</span>
              <span className={`v ${view.worst && (view.worst.s as number) < 0 ? "loss" : ""}`}>
                {view.worst ? fmtNum(view.worst.s, 2) : DASH}
              </span>
              <span className="s">
                {view.worst ? `${envName(view.worst.e)} — inspect this one first` : DASH}
              </span>
            </div>
            <div className="stat">
              <span className="k">Best</span>
              <span className="v">{view.best ? fmtNum(view.best.s, 2) : DASH}</span>
              <span className="s">
                {view.best ? `${envName(view.best.e)} — the one a leaderboard shows` : DASH}
              </span>
            </div>
          </div>

          <div className="tbl-wrap">
            <table className="mx">
              <thead>
                <tr>
                  <th className="l">{ROB_METRICS[metric][0]}</th>
                  {view.tfs.map((t) => <th key={t}>{t}</th>)}
                </tr>
              </thead>
              <tbody>
                {view.classes.map((ck) => (
                  <tr key={ck}>
                    <td className="l">{CLASS_LABEL[ck] ?? ck}</td>
                    {view.tfs.map((t) => (
                      <Cell
                        key={t}
                        env={R.envs.find((e) => e.cls === ck && e.tf === t)}
                        R={R}
                        rule={rule}
                        fill={fill}
                        metric={metric}
                        view={view}
                        mark={markKey}
                        ranked={ranked}
                      />
                    ))}
                  </tr>
                ))}
              </tbody>
              <caption>
                Tinted on the book&apos;s <b>Sharpe against the same universe held
                passively</b>, whichever metric is displayed — green cleared it, red trailed
                it, deeper is further. Built from the full book sheets, ~400 rules per cell,
                so the weak environments are here too; a leaderboard can only show where a
                rule ranked. <b>Any tinted cell opens that environment&apos;s page for this
                same rule.</b>
              </caption>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

/* One square.
 *
 * EVERY SCORED CELL IS A LINK. It used to link only where the sheet's shipped board carried
 * the rule, which here means almost nowhere — a leaderboard ships thirty of ~400 — so most
 * of the matrix was drawn, tinted, titled, and then swallowed the click. A square that does
 * that reads as a broken page, not as an absence. The detail page this opens handles a cell
 * no leaderboard carries by reporting exactly which measurements exist for it.
 *
 * A CELL IS HONEST ABOUT WHY IT IS EMPTY, and there are three different empties:
 *   - no env at all: the class has no sheet at this timeframe;
 *   - `0 trades`: the book was scored and never opened a position, so there is nothing
 *     to score — which is a measurement, not a gap;
 *   - an em-dash, whose tooltip distinguishes a rule the book stage never scored here from
 *     one scored at the OTHER fill. The second is a gap in this pass rather than in the
 *     rule, and it is the one the reader can act on: `5m` was run at `open` only, on
 *     purpose, so every 5m column is blank under `close` and switching the selector fills
 *     it in. Saying "not scored" for both would hide that.
 */
function Cell({ env, R, rule, fill, metric, view, mark, ranked }: {
  env: RobustEnv | undefined;
  R: RobustIndex;
  rule: string;
  fill: "open" | "close";
  metric: string;
  view: CellView;
  mark: string;
  ranked: Set<string> | null;
}) {
  if (!env) {
    return (
      <td className="flat"
          title="no book run covers this cell — the class has no sheet at this timeframe">
        {DASH}
      </td>
    );
  }
  const a = view.cells[env.key];
  if (!a) {
    const other = (fill === "open" ? R.rules : R.open) ?? {};
    const elsewhere = (other[rule] ?? {})[env.key];
    return (
      <td className="flat" title={elsewhere
        ? "not scored at this fill — switch the fill selector to see it"
        : "the book stage did not score this rule here"}>
        {DASH}
      </td>
    );
  }
  if (a[view.iT] === 0) {
    return (
      <td className="flat"
          title="the book opened no positions here — there is nothing to score">
        0 trades
      </td>
    );
  }

  const sv = a[view.iS];
  const bs = view.benchOf(env);
  const beat = sv != null && bs != null && sv > bs;
  const mag = sv != null && bs != null ? Math.min(Math.abs(sv - bs) / 0.5, 1) : 0;
  const lvl = mag > 0.66 ? 3 : mag > 0.33 ? 2 : 1;
  const isHere = mark === env.key;
  const arg = CLASS_ARG[env.cls] ?? env.cls;
  /* Whether that cell has a RANKED SHEET, which is the closest this app can get to the
   * vanilla board's "is the rule on that sheet's shipped rows" check without ranking a
   * sheet per square. It is coarser and the wording says only what it knows: a cell with
   * no ranked sheet certainly has no board row, and one with a sheet may or may not. */
  const hasBoard = ranked ? ranked.has(`${arg}_${env.tf}`) : null;
  const title = `${CLASS_LABEL[env.cls] ?? env.cls} ${env.tf} · Sharpe ${fmtNum(sv, 2)}`
    + ` vs ${fmtNum(bs, 2)} held passively over ${fmtNum(env.years, 1)}y`
    + (isHere ? " · this page"
       : hasBoard === false
         ? " · that sheet has no ranked board — opens the book record"
         : " · open this environment");

  return (
    <td className={`mx-cell tint-${beat ? "g" : "l"}${lvl}${isHere ? " mx-on" : ""}`}
        title={title}>
      <Link
        href={ruleHref(arg, env.tf, rule)}
        // The cell's own tint and outline carry the meaning; a link underline inside a
        // heat map is noise, and the whole square being the target is what makes the
        // matrix walkable.
        style={{ display: "block", color: "inherit", borderBottom: "none" }}
      >
        {ROB_METRICS[metric][1](a[R.fields.indexOf(metric)])}
      </Link>
    </td>
  );
}
