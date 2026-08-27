"use client";

/* The equity chart, with EVERY BENCHMARK HELD AT THE STRATEGY'S OWN VOLATILITY.
 *
 * There is one sizing here and it is the risk-matched one. The vanilla board briefly
 * offered both — "as traded" beside "at equal risk" — and a toggle is the wrong shape for
 * this question: at full size the lines rank by who took the most risk, so leaving that
 * reading on screen as an equal option invites exactly the mistake the rest of the page
 * exists to prevent. On us_stocks 1d QQQ ends 26% above `ibs` at full size and about a
 * third of it at equal risk, at 57% held; only the second of those is about the strategy.
 * The full-size figures survive in the CAPTION as prose, which is where a fact that is
 * true but easy to misread belongs.
 *
 * A benchmark is scaled DOWN with cash, never levered up — no margin, no borrow, nothing
 * that needs an account upgrade. The weight is on every legend entry, so a line can never
 * be mistaken for the instrument itself.
 *
 * The blended series comes from `portfolio_wf` and CANNOT be derived here: the weight
 * multiplies each bar's RETURN before it compounds, so `w*curve + (1-w)` is simply the
 * wrong curve. Each matched line's last point x100 equals its `wealth`, which is the
 * cheapest check that the picture and the table agree.
 *
 * Hand-drawn SVG, no charting library, same as the vanilla board. The one thing this chart
 * must get right is the LOG SCALE: over decades the curve ends hundreds of times its
 * start, so on a linear axis every drawdown before the last few years compresses into the
 * axis line. On a log scale equal vertical distances are equal PERCENTAGE moves, which is
 * what a reader comparing two curves is actually doing. A library would make that a prop
 * somebody can forget.
 */

import { fmtMoney, fmtNum, type MatchedLine, type Matched } from "@/lib/rule";

const W = 960;
const H = 230;
const PAD = { l: 46, r: 8, t: 12, b: 20 };

/* Three distinct strokes, which is why the chart takes at most three series. `colors[2]`
 * was once undefined, which SVG renders as no line at all rather than as an error;
 * `--ink-2` sits between the solid rule and the muted basket, and the longer dash keeps
 * the two dashed lines apart at the scale these are drawn. */
const COLORS = ["var(--ink)", "var(--muted)", "var(--ink-2)"];
const DASHES = ["", "4 3", "1.5 3"];

export interface EquityChartProps {
  /** The book's own curve, growth of 100. */
  curve: number[];
  /** The benchmarks to draw, each already blended to the strategy's volatility. */
  drawn: MatchedLine[];
  dates: string[];
  ruleLabel: string;
  /** `matched.vol_pct` and `matched.strategy`, for the caption's two figures. */
  mm: Matched;
  /** The sentence that points at what comes next. A pair, an off-board single and a
   *  ranked rule each need a DIFFERENT one — see `app/rule/page.tsx`. */
  tail: React.ReactNode;
}

/** The axis, the lines and the legend. Nothing here knows what a strategy is. */
function Plot({ sets, labels, dates }: {
  sets: number[][]; labels: string[]; dates: string[];
}) {
  const all = sets.flat().filter((v) => Number.isFinite(v) && v > 0);
  if (!all.length) return <p className="sec-note">No curve data.</p>;

  const min = Math.min(...all);
  const max = Math.max(...all);
  const lo = Math.log10(min);
  const hi = Math.log10(max);
  const span = hi - lo || 1;
  const n = Math.max(...sets.map((s) => s.length));
  const x = (i: number) => PAD.l + (i / Math.max(n - 1, 1)) * (W - PAD.l - PAD.r);
  const y = (v: number) =>
    PAD.t + (1 - (Math.log10(Math.max(v, 1e-9)) - lo) / span) * (H - PAD.t - PAD.b);

  // Gridlines on DECADE boundaries, so the axis reads 100 / 1k / 10k rather than at
  // arbitrary interpolated values.
  const ticks: number[] = [];
  for (let e = Math.floor(lo); e <= Math.ceil(hi); e++) {
    const v = Math.pow(10, e);
    if (v >= min * 0.5 && v <= max * 2) ticks.push(v);
  }

  const first = dates?.[0];
  const last = dates?.[dates.length - 1];

  return (
    <>
      <svg
        className="chart"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={labels.join(" versus ")}
      >
        {ticks.map((v) => (
          <g key={v}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)}
                  stroke="var(--hair)" strokeWidth="1" />
            <text x={PAD.l - 6} y={y(v) + 3.5} textAnchor="end" fontSize="9"
                  fill="var(--muted)" fontFamily="var(--mono)">
              {v >= 1000 ? `${v / 1000}k` : v}
            </text>
          </g>
        ))}
        {sets.map((s, k) => (
          <polyline
            key={k}
            points={s.map((v, i) => `${x(i)},${y(v)}`).join(" ")}
            fill="none"
            stroke={COLORS[k] ?? "var(--muted)"}
            strokeWidth={k ? 1.1 : 1.7}
            strokeDasharray={DASHES[k] || undefined}
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {first && (
          <>
            <text x={PAD.l} y={H - 4} fontSize="9" fill="var(--muted)"
                  fontFamily="var(--mono)">{first}</text>
            <text x={W - PAD.r} y={H - 4} textAnchor="end" fontSize="9"
                  fill="var(--muted)" fontFamily="var(--mono)">{last}</text>
          </>
        )}
      </svg>
      {/* Named directly rather than by colour alone: two of these three strokes are grey,
          and a colour that is not labelled is not carrying its meaning. */}
      <div className="legend">
        {labels.map((l, k) => (
          <span key={l + k}>
            <i className="sw" style={{ background: COLORS[k] }} />
            {l}
          </span>
        ))}
        <span>log scale · growth of 100</span>
      </div>
    </>
  );
}

export function EquityChart({ curve, drawn, dates, ruleLabel, mm, tail }: EquityChartProps) {
  /* No matched line means a book with no volatility to match anything to — a rule that
   * barely trades. Draw it against the basket at full size rather than nothing, and say
   * so, because an unexplained single line reads as a missing benchmark. */
  if (!drawn.length) {
    return (
      <div className="panel">
        <Plot sets={[curve]} labels={[ruleLabel]} dates={dates} />
        <p className="sec-note">
          This book holds almost nothing, so there is no volatility to match a benchmark
          to; only the strategy is drawn.
        </p>
      </div>
    );
  }

  const labels = [
    ruleLabel,
    ...drawn.map((l) => `${l.label} · ${fmtNum((l.weight ?? 0) * 100, 0)}%`),
  ];
  // Full size is PROSE, never a line. See the header comment.
  const full = drawn.filter((l) => l.raw_wealth != null);

  return (
    <div className="panel">
      <Plot sets={[curve, ...drawn.map((l) => l.curve)]} labels={labels} dates={dates} />
      <p className="sec-note">
        Every line starts at 100 and covers the same out-of-sample bars, and{" "}
        <b>
          every benchmark is held at{" "}
          {mm.vol_pct == null ? "the book's own" : `${fmtNum(mm.vol_pct, 1)}%`} volatility —
          the strategy&apos;s — with the rest in cash
        </b>
        . Scaled down, never levered up: no margin, no borrow. What is left between the
        lines is the signal rather than the risk taken to get it.
        {full.length > 0 && (
          <>
            {" "}At full size these same instruments end on{" "}
            {full.map((l, i) => (
              <span key={l.label}>
                {i > 0 && ", "}
                <b>{l.label}</b> {fmtMoney(l.raw_wealth)}
              </span>
            ))}
            , against {fmtMoney(mm.strategy?.wealth)} for the strategy — more, in some
            cases, and more risk with it. Closing that gap the other way means gearing the
            strategy up, which needs margin and is not tested anywhere here.
          </>
        )}{" "}
        This is the <b>book</b>: the same series the <b>$10k / book</b> column is computed
        from. Idle capital earns nothing, on every line.
        {tail}
      </p>
    </div>
  );
}
