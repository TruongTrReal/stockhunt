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

  return (
    <section className="sec">
      <div className="sec-head">
        <h2>Performance metrics</h2>
        <span className="sec-note">
          the book against every benchmark <b>at its own volatility</b>, same window
        </span>
      </div>

      {hasAssetTable && assetN != null && (
        <div className="note">
          One portfolio holding all {assetN} names at once, at 1&times; size. These are the
          same numbers as the row on the leaderboard &mdash; one book, measured once. The
          per-name breakdown is the <em>Asset by asset</em> table below, and those are{" "}
          {assetN} separate single-name backtests: they do not add up to this, because a
          book diversifies and a list of backtests cannot.
        </div>
      )}

      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th className="l">Metric</th>
              <th>Strategy</th>
              {ix.map((i) => (
                <th key={i.label}>
                  {i.label}
                  {i.weight != null && (
                    <span className="mut"> {fmtNum(i.weight * 100, 0)}%</span>
                  )}
                </th>
              ))}
              <th className="l">What it means</th>
            </tr>
          </thead>
          <tbody>
            {METRIC_ROWS.map(([k, name, help, dp, sfx]) => (
              <tr key={k}>
                <td className="l">{name}</td>
                <td>{mval(metrics, k, dp, sfx)}</td>
                {ix.map((i) => (
                  <td key={i.label}>
                    {i.metrics && k in i.metrics ? mval(i.metrics, k, dp, sfx) : "—"}
                  </td>
                ))}
                <td
                  className="l"
                  style={{ whiteSpace: "normal", color: "var(--muted)", fontSize: "12.5px" }}
                >
                  {help}
                </td>
              </tr>
            ))}
          </tbody>
          <caption>
            Every benchmark column is that instrument <b>held at the strategy&apos;s own
            volatility</b>, the rest in cash — so the volatility row is the same across the
            table by construction, and drawdown, CAGR and total return are directly
            comparable. Scaled down, never levered up.
            {ix.length > 0 && (
              <>
                <br />
                <br />
                <b>Sharpe and Sortino are unchanged by the matching</b> and are the
                full-size instrument&apos;s own: with idle cash earning nothing, holding
                less of something divides its return and its volatility by the same number.
                If a benchmark&apos;s Sharpe beats the strategy&apos;s here, it beat it at
                any size.
              </>
            )}
            <br />
            <br />
            Trade-level statistics (profit factor, win rate, average win and loss) have no
            buy-and-hold counterpart — holding is a single trade that is still open, so its
            win rate is either 100% or 0% and its profit factor has no denominator. None of
            these replace the verdict: a strategy can carry a better Sharpe than every
            column here and still fail the standard, which is exactly what the best rows on
            this sheet do.
          </caption>
        </table>
      </div>
    </section>
  );
}
