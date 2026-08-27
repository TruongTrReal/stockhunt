"use client";

/* An equity chart, hand-drawn in SVG, on purpose.
 *
 * The vanilla board draws its charts the same way and carries no charting library. That is
 * worth preserving for a reason beyond bundle size: the one thing this chart must get right
 * is the LOG SCALE, and a default-linear chart of a 25-year equity curve is actively
 * misleading — it makes the last three years look like the whole result and squashes every
 * drawdown before them into the axis. A library would make that a prop somebody can forget.
 *
 * Two lines and nothing else: the book, and the benchmark it is being judged against. The
 * benchmark is the RISK-MATCHED one the leaderboard ranks on, not raw buy-and-hold, so the
 * gap on screen is the same gap the verdict column is about.
 */

const W = 900;
const H = 320;
const PAD = { l: 8, r: 64, t: 12, b: 26 };

export interface EquityChartProps {
  dates: string[];
  curve: number[];
  bench: number[];
  ruleLabel: string;
}

export function EquityChart({ dates, curve, bench, ruleLabel }: EquityChartProps) {
  const pairs = curve
    .map((v, i) => [v, bench[i]] as const)
    .filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b) && a > 0 && b > 0);
  if (pairs.length < 2) {
    return <p className="sec-note">No equity series is stored for this cell.</p>;
  }

  // Log space, computed once. `Math.log` of a non-positive value is -Infinity and would
  // silently drag the whole domain to it, which is why the filter above is on BOTH series
  // rather than on the one being drawn.
  const lo = Math.min(...pairs.flatMap(([a, b]) => [Math.log(a), Math.log(b)]));
  const hi = Math.max(...pairs.flatMap(([a, b]) => [Math.log(a), Math.log(b)]));
  const span = hi - lo || 1;
  const x = (i: number) =>
    PAD.l + (i / (pairs.length - 1)) * (W - PAD.l - PAD.r);
  const y = (v: number) =>
    PAD.t + (1 - (Math.log(v) - lo) / span) * (H - PAD.t - PAD.b);
  const path = (pick: 0 | 1) =>
    pairs.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[pick]).toFixed(1)}`).join("");

  const last = pairs[pairs.length - 1];
  const first = dates[0];
  const end = dates[dates.length - 1];

  return (
    <figure style={{ margin: "0 0 8px" }}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        role="img"
        aria-label={`Equity of ${ruleLabel} against its risk-matched benchmark, log scale`}
        style={{ display: "block", overflow: "visible" }}
      >
        <line
          x1={PAD.l} x2={W - PAD.r} y1={H - PAD.b} y2={H - PAD.b}
          stroke="var(--hair)" strokeWidth="1"
        />
        {/* The benchmark is drawn first and quieter, so the rule reads as the subject. */}
        <path d={path(1)} fill="none" stroke="var(--muted)" strokeWidth="1.3"
              strokeDasharray="4 3" />
        <path d={path(0)} fill="none" stroke="var(--s1)" strokeWidth="1.8" />
        {/* Labelled at the line's own end rather than in a legend — the stylesheet's rule:
            a colour that is not directly labelled is not carrying its meaning. */}
        <text x={W - PAD.r + 6} y={y(last[0]) + 4} fill="var(--s1)"
              style={{ font: "12px var(--mono)" }}>
          book
        </text>
        <text x={W - PAD.r + 6} y={y(last[1]) + 4} fill="var(--muted)"
              style={{ font: "12px var(--mono)" }}>
          bench
        </text>
        <text x={PAD.l} y={H - 8} fill="var(--muted)" style={{ font: "11.5px var(--mono)" }}>
          {first}
        </text>
        <text x={W - PAD.r} y={H - 8} fill="var(--muted)" textAnchor="end"
              style={{ font: "11.5px var(--mono)" }}>
          {end}
        </text>
      </svg>
      <figcaption className="sec-note">
        Growth on a <b>log scale</b>, book against the <b>risk-matched</b> benchmark — the
        baseline scaled down with T-bills to this rule&apos;s own volatility, which is the
        comparison the verdict is made on. Idle capital earns nothing on both lines.
      </figcaption>
    </figure>
  );
}
