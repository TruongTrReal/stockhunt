"use client";

/* THE PORTFOLIO'S CURVE, against the benchmark blended the same way.
 *
 * Hand-drawn SVG on a LOG SCALE, the same treatment `EquityChart` gives a single rule's
 * book and for the same reason: over decades a curve ends hundreds of times its start, and
 * on a linear axis every drawdown before the last few years compresses into the axis line.
 * Equal vertical distances are equal percentage moves, which is what a reader comparing two
 * curves is actually doing.
 *
 * It is NOT `EquityChart`. That component takes `matched.lines` — benchmarks each held at
 * the strategy's own volatility, with the weight printed on every legend entry — which is a
 * shape `stockhunt/blend.py` does not produce. Passing a blended benchmark through it would
 * print "· 100%" beside a line that was never scaled, which is a claim about the sizing.
 *
 * THREE RULES ABOUT COLOUR, all of them the stylesheet's rather than this file's:
 *
 *   * Green and red mean gained and lost on this site, so the legs are drawn in the six
 *     series hues (`--s1`..`--s6`), which deliberately contain neither.
 *   * THOSE HUES ARE ASSIGNED IN ORDER AND NEVER CYCLED. The order is a colour-vision
 *     safety mechanism — adjacent slots were validated as separable in both themes — so
 *     leg 1 takes `--s1`, leg 2 `--s2`, and a seventh leg is not drawn at all rather than
 *     repeating a hue that already means another leg.
 *   * The portfolio itself and its benchmark are NOT in that palette. They are ink and
 *     muted ink, because they are not two of the legs and must not read as though they
 *     were.
 */

import { SERIES_COLORS } from "@/lib/columns";
import { fmtMoney, fmtNum } from "@/lib/format";
import { growthOf, type Blend } from "@/lib/portfolio";

/** The engine reports fractions — `cagr: 0.085` is 8.5%/yr. Nothing on this page prints one
 *  without going through here, so a percent can never be shown as a fraction or twice. */
const pct = (v: number | null | undefined, d = 1) =>
  v == null ? "—" : `${fmtNum(v * 100, d)}%`;

const W = 960;
const H = 260;
const PAD = { l: 46, r: 8, t: 12, b: 20 };

/** Six is the ceiling because the palette is six. A basket may hold up to 25 legs, so on a
 *  large one the individual lines are simply not offered — the table below carries every
 *  leg, and a chart with 25 hues would be a tangle wearing repeated colours. */
export const MAX_LEG_LINES = SERIES_COLORS.length;

interface Series {
  label: string;
  curve: number[];
  color: string;
  width: number;
  dash?: string;
}

function Plot({ series, dates }: { series: Series[]; dates: string[] }) {
  const all = series.flatMap((s) => s.curve).filter((v) => Number.isFinite(v) && v > 0);
  if (all.length < 2) return <p className="sec-note">No curve to draw.</p>;

  const min = Math.min(...all);
  const max = Math.max(...all);
  const lo = Math.log10(min);
  const hi = Math.log10(max);
  const span = hi - lo || 1;
  const n = Math.max(...series.map((s) => s.curve.length));
  const x = (i: number) => PAD.l + (i / Math.max(n - 1, 1)) * (W - PAD.l - PAD.r);
  const y = (v: number) =>
    PAD.t + (1 - (Math.log10(Math.max(v, 1e-9)) - lo) / span) * (H - PAD.t - PAD.b);

  // Decade boundaries, so the axis reads 100 / 1k / 10k rather than at interpolated values.
  const ticks: number[] = [];
  for (let e = Math.floor(lo); e <= Math.ceil(hi); e++) {
    const v = Math.pow(10, e);
    if (v >= min * 0.5 && v <= max * 2) ticks.push(v);
  }

  return (
    <svg
      className="chart chart-board"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label={series.map((s) => s.label).join(" versus ")}
    >
      {ticks.map((v) => (
        <g key={v}>
          <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} stroke="var(--hair)" strokeWidth="1" />
          <text
            x={PAD.l - 6}
            y={y(v) + 3.5}
            textAnchor="end"
            fontSize="9"
            fill="var(--muted)"
            fontFamily="var(--mono)"
          >
            {v >= 1000 ? `${v / 1000}k` : v}
          </text>
        </g>
      ))}
      {series.map((s, k) => (
        <polyline
          key={`${s.label}-${k}`}
          points={s.curve.map((v, i) => `${x(i)},${y(v)}`).join(" ")}
          fill="none"
          stroke={s.color}
          strokeWidth={s.width}
          strokeDasharray={s.dash}
          vectorEffect="non-scaling-stroke"
        />
      ))}
      {dates.length > 1 && (
        <>
          <text x={PAD.l} y={H - 4} fontSize="9" fill="var(--muted)" fontFamily="var(--mono)">
            {dates[0]}
          </text>
          <text
            x={W - PAD.r}
            y={H - 4}
            textAnchor="end"
            fontSize="9"
            fill="var(--muted)"
            fontFamily="var(--mono)"
          >
            {dates[dates.length - 1]}
          </text>
        </>
      )}
    </svg>
  );
}

export interface PortfolioChartProps {
  blend: Blend;
  /** The name printed on the portfolio's own line. */
  label: string;
  /** Draw each leg as well as the blend. Off by default: the question the chart opens on is
   *  "what did the basket do", and six extra lines answer a different one. */
  showLegs: boolean;
  onShowLegs?: (v: boolean) => void;
}

export function PortfolioChart({ blend, label, showLegs, onShowLegs }: PortfolioChartProps) {
  const drawable = blend.legs.filter((l) => l.curve.length > 1);
  const legLines = showLegs ? drawable.slice(0, MAX_LEG_LINES) : [];

  const series: Series[] = [
    { label, curve: blend.portfolio, color: "var(--ink)", width: 1.9 },
    ...(blend.bench.length > 1
      ? [{ label: blend.benchLabel, curve: blend.bench, color: "var(--muted)", width: 1.2, dash: "4 3" }]
      : []),
    ...legLines.map((l, i) => ({
      label: l.label,
      curve: l.curve,
      color: SERIES_COLORS[i],
      width: 1,
    })),
  ];

  const growth = growthOf(blend.portfolio);
  const benchGrowth = growthOf(blend.bench);
  const m = blend.metrics;

  return (
    <div className="panel">
      <Plot series={series} dates={blend.dates} />

      {/* Named directly rather than by colour alone: two of these strokes are grey and a
          hue that is not labelled is not carrying its meaning. */}
      <div className="legend">
        {series.map((s) => (
          <span key={s.label}>
            <i
              className="sw"
              style={{
                background: s.color,
                ...(s.dash ? { opacity: 0.75 } : {}),
              }}
            />
            {s.label}
          </span>
        ))}
        <span>log scale · growth of 100</span>
      </div>

      <p className="sec-note">
        <span
          className="explains"
          title={
            "One pot of money split equally across the legs and rebalanced back to equal " +
            "weight monthly, blended from each leg's own book curve. Costs are already " +
            "inside those curves and are not charged twice. The benchmark is blended the " +
            "same way as the legs — a benchmark differing from the strategy in more than " +
            "the signal is this repo's most-repeated warning. The span is the INTERSECTION " +
            "of the legs' histories, never the union: legs can come from classes whose " +
            "data begins decades apart."
          }
        >
          one pot, equal weight, rebalanced {blend.rebalance ?? "monthly"}
        </span>
        {blend.capital != null && <> · {fmtMoney(blend.capital)} pot</>}
        {blend.years != null && <> · {fmtNum(blend.years, 1)}y of overlap</>}
        {growth != null && <> · $100 became {fmtMoney(growth * 100)}</>}
        {benchGrowth != null && <>, against {fmtMoney(benchGrowth * 100)} for the benchmark</>}
        {m.cagr != null && <> · {pct(m.cagr)}/yr</>}
        {/* NAMED AS A LOWER BOUND, because that is what it is. The stored curves are
            stride-decimated — one point stands for weeks on a daily sheet — so a trough
            that opened and closed inside one of those bars is invisible here. It is not the
            drawdown on the same rule's dashboard row and must never be compared with it. */}
        {m.max_drawdown != null && (
          <>
            {" "}·{" "}
            <span
              className="explains"
              title={
                "Worst fall this blend can SEE. The stored curves keep roughly 320 points " +
                "per rule whatever the bar count, so one point stands for several weeks on " +
                "a daily sheet and a trough that opened and closed inside one is invisible. " +
                "This is a lower bound on the real figure and is not the drawdown printed " +
                "on the same rule's own page."
              }
            >
              worst fall at least {pct(m.max_drawdown)}
            </span>
          </>
        )}
        {" "}
        <span
          className="explains"
          title={
            "Walk-forward research over the whole history, not the desk's record. Days of " +
            "paper fills and years of walk-forward are different measurements and are never " +
            "added together. Combining rules that each fail this repo's acceptance gates " +
            "does not produce one that passes."
          }
        >
          research, not the live record
        </span>
      </p>

      {/* THE ENGINE'S OWN CAVEATS, on the page rather than in a log. `blend()` warns when a
          leg had to be smoothed onto a coarser grid than its own — which understates that
          leg's volatility and biases the correlation toward zero, and toward zero is the
          flattering direction: it makes five picks off one sheet look like diversification.
          It also warns that the monthly reset moves money and nobody charged the spread. */}
      {blend.warnings.length > 0 && (
        <div className="note">
          <b>Read the blend with these in view.</b>
          <ul className="pf-warn">
            {blend.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      {blend.gridFrom && blend.medianBarDays != null && blend.medianBarDays > 7 && (
        <p className="sec-note">
          <span
            className="explains"
            title={
              "The stored curves are decimated to roughly 320 points per rule, so a bar on " +
              "this axis is not a session. The common axis is the COARSEST leg's own dates, " +
              "clipped to the intersection of every leg's history; finer legs are projected " +
              "onto it. Sampling a coarse leg onto a fine axis is the one thing that must " +
              "not happen — it produces flat bars and catch-up jumps, which inflates " +
              "volatility and biases the correlation toward zero."
            }
          >
            one bar here is about {fmtNum(blend.medianBarDays, 0)} days
          </span>
          , the grid taken from {blend.gridFrom}
          {blend.rebalances != null && <> · {blend.rebalances} rebalances fired</>}
        </p>
      )}

      {onShowLegs && drawable.length > 1 && (
        <div className="lb-tools">
          <button
            type="button"
            className={`pill${showLegs ? " on" : ""}`}
            onClick={() => onShowLegs(!showLegs)}
          >
            {showLegs ? "Just the portfolio" : `Draw the ${drawable.length} legs too`}
          </button>
          {showLegs && drawable.length > MAX_LEG_LINES && (
            <span className="sec-note">
              the first {MAX_LEG_LINES} only — the palette is six hues and a seventh line
              would have to repeat one, which reads as two legs being the same leg
            </span>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- the list's sparkline */

/** A portfolio's combined curve against its benchmark, small. Log scale for the same reason
 *  the big one is — a linear 25-year spark is a flat line with a hook on the end.
 *
 *  NEUTRAL INK, not gain/loss. This is a research equity curve, and colouring it by whether
 *  it finished above its benchmark would put a verdict on a 22-pixel picture. */
export function CurveSpark({
  portfolio,
  bench,
  w = 120,
  h = 26,
}: {
  portfolio: number[];
  bench?: number[];
  w?: number;
  h?: number;
}) {
  const p = portfolio.filter((v) => Number.isFinite(v) && v > 0);
  if (p.length < 2) return null;
  const b = (bench ?? []).filter((v) => Number.isFinite(v) && v > 0);
  const all = p.concat(b);
  const lo = Math.log10(Math.min(...all));
  const hi = Math.log10(Math.max(...all));
  const span = hi - lo || 1;
  const pad = 2;
  const y = (v: number) => pad + (1 - (Math.log10(v) - lo) / span) * (h - pad * 2);
  const pts = (s: number[]) =>
    s.map((v, i) => `${(i / Math.max(s.length - 1, 1)) * w},${y(v)}`).join(" ");

  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden="true">
      {b.length > 1 && (
        <polyline
          points={pts(b)}
          fill="none"
          stroke="var(--muted)"
          strokeWidth="1"
          strokeDasharray="3 2"
          vectorEffect="non-scaling-stroke"
        />
      )}
      <polyline
        points={pts(p)}
        fill="none"
        stroke="var(--ink-2)"
        strokeWidth="1.4"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
