"use client";

/* The chart over the leaderboard, drawn from a file built for exactly this.
 *
 * `curves/<cls>_<tf>.json` — what a detail page fetches — is 300–650 kB a sheet: full
 * resolution plus `matched`, `metrics` and `bench_metrics` for every shipped rule. None of
 * that is wanted here, and paying it on every visit to the board would make the landing
 * page slower to draw a picture that is one dashed line until somebody ticks something.
 * `payload.board_curves` writes `curves/board_<cls>_<tf>.json` instead: the same books,
 * downsampled onto shared indices and rounded, carrying `dates`, `bench` and `rules` and
 * nothing else. Tens of kB, and the TERMINAL POINT IS DELIBERATELY PRESERVED by the
 * downsampler so the end of a line still agrees with the `$10k / book` column beside it.
 *
 * The fetch is memoised per sheet, so switching sheets and coming back is free and ticking
 * a row never touches the network.
 *
 * SVG by hand, no chart library, and the scale is LOG. Linear is unreadable here: over
 * twenty-plus years a curve ends hundreds of times its start, so the first two decades —
 * including every drawdown that matters — compress into a flat line along the axis. On a
 * log scale equal vertical distances are equal percentage moves, which is what a reader is
 * actually comparing when two lines diverge.
 */

import { useEffect, useState } from "react";
import { board } from "@/lib/api";
import { clip, fmtMoney, stemName } from "@/lib/format";
import { LB_SEL_MAX } from "@/lib/columns";

/* The curve files are named after `dash_config.GROUPS`' KEY, which is not the class name
 * for three of the five. `api.sheets()` speaks class names, so the two have to be mapped
 * here or the chart asks for `board_us_stocks_1d.json` and gets a 404 on a sheet that has
 * curves. Same table as `CLASS_ARG` in the vanilla board, read the other way round.
 *
 * EXPORTED because `/v1/board/meta`'s `groups` are keyed the same way, and the board needs
 * the same translation to find a sheet's tab label. One table, two callers: a second copy
 * is how the two would eventually disagree about which class `etf` is. */
export const CLASS_GROUP: Record<string, string> = {
  us_stocks: "stocks",
  crypto: "crypto",
  us_etfs: "etf",
  commodities: "commodities",
  cme_futures: "futures",
};

interface BoardCurves {
  dates: string[];
  bench: number[];
  rules: Record<string, number[]>;
}

type Loaded = { data: BoardCurves; error?: undefined } | { data?: undefined; error: string };

const cache = new Map<string, Promise<Loaded>>();

function loadCurves(group: string, tf: string): Promise<Loaded> {
  const key = `${group}_${tf}`;
  let p = cache.get(key);
  if (!p) {
    p = board
      .boardCurves(group, tf)
      .then((d) => ({ data: d as BoardCurves }))
      // A sheet with no published curves is a real state, not a fault: the ranking below
      // stands on its own and the caption says so. Cached like a success so a missing file
      // is not re-requested on every tick.
      .catch((e: Error) => ({ error: e.message || "not published" }));
    cache.set(key, p);
  }
  return p;
}

interface Series {
  label: string;
  values: number[];
  color: string;
  w?: number;
  dash?: string;
}

/* Buy-and-hold is muted ink, dashed, and is NOT one of the six series colours: it is the
 * line the others are read against rather than a seventh competitor. */
const BENCH_STYLE = { color: "var(--muted)", w: 1.2, dash: "3 3" };

/* Growth of 100 on a log scale, every line named at its own right-hand end.
 *
 * A separate function from the detail page's equity chart rather than another argument to
 * it. That one draws ONE strategy against its risk-matched benchmarks and names them in a
 * legend underneath; this draws up to seven independent books and has to answer "which
 * line is that?" at a glance, which a legend cannot do once the lines cross. The label
 * column is real width taken out of the plot (`pad.r`), not an overlay, so a long name can
 * never end up sitting on top of the data.
 *
 * The label TEXT is ink, never the line's colour: a short leader in the series colour
 * carries the identity, and the name stays legible at the two light-mode steps that sit
 * under 3:1 against the paper.
 */
function PnlLines({ series, dates, h = 300 }: { series: Series[]; dates: string[]; h?: number }) {
  const w = 960;
  const pad = { l: 48, r: 158, t: 14, b: 22 };
  let min = Infinity;
  let max = -Infinity;
  series.forEach((x) =>
    x.values.forEach((v) => {
      if (v > 0 && isFinite(v)) {
        if (v < min) min = v;
        if (v > max) max = v;
      }
    }),
  );
  if (min === Infinity) return <p className="sec-note">No curve data.</p>;
  const lo = Math.log10(min);
  const hi = Math.log10(max);
  const span = hi - lo || 1;
  const n = Math.max(...series.map((x) => x.values.length));
  const px = (i: number) => pad.l + (i / Math.max(n - 1, 1)) * (w - pad.l - pad.r);
  const py = (v: number) =>
    pad.t + (1 - (Math.log10(Math.max(v, 1e-9)) - lo) / span) * (h - pad.t - pad.b);

  // Decade gridlines, so the axis reads 100 / 1k / 10k rather than at interpolated values.
  const ticks: number[] = [];
  for (let e = Math.floor(lo); e <= Math.ceil(hi); e++) {
    const v = Math.pow(10, e);
    if (v >= min * 0.5 && v <= max * 2) ticks.push(v);
  }

  /* Where each line ends, and where its NAME ends up — not the same y once two books
   * finish within a few pixels of each other. One pass down pushes every label clear of
   * the one above it; one pass up pulls the pile back inside the box from the bottom.
   * Without the second, a sheet whose lines all finish high runs its last labels off the
   * canvas, which is the failure mode of every end-labelled chart that only sorts. */
  const GAP = 15;
  const ends: { s: Series; x: number; y: number; v: number; ly: number }[] = [];
  series.forEach((x) => {
    let i = x.values.length - 1;
    while (i >= 0 && !(x.values[i] > 0)) i--;
    if (i >= 0) ends.push({ s: x, x: px(i), y: py(x.values[i]), v: x.values[i], ly: 0 });
  });
  ends.sort((a, b) => a.y - b.y);
  let floorY = pad.t + 5;
  ends.forEach((p) => {
    p.ly = Math.max(p.y, floorY);
    floorY = p.ly + GAP;
  });
  let ceilY = h - pad.b - 2;
  for (let i = ends.length - 1; i >= 0; i--) {
    ends[i].ly = Math.min(ends[i].ly, ceilY);
    ceilY = ends[i].ly - GAP;
  }
  const lx = w - pad.r + 12;
  const first = dates && dates[0];
  const last = dates && dates[dates.length - 1];

  return (
    <svg
      className="chart chart-board"
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label={series.map((x) => x.label).join(" versus ")}
    >
      {ticks.map((v) => (
        <g key={v}>
          <line x1={pad.l} x2={w - pad.r} y1={py(v)} y2={py(v)} stroke="var(--hair)" strokeWidth={1} />
          <text
            x={pad.l - 6}
            y={py(v) + 3.5}
            textAnchor="end"
            fontSize={9}
            fill="var(--muted)"
            fontFamily="var(--mono)"
          >
            {v >= 1000 ? v / 1000 + "k" : v}
          </text>
        </g>
      ))}
      {series.map((x, k) => (
        <polyline
          key={k}
          points={x.values
            .map((v, i) => (v > 0 ? `${px(i)},${py(v)}` : ""))
            .filter(Boolean)
            .join(" ")}
          fill="none"
          stroke={x.color}
          strokeWidth={x.w || 1.5}
          strokeLinejoin="round"
          strokeDasharray={x.dash}
          vectorEffect="non-scaling-stroke"
        />
      ))}
      {ends.map((p, k) => (
        <g key={k}>
          <line
            x1={p.x}
            y1={p.y}
            x2={lx - 5}
            y2={p.ly}
            stroke={p.s.color}
            strokeWidth={1}
            opacity={0.5}
            vectorEffect="non-scaling-stroke"
          />
          <circle cx={p.x} cy={p.y} r={2.4} fill={p.s.color} />
          <text x={lx} y={p.ly + 3.6} fontSize={11} fontFamily="var(--sans)" fill="var(--ink)">
            {clip(p.s.label, 15)}
            <tspan fill="var(--muted)" fontFamily="var(--mono)">
              {" " + fmtMoney(p.v * 100)}
            </tspan>
          </text>
        </g>
      ))}
      {first ? (
        <>
          <text x={pad.l} y={h - 4} fontSize={9} fill="var(--muted)" fontFamily="var(--mono)">
            {first}
          </text>
          <text
            x={w - pad.r}
            y={h - 4}
            textAnchor="end"
            fontSize={9}
            fill="var(--muted)"
            fontFamily="var(--mono)"
          >
            {last}
          </text>
        </>
      ) : null}
    </svg>
  );
}

export default function BoardChart({
  cls, tf, picked, colorOf, touched,
}: {
  cls: string;
  tf: string;
  /** The ticked rules, in the order they were picked. */
  picked: string[];
  colorOf: (rule: string) => string;
  /** False while the selection is still the sheet's own seeded five. */
  touched: boolean;
}) {
  const group = CLASS_GROUP[cls] ?? cls;
  const [loaded, setLoaded] = useState<Loaded | null>(null);

  useEffect(() => {
    let live = true;
    setLoaded(null);
    loadCurves(group, tf).then((d) => {
      // The sheet was switched while the fetch was in flight; that answer is about a
      // different chart now.
      if (live) setLoaded(d);
    });
    return () => {
      live = false;
    };
  }, [group, tf]);

  const data = loaded?.data;
  const drawn = data ? picked.filter((r) => data.rules?.[r]) : [];
  const missing = data ? picked.filter((r) => !data.rules?.[r]) : [];

  const series: Series[] = [];
  if (data?.bench?.length)
    series.push({ label: "Buy & hold", values: data.bench, ...BENCH_STYLE });
  drawn.forEach((r) =>
    series.push({ label: stemName(r), values: data!.rules[r], color: colorOf(r), w: 1.6 }),
  );

  /* `bc-note` says which of the two states is on screen — the sheet's own opening
   * position, or a selection the reader built. A count alone cannot tell them apart. */
  const note = !data
    ? ""
    : !drawn.length
      ? "buy & hold only — tick a strategy below to add its book"
      : touched
        ? `${drawn.length} of ${LB_SEL_MAX} strategies · buy & hold always drawn`
        : `the top ${drawn.length} on this sheet · tick or untick below to change · buy & hold always drawn`;

  return (
    <section className="sec" id="board-chart-sec">
      {/* The chart sits ABOVE the ranking because the ticking happens in the ranking: the
          retired Compare page put this picture one navigation away from the checkboxes
          that build it, so adding a line meant a round trip. Buy-and-hold is drawn whether
          or not anything is selected — an empty chart with an explanation is a worse
          landing than the one line every row on the table is scored against. */}
      <div className="sec-head">
        <h2>Cumulative P&amp;L</h2>
        <span className="sec-note">{note}</span>
      </div>
      <div className="panel" id="board-chart">
        {!loaded ? (
          <p className="sec-note">Loading equity curves…</p>
        ) : !data || !data.dates ? (
          <p className="sec-note">
            No equity curves are published for this sheet
            {loaded.error ? ` (${loaded.error})` : ""}, so the ranking below stands on its own.
          </p>
        ) : (
          <>
            <PnlLines series={series} dates={data.dates} />
            <p className="sec-note">
              Growth of 100 on a log scale, each book <b>as traded</b> — not risk-matched,
              so a line that simply stayed invested longer finishes higher for that reason
              alone. <b>book vs B&amp;H</b> in the table below prices that exposure away,
              and each strategy&apos;s own page draws the risk-matched version of this
              chart. Idle capital earns nothing, on every line.
              {missing.length ? (
                <>
                  {" "}
                  No curve is stored for{" "}
                  {missing.map((r, i) => (
                    <span key={r}>
                      {i ? ", " : ""}
                      <b>{stemName(r)}</b>
                    </span>
                  ))}{" "}
                  on this sheet.
                </>
              ) : null}
            </p>
          </>
        )}
      </div>
    </section>
  );
}
