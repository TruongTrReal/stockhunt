"use client";

/* The performance metrics table — the book against every benchmark AT ITS OWN VOLATILITY.
 *
 * One column per benchmark, on the same sizing as the chart above it, because a table on a
 * different basis from the picture over it is the mistake this page spent a day removing.
 * The sheet's own basket is one of these columns rather than a special case: at equal risk
 * it is just another thing you could have held instead.
 *
 * WHAT MATCHING DOES AND DOES NOT MOVE is the thing to know while reading this, and the
 * caption says it. With idle cash earning nothing, scaling a benchmark by `w` scales its
 * mean and its standard deviation by the same number, so **Sharpe and Sortino are
 * unchanged** from the full-size instrument. Volatility, drawdown, CAGR and terminal wealth
 * all move, and volatility lands on the strategy's own by construction — the volatility row
 * being identical across the table is the POINT, not a bug, and it has been reported as one.
 */

import { METRIC_ROWS, mval, fmtNum, type MatchedLine } from "@/lib/rule";

export interface MetricsTableProps {
  /** The book's own metrics, out of the curve file. */
  metrics: Record<string, number | null> | null | undefined;
  /** Every matched line that carries metrics — the basket included, unlike the chart. */
  lines: MatchedLine[];
  /** How many names the book held, for the note. Null on a cell with no per-asset rows. */
  assetN?: number | null;
  /** Whether an `Asset by asset` table is actually on the page below. Gated on the ROWS
   *  and never on the count: a pair carries the count (it is the sheet's universe size)
   *  but ships no per-symbol table, so keying on the count would point a reader at a
   *  section that is not there. */
  hasAssetTable: boolean;
}

export function MetricsTable({ metrics, lines, assetN, hasAssetTable }: MetricsTableProps) {
  if (!metrics) return null;
  const ix = (lines ?? []).filter((i) => i && i.metrics);

  /* Holding is a single trade that is still open, so its win rate is either 100% or 0% and
   * its profit factor has no denominator. The em-dashes on these rows are therefore a fact
   * about buy-and-hold rather than missing data, which is worth saying ON them. */
  const NO_BENCH = new Set(["profit_factor", "win_rate_pct", "trades",
                            "avg_win_pct", "avg_loss_pct"]);
  const noBench = "No buy-and-hold counterpart: holding is one trade that is still open, "
    + "so its win rate is 100% or 0% and its profit factor has no denominator.";

  return (
    <section className="sec">
      <div className="sec-head">
        <h2>Performance metrics</h2>
        <span className="sec-note">
          the book against every benchmark <b>at its own volatility</b>, same window
          &middot; hover a name for what it means
        </span>
      </div>

      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th className="l">Metric</th>
              <th>Strategy</th>
              {ix.map((i) => (
                <th key={i.label}>
                  {/* WHY THIS COLUMN IS NOT WHAT IT LOOKS LIKE, on the column itself. The
                      caption that used to carry it ran to three paragraphs under a table
                      most readers had already stopped reading. */}
                  <span
                    className="explains"
                    title={
                      `${i.label} held at the strategy's own volatility, the rest in cash ` +
                      "— scaled down, never levered up. That is why the volatility row is " +
                      "identical across the table: it is the point, not a bug. Drawdown, " +
                      "CAGR and total return are directly comparable. " +
                      "Sharpe and Sortino are UNCHANGED by the matching and are the " +
                      "full-size instrument's own — holding less of something divides its " +
                      "return and its volatility by the same number. If a benchmark's " +
                      "Sharpe beats the strategy's here, it beat it at any size."
                    }
                  >
                    {i.label}
                  </span>
                  {i.weight != null && (
                    <span className="mut"> {fmtNum(i.weight * 100, 0)}%</span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {METRIC_ROWS.map(([k, name, help, dp, sfx]) => (
              <tr key={k}>
                {/* The explanation moved OFF the row and onto it. It was a column —
                    twelve sentences of prose standing permanently beside twelve numbers,
                    which is the shape the leaderboard's 800-word legend had before it
                    became a per-column `doc`. A reader who does not recognise `Calmar`
                    asks; one who does should not have to read past the answer. */}
                <td className="l">
                  <span
                    className="explains"
                    title={NO_BENCH.has(k) ? `${help}  ${noBench}` : help}
                  >
                    {name}
                  </span>
                </td>
                <td>{mval(metrics, k, dp, sfx)}</td>
                {ix.map((i) => (
                  <td key={i.label}>
                    {i.metrics && k in i.metrics ? mval(i.metrics, k, dp, sfx) : "—"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
