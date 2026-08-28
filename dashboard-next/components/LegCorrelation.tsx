"use client";

/* IS THIS FIVE BETS OR ONE?
 *
 * The honest question about any basket assembled here, and the reason it is a first-class
 * panel rather than a detail: five rules picked off one leaderboard all trade the same
 * universe on the same bars, so they can be five names for one exposure. Combining rules
 * that each fail this repo's acceptance gates does not produce one that passes, and
 * combining five copies of one rule does not diversify anything.
 *
 * A CORRELATION MATRIX IS THE WRONG ANSWER TO IT. An n x n grid of three-decimal numbers is
 * a lookup table: it answers "what is the correlation of leg 2 with leg 4" — which nobody
 * asks — and answers "are these one bet" only after the reader has done the arithmetic
 * themselves. So the panel is built the other way round, in three layers, coarse first:
 *
 *   1. ONE NUMBER. `n^2 / sum(rho)` — how many independent, equally-sized bets would have
 *      this basket's variance. Five legs at rho=0.9 is 1.2 bets, and that sentence is the
 *      whole finding.
 *   2. THE PAIRS THAT DRIVE IT, sorted, with the two legs named. Anybody who wants to act
 *      on the number needs to know which pair to drop.
 *   3. Each leg's average with the rest, so a single leg carrying the redundancy is visible
 *      without reading ten pairs.
 *
 * COLOUR. Green and red mean gained and lost on this site, so a correlation heat-map's tint
 * would be read as profit and loss — which is exactly the mistake the stylesheet's palette
 * argument exists to prevent. Magnitude is therefore carried by BAR LENGTH from a centre
 * line and by nothing else, in one neutral ink; the sign is which side of the line the bar
 * is on, and it is also printed. The only hues here are the six series ones, used for leg
 * IDENTITY and assigned in their fixed order — the same slot a leg has on the chart above,
 * so a swatch means one leg on both.
 */

import { SERIES_COLORS } from "@/lib/columns";
import { fmtNum } from "@/lib/format";
import { readCorr, type Blend } from "@/lib/portfolio";

/** How many pair rows before the list stops being a glance. Ten is two screenfuls of nothing
 *  on a 25-leg basket, which has 300 pairs — the ones that matter are the top of the sort. */
const MAX_PAIRS = 10;

/** Beyond this many legs the per-leg summary earns its place: with three legs it is the
 *  pair list again with the arithmetic done. */
const PER_LEG_FROM = 4;

const hue = (i: number) =>
  i < SERIES_COLORS.length ? SERIES_COLORS[i] : "var(--muted)";

/** rho in [-1, 1] drawn from a centre line. Length is the whole encoding. */
function CorrBar({ rho }: { rho: number }) {
  const v = Math.max(-1, Math.min(1, rho));
  const half = Math.abs(v) * 50;
  return (
    <span className="corr-track" aria-hidden="true">
      <span className="corr-mid" />
      <span
        className="corr-fill"
        style={v >= 0 ? { left: "50%", width: `${half}%` } : { right: "50%", width: `${half}%` }}
      />
    </span>
  );
}

function Swatch({ i }: { i: number }) {
  return <i className="corr-dot" style={{ background: hue(i) }} aria-hidden="true" />;
}

export function LegCorrelation({ blend }: { blend: Blend }) {
  const c = readCorr(blend.corr);
  const names = blend.corrLabels;
  const name = (i: number) => names[i] ?? `leg ${i + 1}`;

  if (c.n < 2 || c.mean == null) {
    return (
      <p className="sec-note">
        {blend.legs.length < 2
          ? "One leg, so there is nothing to be correlated with."
          : "The blend returned no correlation between the legs, so whether this basket is " +
            "several bets or one cannot be answered here. It is the first question to ask " +
            "of it, so treat the absence as an open question rather than a clean bill."}
      </p>
    );
  }

  const eff = c.effective;
  /* The sentence, and it is deliberately blunt at the top of the range. `effective` counts
     bets, and a basket of five that behaves like one and a bit is not a portfolio — it is
     one position held five times, and the reader has to meet that before the money. */
  const verdict =
    eff == null
      ? "how many independent bets this is cannot be worked out from this matrix"
      : eff < 1.5
        ? "these legs are close to one bet held several times over"
        : eff < c.n * 0.5
          ? "fewer than half as many bets as there are legs"
          : eff < c.n * 0.8
            ? "somewhat fewer bets than legs"
            : "close to as many bets as there are legs";

  const shown = c.pairs.slice(0, MAX_PAIRS);
  const top = c.pairs[0];

  return (
    <>
      <div className="strip">
        <div className="stat">
          <span className="k">Legs</span>
          <span className="v">{c.n}</span>
          <span className="s">held together, equal weight</span>
        </div>
        <div className="stat">
          <span className="k">Independent bets</span>
          <span className="v">{eff == null ? "—" : `≈ ${fmtNum(eff, 1)}`}</span>
          <span className="s">
            <span
              className="explains"
              title={
                "n squared over the sum of every entry in the correlation matrix: the " +
                "number of independent, equally-sized bets whose equal-weight blend would " +
                "have this basket's variance. It is n when the legs are uncorrelated and 1 " +
                "when they are the same bet. It assumes the legs carry similar volatility " +
                "— which is what equal weighting already assumes — so it is an " +
                "approximation, and it is printed as one."
              }
            >
              {verdict}
            </span>
          </span>
        </div>
        <div className="stat">
          <span className="k">Typical pair</span>
          <span className="v">{fmtNum(c.mean, 2)}</span>
          <span className="s">
            mean of the {c.pairs.length} pair{c.pairs.length === 1 ? "" : "s"} · range{" "}
            {fmtNum(c.min, 2)} to {fmtNum(c.max, 2)}
          </span>
        </div>
        {top && (
          <div className="stat">
            <span className="k">Most alike</span>
            <span className="v">{fmtNum(top.rho, 2)}</span>
            <span className="s">
              {name(top.a)} and {name(top.b)}
            </span>
          </div>
        )}
      </div>

      <div className="corr-list">
        {shown.map((p) => (
          <div className="corr-row" key={`${p.a}-${p.b}`}>
            <span className="corr-name">
              <Swatch i={p.a} />
              {/* Each label truncates on its own, so a long name eats its own row rather
                  than the other name's — the pair is only readable if BOTH ends are. */}
              <span className="corr-lbl" title={name(p.a)}>{name(p.a)}</span>
              <span className="corr-x">×</span>
              <Swatch i={p.b} />
              <span className="corr-lbl" title={name(p.b)}>{name(p.b)}</span>
            </span>
            <CorrBar rho={p.rho} />
            <span className="corr-val num">{fmtNum(p.rho, 2)}</span>
          </div>
        ))}
      </div>

      {c.pairs.length > shown.length && (
        <p className="sec-note">
          The {shown.length} most alike of {c.pairs.length} pairs. The rest are less
          correlated than the last row above, so they are not what is driving the count.
        </p>
      )}

      {c.n >= PER_LEG_FROM && (
        <div className="corr-list corr-legs">
          {c.perLeg.map((v, i) =>
            v == null ? null : (
              <div className="corr-row" key={i}>
                <span className="corr-name">
                  <Swatch i={i} />
                  <span className="corr-lbl" title={name(i)}>{name(i)}</span>
                </span>
                <CorrBar rho={v} />
                <span className="corr-val num">{fmtNum(v, 2)}</span>
              </div>
            ),
          )}
        </div>
      )}

      <p className="sec-note" style={{ maxWidth: "72ch" }}>
        {c.n >= PER_LEG_FROM
          ? "The second list is each leg against the average of the others — a leg near the top of it is where the redundancy lives. "
          : ""}
        Bars run left and right of the centre line: length is how alike, side is the sign.{" "}
        <span
          className="explains"
          title={
            "Correlation of the legs' own book curves over the span they share — the " +
            "research measurement, on the intersection of their histories, not the desk's " +
            "record. The blend does not net overlapping positions between legs either: two " +
            "legs long the same name make the portfolio more long that name, which is what " +
            "an equal-weight basket of strategies is and what the live desk will actually " +
            "do."
          }
        >
          measured on the research curves, over the overlap
        </span>
        .
      </p>
    </>
  );
}
