"use client";

/* THE PAPER SIDE'S CHARTS. Hand-drawn SVG, no library, same as the vanilla board — and
 * here that is load-bearing rather than a preference, because two of the three decisions
 * these charts encode are the kind a library turns into a prop somebody forgets.
 *
 *   BASELINE AT 0, NOT 100. `paper_curve` is cumulative P&L in PERCENT since a system's
 *   first fill. `lineChart` used to draw this series with its reference line at 100 — an
 *   INDEX baseline — which put the reference off the top of every chart it drew. The
 *   function was deleted with the bug rather than kept as a second convention.
 *
 *   THE LINE IS CUT AT `curve_breaks`, NEVER DRAWN THROUGH THEM. A break is a point where
 *   the record lost a bar, and a straight segment across an outage is a claim that nothing
 *   happened during it when the truth is that nobody was watching.
 *
 *   A SEGMENT OF ONE POINT IS A DOT. A young system with a restart in it is exactly two
 *   points either side of a gap, and a polyline-only renderer draws nothing at all for it —
 *   which reads as "no record" when the record is simply short.
 *
 * `PnlSpark` is the odd one out and keeps its reference at 100: it draws the SIMULATED
 * windows from `paper_curves.py`, which are indexed series and not P&L in percent.
 */

import { fmtPct, sign } from "@/lib/live";

/* ---------------------------------------------------------------- the live record */

export interface PnlLiveProps {
  curve: number[];
  /** Drawn ONLY on the ranked list, at 34px, where a market line is context rather than a
   *  verdict. Every full-size figure on the paper side passes null — see `PnlFigure`. */
  bench?: number[] | null;
  breaks?: number[];
  w?: number;
  h?: number;
}

export function PnlLive({ curve, bench, breaks, w = 620, h = 128 }: PnlLiveProps) {
  const cur = (curve || []).filter((v) => Number.isFinite(v));
  if (cur.length < 2) return null;
  const bn = (bench || []).filter((v) => Number.isFinite(v));
  const all = cur.concat(bn, [0]);
  let lo = Math.min(...all);
  let hi = Math.max(...all);
  // A desk one day old is flat at exactly 0.00 on every line, and a degenerate range pins
  // that to the bottom of the box — a line on the floor reads as a loss. Half a point
  // either side, so nothing-yet is drawn through the middle.
  if (hi - lo < 1e-9) {
    const mid = (hi + lo) / 2;
    lo = mid - 0.5;
    hi = mid + 0.5;
  }
  const span = hi - lo;
  const pad = 7;
  const x = (i: number) => (i / Math.max(cur.length - 1, 1)) * w;
  const y = (v: number) => pad + (1 - (v - lo) / span) * (h - pad * 2);
  const cut = new Set(breaks || []);

  /* The benchmark is held apart by INK, not by a dash pattern. Both lines are solid: at
   * 34px a dashed polyline breaks into a row of ticks that reads as a broken record rather
   * than a second series, and the record is exactly the thing this chart must not cast
   * doubt on. Grey against the gain/loss colour, and thinner, is separation enough. */
  const line = (s: number[], muted: boolean, tag: string) => {
    const ink = muted
      ? "var(--muted)"
      : cur[cur.length - 1] >= 0
        ? "var(--gain)"
        : "var(--loss)";
    const parts: [number, number][][] = [];
    let run: [number, number][] = [];
    s.forEach((v, i) => {
      if (cut.has(i) && run.length) {
        parts.push(run);
        run = [];
      }
      run.push([x(i), y(v)]);
    });
    if (run.length) parts.push(run);
    return parts.map((p, i) =>
      p.length > 1 ? (
        <polyline
          key={`${tag}-${i}`}
          points={p.map(([a, b]) => `${a},${b}`).join(" ")}
          fill="none"
          stroke={ink}
          strokeWidth={muted ? 1 : 1.7}
          vectorEffect="non-scaling-stroke"
        />
      ) : (
        <circle
          key={`${tag}-${i}`}
          cx={p[0][0]}
          cy={p[0][1]}
          r={muted ? 1.2 : 1.8}
          fill={ink}
        />
      ),
    );
  };

  return (
    <svg
      className="pnl-chart"
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <line
        x1="0"
        x2={w}
        y1={y(0)}
        y2={y(0)}
        stroke="var(--hair-2)"
        strokeWidth="1"
        vectorEffect="non-scaling-stroke"
      />
      {bn.length > 1 ? line(bn, true, "b") : null}
      {line(cur, false, "c")}
    </svg>
  );
}

/* ------------------------------------------------- the live record, as a figure */

/* The plot on its own is unreadable in one specific way: it carries NO VERTICAL SCALE.
 * `PnlLive` fits its box to whatever range the data happens to span, so a system that
 * moved four basis points and one that moved forty percent draw the identical picture.
 * Without a number on the axis the shape is not merely uninformative, it is misleading.
 *
 * So the figure adds the three things the plot cannot carry itself: the range it was drawn
 * over, a key, and the endpoints of the time axis. The labels are HTML beside the SVG
 * rather than <text> inside it, because the plot is drawn with `preserveAspectRatio="none"`
 * and any text within would stretch with it.
 *
 * ONE LINE: the system's own record. Every detail chart on the paper side lost its
 * benchmark on 2026-08-17. The comparison a strategy is judged on is the risk-matched one
 * over decades, on the Research side; days of paper fills against a basket held over the
 * same days is not that comparison and was being read as though it were.
 */
export interface PnlFigureProps {
  curve: number[];
  breaks?: number[];
  from?: string;
  to?: string;
}

export function PnlFigure({ curve, breaks, from, to }: PnlFigureProps) {
  const cur = (curve || []).filter((v) => Number.isFinite(v));
  if (cur.length < 2) return null;
  const all = cur.concat([0]);
  let lo = Math.min(...all);
  let hi = Math.max(...all);
  if (hi - lo < 1e-9) {
    const mid = (hi + lo) / 2;
    lo = mid - 0.5;
    hi = mid + 0.5;
  }
  const last = cur[cur.length - 1];

  return (
    <figure className="pnl-fig">
      <div className="pnl-plot">
        <div className="pnl-scale" aria-hidden="true">
          <span>{fmtPct(hi)}</span>
          <span>{fmtPct(lo)}</span>
        </div>
        {/* The full 1240px rail, not capped at 820: the record is what these pages are for. */}
        <PnlLive curve={cur} bench={null} breaks={breaks} w={1200} h={220} />
      </div>
      <div className="pnl-axis">
        <span>{from || "start"}</span>
        <span>{to || ""}</span>
      </div>
      <figcaption className="pnl-key">
        <span className="key">
          <i className={`key-line ${sign(last)}`} />
          this system <b className={`num ${sign(last)}`}>{fmtPct(last)}</b>
        </span>
      </figcaption>
    </figure>
  );
}

/* ------------------------------------------------------- the simulated windows */

/* A compact area-less line on a LINEAR scale. These windows span months, not decades, so
 * the log treatment the research charts need would only flatten the detail here.
 *
 * ONE series: the strategy. The dashed "same basket held" line that used to be drawn
 * underneath came off the paper pages with everything else on 2026-08-17.
 *
 * The reference is at 100 and not 0, and that is not an inconsistency with `PnlLive`: this
 * draws the INDEXED simulated curves `paper_curves.py` publishes, not cumulative P&L.
 */
export function PnlSpark({
  curve,
  w = 560,
  h = 96,
}: {
  curve: number[];
  w?: number;
  h?: number;
}) {
  const s = (curve || []).filter((v) => Number.isFinite(v));
  if (s.length < 2) return null;
  const lo = Math.min(...s, 100);
  const hi = Math.max(...s, 100);
  const span = hi - lo || 1;
  const pad = 6;
  const y = (v: number) => pad + (1 - (v - lo) / span) * (h - pad * 2);
  return (
    <svg
      className="pnl-chart"
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <line
        x1="0"
        x2={w}
        y1={y(100)}
        y2={y(100)}
        stroke="var(--hair-2)"
        strokeWidth="1"
        vectorEffect="non-scaling-stroke"
      />
      <polyline
        points={s
          .map((v, i) => `${(i / Math.max(s.length - 1, 1)) * w},${y(v)}`)
          .join(" ")}
        fill="none"
        stroke={s[s.length - 1] >= 100 ? "var(--gain)" : "var(--loss)"}
        strokeWidth="1.6"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
