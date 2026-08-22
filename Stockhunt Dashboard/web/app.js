/* Two sections, hash-routed, so either can be bookmarked and sent on its own:
 *   #/paper                        every system running in the sandbox, ranked
 *   #/paper/sys/<cls>/<tf>/<rule>  one SYSTEM: its live record and every name it holds
 *   #/paper/<id>                   one deployment's live paper progress
 *   #/backtest                     research leaderboards, per asset class and timeframe
 *   #/backtest/<cls>/<tf>/<rule>   one rule, broken down asset by asset
 *
 * Paper and backtest are separate sections rather than two panels on one screen. They
 * are different periods and different sample sizes — weeks of simulated fills against
 * years of walk-forward out-of-sample — and putting them side by side invites the
 * conclusion that a good paper fortnight validates a rule the research scores negative.
 */
const D = window.DASH;
const $ = (s, r = document) => r.querySelector(s);
const app = $("#app");

const fmtPct = (v, d = 2) => v == null ? "—"
  : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d) + "%";
const fmtIR = v => v == null ? "—" : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(3);
/* Annualised growth, printed unsigned: 17.51% reads as a rate, +17.51% reads as a gain. */
const fmtCagr = (v, d = 1) => v == null ? "—" : v.toFixed(d) + "%";
/* A CAGR *difference*, always signed, and with the typographic minus `fmtDelta` uses so
 * the two comparison columns line up against each other. */
const fmtCagrDelta = (v, d = 1) => v == null ? "—"
  : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d) + "%";
const fmtNum = (v, d = 1) => v == null ? "—" : Number(v).toFixed(d);
/* Signed, with the typographic minus the rest of the page uses. `toFixed` emits an ASCII
 * hyphen, which sits a different height and width to "−" and makes a column of numbers
 * look misaligned next to one formatted by fmtIR. */
const fmtSigned = (v, d = 2) => v == null ? "—"
  : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d);

/* ---------- P&L: what a fixed stake became ----------
 * A percentage return over 41 years is unreadable (+74,735%) and a percentage-point gap
 * against the benchmark is worse (−89,644 points, which is not a quantity that means
 * anything). The same result as money — $10k became $7.5M against $16.4M holding — is
 * immediately legible, and the ratio underneath says the rest: 0.45x buy-and-hold.
 */
const STAKE = 10000;
const grew = pct => pct == null ? null : STAKE * (1 + pct / 100);
const fmtMoney = v => {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return "$" + (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return "$" + (v / 1e3).toFixed(0) + "k";
  return "$" + v.toFixed(0);
};
/* P&L against the benchmark, as money.
 *
 * The first version of this divided the rule's profit by the benchmark's, and it was
 * wrong in exactly the case that matters most. AVAX/USD buy-and-hold LOST money over the
 * window ($10k -> $1k), so the denominator went negative and the rule — which turned the
 * same $10k into $14k and scores IR +0.354 — was rendered as "-0.47x" in red beside a
 * verdict of "beat". A ratio to a negative base carries no meaning and flips its own sign.
 *
 * The difference in final value has no such hole: it is signed correctly whether the
 * benchmark rose or fell, it reads directly ("$13k more than holding"), and it needs no
 * caveat. The multiple is kept only for the sheet headline, where the benchmark is a broad
 * index that did make money — and even there it is suppressed if it did not. */
const pnlDelta = (net, bh) => net == null || bh == null ? null : grew(net) - grew(bh);
const fmtDelta = v => v == null ? "—"
  : (v >= 0 ? "+" : "−") + fmtMoney(Math.abs(v));
const pnlRatio = (net, bh) => {
  if (net == null || bh == null) return null;
  const b = grew(bh) - STAKE;
  return b <= 0 ? null : (grew(net) - STAKE) / b;      // undefined against a losing base
};
const fmtRatio = v => v == null ? "—"
  : (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2)) + "×";
const money = v => "$" + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
/* A signed P&L in dollars, to the cent. `null` prints an em-dash rather than $0.00: on a
 * fill's realised P&L those are different facts — "closed nothing" and "closed at cost". */
const cash = v => v == null ? "—"
  : (v >= 0 ? "+" : "−") + "$" + Math.abs(v).toLocaleString(undefined,
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const sign = v => v > 0 ? "gain" : v < 0 ? "loss" : "flat";
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const slug = s => s.replace(/[^A-Za-z0-9_]/g, "-");

const STATUS = { running: ["run", "live"], warming: ["warm", "warming up"], halted: ["halt", "halted"] };
const statusChip = s => { let [c, l] = STATUS[s.status] || ["mut", s.status];
  // A replayed run is "running" in the state file because the strategy genuinely ran —
  // but labelling it "live" on screen would be a lie about where the bars came from.
  if (l === "live" && isReplay()) [c, l] = ["mut", "replay"];
  return `<span class="chip ${c}">${l}</span>`; };
/* Fractional instruments hold positions to 6dp. 2624.767935 units is precision nobody
 * reads; 4 significant decimals keeps crypto legible without turning shares into 2,625. */
const fmtUnits = v => !v ? "—" : Math.abs(v) >= 100 ? v.toFixed(2)
  : Math.abs(v) >= 1 ? v.toFixed(3) : v.toFixed(6);
const stateCell = s => `<span class="pos-${s.state}">${s.state}</span>`;
/* ---------- the acceptance standard, on the row ----------
 * `D.edge_criteria` comes from `config.GATES`, so the letters and their order are whatever
 * the standard currently says rather than a copy here that can drift from it.
 */
/* `n of 6`, not the STRCWH letter strip. The strip encoded which criteria passed in a
 * six-character monogram nobody could read without the legend, and the legend was the
 * column header — so the header stopped naming the column and started being a key. The
 * count is the number a reader actually wants; *which* six is a tooltip, where detail
 * that matters occasionally belongs. */
const edgeCount = e => {
  if (e == null) return '<span class="gates none">—</span>';
  const named = (D.edge_criteria || []).map((c, i) =>
    `${e.gates[i] ? "✓" : "✗"} ${c.k}  ${c.target}  ${c.name}`).join("\n");
  const cls = e.verdict === "PASS" ? "" : " none";
  /* The T criterion's printed target is ">= 2.0", which is the bar for a single
   * pre-specified test and not for this one: searching ~400 candidates raises it. A
   * reader seeing T failed beside "target >= 2.0" and a t of 2.81 is owed the number it
   * actually had to clear, and where that number came from. */
  const bar = e.t_bar == null ? "" :
    `\n\nT is scored against ${e.t_bar.toFixed(2)}, not 2.0: ${
      e.n_candidates ? e.n_candidates + " candidates were" : "the panel was"} searched${
      e.t_bar_source === "maxT"
        ? `, and that bar is measured by sign-flip permutation of this sheet's own per-fold edges${
            e.t_bar_bonferroni
              ? ` — Bonferroni would have assumed ${e.t_bar_bonferroni.toFixed(2)}` : ""}`
        : ""}.`;
  const why = (e.verdict === "underpowered"
    ? `too few folds to resolve — cannot tell, not "no"\n\n${named}` : named) + bar;
  return `<span class="gates${cls}" title="${esc(why)}">${e.passed}/${e.n}</span>`;
};

/* A figure shown against the benchmark's own value rather than alone, and coloured by that
 * comparison. Raw Sharpe especially: on a rising market it largely measures how much of the
 * time a rule was invested, so a bare 0.66 looks like skill until you see buy-and-hold
 * scored 0.63 over the same bars. The comparison is the number; the level is context.
 * Blanks on a missing value rather than colouring an em-dash. */
const vsCell = (v, bench, fmt, better, tip) => {
  if (v == null) return '<td class="flat">—</td>';
  const cls = bench == null ? "" : better(v, bench) ? "gain" : "loss";
  const t = bench == null ? "" : ` title="${esc(`buy & hold: ${fmt(bench)}${tip ? " — " + tip : ""}`)}"`;
  return `<td class="${cls}"${t}>${fmt(v)}</td>`;
};
const fmtSharpe = v => fmtNum(v, 3);
const fmtDD = v => fmtNum(v, 1) + "%";
/* ---------- book cells ----------
 *
 * The leaderboard reads one measurement now: `r.book`, from `book_<class>_<tf>.csv`. These
 * are the shared renderers for it. Every one blanks when there is no book record — never
 * falls back to `r.edge`, which is the median single asset over a different span. Mixing
 * the two inside one column is the defect this replaced, and a fallback would restore it
 * invisibly on exactly the rows where the book run is missing.
 */
const bookExposure = r => (r.book && r.book.exposure != null) ? r.book.exposure : null;
const bookNum = (r, v, fmt) => v == null
  ? `<td class="flat">—</td>` : `<td>${fmt(v)}</td>`;
/* The book's drawdown against the same universe held passively. `sh.book_bench.dd` is one
 * figure for the whole sheet — the passive book is the same portfolio on every row — so it
 * is passed in rather than read off the row, which carries no benchmark drawdown of its
 * own. Rows with no book run print an em-dash rather than falling back to the per-asset
 * number, which would put two different measurements in one column. */
const bookDdCell = (r, bench) => vsCell(r.book && r.book.dd, bench, fmtDD, (a, b) => a > b,
  "worst peak-to-trough fall of the passive book over the same bars");
/* Profit factor is scored against 1.0, not against the benchmark: buy-and-hold holds one
 * position for the whole window, so it has no closed losing trade to divide by and no
 * profit factor to compare with. Break-even is the only honest reference. */
/* How many positions the rule opened on a typical asset. Not coloured — trading a lot is
 * neither good nor bad on its own — but it is what makes the profit factor beside it
 * readable: 1,283 trades is a distribution, 3 is an anecdote. Counted per asset because
 * the sheet pools twenty symbols and a combined total cannot be sized against a holding
 * period. */
const tradesCell = e => {
  const v = e && e.trades;
  if (v == null) return '<td class="flat">—</td>';
  const why = v < 30
    ? `${v} trades on a typical asset — too few to read the profit factor as a rate`
    : `${v} positions opened on a typical asset, out-of-sample`;
  return `<td class="flat" title="${esc(why)}">${Math.round(v).toLocaleString()}</td>`;
};

/* What the median asset's $10,000 became, with its benchmark on the SAME basis in the
 * tooltip — the median asset held, not the mean.
 *
 * That distinction is the whole reason this cell needs a tooltip at all. The sheet's prose
 * used to quote the benchmark as $16.4M, which is `wf_summary`'s figure and is a MEAN
 * across assets; this column is a MEDIAN, and the median asset held becomes $1.65M. A
 * reader comparing the column against the quoted number was comparing a median against a
 * mean and would conclude a rule beating buy-and-hold by 6.9x had lost to it by a third.
 * Same $10,000, same twenty assets, two different summary statistics an order of magnitude
 * apart — quote the one that matches the column. */
/* The book's terminal wealth, coloured against the book's OWN buy-and-hold.
 *
 * The colour is the RAW money question — did this account end up with more than holding —
 * and deliberately not the risk-matched one, even though the risk-matched figure is what
 * the table is ranked on. The two genuinely disagree and the split is the point: on
 * us_stocks 1d `MAXINDEX~HT_PHASOR|and` clears holding by +1.44%/yr at equal risk while
 * ending on $81k against the benchmark's $191k, because it was only invested 46% of the
 * time. Colour that cell green and the reader is told they made money they did not make;
 * put the risk-matched verdict in its own column, in its own colour, and both facts
 * survive. Money here, skill next door.
 *
 * Not coloured against the per-asset benchmark either — that is the median asset over
 * different bars, so a rule would be painted for clearing a bar it was never compared
 * with. That column is gone from this table for the same reason. */
const bookWealthCell = b => {
  if (b == null) return '<td class="flat">—</td>';
  const bw = b.bench_wealth;
  const cls = bw == null ? "" : b.wealth > bw ? "gain" : "loss";
  const mult = bw > 0 ? b.wealth / bw : null;
  const t = (bw == null ? "" : `holding the same universe over the same bars returns ${
      fmtMoney(bw)}${mult ? ` — this is ${fmtNum(mult, 2)}x that` : ""}`)
    + (b.exposure == null ? "" : `, and it was invested ${fmtNum(b.exposure * 100, 0)}% of the time`)
    + (b.cm_excess_cagr == null ? ""
      : `. At equal risk: ${fmtPct(b.cm_excess_cagr * 100, 2)}/yr`
        + (b.cm_ratio ? `, ${fmtNum(b.cm_ratio, 1)}x the money` : ""));
  return `<td class="${cls}" title="${esc(t)}">${fmtMoney(b.wealth)}</td>`;
};

const pfCell = (e, longFrac) => {
  const v = e && e.profit_factor;
  if (v == null) return '<td class="flat">—</td>';
  // A rule that is in the market ~always closes almost nothing, so its profit factor is a
  // couple of trades rather than a distribution — the same reason the benchmark has none.
  // Greyed rather than hidden: the number is real, it is just not comparable with the
  // 1,283-trade rule above it, and the Long % flag on the same row says why.
  const uncountable = longFrac != null && longFrac > 0.9;
  const cls = uncountable ? "flat" : v > 1 ? "gain" : "loss";
  const why = uncountable
    ? "barely closes a trade at this exposure — not comparable with a rule that turns over"
    : "gross winnings ÷ gross losses, per closed trade — 1.00 is break-even. "
      + "Buy-and-hold never closes a trade, so it has none to compare.";
  return `<td class="${cls}" title="${esc(why)}">${fmtNum(v, 2)}</td>`;
};

/* ---------- charts: plain SVG, no library, legible in both themes ---------- */
function sparkline(series, w = 260, h = 30) {
  const lo = Math.min(...series), hi = Math.max(...series), span = hi - lo || 1;
  const pts = series.map((v, i) =>
    `${(i / (series.length - 1)) * w},${h - ((v - lo) / span) * (h - 4) - 2}`).join(" ");
  const c = series[series.length - 1] >= series[0] ? "var(--gain)" : "var(--loss)";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <polyline points="${pts}" fill="none" stroke="${c}" stroke-width="1.5"
      vector-effect="non-scaling-stroke"/></svg>`;
}

/* Equity chart on a log scale.
 *
 * Linear is unreadable here: over 41 years the curve ends ~1,500x its start, so the first
 * three decades — including every drawdown that matters — compress into a flat line along
 * the axis. On a log scale equal vertical distances are equal *percentage* moves, which is
 * what a reader is actually comparing when two curves diverge. Zero and negative equity
 * cannot be plotted, but equity is a product of (1+r) with r floored above -1, so it never
 * reaches zero.
 */
function equityChart(sets, labels, dates, h = 230) {
  const w = 960, pad = { l: 46, r: 8, t: 12, b: 20 };
  const all = sets.flat().filter(v => v > 0);
  if (!all.length) return `<p class="sec-note">No curve data.</p>`;
  const lo = Math.log10(Math.min(...all)), hi = Math.log10(Math.max(...all));
  const span = (hi - lo) || 1;
  const n = Math.max(...sets.map(s => s.length));
  const x = i => pad.l + (i / Math.max(n - 1, 1)) * (w - pad.l - pad.r);
  const y = v => pad.t + (1 - (Math.log10(Math.max(v, 1e-9)) - lo) / span) * (h - pad.t - pad.b);

  // Gridlines on decade boundaries, so the axis reads 100 / 1k / 10k rather than at
  // arbitrary interpolated values.
  const ticks = [];
  for (let e = Math.floor(lo); e <= Math.ceil(hi); e++) {
    const v = Math.pow(10, e);
    if (v >= Math.min(...all) * 0.5 && v <= Math.max(...all) * 2) ticks.push(v);
  }
  const grid = ticks.map(v => `
    <line x1="${pad.l}" x2="${w - pad.r}" y1="${y(v)}" y2="${y(v)}"
      stroke="var(--hair)" stroke-width="1"/>
    <text x="${pad.l - 6}" y="${y(v) + 3.5}" text-anchor="end" font-size="9"
      fill="var(--muted)" font-family="var(--mono)">${v >= 1000 ? (v / 1000) + "k" : v}</text>`).join("");

  // Three series, and the third needs its own stroke: `colors[2]` was undefined, which SVG
  // renders as no line at all rather than as an error. `--ink-2` sits between the solid
  // rule and the muted basket, and a longer dash keeps the two dashed lines apart at the
  // scale these are drawn — the legend alone cannot do it when both are grey.
  const colors = ["var(--ink)", "var(--muted)", "var(--ink-2)"];
  const dashes = ["", '4 3', '1.5 3'];
  const lines = sets.map((s, k) => `
    <polyline points="${s.map((v, i) => `${x(i)},${y(v)}`).join(" ")}" fill="none"
      stroke="${colors[k] || "var(--muted)"}" stroke-width="${k ? 1.1 : 1.7}"
      ${dashes[k] ? `stroke-dasharray="${dashes[k]}"` : ""}
      vector-effect="non-scaling-stroke"/>`).join("");

  const first = dates && dates[0], last = dates && dates[dates.length - 1];
  const axis = first ? `
    <text x="${pad.l}" y="${h - 4}" font-size="9" fill="var(--muted)"
      font-family="var(--mono)">${first}</text>
    <text x="${w - pad.r}" y="${h - 4}" text-anchor="end" font-size="9" fill="var(--muted)"
      font-family="var(--mono)">${last}</text>` : "";

  return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet"
      role="img" aria-label="${labels.join(" versus ")}">${grid}${lines}${axis}</svg>
    <div class="legend">${labels.map((l, k) =>
      `<span><i class="sw" style="background:${colors[k]}"></i>${esc(l)}</span>`).join("")}
      <span>log scale · growth of 100</span></div>`;
}

/* The equity chart, with every benchmark held at the strategy's own volatility.
 *
 * There is one sizing here and it is the risk-matched one. The chart briefly offered both
 * — "as traded" beside "at equal risk" — and a toggle is the wrong shape for this: full
 * size ranks lines by who took the most risk, so leaving it on screen invites the reading
 * the rest of the page exists to prevent. On us_stocks 1d QQQ ends 26% above `ibs` at full
 * size and a third of it at equal risk; only the second of those is about the strategy.
 *
 * A benchmark is scaled DOWN with cash, never levered up — no margin, no borrow, nothing
 * that needs an account upgrade. The weight is on every legend entry, so the line can
 * never be mistaken for the instrument itself, and the caption carries the full-size
 * figure so that fact is stated rather than hidden.
 *
 * The blended series comes from `portfolio_wf`; it cannot be derived here, because the
 * weight multiplies each bar's RETURN before it compounds.
 */
function equitySection(c, r, drawn, names, tail) {
  const mm = c.matched || {};
  const byLabel = {};
  (mm.lines || []).forEach(l => { if (l.curve && l.curve.length) byLabel[l.label] = l; });
  // The CHART takes the index lines only — `equityChart` has three distinct strokes and
  // four lines on a log axis is a tangle. The TABLE has no such limit, so it gets every
  // matched line including the sheet's own basket, which is what the leaderboard's
  // verdict is scored against and therefore has to stay a column.
  const matched = drawn.map(i => byLabel[i.symbol]).filter(Boolean);
  const all = (mm.lines || []).filter(l => l && l.metrics);

  // No matched line means a book with no volatility to match anything to — a rule that
  // barely trades. Draw it against the basket at full size rather than nothing, and say so.
  if (!matched.length) {
    return { matched: [], all, html: `<div class="panel">${equityChart(
        [c.curve, c.bench], [esc(r.rule), "Equal-weight basket"], c.dates)}
      <p class="sec-note">This book holds almost nothing, so there is no volatility to
      match a benchmark to; the basket is drawn at full size.</p></div>` };
  }

  const labels = [esc(r.rule),
                  ...matched.map(l => `${esc(l.label)} · ${fmtNum(l.weight * 100, 0)}%`)];
  const full = matched.filter(l => l.raw_wealth);
  return { matched, all, html: `<div class="panel">${
    equityChart([c.curve, ...matched.map(l => l.curve)], labels, c.dates)}
    <p class="sec-note">Every line starts at 100 and covers the same out-of-sample bars,
    and <b>every benchmark is held at ${mm.vol_pct == null ? "the book's own"
      : fmtNum(mm.vol_pct, 1) + "%"} volatility — the strategy's — with the rest in
    cash</b>. Scaled down, never levered up: no margin, no borrow. What is left between the
    lines is the signal rather than the risk taken to get it.${full.length
      ? ` At full size these same instruments end on ${full.map(l =>
          `<b>${esc(l.label)}</b> ${fmtMoney(l.raw_wealth)}`).join(", ")}, against
      ${fmtMoney((mm.strategy || {}).wealth)} for the strategy — more, in some cases, and
      more risk with it. Closing that gap the other way means gearing the strategy up,
      which needs margin and is not tested anywhere here.` : ""}
    This is the <b>book</b>: the same series the <b>$10k / book</b> column is computed
    from. Idle capital earns nothing, on every line.${
      tail != null ? tail
      : r.per_asset && r.per_asset.length
      ? ` How the rule did name by name is the <em>Asset by asset</em> table below —
      single-name backtests, which will not add up to this.`
      /* A pair has no per-symbol rows to point at, so it gets the breadth figure it does
         have instead of a pointer to a table that is not on the page. */
      : ` A pair has no per-name table — the sweep records leg diagnostics instead — so
      breadth is all this sheet knows about where it worked.`}</p></div>` };
}

/* `lineChart` lived here and drew its reference line at 100 — an INDEX baseline. Its
 * only caller was the paper detail page, whose series is cumulative P&L in percent, so
 * the reference was off the top of every chart it drew. `pnlLive` replaced it and the
 * function went with the bug rather than staying as a second convention nobody wants. */

function bindGo(root) {
  root.querySelectorAll("[data-go]").forEach(el => {
    el.onclick = () => { location.hash = el.dataset.go; };
    /* Anything that carries a `tabindex` was put in the tab order to be reached by
     * keyboard, and a focusable thing that only answers the mouse is worse than one that
     * cannot be focused at all. Table rows are not focusable and are unaffected. */
    if (el.hasAttribute("tabindex")) {
      el.onkeydown = e => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          location.hash = el.dataset.go;
        }
      };
    }
  });
  enhanceTables(root);
}

/* ---------- a way across a wide table ----------
 * The leaderboard is twelve columns and does not fit a laptop. `overflow-x:auto` alone is
 * a silent affordance: the scrollbar appears only once you are already scrolling, and on a
 * trackpad it never appears at all, so the last four columns read as "not there" rather
 * than "further right".
 *
 * Applied here rather than in the six templates that draw a table: the markup stays a
 * plain `<table>` in a plain wrap, and a table added later is covered without being told
 * about any of this. Called from bindGo, so it re-applies after every repaint — paintPaper
 * rewrites innerHTML on each tick and takes the wrappers with it.
 */
function enhanceTables(root) {
  root.querySelectorAll(".tbl-wrap").forEach(wrap => {
    if (wrap.parentElement && wrap.parentElement.classList.contains("tbl-scroll")) return;

    const box = document.createElement("div");
    box.className = "tbl-scroll";
    wrap.parentNode.insertBefore(box, wrap);
    box.appendChild(wrap);

    for (const side of ["l", "r"]) {
      // The button lives outside the scroller, in a full-height rail, so it holds its
      // place while the columns move underneath it.
      const rail = document.createElement("div");
      rail.className = `tbl-rail ${side}`;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tbl-btn";
      b.setAttribute("aria-label", side === "l" ? "Scroll columns left" : "Scroll columns right");
      b.textContent = side === "l" ? "‹" : "›";
      // Four fifths of a screenful, not a pixel count: one press moves the same fraction
      // of the table on a phone as on a wide monitor, and the overlap keeps a column of
      // context either side of the jump.
      b.onclick = () => wrap.scrollBy({
        left: (side === "l" ? -1 : 1) * wrap.clientWidth * 0.8, behavior: "smooth" });
      rail.appendChild(b);
      box.appendChild(rail);
    }

    // Measured now, before `.wide` exists, and never again: once the table has been let
    // out of the text column it is stretched to fill whatever it was given, so its width
    // stops saying anything about what it needs. `natural` is the honest number, and
    // comparing it against the *parent* — which the breakout does not touch — keeps the
    // test stable instead of oscillating in and out of its own effect.
    const natural = wrap.scrollWidth;

    // Each edge shows a button only when there is table behind it, so a narrow table that
    // already fits shows nothing at all — and on a wide screen the breakout is usually
    // what removes the overflow, leaving no buttons to press.
    // The section's heading rule follows the table out of the text column, so the underline
    // ends where the last column does. Read back off the DOM rather than from `natural`, so
    // a section holding two tables is wide if either of them is, whichever synced last.
    const sec = wrap.closest(".sec");
    const secHead = sec && sec.querySelector(":scope > .sec-head");

    const sync = () => {
      const room = box.parentElement ? box.parentElement.clientWidth : wrap.clientWidth;
      box.classList.toggle("wide", natural > room + 1);
      if (secHead) secHead.classList.toggle("wide", !!sec.querySelector(".tbl-scroll.wide"));
      const max = wrap.scrollWidth - wrap.clientWidth;
      box.classList.toggle("has-l", wrap.scrollLeft > 1);
      box.classList.toggle("has-r", wrap.scrollLeft < max - 1);
    };
    wrap.addEventListener("scroll", sync, { passive: true });
    // The measurement that matters is the container's width, and that changes on resize
    // and on rotate without any scroll event firing.
    if (window.ResizeObserver) new ResizeObserver(sync).observe(wrap);
    sync();
  });
}

/* Filters swap only the data region. Re-rendering the whole view on every click
 * repainted the header and hero and threw the scroll position back to the top —
 * the buttons appeared to reload the page. Now the hero, the filter row and the
 * scroll position all stay put, and only the container below them is rewritten. */
function setActive(attr, value) {
  document.querySelectorAll(`[${attr}]`).forEach(b =>
    b.classList.toggle("on", b.getAttribute(attr) === value));
}

const pills = (opts, active, attr) => opts.map(([v, label]) =>
  `<button class="pill ${active === v ? "on" : ""}" ${attr}="${v}">${esc(label)}</button>`).join("");

/* ================================ PAPER ================================ */
/* No "all" on any of the three. A desk that runs four classes at two horizons for several
 * people has no useful "everything" view — it is a pile, and the strip above it then
 * averages numbers that are not comparable. Each filter opens on a real selection. */
let pf = { cls: "us_stocks", tf: "1d", who: "mine" };

/* The paper filter is built from whatever classes are actually on the desk, not from a
 * hard-coded pair. It was `All / Equities / Crypto`, which was the whole desk until the
 * research gained ETFs and commodities — after which those systems ran, published, and
 * could only be seen under "All", with the strip counting them as neither. A list derived
 * from the data cannot fall behind the desk that way.
 *
 * The order is the research's own, so the pills read the same way as the backtest section's
 * rather than in whatever order the systems happened to register. */
const PAPER_CLASS_ORDER = ["us_stocks", "us_etfs", "crypto", "commodities"];
const PAPER_CLASS_LABEL = {
  us_stocks: "Top 100 stocks", us_etfs: "ETFs", crypto: "Crypto", commodities: "Commodities",
  /* Pre-2026-08-11 records, and any replay written from one, carry the old two-valued
   * class. Labelled rather than renamed: the sid is what identifies a system, so an old
   * row still belongs to the same record and is still worth showing under its own name. */
  equity: "Equities (legacy)",
};
/* Every class the desk CAN run, not just the ones something is deployed on today. The
 * list used to be derived from the live rows, which meant a class you had not promoted
 * anything to yet was simply absent — so there was no way to look at ETFs and see that
 * nothing was running there, which is a fact worth being able to check. Anything unknown
 * that does show up is appended, so an old record still has a home. */
const paperClasses = () => {
  const seen = new Set(D.strategies.map(s => s.cls).filter(Boolean));
  return [...PAPER_CLASS_ORDER,
          ...[...seen].filter(c => !PAPER_CLASS_ORDER.includes(c)).sort()];
};
const paperClassPills = () =>
  paperClasses().map(c => [c, PAPER_CLASS_LABEL[c] || c]);

/* The same argument, one axis over. This strip was the literal pair `1d / 4h` — the two
 * horizons the HOUSE promotes its own books at — while the desk accepts a registration at
 * any of six (`paper_config.MEMBER_TIMEFRAMES`, which is what `/v1/limits` advertises and
 * what the join wizard offers). A member registering at 1h or 5m therefore got a strategy
 * that ran, filled and published, and a board with no button that could reach it.
 *
 * `D.paper_timeframes` carries the desk's own list through `payload.paper_state`, so the
 * two cannot drift again; the constant below is only the fallback for a payload built
 * before the field existed. Coarse to fine, and anything unknown that shows up in the rows
 * is appended rather than dropped — an old record still has a home. */
const PAPER_TF_ORDER = ["1d", "4h", "2h", "1h", "15m", "5m"];
const paperTimeframes = () => {
  const offered = (D.paper_timeframes && D.paper_timeframes.length)
    ? D.paper_timeframes : PAPER_TF_ORDER;
  const seen = new Set(D.strategies.map(s => s.tf).filter(Boolean));
  return [...offered, ...[...seen].filter(t => !offered.includes(t))];
};
const paperTfPills = () => paperTimeframes().map(t => [t, t]);

/* Mine versus everybody else's. The rows carry an `account`, `D.account` says who is
 * looking, and `D.house` names the desk's own — a promoted book belongs to the desk
 * rather than to a person, so the owner reads it as theirs.
 *
 * A member never receives another member's rows at all, so for them this is Mine versus
 * the desk's. The owner does receive everybody's, which is why the split exists: it keeps
 * their page the same SHAPE as a member's, one group at a time, instead of one long
 * mixed list nobody else ever sees. */
const isMine = s => {
  const a = String(s.account || D.house || "00");
  return a === String(D.account) || (D.is_admin && a === String(D.house || "00"));
};
const paperWhoPills = () => [
  ["mine", D.is_admin ? "Mine & the desk" : "Mine"],
  ["others", D.is_admin ? "Members" : "The desk"],
];

/* Nothing is running until the Nautilus node has filled an order and written
 * `results/paper_state.json`. That is the honest state, and it gets its own screen rather
 * than a dashboard of zeroes and em-dashes pretending to be a live desk. */
function paperEmpty() {
  app.innerHTML = `
  <div class="hero">
    <h1>Paper trading</h1>
    <p class="lede">Nothing is trading yet. The sandbox writes its state only once it has
    filled an order, and it has not run long enough to do that.</p>
  </div>

  <div class="strip">
    <div class="stat"><span class="k">Systems live</span><span class="v">0</span>
      <span class="s">node not started</span></div>
    <div class="stat"><span class="k">Data feed</span>
      ${feedValue()}
      ${feedNote(`${esc(D.feed.source)} · ${esc(D.feed.plan)}`)}</div>
    <div class="stat"><span class="k">Sandbox equity</span>
      <span class="v">${money(D.venue.equity)}</span>
      <span class="s">${esc(D.venue.name)}</span></div>
  </div>

  <div class="note">Three things stand between here and a live number: the order path has
  never filled, so it is unproven; nothing yet writes the state file this page reads; and the
  node has to stay up for days to accumulate anything worth looking at. Until then the
  research is the real content — see <a href="#/backtest">Backtest</a>.</div>`;
}

/* A run replayed from cached bars is NOT paper trading, and the difference is not a
 * technicality: it knows the whole price history, it completes in seconds, and its P&L
 * covers years rather than days. It is shown because it proves the order path — but it
 * has to be labelled every time it appears, or the first person to screenshot this page
 * reports a 283% gain as a live result. */
const isReplay = () => D.feed.status === "backtest";

/* ---------- is anyone home? ----------
 * `feed.status` is whatever the node last published, and a process that dies publishes
 * nothing further — so a dead desk keeps showing the state it happened to be in when it
 * went, which for a node killed during start-up is "starting", forever. Read at face value
 * that is indistinguishable from a node still warming its indicators, and this page showed
 * exactly that for five hours.
 *
 * `generated_at` is the heartbeat instead: `run_paper.start_marker` rewrites it once a
 * minute for as long as the process lives. Three marks of slack, because the window is
 * one API call wide and a single failed poll must not raise a false alarm. A replay has no
 * feed and no heartbeat, so it is exempt. */
const STALE_AFTER_MS = 180000;
function feedAgeMs() {
  // "2026-08-11 08:57 UTC" -> parseable. Anything unrecognised returns null, which every
  // comparison below treats as "cannot tell" rather than as stale.
  const t = Date.parse(String(D.generated_at || "").trim()
                        .replace(" UTC", "Z").replace(" ", "T"));
  return Number.isFinite(t) ? Date.now() - t : null;
}
/* `__SNAPSHOT__` is the one-file build, which embeds `live.json` and cannot refresh — it is
 * *meant* to be read months later, so age there says nothing about whether a desk is up. */
const feedStale = () =>
  !isReplay() && !window.__SNAPSHOT__ && feedAgeMs() > STALE_AFTER_MS;
const fmtAge = ms => {
  const m = Math.round(ms / 60000);
  return m < 90 ? `${m} min` : (m < 2880 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} d`);
};
const feedValue = () => feedStale()
  ? `<span class="v loss">stale</span>`
  : `<span class="v ${D.feed.status === "ok" ? "gain" : ""}">${esc(D.feed.status)}</span>`;
const feedNote = sub => feedStale()
  ? `<span class="s loss">no update in ${fmtAge(feedAgeMs())}</span>`
  : `<span class="s">${sub}</span>`;
const staleBanner = () => feedStale()
  ? `<div class="note"><b>The desk is not running.</b> Nothing has been published for
     ${fmtAge(feedAgeMs())} — the last word from the node was
     "${esc(D.feed.status)}" at ${esc(D.generated_at)}. Every figure below is that
     snapshot, not a live number. Restart with <code>python run_paper.py</code>.</div>`
  : "";
const replayBanner = () => isReplay()
  ? `<div class="note"><b>Replay, not live.</b> These figures come from running the live
     strategy over cached historical bars inside Nautilus — the run that proves bars,
     signals, orders and fills all connect. Nothing is trading against a live feed yet, so
     read the P&amp;L as a test of the plumbing and nothing else.</div>` : "";

function paperMaster() {
  if (!D.strategies.length) return paperEmpty();
  app.innerHTML = `
  <div class="hero">
    <h1>${isReplay() ? "Strategy replay" : "Paper trading"}</h1>
    <p class="lede">${isReplay()
      ? `The live strategy class, the live signal layer and the live order path — run over
         cached bars in a Nautilus backtest engine. Same code, historical clock.`
      : `Live simulated fills from the Nautilus sandbox on real Twelve Data bars.`}
    This section is about whether the <em>pipeline</em> works. Whether the rules work is a
    different question, answered in <a href="#/backtest">Backtest</a>.</p>
  </div>

  ${replayBanner()}
  ${staleBanner()}

  <div id="paper-strip"></div>

  <div class="filters">
    <span class="f-group"><span class="f-label">Whose</span>
      ${pills(paperWhoPills(), pf.who, "data-who")}</span>
    <span class="f-group"><span class="f-label">Asset</span>
      ${pills(paperClassPills(), pf.cls, "data-cls")}</span>
    <span class="f-group"><span class="f-label">Timeframe</span>
      ${pills(paperTfPills(), pf.tf, "data-tf")}</span></div>

  <div id="paper-body"></div>`;

  paintPaper();
  loadPaperCurves().then(c => { if (c) repaintPaper(); });
  document.querySelectorAll("[data-cls]").forEach(b =>
    b.onclick = () => { pf.cls = b.dataset.cls; setActive("data-cls", pf.cls); paintPaper(); });
  document.querySelectorAll("[data-tf]").forEach(b =>
    b.onclick = () => { pf.tf = b.dataset.tf; setActive("data-tf", pf.tf); paintPaper(); });
  document.querySelectorAll("[data-who]").forEach(b =>
    b.onclick = () => { pf.who = b.dataset.who; setActive("data-who", pf.who); paintPaper(); });
}

/* The headline figures live in their own container because the tick repaint rewrites only
 * the region it is given. They used to sit outside it, so every number here stayed frozen
 * at page load while the rows underneath updated several times a second.
 *
 * It used to be five stat tiles, two of which added the desk up: a mean P&L across every
 * system and a dollar total on the capital deployed. Both are gone on purpose. **Nobody
 * decides anything with them.** The desk is not a fund and its systems are not a
 * portfolio — they are 24 separate forward tests that happen to run in one process, so
 * their sum is an artifact of how many are switched on, and their mean says a rule is
 * "flat" when half are up and half are down. What is left is the housekeeping a reader
 * needs to trust the rows below: how many are live, how many fills exist, and whether the
 * feed is up. The performance is per system, downstairs, where it means something. */
function paperStrip() {
  const host = document.getElementById("paper-strip");
  if (!host) return;
  const running = D.strategies.filter(s => isReplay() || s.status === "running");
  const fills = D.strategies.reduce((a, s) => a + fillsOf(s), 0);
  const since = D.strategies.map(s => s.since).filter(Boolean).sort()[0];
  host.innerHTML = `
  <p class="deskline">
    <span><b>${countSystems(running)}</b> of ${countSystems(D.strategies)}
      ${isReplay() ? "systems" : "systems live"}</span>
    <span>${paperClasses().map(c =>
        `${countSystems(D.strategies.filter(s => s.cls === c))} ${PAPER_CLASS_LABEL[c] || c}`
      ).join(" · ")}</span>
    <span><b>${fills}</b> fills${since ? ` since ${esc(since)}` : ""}</span>
    <span>feed ${feedValue()}${feedStale() ? "" : ` · ${esc(D.feed.source)}`}</span>
  </p>`;
}

/* ---------- the order of the list ----------
 * These are 24 independent forward tests, and the only question a reader brings to them is
 * "which of mine is working" — so they are ranked on their own P&L rather than by name.
 *
 * The catch is that the numbers move several times a second on the tick stream, and a list
 * that re-sorts under the cursor cannot be read: a row you are reaching for slides away.
 * So the ranking is FROZEN. It is recomputed when the reader does something — opens the
 * view, clicks a filter — and every tick repaint in between reuses the same order while the
 * figures inside the rows keep moving. A system that appears mid-session (a promotion) has
 * no frozen rank and goes to the bottom until the next re-rank, which is visible rather
 * than surprising. */
let paperRank = null;
function orderSystems(rows, refreeze) {
  const keys = [...new Set(rows.map(systemKey))];
  if (refreeze || !paperRank) {
    paperRank = new Map();
    keys.map(k => [k, aggregate(rows.filter(s => systemKey(s) === k)).mean])
        .sort((a, b) => (b[1] - a[1]) || a[0].localeCompare(b[0]))
        .forEach(([k], i) => paperRank.set(k, i));
  }
  const at = k => paperRank.has(k) ? paperRank.get(k) : Number.MAX_SAFE_INTEGER;
  return keys.sort((a, b) => at(a) - at(b) || a.localeCompare(b));
}

function paintPaper(refreeze = true) {
  paperStrip();
  const rows = D.strategies.filter(s =>
    s.cls === pf.cls && s.tf === pf.tf
    && (pf.who === "mine" ? isMine(s) : !isMine(s)));
  const host = document.getElementById("paper-body");
  host.innerHTML = `
  <section class="sec">
    <div class="sec-head"><h2>${isReplay() ? "Replayed systems" : "Running systems"}</h2>
      <span class="sec-note">${countSystems(rows)} systems, best first · each one is its
        own record · tap a system for its record and every name it holds</span></div>

    ${systemList(rows, orderSystems(rows, refreeze))}
  </section>

  <p class="sec-note" style="max-width:62ch">${isReplay()
    ? `Simulated fills over historical bars — the proof that orders reach positions. Not a
       forward result and not evidence about a rule; see
       <a href="#/backtest">Backtest</a> for the walk-forward answer.`
    : `Paper P&amp;L is days old and is not evidence about a rule — see
       <a href="#/backtest">Backtest</a> for the multi-year result. Simulated fills only,
       no real money.`}</p>`;
  bindGo(host);
}

/* A *system* is one rule at one horizon on one asset class — the thing the research
 * ranked and the thing you would decide to keep or drop. Running it across 20 mega-caps is
 * deployment, not twenty systems. Counting instances made the headline read 330 when there
 * are 20 distinct systems on the desk, which overstated the breadth of what is being
 * tested by more than an order of magnitude. */
const systemKey = s => `${s.cls}|${s.tf}|${s.rule}`;
const countSystems = list => new Set(list.map(systemKey)).size;

/* `fills` counts the LIFETIME record, not this session.
 *
 * It used to read `paper_trades`, which is a counter the strategy keeps in memory and
 * starts at zero on every restart. The desk had 1,389 fills behind it and the page said
 * 39 — and said "0 fills" beside systems with hundreds, because those happened not to
 * trade in the hours since the last deploy. `lifetime_trades` is `store.fill_count`, the
 * count in the database, which is the number the word "fills" promises on a page whose
 * whole subject is a record that survives restarts. */
function aggregate(list) {
  const n = list.length;
  const mean = n ? list.reduce((a, s) => a + (s.paper_pnl_pct || 0), 0) / n : 0;
  return {
    n, mean,
    live: list.filter(s => s.status === "running").length,
    fills: list.reduce((a, s) => a + fillsOf(s), 0),
    session: list.reduce((a, s) => a + (s.paper_trades || 0), 0),
    open: list.filter(s => s.state && s.state !== "flat").length,
  };
}
const fillsOf = s => s.lifetime_trades != null ? s.lifetime_trades : (s.paper_trades || 0);

/* Turnover for the SYSTEM, averaged over its deployments. Reported per name per year by
 * `paper_state._set_turnover`, which is the unit the walk-forward sheets use — so the
 * figure here and the one in the backtest answer the same question and can be compared
 * without converting anything. Absent rather than zero when nothing has published one. */
function turnoverOf(rows) {
  const vs = rows.map(s => s.turnover).filter(v => typeof v === "number");
  return vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null;
}

/* ---------- simulated P&L history ----------
 * Fetched once and cached. The desk is hours old, so its own P&L cannot fill a YTD or
 * 3-month chart; these curves are the same rule run over the same instrument's recent
 * history by `paper_curves.py`. That is a backtest of a live system, and every chart says
 * so — it answers "how would this have done this year", not "how has it done since we
 * started it". */
let pcurves = null, pcurvesTried = false;
// Both windows are shown side by side rather than behind a toggle: the comparison
// people make is "lately versus this year", and a control that hides one of them
// turns a glance into two clicks and a memory test.
const PC_WINDOWS = [["3m", "3 months"], ["ytd", "Year to date"]];

async function loadPaperCurves() {
  if (pcurvesTried) return pcurves;
  pcurvesTried = true;
  try {
    const res = await fetch("paper_curves.json", { cache: "no-cache" });
    if (res.ok) pcurves = await res.json();
  } catch (e) { pcurves = null; }
  return pcurves;
}

/* A compact area-less line on a linear scale. These windows span months, not decades, so
 * the log treatment the research charts need would only flatten the detail here.
 *
 * ONE series: the strategy. The dashed "same basket held" line that used to be drawn
 * underneath came off the paper pages on 2026-08-17 — see the note on `pnlFigure`. */
function pnlSpark(curve, w = 560, h = 96) {
  const s = (curve || []).filter(v => isFinite(v));
  if (s.length < 2) return "";
  const lo = Math.min(...s, 100), hi = Math.max(...s, 100), span = (hi - lo) || 1;
  const pad = 6;
  const y = v => pad + (1 - (v - lo) / span) * (h - pad * 2);
  return `<svg class="pnl-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
      aria-hidden="true">
    <line x1="0" x2="${w}" y1="${y(100)}" y2="${y(100)}" stroke="var(--hair-2)"
      stroke-width="1" vector-effect="non-scaling-stroke"/>
    <polyline points="${s.map((v, i) =>
        `${(i / Math.max(s.length - 1, 1)) * w},${y(v)}`).join(" ")}" fill="none"
      stroke="${s[s.length - 1] >= 100 ? "var(--gain)" : "var(--loss)"}"
      stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>`;
}

/* ---------- the live record, per system ----------
 * `paper_curve` is what the desk actually did: cumulative P&L in PERCENT since this
 * system's first fill, chained across restarts by `paper_state.lifetime_curve`. Zero-based,
 * so the reference line is 0 and not 100 — it is money made on the desk, not an index, and
 * it is the one series on this page that is neither simulated nor a backtest.
 *
 * `curve_breaks` marks the points where the record LOST A BAR. The line is CUT there rather
 * than drawn straight through: a straight segment across an outage is a claim that nothing
 * happened during it, when the truth is that nobody was watching.
 *
 * A restart is not by itself a break, and the desk decides that, not this file — see
 * `store._missed_a_bar`. It used to mark every session boundary, and since the desk
 * restarts far more often than a bar closes, every point arrived here as its own
 * single-point segment: the dot fallback below fired for all of them and the chart drew a
 * field of dots with no line anywhere. */
function pnlLive(curve, bench, breaks, w = 620, h = 128) {
  const cur = (curve || []).filter(v => isFinite(v));
  if (cur.length < 2) return "";
  const bn = (bench || []).filter(v => isFinite(v));
  const all = cur.concat(bn, [0]);
  let lo = Math.min(...all), hi = Math.max(...all);
  // A desk one day old is flat at exactly 0.00 on every line, and a degenerate range pins
  // that to the bottom of the box — a line on the floor reads as a loss. Give it half a
  // point either side so nothing-yet is drawn through the middle.
  if (hi - lo < 1e-9) { const mid = (hi + lo) / 2; lo = mid - 0.5; hi = mid + 0.5; }
  const span = hi - lo;
  const pad = 7;
  const x = i => (i / Math.max(cur.length - 1, 1)) * w;
  const y = v => pad + (1 - (v - lo) / span) * (h - pad * 2);
  const cut = new Set(breaks || []);
  const line = (s, dashed) => {
    const ink = dashed ? "var(--muted)"
      : (cur[cur.length - 1] >= 0 ? "var(--gain)" : "var(--loss)");
    const parts = []; let run = [];
    s.forEach((v, i) => {
      if (cut.has(i) && run.length) { parts.push(run); run = []; }
      run.push([x(i), y(v)]);
    });
    if (run.length) parts.push(run);
    // A segment of ONE point is drawn as a dot rather than dropped. A young system with a
    // restart in it has exactly that shape — two points either side of a gap — and a
    // polyline-only renderer drew nothing at all for it, which reads as "no record" when
    // the record is simply short.
    return parts.map(p => p.length > 1
      ? `<polyline points="${p.map(([a, b]) => `${a},${b}`).join(" ")}" fill="none"
          stroke="${ink}" stroke-width="${dashed ? 1 : 1.7}"
          ${dashed ? 'stroke-dasharray="3 2"' : ""} vector-effect="non-scaling-stroke"/>`
      : `<circle cx="${p[0][0]}" cy="${p[0][1]}" r="${dashed ? 1.2 : 1.8}"
          fill="${ink}"/>`).join("");
  };
  return `<svg class="pnl-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
      aria-hidden="true">
    <line x1="0" x2="${w}" y1="${y(0)}" y2="${y(0)}" stroke="var(--hair-2)"
      stroke-width="1" vector-effect="non-scaling-stroke"/>
    ${bn.length > 1 ? line(bn, true) : ""}${line(cur, false)}</svg>`;
}

/* ---------- the live record as a figure, not a bare line ----------
 * The plot on its own was unreadable in one specific way: it carried NO VERTICAL SCALE.
 * `pnlLive` fits its box to whatever range the data happens to span, so a system that
 * moved four basis points and one that moved forty percent drew the identical picture.
 * Without a number on the axis the shape is not just uninformative, it is misleading.
 *
 * So the figure adds the three things the plot cannot carry itself: the range it was
 * drawn over, a key, and the endpoints of the time axis.
 *
 * The labels are HTML beside the SVG rather than <text> inside it, because the plot is
 * drawn with `preserveAspectRatio="none"` — it stretches to its container, and any text
 * within would stretch with it.
 *
 * **ONE LINE: the system's own record** (2026-08-17). The dashed "same basket held"
 * benchmark is gone from every paper-side chart. It is not gone from the repo — the
 * comparison a strategy is actually judged on is the risk-matched one on the backtest
 * detail page, over years, and that is where it belongs. Days of paper fills against a
 * basket held over the same days is not that comparison and was reading as though it
 * were. `bench_curve` is still published and still drawn on the ranked list, where the
 * market line is context for a 34px sparkline rather than a verdict. */
function pnlFigure(curve, breaks, opts = {}) {
  const cur = (curve || []).filter(v => isFinite(v));
  if (cur.length < 2) return "";
  const all = cur.concat([0]);
  let lo = Math.min(...all), hi = Math.max(...all);
  if (hi - lo < 1e-9) { const mid = (hi + lo) / 2; lo = mid - 0.5; hi = mid + 0.5; }
  const last = cur[cur.length - 1];
  return `
  <figure class="pnl-fig">
    <div class="pnl-plot">
      <div class="pnl-scale" aria-hidden="true">
        <span>${fmtPct(hi)}</span><span>${fmtPct(lo)}</span>
      </div>
      ${pnlLive(cur, null, breaks, 1200, 220)}
    </div>
    <div class="pnl-axis">
      <span>${esc(opts.from || "start")}</span>
      <span>${esc(opts.to || "")}</span>
    </div>
    <figcaption class="pnl-key">
      <span class="key"><i class="key-line ${sign(last)}"></i>this system
        <b class="num ${sign(last)}">${fmtPct(last)}</b></span>
    </figcaption>
  </figure>`;
}

/* ---------- the live record, as numbers ----------
 * The same table the backtest detail page carries, computed over what the DESK did rather
 * than over a 23-year book — and with **no benchmark column**. That is deliberate and it
 * is not the same decision as the one on the backtest page: there, a strategy is scored
 * against the same basket held at the strategy's own volatility over decades, which is a
 * comparison that means something. Days of paper fills against days of holding is not
 * that comparison, and printing it beside these figures invited it to be read as one.
 * The verdict lives on `#/backtest`; this page reports the record.
 *
 * Everything here is arithmetic over `paper_curve` and the published fills, so it moves
 * with the tick stream instead of freezing at build time. It is deliberately NOT the
 * research definition — `stockhunt/stats.py` owns that one, it works on a returns series
 * with a bill rate, and nothing on this page should be quoted against a sheet. The
 * caption says so.
 *
 * Nominal bars a year, for annualising volatility and Sharpe. Crypto prints around the
 * clock; the equity, ETF and commodity legs get a 6.5-hour session, which is why a 4h
 * "day" is two bars and not six — the same stub `stockhunt.stats.bars` warns about. */
const BARS_PER_YEAR = {
  "1d":  { crypto: 365,    other: 252 },
  "4h":  { crypto: 2190,   other: 504 },
  "2h":  { crypto: 4380,   other: 819 },
  "1h":  { crypto: 8760,   other: 1638 },
  "15m": { crypto: 35040,  other: 6552 },
  "5m":  { crypto: 105120, other: 19656 },
};
const barsPerYear = (cls, tf) => {
  const e = BARS_PER_YEAR[tf];
  return e ? (cls === "crypto" ? e.crypto : e.other) : null;
};
/* Under this many bars, a standard deviation is a rumour and annualising it is a lie with
 * three decimal places. Those rows print an em-dash until the record is long enough,
 * which is the honest answer for a desk that has been up for two days. */
const MIN_METRIC_BARS = 20;

function liveMetrics(rows, curve, cls, tf) {
  const c = (curve || []).filter(v => isFinite(v));
  // `paper_curve` is cumulative P&L in PERCENT, so the equity index is 1 + pct/100 and a
  // per-bar return is the ratio of consecutive points — not their difference.
  const eq = c.map(v => 1 + v / 100);
  const rets = eq.slice(1).map((v, i) => eq[i] ? v / eq[i] - 1 : 0);
  const n = rets.length;
  const mean = n ? rets.reduce((a, b) => a + b, 0) / n : null;
  const sd = n > 1
    ? Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1)) : null;
  const bpy = barsPerYear(cls, tf);
  const enough = n >= MIN_METRIC_BARS && sd > 0 && bpy;

  let peak = -Infinity, dd = 0;
  c.forEach(v => { peak = Math.max(peak, v); dd = Math.min(dd, v - peak); });

  /* A closed trade is one the desk says closed something — `realised` is null on a fill
   * that opened or added, and a float (possibly 0.00, on a scratch) when it closed.
   *
   * This used to filter on `pnl !== 0`, and `pnl` is the whole BOOK's mark at the fill,
   * not the trade's result. So an opening buy counted as a closed trade whenever some
   * unrelated name in the book had moved since its last mark, and two names filling in
   * the same second contributed the same number twice. On the IBS equity book that
   * turned eight round trips — every one of them a winner, +$188.43 together — into
   * "23 closed trades, 13% win rate, profit factor 0.04". Never reintroduce it: the
   * null is the signal, and zero is a real answer.
   *
   * And a payload published before the desk recorded a realised P&L carries no `realised`
   * key at all, where "0 closed trades" would be an assertion about it rather than the
   * absence of one. `priced` separates those, so an old `live.json` — or the snapshot
   * embedded in `dist/dashboard.html`, which is frozen at build time — prints em-dashes
   * and says why instead of reporting a desk that never closed anything. */
  const fills = systemFills(rows);
  const priced = fills.some(t => "realised" in t);
  const closed = priced ? fills.filter(t => t.realised != null) : [];
  const wins = closed.filter(t => t.realised > 0), losses = closed.filter(t => t.realised < 0);
  const sum = l => l.reduce((a, t) => a + t.realised, 0);
  const grossLoss = Math.abs(sum(losses));

  return {
    total: c.length ? c[c.length - 1] : null,
    bars: c.length,
    best: n ? Math.max(...rets) * 100 : null,
    worst: n ? Math.min(...rets) * 100 : null,
    maxdd: c.length ? dd : null,
    vol: enough ? sd * Math.sqrt(bpy) * 100 : null,
    sharpe: enough ? mean / sd * Math.sqrt(bpy) : null,
    bpy,
    lifetime: rows.reduce((a, s) => a + fillsOf(s), 0),
    priced,
    closed: priced ? closed.length : null,
    realised: closed.length ? sum(closed) : null,
    capped: fills.length < rows.reduce((a, s) => a + fillsOf(s), 0),
    win_rate: closed.length ? wins.length / closed.length * 100 : null,
    profit_factor: grossLoss ? sum(wins) / grossLoss : null,
    avg_win: wins.length ? sum(wins) / wins.length : null,
    avg_loss: losses.length ? sum(losses) / losses.length : null,
    turnover: turnoverOf(rows),
  };
}

/* [label, printed value, what it means]. Flat rather than driven by a key list like
 * `METRIC_ROWS`, because half of these are counts and dollars that need their own
 * formatter and a shared one would be a switch statement pretending to be a table. */
function liveMetricRows(m) {
  const dash = "—";
  const stale = "not in this payload — the desk published these fills before it recorded " +
    "what each one closed. It fills in on the desk's next start; the record itself is " +
    "complete, in paper.db.";
  const short = `not yet — needs ${MIN_METRIC_BARS} bars of record, there ` +
    `${m.bars === 1 ? "is" : "are"} ${m.bars}`;
  return [
    ["Total P&L", fmtPct(m.total),
     "Cumulative percent since this system's first fill, chained across restarts."],
    ["Max drawdown", m.maxdd == null ? dash : fmtPct(m.maxdd, 2),
     "Worst fall from a high-water mark of the live record. Percentage points of P&L, not of equity."],
    ["Volatility", m.vol == null ? dash : fmtNum(m.vol, 1) + "%",
     m.vol == null ? short
       : `Annualised standard deviation of the bar-to-bar record, on ${m.bpy} bars a year.`],
    ["Sharpe", m.sharpe == null ? dash : fmtNum(m.sharpe, 2),
     m.sharpe == null ? short
       : "Mean bar return over its standard deviation, annualised, idle cash at 0%. Months of record before this means anything."],
    ["Best bar", m.best == null ? dash : fmtPct(m.best),
     "Largest single-bar gain on the record."],
    ["Worst bar", m.worst == null ? dash : fmtPct(m.worst),
     "Largest single-bar loss on the record."],
    ["Fills", m.lifetime.toLocaleString(),
     "Every order that filled, lifetime — the count in the database, not this session's."],
    ["Closed trades", m.priced ? m.closed.toLocaleString() : dash,
     m.priced
       ? "Fills that closed part of a position. An opening or adding fill is not one, because it closed nothing."
       : stale],
    ["Realised P&L", m.priced ? cash(m.realised) : dash,
     m.priced
       ? "Cash actually booked by the closed trades above, against what the closed part cost. Open positions are not in it — those are in Total P&L."
       : stale],
    ["Win rate", m.win_rate == null ? dash : fmtNum(m.win_rate, 1) + "%",
     m.priced
       ? "Share of closed trades that realised a gain. A low rate is fine if the wins are large."
       : stale],
    ["Profit factor", m.profit_factor == null ? dash : fmtNum(m.profit_factor, 2),
     !m.priced ? stale
       : m.closed && m.profit_factor == null
         ? "No closed trade has lost yet, so there is nothing to divide by."
         : "Gross winnings ÷ gross losses. Above 1 means the wins outweigh the losses."],
    ["Average win", m.priced ? cash(m.avg_win) : dash,
     m.priced ? "Mean realised P&L of a winning trade." : stale],
    ["Average loss", m.priced ? cash(m.avg_loss) : dash,
     m.priced ? "Mean realised P&L of a losing trade." : stale],
    ["Turnover / yr", m.turnover == null ? dash : fmtNum(m.turnover, 1),
     "Round trips per name per year — the unit the walk-forward sheets report, so the two compare."],
    ["Bars recorded", m.bars.toLocaleString(),
     "Closed bars behind every figure above. This is the number that says how much to trust them."],
  ];
}

function liveMetricsSection(rows, curve, cls, tf) {
  const m = liveMetrics(rows, curve, cls, tf);
  return `
  <section class="sec">
    <div class="sec-head"><h2>Performance metrics</h2>
      <span class="sec-note">the live record itself — no benchmark column</span></div>
    <div class="tbl-wrap metrics-box"><table>
      <thead><tr><th class="l">Metric</th><th>Value</th>
        <th class="l">What it means</th></tr></thead>
      <tbody>${liveMetricRows(m).map(([name, val, help]) => `
        <tr><td class="l">${name}</td>
          <td class="num">${val}</td>
          <td class="l" style="white-space:normal;color:var(--muted);font-size:12.5px">${help}</td>
        </tr>`).join("")}</tbody>
      <caption>Measured over ${m.bars} closed bar${m.bars === 1 ? "" : "s"} of
      ${isReplay() ? "replay" : "paper trading"}${m.capped ? ` and the last
      ${systemFills(rows).length} fills the board carries` : ""} — a record this short
      describes the plumbing, not the rule. There is deliberately no buy-and-hold column:
      the comparison that decides whether a strategy is worth running is the risk-matched
      one over decades, on the <a href="#/backtest">backtest</a> page. These are also the
      desk's own arithmetic and not the research definitions in
      <code>stockhunt/stats.py</code>, so do not quote them against a sheet.</caption>
    </table></div>
  </section>`;
}

/* ---------- the fills ----------
 * Every deployment's published fills as one list, newest first, with the symbol carried
 * onto each row — a book's fills already name theirs, a per-symbol deployment's do not.
 *
 * `paper_state.MAX_TRADES` caps what the desk publishes at 200 per strategy while
 * `lifetime_trades` counts the whole database, so the two disagree on a busy system and
 * the page has to say which it is showing rather than quietly printing the shorter one. */
const systemFills = rows => rows
  .flatMap(s => (s.trades || []).map(t => ({ ...t, symbol: t.symbol || s.symbol })))
  .sort((a, b) => String(b.ts || "").localeCompare(String(a.ts || "")));

const csvCell = v => {
  const s = v == null ? "" : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

/* Both P&L columns, under the names they actually mean. The export used to head the book
 * snapshot `realised_pnl`, so a spreadsheet built off it inherited the same mistake the
 * page was making — and kept it after the page was fixed. */
function fillsCsv(rows) {
  const head = ["time", "symbol", "side", "qty", "price", "realised_pnl", "book_pnl"];
  const body = systemFills(rows).map(t =>
    [t.ts, t.symbol, t.side, t.qty, t.price,
     t.realised == null ? "" : t.realised, t.pnl].map(csvCell).join(","));
  return [head.join(","), ...body].join("\n") + "\n";
}

/* A Blob and a synthetic click — the board is static files behind a login and has no
 * endpoint to ask for a file, and it does not need one: everything in the table is
 * already in the page. */
function downloadFills(rows, cls, tf, rule) {
  const blob = new Blob([fillsCsv(rows)], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `stockhunt-${slug(cls)}-${slug(tf)}-${slug(rule)}-fills.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

function fillsSection(rows) {
  const fills = systemFills(rows);
  const lifetime = rows.reduce((a, s) => a + fillsOf(s), 0);
  return `
  <section class="sec">
    <div class="sec-head"><h2>Trade history</h2>
      <span class="sec-note">${fills.length < lifetime
        ? `the last ${fills.length.toLocaleString()} of ${lifetime.toLocaleString()} fills`
        : `${fills.length.toLocaleString()} fill${fills.length === 1 ? "" : "s"}`},
        newest first</span></div>
    ${fills.length ? `
    <div class="tbl-tools">
      <button class="btn" data-csv="fills">Export CSV</button>
      ${fills.length < lifetime ? `<span class="sec-note">The desk publishes its most
        recent ${fills.length.toLocaleString()} fills per system; the full record stays in
        <code>paper.db</code>.</span>` : ""}
    </div>
    <div class="tbl-wrap fills-box"><table>
      <thead><tr><th class="l">Time</th><th class="l">Asset</th><th class="l">Side</th>
        <th>Qty</th><th>Price</th><th>Realised P&amp;L</th><th>Book P&amp;L</th></tr></thead>
      <tbody>${fills.map(t => `
        <tr><td class="l">${esc(t.ts || "")}</td>
          <td class="l">${esc(t.symbol || "")}</td>
          <td class="l ${t.side === "BUY" ? "gain" : "loss"}">${esc(t.side || "")}</td>
          <td>${fmtUnits(t.qty)}</td>
          <td>${t.price == null ? "—"
            : Number(t.price).toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
          <td class="${t.realised == null ? "" : sign(t.realised)}"
            ${t.realised == null ? 'title="this fill opened or added — it closed nothing"' : ""}
            >${cash(t.realised)}</td>
          <td class="book-pnl ${sign(t.pnl)}">${cash(t.pnl)}</td></tr>`).join("")}</tbody>
      <caption>Two different numbers, deliberately side by side.
      <b>Realised P&amp;L</b> is what that one fill closed, against what the closed part
      cost — blank on a fill that opened or added, because it closed nothing.
      <b>Book P&amp;L</b> is the whole book's mark at that moment, so every name filling in
      the same second carries the same value. The trade statistics above count only the
      first column.</caption>
    </table></div>`
    : `<p class="sec-note">No fills yet — this system has not opened a position.</p>`}
  </section>`;
}

/* One curve for the SYSTEM, from however many deployments it has. Books are one row and
 * come through unchanged; a rule spread over twenty names is averaged equal-weight, which
 * is the same weighting `aggregate` reports its P&L on, so the line and the number beside
 * it are the same statistic.
 *
 * Aligned at the TAIL. A system deployed later has a shorter record, and stretching it to
 * the longest would invent history for it; the early bars are simply averaged over
 * whoever was trading then. */
function systemCurve(rows, field) {
  const cs = rows.map(s => s[field]).filter(c => Array.isArray(c) && c.length > 1);
  if (cs.length < 2) return cs[0] || [];
  const n = Math.max(...cs.map(c => c.length));
  return Array.from({ length: n }, (_, i) => {
    let sum = 0, k = 0;
    for (const c of cs) { const j = i - (n - c.length); if (j >= 0) { sum += c[j]; k++; } }
    return k ? sum / k : 0;
  });
}
// Breaks belong to ONE curve. Averaging several deployments blends their gaps together, so
// the marks are kept only where they can still be read literally: a single record.
const systemBreaks = rows => rows.length === 1 ? (rows[0].curve_breaks || []) : [];

/* One simulated window. The rule's own line and nothing else — no dashed basket and no
 * "buy & hold x%" beside the label, for the reason given on `pnlFigure`. */
function pnlPanel(entry, label) {
  if (!entry) return `<p class="sec-note">No simulated history for this window.</p>`;
  const d = entry.dates || [];
  return `
  <div class="pnl-wrap">
    <div class="pnl-head">
      <span class="pnl-val num ${sign(entry.pnl_pct)}">${fmtPct(entry.pnl_pct)}</span>
      <span class="pnl-lbl">${label}</span>
    </div>
    ${pnlSpark(entry.curve, 600, 150)}
    <div class="pnl-axis"><span>${esc(d[0] || "")}</span><span>${esc(d[d.length - 1] || "")}</span></div>
  </div>`;
}


/* "1 assets" is a lie about a book. A book is ONE strategy row that holds a whole class
 * internally, so the count the reader wants is the names inside it, not the number of
 * rows — a $100,000 account over the top 100 was reading as a single asset. */
function assetCount(rows) {
  const books = rows.filter(s => s.kind === "book");
  if (!books.length) return `${rows.length} assets`;
  const names = books.reduce((a, s) => a + (s.names || 0), 0);
  const held = books.reduce((a, s) => a + (s.held || 0), 0);
  const rest = rows.length - books.length;
  return `${names} names, ${held} held` + (rest ? ` · ${rest} assets` : "");
}


/* A book is one strategy holding a whole class, so it expands into one row PER NAME
 * rather than the single row every other system gets.
 *
 * Every name is listed, held or not. "46 of 100 held" only reads if the other 54 are
 * visible as waiting — a name the rule is out of is holding its slice in cash, which is a
 * state and not an absence. Held names sort to the top because they are the ones doing
 * something; the rest stay alphabetical so a reader can find one. */
function bookRows(s) {
  const rows = (s.holdings || []);
  if (!rows.length) {
    return `<tr><td class="l" colspan="${ASSET_COLS}">${esc(s.symbol || "the book")} —
      no holdings published yet</td></tr>`;
  }
  const rank = h => (Math.abs(h.units || 0) > 0 ? 0 : 1);
  const sorted = [...rows].sort(
    (a, b) => rank(a) - rank(b) || a.symbol.localeCompare(b.symbol));
  const num = v => v == null ? "—"
    : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return sorted.map(h => `
    <tr>
      <td class="l">${esc(h.symbol)}</td>
      <td class="l">${h.warming ? "warming" : esc(h.state)}</td>
      <td>${fmtUnits(h.units)}</td>
      <td>${num(h.entry)}</td>
      <td>${num(h.mark)}</td>
      <td>${h.value ? money(h.value) : "—"}</td>
      <td class="${h.pnl_pct == null ? "" : sign(h.pnl_pct)}">${
        h.pnl_pct == null ? "—" : fmtPct(h.pnl_pct)}</td>
      <td>${h.trades || 0}</td>
      <td class="l">${h.warming ? "waiting for bars"
        : (Math.abs(h.units || 0) > 0 ? "holding" : "in cash")}</td></tr>`).join("");
}

/* Nine, and it is a constant because three places have to agree on it: the header, the
 * book's "nothing published yet" colspan, and the per-symbol row for a non-book system. */
const ASSET_COLS = 9;
const assetHead = () => `<thead><tr><th class="l">Asset</th><th class="l">State</th>
  <th>Units</th>
  <th title="what the units currently held cost, averaged over every fill that built
    the position">Avg cost</th>
  <th>Mark</th><th>Value</th>
  <th>${isReplay() ? "Replay P&amp;L" : "Paper P&amp;L"}</th>
  <th>Trades</th><th class="l">Status</th></tr></thead>`;

/* One row for a system that is deployed on a single instrument. A book expands into one
 * row per name instead — see `bookRows` — and the two shapes share a header, so they have
 * to print the same columns in the same order. */
const assetRow = s => `
  <tr data-go="#/paper/${s.id}">
    <td class="l">${esc(s.symbol)}</td>
    <td class="l">${stateCell(s)}</td>
    <td>${fmtUnits(s.position_units)}</td>
    <td>${s.entry ? s.entry.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—"}</td>
    <td>${s.mark_price ? s.mark_price.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—"}</td>
    <td>${s.position_units && s.mark_price
      ? money(Math.abs(s.position_units * s.mark_price)) : "—"}</td>
    <td class="${sign(s.paper_pnl_pct)}">${fmtPct(s.paper_pnl_pct)}</td>
    <td>${s.paper_trades}</td>
    <td class="l">${statusChip(s)}</td></tr>`;


/* ---------- the system list ----------
 * One row per system, and the row is a LINK. Everything a system has to say — its live
 * curve at full size, the simulated windows, the group notes and the name-by-name
 * holdings — lives on its own page now, `#/paper/sys/<cls>/<tf>/<rule>`.
 *
 * It was an accordion until 2026-08-17, and the accordion had grown into a detail view
 * wearing a list item's clothes: every system on this desk is a *book* holding a whole
 * asset class, so opening one unfolded a hundred-name table inside the list, and opening
 * two made the ranking — which is the entire point of the list — impossible to scan. None
 * of it could be linked to, bookmarked or sent to anybody either, because a disclosure
 * triangle has no URL.
 *
 * The row shows exactly what the old `<summary>` showed — name, deployment, live
 * sparkline, P&L — so the list itself reads the same. What changed is where detail lives.
 */
function systemList(rows, systems) {
  // A system is the thing that was researched and the thing you would keep or drop; the
  // assets under it are where it happens to be deployed. The order comes in frozen from
  // `orderSystems` — see the note there.
  const blocks = (systems || [...new Set(rows.map(systemKey))].sort()).map(key => {
    const mine = rows.filter(s => systemKey(s) === key);
    if (!mine.length) return "";
    /* Off the ROW, never off the key. `systemKey` joins on "|" and a pair's rule name
     * contains one — `MININDEX~SAREXT|and` — so splitting the key back apart truncated
     * every pair at its operator and the list printed `MININDEX~SAREXT` for two systems
     * that differ only in whether the legs vote or agree. */
    const { cls, tf, rule } = mine[0];
    const a = aggregate(mine);

    /* The system's OWN record. `live` is cumulative paper P&L in percent since its first
     * fill; `bench` is the same basket held over the same bars, so the gap between the two
     * lines is the signal and not the market. */
    const live = systemCurve(mine, "paper_curve");
    const bench = systemCurve(mine, "bench_curve");
    const breaks = systemBreaks(mine);

    return `
    <div class="grp"><div class="grp-row" role="link" tabindex="0"
        data-go="${systemHash(cls, tf, rule)}"
        aria-label="${esc(rule)}, ${esc(tf)} ${esc(cls)} — open this system">
      <span class="grp-id"><span class="grp-name">${esc(rule)}</span>
        <span class="grp-meta">${esc(tf)} · ${esc(cls)} · ${assetCount(mine)} ·
          ${a.fills} fill${a.fills === 1 ? "" : "s"}</span></span>
      <span class="grp-live">${live.length > 1
        ? pnlLive(live, bench, breaks, 300, 34)
        : `<span class="hist-lbl">no curve yet</span>`}</span>
      <span class="grp-pnl num ${sign(a.mean)}">${fmtPct(a.mean)}</span>
    </div></div>`;
  }).join("");

  return blocks || `<p class="sec-note">Nothing matches this filter.</p>`;
}


/* ================================ ONE SYSTEM ================================ */
/* `#/paper/sys/<cls>/<tf>/<rule>` — the live record of one system, at the size the thing
 * deserves: its own strip of figures, its curve as a figure rather than a 34px sparkline,
 * the two simulated windows, and every name it holds.
 *
 * The rule is SLUGGED into the URL the same way the backtest detail page slugs its own,
 * because a rule name carries `|` and `~` and a raw one in a hash is unreadable and
 * fragile. Nothing round-trips it back: the page finds its rows by matching `slug(s.rule)`
 * against the segment, so the slug never has to be reversible. */
const systemHash = (cls, tf, rule) =>
  `#/paper/sys/${encodeURIComponent(cls)}/${encodeURIComponent(tf)}/${slug(rule)}`;

/* The universes a system can be deployed across, with the note that says what each one is
 * worth as evidence. `D.paper_groups` carries those notes and nothing rendered them until
 * this page existed — they are the difference between "the universe the rule was ranked
 * on" and "a transfer onto instruments the research never held", which is exactly the
 * question somebody opening a system's holdings has. */
const paperGroupList = () => (D.paper_groups && D.paper_groups.length ? D.paper_groups
  : [{ key: "crypto", label: "Crypto" }, { key: "megacap", label: "Equities" },
     { key: "etf", label: "ETFs" }]);

/* What `#sys-body` is currently drawing, so a tick repaint can rebuild it without
 * re-reading the hash. Cleared by nothing: `repaintPaper` gates on the hash instead, so a
 * stale value cannot paint over another view. */
let sysView = null;

const systemMembers = (cls, tf, rule) =>
  D.strategies.filter(s => s.cls === cls && s.tf === tf && s.rule === rule);

/* A pointer to the multi-year answer for THIS rule — but only when the rule is actually on
 * that sheet. The desk runs promotions whose leaderboard row was cut by `TOP_N`, and a
 * link that bounces the reader back to the leaderboard is worse than a sentence saying the
 * page is not there. */
function backtestHref(cls, tf, rule) {
  const grp = Object.keys(CLASS_ARG).find(k => CLASS_ARG[k] === cls);
  const sh = grp && D.backtest[grp] ? sheetOf(grp, tf) : null;
  return sh && sh.rows.some(x => x.rule === rule)
    ? `#/backtest/${grp}/${tf}/${slug(rule)}` : null;
}

function paperSystem(cls, tf, ruleSlug) {
  const rows = D.strategies.filter(
    s => s.cls === cls && s.tf === tf && slug(s.rule) === ruleSlug);
  if (!rows.length) return (location.hash = "#/paper");
  const rule = rows[0].rule;
  sysView = { cls, tf, rule };
  const href = backtestHref(cls, tf, rule);

  app.innerHTML = `
  <a class="back" href="#/paper">← ${isReplay() ? "strategy replay" : "paper trading"}</a>
  <div class="hero">
    <div class="d-head"><span class="d-name">${esc(rule)}</span>
      <span class="chip mut">${esc(tf)}</span>
      <span class="chip mut">${esc(PAPER_CLASS_LABEL[cls] || cls)}</span>
      ${rows.length === 1 ? statusChip(rows[0]) : ""}</div>
    <p class="lede">${esc(rows[0].note || "")}</p>
  </div>

  ${replayBanner()}
  ${staleBanner()}

  <div id="sys-body"></div>

  <div class="note">${href
    ? `Whether this rule actually works is the multi-year question, and it is not answered
       here — see <a href="${href}">the walk-forward result for ${esc(rule)}</a>. What this
       page shows is ${isReplay() ? "a replay over cached bars" : "days of simulated fills"},
       which is evidence about the pipeline and about nothing else.`
    : `Whether this rule actually works is the multi-year question, answered in
       <a href="#/backtest">Backtest</a> — this rule has no row on the
       ${esc(PAPER_CLASS_LABEL[cls] || cls)} ${esc(tf)} leaderboard, which ships only its
       top rows, so there is no page to link to.`}</div>`;

  paintSystem();
  // A deep link lands here without ever having drawn the master list, so the simulated
  // windows have to be fetched from this page too. Cached after the first call.
  loadPaperCurves().then(c => { if (c) paintSystem(); });
}

/* Everything volatile in one container, rewritten whole on each tick. The hero, the
 * banners and the backtest pointer sit outside it and never move. */
function paintSystem() {
  const host = document.getElementById("sys-body");
  if (!host || !sysView) return;
  const { cls, tf, rule } = sysView;
  const rows = systemMembers(cls, tf, rule);
  if (!rows.length) return;

  const a = aggregate(rows);
  const live = systemCurve(rows, "paper_curve");
  const breaks = systemBreaks(rows);
  const since = rows[0].since;
  const days = Math.max(...rows.map(s => s.days || 0), 0);
  const turn = turnoverOf(rows);
  const sim = pcurves && pcurves[systemKey(rows[0])]
    && pcurves[systemKey(rows[0])].system;

  /* A book is one row holding a whole class, so "with a position" would be a single yes/no
   * about the account. Names held out of names carried is the same question asked of the
   * thing that has an answer. */
  const books = rows.filter(s => s.kind === "book");
  const held = books.reduce((x, s) => x + (s.held || 0), 0);
  const names = books.reduce((x, s) => x + (s.names || 0), 0);
  const equity = rows.reduce((x, s) => x + (s.equity || 0), 0);
  const capital = rows.reduce((x, s) => x + (s.capital || 0), 0);

  const groups = paperGroupList();
  const assets = groups.map(g => {
    const gs = rows.filter(s => (s.group || "") === g.key)
                   .sort((x, y) => x.symbol.localeCompare(y.symbol));
    if (!gs.length) return "";
    const ga = aggregate(gs);
    const gBooks = gs.filter(s => s.kind === "book");
    return `
    <section class="sec">
      <div class="sec-head"><h2>${esc(g.label || g.key)}</h2>
        <span class="sec-note">${assetCount(gs)}${gBooks.length ? ""
          : ` · ${ga.open} with a position`} · ${ga.fills} fills</span></div>
      ${g.note ? `<p class="grp-note">${esc(g.note)}</p>` : ""}
      <div class="tbl-wrap"><table>
        ${assetHead()}
        <tbody>${gs.map(s => s.kind === "book" ? bookRows(s) : assetRow(s)).join("")}</tbody>
      </table></div>
      ${(() => {
        /* Per-name sparklines, where `paper_curves.py` published any. It drops them for a
         * book on purpose (6.3 MB against 0.2 MB), so in practice this renders for the
         * older per-symbol deployments and nothing else. */
        const c = pcurves && pcurves[systemKey(gs[0])];
        if (!c || !c.assets) return "";
        const cards = gs.map(s => {
          const av = c.assets[s.symbol];
          if (!av) return "";
          return `<figure class="mini-card">
            <figcaption><span class="mini-sym">${esc(s.symbol)}</span></figcaption>
            ${PC_WINDOWS.map(([w, label]) => { const e = av[w];
              if (!e) return "";
              return `<div class="mini-win">
                <span class="hist-lbl">${label}</span>
                ${pnlSpark(e.curve, 300, 46)}
                <span class="hist-nums"><b class="${sign(e.pnl_pct)}">${fmtPct(e.pnl_pct)}</b></span>
              </div>`; }).join("")}
          </figure>`; }).join("");
        return cards ? `<div class="minis">${cards}</div>` : "";
      })()}
    </section>`;
  }).join("");

  host.innerHTML = `
  <div class="strip">
    <div class="stat"><span class="k">${isReplay() ? "Replay P&amp;L" : "Paper P&amp;L"}</span>
      <span class="v ${sign(a.mean)}">${fmtPct(a.mean)}</span>
      <span class="s">cumulative${since ? `, since ${esc(since)}` : ""}</span></div>
    <div class="stat"><span class="k">Fills</span>
      <span class="v">${a.fills.toLocaleString()}</span>
      <span class="s">lifetime, carried across restarts</span></div>
    <div class="stat"><span class="k">${books.length ? "Holding" : "With a position"}</span>
      <span class="v">${books.length ? `${held} / ${names}` : `${a.open} / ${rows.length}`}</span>
      <span class="s">${books.length ? "names held right now"
        : "deployments with exposure"}</span></div>
    <div class="stat"><span class="k">Turnover / yr</span>
      <span class="v">${turn == null ? "—" : turn.toFixed(1)}</span>
      <span class="s">per name — the unit the backtest reports, so the two compare</span></div>
    <div class="stat"><span class="k">Equity</span>
      <span class="v">${equity ? money(equity) : "—"}</span>
      <span class="s">${capital ? `of ${money(capital)} staked` : "paper only"}</span></div>
    <div class="stat"><span class="k">Running</span>
      <span class="v">${rows.length === 1 ? statusChip(rows[0])
        : `${a.live} / ${rows.length}`}</span>
      <span class="s">${rows.length === 1 ? "one deployment"
        : `${rows.length} deployments of one rule`}</span></div>
  </div>

  <section class="sec">
    <div class="sec-head"><h2>${isReplay() ? "Replayed record" : "Live record"}</h2>
      <span class="sec-note">${days} day${days === 1 ? "" : "s"} of ${
        isReplay() ? "replayed" : "simulated"} fills${
        breaks.length ? ` · cut at ${breaks.length} outage${breaks.length === 1 ? "" : "s"}`
        : ""}</span></div>
    <div class="sys-live">
      <div class="sys-headline">
        <span class="pnl-val num ${sign(a.mean)}">${fmtPct(a.mean)}</span>
        <span class="pnl-lbl">cumulative ${isReplay() ? "replay" : "paper"} P&amp;L${
          since ? ` since ${esc(since)}` : ""}</span>
      </div>
      ${live.length > 1
        ? pnlFigure(live, breaks, {
            from: since || "start",
            to: days ? `${days} day${days === 1 ? "" : "s"} in` : "today" })
        : `<p class="pnl-young">The live record is
             ${live.length} closed bar${live.length === 1 ? "" : "s"} old — a line needs
             two. The figure above it is live either way, and the simulated windows below
             are what this rule did over the same instruments' recent history.</p>`}
    </div>
  </section>

  ${liveMetricsSection(rows, live, cls, tf)}

  <section class="sec">
    <div class="sec-head"><h2>Simulated history</h2>
      <span class="sec-note">not traded — the same rule over recent bars</span></div>
    ${sim ? `
    <div class="sim-wins">${PC_WINDOWS.map(([w, label]) =>
      pnlPanel(sim[w], label)).join("")}</div>
    <p class="sec-note pnl-caveat">Those two are <b>simulated</b>, not traded: this rule
      over the same instruments' recent history. They say how it <em>would</em> have gone;
      the record above is what it did. Whether it beats holding is the multi-year
      question, and it is answered on the backtest page, not by three months of either
      line.</p>`
    : `<p class="sec-note pnl-caveat">No simulated history for this system —
      <code>python paper_curves.py</code> has not been run since it was promoted, so there
      is nothing to show beside the live record.</p>`}
  </section>

  ${fillsSection(rows)}

  ${assets || `<p class="sec-note">No holdings published for this system yet.</p>`}`;

  bindGo(host);
  /* Re-bound on every tick repaint, which is the point: `#sys-body` is rewritten whole, so
   * a listener attached once would be attached to a node that no longer exists. The rows
   * are read at click time, so the file is always the fills currently on the page. */
  const dl = host.querySelector("[data-csv]");
  if (dl) dl.onclick = () => downloadFills(rows, cls, tf, rule);
}


function paperDetail(id) {
  const s = D.strategies.find(x => x.id === id);
  if (!s) return (location.hash = "#/paper");
  /* Back to the SYSTEM, not to the list. This page is reached from a system's holdings
   * table now, and a back link that skips the page you came from is a link to somewhere
   * else. */
  const up = systemHash(s.cls, s.tf, s.rule);
  app.innerHTML = `
  <a class="back" href="${up}">← ${esc(s.rule)}</a>
  <div class="hero">
    <div class="d-head"><span class="d-name">${esc(s.symbol)} · ${esc(s.rule)}</span>
      <span class="chip mut">${s.tf}</span>${statusChip(s)}</div>
    <p class="lede">${esc(s.note)}</p>
  </div>

  ${replayBanner()}

  <div class="strip">
    <div class="stat"><span class="k">${isReplay() ? "Replay P&amp;L" : "Paper P&amp;L"}</span>
      <span class="v ${sign(s.paper_pnl_pct)}">${fmtPct(s.paper_pnl_pct)}</span>
      <span class="s">over ${s.days} days${isReplay() ? " of history" : " live"}</span></div>
    <div class="stat"><span class="k">Position</span><span class="v">${stateCell(s)}</span>
      <span class="s">${s.position_units ? fmtUnits(s.position_units) + " units" : "no exposure"}</span></div>
    <div class="stat"><span class="k">Avg cost</span>
      <span class="v">${s.entry ? s.entry.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—"}</span>
      <span class="s">${s.paper_trades} fills</span></div>
    <div class="stat"><span class="k">Turnover / yr</span>
      <span class="v">${s.turnover == null ? "—" : s.turnover.toFixed(1)}</span>
      <span class="s">watch for drift vs backtest</span></div>
  </div>

  <section class="sec">
    <div class="sec-head"><h2>${isReplay() ? "Replayed progress" : "Live progress"}</h2>
      <span class="sec-note">${s.days} days of simulated fills</span></div>
    ${s.paper_curve && s.paper_curve.length > 1
      /* `pnlLive`, not `lineChart`: this series is cumulative P&L in PERCENT, and
         `lineChart` draws its reference line at 100 — an index baseline, off the top of a
         chart that runs either side of zero. It also knows about `curve_breaks`. */
      ? `<div class="panel sys-live">
          ${pnlLive(s.paper_curve, null, s.curve_breaks, 1200, 220)}
          <div class="legend"><span><i class="sw"
              style="background:${(s.paper_pnl_pct || 0) >= 0 ? "var(--gain)" : "var(--loss)"}"></i>
              Cumulative P&amp;L</span></div>
          <p class="sec-note">${isReplay()
            ? `Historical bars, so this <em>is</em> long enough to look at — but it is the
               same period the research already scored, not new evidence. Its job here is
               to prove bars arrive, signals compute and orders fill.`
            : `Far too short to judge the rule. Its job is to prove bars arrive, signals
               compute and orders fill.`}</p></div>`
      : `<p class="sec-note">No curve yet — this strategy has not completed a bar.</p>`}
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Fills</h2><span class="sec-note">newest last</span></div>
    <div class="tbl-wrap"><table>
      <thead><tr><th class="l">Time</th><th class="l">Side</th><th>Qty</th>
        <th>Price</th><th>Realised P&amp;L</th><th>Book P&amp;L</th></tr></thead>
      <tbody>${s.trades.length ? s.trades.map(t => `
        <tr><td class="l">${t.ts}</td>
          <td class="l ${t.side === "BUY" ? "gain" : "loss"}">${t.side}</td>
          <td>${fmtUnits(t.qty)}</td>
          <td>${t.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
          <td class="${t.realised == null ? "" : sign(t.realised)}"
            ${t.realised == null ? 'title="this fill opened or added — it closed nothing"' : ""}
            >${cash(t.realised)}</td>
          <td class="book-pnl ${sign(t.pnl)}">${cash(t.pnl)}</td></tr>`).join("")
      : `<tr><td class="l" colspan="6" style="color:var(--muted)">No fills yet — still warming up.</td></tr>`}
      </tbody></table></div>
  </section>

  <div class="note">Looking for whether this rule actually works? That is the multi-year
  question — see <a href="#/backtest">the backtest for ${esc(s.rule)}</a>.</div>`;
}

/* ================================ BACKTEST ================================ */
/* One leaderboard per asset class. Single rules and pairs are ranked together because they
 * are the same kind of object — a strategy, walked forward over the same folds and scored
 * against the same benchmark — and which of the two sweeps emitted a row is a fact about
 * this repo's plumbing, not about whether the thing is worth trading. What does change from
 * tab to tab is the price series, the fee schedule and the benchmark, so asset class and
 * timeframe are the only two filters. */
/* Three filters, and `board` is the outermost: it decides which two lists the other
 * two are drawn from. The house catalogue has sheets at 1d and 4h; the converted
 * strategies were written for minute charts and were scored at 1m/2m/3m/5m as well, so
 * the timeframe strip cannot be one literal pair shared by both. */
let bf = { board: "house", tf: "1d", cls: "stocks" };
const BOARDS = [["house", "Research catalogue"], ["conv", "Converted strategies"]];
const sheetOf = (cls, tf) => D.backtest[cls].sheets.find(s => s.timeframe === tf);
/* Keyed by the GROUP key from `dash_config.GROUPS`, which is not the class name for three
 * of the four. `CLASS_ARG` maps back, and it is not decoration: it is what the two empty
 * states print as the command to run, so a key missing from it tells a reader to run
 * `walkforward.py --class undefined`. Add to both when adding a tab. */
const CLASS_LABEL = { stocks: "Top 100 US Stocks", crypto: "Crypto", etf: "ETFs",
                      commodities: "Commodities", futures: "CME Futures" };
const CLASS_ARG = { stocks: "us_stocks", crypto: "crypto", etf: "us_etfs",
                    commodities: "commodities", futures: "cme_futures" };
const universePills = () => Object.keys(D.backtest).map(k => [k, CLASS_LABEL[k] || k]);
/* `dash_config.TIMEFRAMES` through the payload, not a literal pair. It is the list
 * `payload.build` asked for sheets on, so a timeframe the research gains appears here the
 * moment its sheets do — and one it has no sheet for still gets a button, which is what
 * leaves the empty state (the command to run) reachable. */
const btTimeframes = () => (D.timeframes && D.timeframes.length ? D.timeframes
  : ["1d", "4h"]);
const btTfPills = () => btTimeframes().map(t => [t, t]);

/* The two boards' heroes say different things because they are answering different
 * questions. The house one is "does anything in our own catalogue work"; the conversions
 * one is "does the set somebody handed us work". Same machinery, same benchmark, same
 * ranking key — different population, and the lede has to say which one is on screen or
 * the numbers underneath are unattributable. */
const BOARD_HERO = {
  house: `<h1>Backtest results</h1>
    <p class="lede">Every strategy run independently on each asset, walk-forward:
    parameters re-picked on each in-sample window and applied to the next. Scored as
    information ratio against buy-and-hold on the same asset — zero means matching it,
    positive means beating it. Single rules and pairs of rules are ranked in one list; only
    the asset class separates them, because only the asset class changes the prices, the
    costs and the benchmark.</p>`,
  conv: `<h1>Converted strategies</h1>
    <p class="lede">Thirteen strategies supplied from outside this repo — eight TradingView
    Pine scripts, four freqtrade strategies and one pair of notebooks — put through the
    same machinery as everything else and ranked on the same key. Every cell was
    pre-registered before it was scored. They keep their own board because they were tested
    on timeframes the catalogue has no sheets for, down to one-minute bars, and because
    they carry three facets no house rule does: a short side that reverses rather than
    selling to cash, a Heikin-Ashi signal variant, and an overnight-flat variant.</p>`,
};

const boardTfPills = () => bf.board === "conv" ? convTfPills() : btTfPills();
const boardClassPills = () => bf.board === "conv" ? convClassPills() : universePills();

/* A board switch changes what the other two strips can offer, so a selection that the new
 * board has no sheet for is corrected here rather than left to render an empty state that
 * the reader did not ask for. */
function normaliseBoard() {
  const cls = boardClassPills().map(p => p[0]);
  if (cls.length && !cls.includes(bf.cls)) bf.cls = cls[0];
  const tfs = boardTfPills().map(p => p[0]);
  if (tfs.length && !tfs.includes(bf.tf)) bf.tf = tfs[0];
}

function backtestMaster() {
  if (!D.backtest[bf.cls] && bf.board !== "conv") bf.cls = Object.keys(D.backtest)[0];
  if (bf.board === "conv" && !Object.keys((D.conversions || {}).groups || {}).length)
    bf.board = "house";
  normaliseBoard();
  app.innerHTML = `
  <div class="hero">
    ${BOARD_HERO[bf.board] || BOARD_HERO.house}
  </div>

  <div id="bt-head"></div>

  ${/* The filters sit below the summary and its notes rather than under the hero, because
      the thing they switch is the table: reaching for another asset class happens while
      reading the ranking, and up beside the lede they were a screenful of prose away from
      it. They stay outside both painted regions so the buttons survive a repaint — and so
      a sheet that does not exist still leaves you something to click. */""}
  <div class="filters wide">
    <span class="f-group"><span class="f-label">Leaderboard</span>
      ${pills(BOARDS, bf.board, "data-bboard")}</span>
    <span class="f-group"><span class="f-label">Asset class</span>
      ${pills(boardClassPills(), bf.cls, "data-bcls")}</span>
    <span class="f-group"><span class="f-label">Timeframe</span>
      ${pills(boardTfPills(), bf.tf, "data-btf")}</span></div>

  <div id="bt-body"></div>`;

  paintBacktest();
  document.querySelectorAll("[data-bcls]").forEach(b =>
    b.onclick = () => { bf.cls = b.dataset.bcls; setActive("data-bcls", bf.cls); paintBacktest(); });
  document.querySelectorAll("[data-btf]").forEach(b =>
    b.onclick = () => { bf.tf = b.dataset.btf; setActive("data-btf", bf.tf); paintBacktest(); });
  // Switching board rebuilds the whole master: the other two strips are drawn from lists
  // this choice selects, so repainting only the body would leave 4h and 2m offered side by
  // side with nothing behind one of them.
  document.querySelectorAll("[data-bboard]").forEach(b =>
    b.onclick = () => {
      bf.board = b.dataset.bboard;
      lbSort = convSort = null;      // a sort is a statement about one board's columns
      backtestMaster();
    });
}

/* A pair is two rules joined by an operator (`or`, `and`, `vote`, `gate`) and carries that
 * operator inside its own name — `HT_TRENDMODE~MAXINDEX|or`. The table prints the stem and
 * shows the operator as a chip, which is also what marks the row as a pair rather than a
 * single rule; there is no separate type column, because the two are not separate lists. */
const stemName = r => String(r).split("|")[0];
const opLabel = o => !o || o === "nan" || o === "None" ? "" : o;
const pctOr = v => v == null ? "—" : (v * 100).toFixed(0) + "%";

/* ---------- the leaderboard's columns ----------
 * Declared as a list rather than written inline, because the phone and the desktop want
 * them in a different order and a table cannot reorder its own columns in CSS.
 *
 * Sixteen columns never fit a phone, and the three that decide whether a row is worth
 * opening at all — how much Sharpe it added over holding, how much money that was, and how
 * much of the standard it cleared — were sitting behind ten columns of diagnostics, so the
 * first screenful of the ranking showed nothing you could rank on. `lead` marks those
 * three; on a narrow screen they move up beside the frozen name and everything else keeps
 * its order behind them. Derived from the desktop list rather than written out twice, so a
 * column added later cannot go missing from one order and not the other.
 *
 * The table ranks on RAW SHARPE, and the six acceptance criteria take no part in the
 * ordering. Ranking and validation answer different questions — "which of these looks
 * best" against "can this sheet support the claim at all" — and sorting by the second
 * buries a strong rule under a weak one that happened to sit on a longer sheet. The
 * Standard column still rides on every row, so nothing is hidden; it is a column, not a
 * sort key.
 *
 * Raw Sharpe must never be read without its benchmark, which is why `bench_sharpe` prints
 * in the same cell and the benchmark is spliced into the list as its own row. That is the
 * job ΔSharpe used to do by leading the table, and it still sits two columns away.
 *
 * Expectancy is a column and deliberately NOT the tiebreak. It is per trade, so it rewards
 * trading rarely: buy-and-hold scores +894% on the one position it holds for 23 years, and
 * a coin-flip opening 72 positions beats a real rule opening 642. Trades sits beside it for
 * that reason and must not be dropped.
 */
const numCell = (e, v, f) => e == null ? '<td class="flat">—</td>'
  : `<td class="${sign(v)}">${f(v)}</td>`;

/* Each column carries three things beyond how it draws a cell:
 *
 * `doc` is what it means, shown on hover. It used to be one caption under the table —
 * eight hundred words of legend that a reader either read before they had a question or
 * scrolled past forever. Same text, asked for a column at a time. A function where the
 * answer depends on the sheet (its folds, its universe, its benchmark), a plain string
 * where it does not.
 *
 * `sv` is the value the ranking sorts on when the header is clicked, and `bsv` is the
 * benchmark row's value for the same column — null where buy-and-hold has none, which is
 * the same set of columns that print an em-dash on its row. `text: true` marks the two
 * columns that sort alphabetically and therefore ascend on the first click. */
const LB_COLS = [
  { h: "Strategy", l: true, cell: r => {
      const op = opLabel(r.op);
      return `<td class="l">${esc(stemName(r.rule))}${op
        ? ` <span class="chip mut">${esc(op)}</span>` : ""}</td>`; },
    doc: `The rule, and what it is made of. A chip after the name marks a <b>pair</b> and
      gives its operator: <code>or</code> takes a position if either leg does (the most
      exposed), <code>and</code> only when both agree, <code>vote</code> by majority,
      <code>gate</code> uses one leg as a filter on the other. Single rules and pairs are
      ranked in one list because they are the same kind of object — same folds, same
      benchmark, same six criteria. Everything here is <b>walk-forward</b>: parameters are
      re-picked on each in-sample window and applied to the next, so what you are reading
      is out-of-sample.`,
    text: true, sv: r => stemName(r.rule).toLowerCase() },
  /* `Side` used to sit here and is gone (2026-08-13). It named the side the per-asset
   * standard picked — long/flat or long/short, whichever scored better on a typical name.
   * Now that the verdict is computed on the book and the book is built long/flat only,
   * every row would read "long/flat" and the column would be a constant that still
   * implied a choice had been made. The short-side scores survive in `edge_standard.csv`;
   * scoring long/short BOOKS would double the run and is a separate decision. */
  /* Every column from here down is the BOOK — one account holding the whole universe —
   * unless its `doc` says otherwise. Until 2026-08-13 most of them were the MEDIAN SINGLE
   * ASSET out of `edge_standard.csv`, which is a different portfolio over a different span
   * (11.99 years against 23.6 on us_stocks 1d), so a row mixed two measurements and the
   * chart on the detail page agreed with neither. `bookNum` is the shared renderer: it
   * blanks on rows with no book run rather than falling back to the per-asset figure,
   * because a column holding two different measurements is the bug being fixed. */
  { h: "Long %", cell: r =>
      `<td class="${bookExposure(r) != null && bookExposure(r) > 0.9 ? "loss" : ""}">${
        pctOr(bookExposure(r))}</td>`,
    bh: () => `<td class="flat">100%</td>`,
    doc: ({ sh }) => `Share of bars <b>the book</b> holds a position, weighted across every
      name it holds — how much of the time its capital was at work. <b>Read it before any
      money column.</b> Anything above 90% is flagged: at that point the rule is
      approximately buy-and-hold, and it scores near the benchmark for that reason rather
      than through skill.${
        sh.exposure_corr == null ? "" : ` On this sheet exposure and IR correlate at
      <b>${fmtSigned(sh.exposure_corr, 2)}</b>${sh.exposure_corr > 0.5
        ? " — so the old IR ranking was largely a ranking of time invested, which is why"
          + " this table is ranked on the Standard column instead, broken by a"
          + " risk-matched figure, with ROE/yr beside ROI/yr to show"
          + " what the capital earned while it was actually deployed."
        : ", so the ranking here is not simply a ranking of exposure — unlike the equity"
          + " sheets, where it is."}`}`,
    sv: r => bookExposure(r), bsv: () => 1 },
  { h: "&Delta;Sharpe",
    cell: r => bookNum(r, r.book && r.book.dsharpe, fmtIR),
    // Zero by construction — the benchmark measured against itself. This is the number
    // that places the row, and the line every rule above it has cleared.
    bh: () => `<td class="flat">0.000</td>`,
    doc: `<b>The book's</b> Sharpe minus the same universe held passively — one account
      against one account, computed <b>per fold and then averaged</b>, which is how
      <code>config.EDGE_STANDARD</code> defines the criterion the <b>Standard</b> column
      scores. The pooled version (one Sharpe over all bars, minus one) is stored beside it
      and runs a little higher; a per-fold mean weights every fold equally where pooling
      weights every bar equally. Sharpe is used rather than information ratio throughout
      because IR compares a part-time rule against a full-time one and scores capital
      deployment as much as skill.
      <br><br><b>Idle capital earns nothing here.</b> A rule that sits out half the time
      is credited with no interest for those bars, and neither is the passive book it is
      measured against — the two lose the credit together, so what goes is a return that
      was never the signal's. The per-asset version of this
      number lives in <code>edge_standard.csv</code> and is what the <b>Standard</b>
      column still counts; it is a median across names rather than an account, so the two
      do not have to agree.`,
    sv: r => r.book && r.book.dsharpe, bsv: () => 0 },
  { h: "t", cell: r => bookNum(r, r.book && r.book.t, v => fmtSigned(v, 2)),
    doc: ({ sh }) => `How reliable the ΔSharpe beside it is: its mean divided by its own
      standard error <b>across the book's walk-forward folds</b>. <b>t ≥ 2 is the bar</b>,
      and the <b>Standard</b> column raises it for multiplicity — with ~400 candidates
      searched the bar lands near 3.8, so a row can clear 2.0 here and still fail that
      criterion. Hover <b>Standard</b> for the bar this sheet measured.
      <br><br><b>The bar is measured, not assumed.</b> Correcting for a search means
      asking how high the BEST of ~400 candidates would score if none of them had an edge,
      and that depends on how alike the candidates are — dozens of near-identical candle
      patterns are not dozens of separate chances. So the sheet's own per-fold edges are
      sign-flipped at random, all rules at once, ten of thousands of times: the edge is
      destroyed, the correlation between rules is preserved, and the 95th percentile of
      the best rule's t under that null is the bar. It is exact for any correlation
      structure and needs no assumption about it.
      <br><br>It can move the bar either way. On us_stocks 1d it lands at <b>3.76</b>
      where Bonferroni asked <b>3.84</b> — nearly the same number for two cancelling
      reasons: Bonferroni is too strict about independence and too lenient about the fat
      tails of a 21-fold average. Measured against simulated independent panels, those 387
      candidates behave like about <b>85</b> separate tests, not 387.
      <br><br>Measured across <b>time</b>, never across assets. The account IS every name
      at once, so breadth cannot inflate it — which is the failure mode a per-asset t has
      to be defended against, since twenty stocks that move together are not twenty
      independent tests.
      <br><br>A block bootstrap over the same book is also stored and is <b>looser</b>:
      <code>ibs</code> on us_stocks 1d bootstraps to +3.87 and scores +2.81 across its 21
      folds. The threshold was calibrated on fold-to-fold spread, so this is the number the
      verdict reads; the bootstrap is a second opinion, not a better one.
      <br><br><b>Clearing the bar is not the same as clearing luck.</b> This asks whether
      one rule beat its benchmark reliably; the deflated Sharpe prices how many rules were
      looked at before it was picked, and lives in
      <code>portfolio_wf.py --n-trials --trial-dispersion</code>.`,
    sv: r => r.book && r.book.t },
  { h: "Expectancy", cell: r => bookNum(r, r.book && r.book.expectancy,
      v => fmtSigned(v * 100, 2) + "%"),
    doc: `What one <b>trade</b> is worth on average, as a percentage:
      <code>win% × avg win − loss% × avg loss</code>, pooled across every name the book
      holds. A trade is one position held from open to close, not one bar.
      <b>Only readable beside Trades.</b> It rewards trading rarely, so it is a column and
      never the sort key: buy-and-hold shows a huge expectancy on the single position it
      holds for the whole window, and a coin-flip rule that opens 72 positions beats a real
      one that opens 642. High expectancy with a handful of trades is a small sample, not an
      edge.`,
    sv: r => r.book && r.book.expectancy },
  { h: "Win %", cell: r => bookNum(r, r.book && r.book.win_rate,
      v => fmtNum(v * 100, 1) + "%"),
    doc: `Share of the book's trades that closed profitably, pooled across its names.
      Deliberately <b>not</b> a ranking metric and not a virtue on its own — it is one half
      of expectancy, and the half that can be pushed arbitrarily high by cutting winners
      early and holding losers. Read it against the average win and loss it is paired
      with.`,
    sv: r => r.book && r.book.win_rate },
  { h: "ROI/yr", cell: r => bookNum(r, r.book && r.book.cagr,
      v => fmtSigned(v * 100, 1) + "%"),
    // The passive book's own annual rate, from the sheet rather than the row: holding is
    // one portfolio, so it is the same figure opposite every rule.
    bh: (b, sh) => `<td class="flat">${sh.book_bench && sh.book_bench.cagr != null
      ? fmtSigned(sh.book_bench.cagr * 100, 1) + "%" : "—"}</td>`,
    doc: `Annualised return of <b>the book</b> — what the whole account earned, including
      the time its capital sat in cash earning <b>nothing</b>. This is the honest "what
      did I make" number, and it is the one that penalises a rule for being out of the
      market — the more so since 2026-08-13, when the T-bill credit came off both
      sides. It is the same series the equity chart on the detail page draws, so the two
      cannot disagree.`,
    sv: r => r.book && r.book.cagr,
    bsv: (b, sh) => sh.book_bench && sh.book_bench.cagr },
  { h: "ROE/yr", cell: r => bookNum(r, r.book && r.book.roe_ann,
      v => fmtSigned(v * 100, 1) + "%"),
    // Identical to ROI on this row and that is the point: buy-and-hold is never idle, so
    // it has no gap between what the account earned and what the deployed money earned.
    bh: (b, sh) => `<td class="flat">${sh.book_bench && sh.book_bench.cagr != null
      ? fmtSigned(sh.book_bench.cagr * 100, 1) + "%" : "—"}</td>`,
    doc: `The book's return on capital <b>while it was actually deployed</b>: the interest
      earned on the idle fraction is stripped out, and what is left is annualised over
      <i>deployed</i> years — calendar years × time invested — rather than calendar years.
      ROI asks what the account earned; this asks what the money earned when it was at
      work, and the two differ by exactly the idle time. It is here because exposure and IR
      correlate at 0.881 on daily equities, so an account-level ranking is substantially a
      ranking of who stayed invested longest. Buy-and-hold's ROI and ROE are identical
      because it is never idle; a rule holding 46% of the time can earn far more per
      deployed dollar and still show a smaller ROI.`,
    sv: r => r.book && r.book.roe_ann,
    bsv: (b, sh) => sh.book_bench && sh.book_bench.cagr },
  { h: "Sharpe", lead: true,
    cell: r => vsCell(r.book && r.book.sharpe, r.book && r.book.sharpe_bench, fmtSharpe,
      (a, b) => a > b, "the same universe held passively, over the same bars"),
    bh: (b, sh) => `<td class="flat">${fmtNum(
      sh.book_bench && sh.book_bench.sharpe, 3)}</td>`,
    doc: `The book's return per unit of volatility. Idle capital earns nothing, so a rule
      is not paid for the bars it sat out. Coloured against <b>the same
      universe held passively over the same bars</b> rather than against nothing, and
      hovering the cell gives that value: raw Sharpe largely rewards time in the market, so
      0.66 reads like skill until you see the benchmark scored 0.63. The level is context;
      the comparison is the number.
      <br><br><b>Measured at the optimistic fill.</b> Every figure on this row is computed
      buying at the same close whose high, low and close produced the signal — a price
      nobody knew when the decision was made. Removing that costs real performance, and how
      much depends on whether you can trade the closing auction: with a market-on-close
      order and the signal computed minutes early it is a small haircut, and if you have to
      wait for the next open it is a large one. Treat this column as the <b>top</b> of a
      range, not a result. <code>portfolio_wf.py --fill</code> prices both ends.`,
    sv: r => r.book && r.book.sharpe,
    bsv: (b, sh) => sh.book_bench && sh.book_bench.sharpe },
  /* The BOOK's drawdown, not the median asset's, since 2026-08-13.
   *
   * They are wildly different numbers and the page was showing the less useful one: `ibs`
   * on us_stocks 1d falls 39.6% as a typical single stock and 18.7% as the book, because
   * 189 names falling on different days is the whole point of holding 189 names. Nobody
   * trades the median asset, so nobody ever lived through its drawdown. This column now
   * matches the equity chart on the detail page — same series, same number — and the
   * per-asset figure is one line down in the doc for anyone who wants it. */
  { h: "Max DD", cell: (r, sh) => bookDdCell(r, sh && sh.book_bench && sh.book_bench.dd),
    bh: (b, sh) => `<td class="flat">${sh.book_bench && sh.book_bench.dd != null
      ? fmtNum(sh.book_bench.dd, 1) + "%" : "—"}</td>`,
    doc: ({ sh }) => `The worst peak-to-trough fall of <b>the book</b> — one account
      holding every name at once — against the same universe held passively${
        sh.book_bench && sh.book_bench.dd != null
          ? `, which fell <b>${fmtNum(sh.book_bench.dd, 1)}%</b> over these bars` : ""}.
      Hover a cell for the comparison.
      <br><br><b>This is not the drawdown of a typical single stock</b>, which is the
      figure this column used to carry and is roughly twice as deep: individual names fall
      on different days, so a book of them falls far less than any of its parts. The
      per-asset figure still exists in <code>edge_standard.csv</code>; it is just not what
      anyone would have experienced. A rule that sits out part of the time ought to fall
      less than one that never does; many here do not, and that is worth knowing before
      the money columns are believed.`,
    // Drawdowns are negative, so the largest number is the shallowest fall — sorting
    // descending puts the least painful first, like every other column here.
    sv: r => r.book && r.book.dd, bsv: (b, sh) => sh.book_bench && sh.book_bench.dd },
  { h: "Trades/asset", cell: r => tradesCell(r.book && {
      trades: r.book.trades_per_asset }),
    bh: () => `<td class="flat" title="one position, opened at the start and never closed">1</td>`,
    doc: `The book's trades divided by the ${""}names it holds — positions opened on a
      typical name over the whole out-of-sample window. Not good or bad on its own; it is
      <b>what makes the profit factor beside it readable</b>. 1,283 trades is a
      distribution; 3 is an anecdote, and a rule that trades three times can post a profit
      factor of 32 without having found anything. Buy-and-hold shows 1: opened at the
      start, never closed.`,
    sv: r => r.book && r.book.trades_per_asset, bsv: () => 1 },
  { h: "Profit factor", cell: r => pfCell(r.book, bookExposure(r)),
    doc: `Gross winnings ÷ gross losses across the book's closed trades, pooled over its
      names. Scored against <b>1.00</b>, not against the benchmark, because buy-and-hold
      holds one position throughout and never closes a trade — it has no profit factor to
      compare with, and inventing one would be worse than leaving the cell blank. Greyed
      above 90% invested, where a rule barely closes anything. Read it with
      Trades/asset.`,
    sv: r => r.book && r.book.profit_factor },
  { h: "vs random", cell: r => bookNum(r, r.book && r.book.vs_random, fmtIR),
    doc: `The book's Sharpe above a <b>signal-free control invested exactly as often</b>,
      at random. Being in the market pays in a rising market whether or not you were right,
      so this prices that handicap and leaves what the signal itself did. A rule that
      cannot beat its own coin-flip has found exposure, not an edge.
      <br><br>The controls are not a model: <code>RANDOM_25/50/75/90</code> are backtested
      as books by the same run, on the same bars and fees, and the curve through their
      measured Sharpes is read at this rule's own exposure. Each of them therefore scores
      exactly +0.000 here, which is the check that the curve is honest.`,
    sv: r => r.book && r.book.vs_random },
  { h: "vs constant", cell: r => bookNum(r, r.book && r.book.vs_constant, fmtIR),
    doc: `The same question asked a second way: return per unit of drawdown (CAGR ÷ max
      drawdown) against simply <b>owning less of the same basket, all the time</b>, at the
      book's own average weight and the rest in cash. Anyone can hold 47% of a basket
      and keep the rest in cash — a rule has to beat that before its timing is worth
      anything.`,
    sv: r => r.book && r.book.vs_constant },
  /* `$10k / asset` and a per-asset `vs B&H` used to sit here, and they are gone
   * (2026-08-13). Both were the MEDIAN SINGLE NAME — a portfolio nobody holds, over a
   * ~12-year membership spell rather than the sheet's 23.6-year out-of-sample span — and
   * the two columns immediately below ask the identical questions of the account that a
   * reader would actually have owned: what it became, and what it beat holding by once
   * the risk was matched. Two money columns on two different measurements invited exactly
   * one mistake, which is reading them as a bigger and a smaller version of one number.
   * The per-asset figures survive in `edge_standard.csv` and in the per-asset table on
   * each detail page, where the header says what they are. */
  { h: "$10k / book", lead: true, cell: r => bookWealthCell(r.book),
    bh: (b, sh) => `<td class="flat">${fmtMoney(sh.book_bench && sh.book_bench.wealth)}</td>`,
    doc: ({ sh }) => {
      const bb = sh.book_bench;
      if (!bb) return `No book run covers this sheet, so every row prints an em-dash.
        The columns to the left are per-asset medians and remain the sheet's only money.`;
      return `What $10,000 became in <b>one account holding the whole universe</b> —
        ${bb.n_names} names, equal-weighted, rebalanced every bar, held only on the dates
        each was actually a member — over <b>${fmtNum(bb.years, 1)} years</b>
        (${esc(bb.start)} to ${esc(bb.end)}).
        <br><br>This is the number a reader means by "what would I have made". It is
        <b>not</b> a bigger version of <i>$10k / asset</i>: that one is the median single
        stock over its own membership spell, this is the portfolio over the sheet's whole
        out-of-sample span, and diversification, rebalancing and universe churn all live in
        the gap. Holding this universe passively over these bars returned
        <b>${fmtMoney(bb.wealth)}</b>${bb.index_wealth
          ? `, against ${fmtMoney(bb.index_wealth)} for ${esc(bb.index_symbol)} — the
        purchasable index — over the same bars` : ""}.
        <br><br><b>Green here and green in <i>book vs B&amp;H</i> are different claims,
        and rows routinely have one without the other.</b> This column is coloured on raw
        money — did the account end with more than holding. The next one is coloured on
        the risk-matched comparison. A rule invested half the time can clear holding
        comfortably per unit of risk and still finish with far less money, because it was
        only ever exposed to half the market. Neither number is the trick; read them
        together.
        <br><br>Scored on the same walk-forward out-of-sample bars as everything else on
        the table: the run starts at the first bar that was ever out-of-sample, so no rule
        is credited with the history it was selected on.`;
    },
    sv: r => r.book && r.book.wealth, bsv: (b, sh) => sh.book_bench && sh.book_bench.wealth },
  { h: "book vs B&amp;H", lead: true,
    cell: r => numCell(r.book, r.book && r.book.cm_excess_cagr != null
      ? r.book.cm_excess_cagr * 100 : null, v => fmtPct(v, 2)),
    bh: () => `<td class="flat">+0.00%</td>`,
    doc: `<b>The tiebreak this table is ordered by</b>, inside each tier of the Standard
      column. Annual return of the book above the
      same universe held passively, after the passive side has been scaled <i>down</i> with
      cash to the rule's own volatility — never levered up, so no margin and no borrow.
      <br><br>Matching the risk first is what makes it a measure of skill rather than of
      nerve. <code>corr(IR, long_frac)</code> is 0.881 on daily equities, so ranking on
      plain return is largely ranking on who stayed invested longest; a rule in the market
      47% of the time is compared here with holding 47% of the market and the rest in
      cash. A rule that beats holding only by taking more risk scores +0.00% here, which
      is the honest answer.`,
    sv: r => r.book && r.book.cm_excess_cagr, bsv: () => 0 },
  { h: "fees", cell: r => bookNum(r, r.book && r.book.headroom, v => fmtNum(v, 1) + "x"),
    doc: `Cost headroom: how many times the modelled commission and spread could rise
      before <b>the book</b> stops beating the same basket held at its own volatility.
      <b>0.0x means it already does not</b>, at the real cost. A high-turnover rule with a
      thin edge dies here first, which is why the number sits on the row rather than in an
      appendix.
      <br><br>Measured, not extrapolated: every term in the cost model is linear in its
      own rate, so the account at 5x the schedule is its zero-cost book minus five times
      its measured drag — exact arithmetic on the same series, not a re-fit. The ladder is
      0.5, 1, 2, 3, 5, 10, 20 and it stops at the first multiple that fails.`,
    sv: r => r.book && r.book.headroom },
  { h: "Standard", l: true, lead: true,
    cell: r => `<td class="l">${edgeCount(r.book && r.book.standard)}</td>`,
    doc: ({ sh }) => `<b>The column this table is ranked on.</b> How many of the <b>six
      acceptance criteria</b> the row cleared — hover the cell for which ones, with each
      target. All six or it is not an edge; nothing here has cleared them. This is the only
      verdict on the page, and it is computed on the book with idle capital earning
      nothing. A row the standard has not scored prints em-dashes rather than
      being dropped, and a sheet with too few folds says <i>cannot tell</i> rather than
      <i>no</i>.${sh.powered === false ? `
      <br><br><b>This sheet is one of those</b>: the book spans ${sh.book_folds
        ? `<b>${sh.book_folds} fold${sh.book_folds === 1 ? "" : "s"}</b>` : "too few folds"}
      against the 20 the threshold was calibrated on, so neither a pass nor a fail in this
      column means anything here. The money columns are unaffected — what the account did
      is a measurement, and only this one needs statistical power it does not have.` : ""}
      <br><br><b>Computed on the BOOK</b>, like every other column here — the six criteria
      are the same and so are their thresholds (<code>metrics.apply_edge_standard</code> is
      shared with the per-asset stage), but they are fed the account's numbers. Four of
      them are columns you can read on this row: <i>ΔSharpe</i>, <i>vs random</i>,
      <i>vs constant</i> and <i>fees</i>. The fifth is the money above the
      volatility-matched basket, and the sixth is the <i>t</i> beside them.
      <br><br><b>Two of the six are not the same statistic they are per asset.</b> ΔSharpe
      here is the difference of two pooled Sharpes, not the mean of per-fold differences;
      and <i>t</i> is a block bootstrap over the book's bars, not a t across folds. Both
      are still measured across <b>time</b> — a book cannot borrow significance from
      breadth, since it is every name at once — but a bootstrap over 5,900 bars is a
      looser test than 54 folds, and rows pass here that do not pass per asset.
      <br><br>It is a <b>coarse</b> key — six integer tiers, and nothing reaches the top
      one — so most of the visible order comes from the tiebreak inside each tier, which is
      <b>${sh.ranked_tiebreak === "book_cm_excess_cagr" ? "book vs B&amp;H"
        : "Sharpe"}</b> on this sheet. Read the tiers as how much evidence a row has and
      the order within one as how much money it made at equal risk; a rule cannot climb a
      tier by earning more.
      <br><br>Buy-and-hold clears none of the six and is drawn where that puts it. It is
      not competing: the six are measured <i>against</i> it, so it cannot pass its own
      test, and the rows above it are the ones with something the standard could score.`,
    sv: r => r.book && r.book.standard && r.book.standard.passed },
];

/* ---------- what a click on a header does ----------
 * Re-orders **the rows the Standard ranking already selected**. It does not go back to the
 * sheet and fetch the same number of best rows by the clicked column — `payload.py` cut the
 * list long before the page saw it. That distinction is the difference between reading a
 * leaderboard and selecting on a test column, which this repo has done once and had to
 * retract, so the note above the table says which of the two you are looking at whenever
 * the order is not the default.
 *
 * Missing is not small. An unscored row, and the benchmark in the columns where it has no
 * comparable value, sink to the bottom in BOTH directions rather than winning an ascending
 * sort with a blank. Ties keep the delivered order they arrived in. */
let lbSort = null;

function lbOrder(sh, benchEdge, sort) {
  const rows = sh.rows.map(row => ({ row }));
  if (!sort) {
    // The delivered order, with the benchmark spliced in at the rank ITS OWN SHARPE earns:
    // where that line falls in the list IS the result.
    //
    // Must key off the same column the list is sorted by. It used to find the first row
    // with a negative ΔSharpe, which was right while ΔSharpe did the ranking and became
    // wrong the moment raw Sharpe did: on us_stocks 1d the benchmark landed at 0.460
    // ABOVE `volmanaged` at 0.489, because `volmanaged` has a negative per-fold ΔSharpe
    // while still out-Sharpe-ing the benchmark overall. The two disagree because ΔSharpe
    // is the mean of per-fold differences and this column is the pooled ratio — a real
    // distinction, but not one that may reorder the table against its own sort key.
    //
    // The key is now the standard's own count, so the benchmark is placed by that count
    // and not by the tiebreak beneath it. It clears NONE of the six — not because it is
    // bad but because it is the bar the six are measured against — so it sits below every
    // rule that cleared at least one criterion and above every rule that cleared none.
    // That keeps the line monotone with the column the table is sorted on; placing it by
    // the tiebreak instead would drop it into the middle of a tier, above rows that
    // cleared more of the standard than it did.
    const below = sh.ranked_on === "edge_passed"
      ? sh.rows.findIndex(r => r.book && r.book.standard
                               && r.book.standard.passed <= 0)
      : sh.ranked_on === "book_cm_excess_cagr"
      ? sh.rows.findIndex(r => r.book && r.book.cm_excess_cagr != null
                               && r.book.cm_excess_cagr < 0)
      : sh.rows.findIndex(r => r.edge && r.edge.sharpe != null
                               && r.edge.sharpe < benchEdge.bench_sharpe);
    if (benchEdge) rows.splice(below < 0 ? rows.length : below, 0, { bench: true });
    return rows;
  }
  const c = LB_COLS[sort.i];
  if (benchEdge) rows.push({ bench: true });
  const val = e => {
    const v = e.bench ? (c.bsv ? c.bsv(benchEdge, sh) : null) : c.sv(e.row);
    return v == null || (typeof v === "number" && !isFinite(v)) ? null : v;
  };
  return rows.map((e, i) => ({ e, i, v: val(e) }))
    .sort((a, b) => (a.v == null) - (b.v == null)
      || (a.v == null ? a.i - b.i
        : (typeof a.v === "string" ? sort.dir * a.v.localeCompare(b.v)
                                   : sort.dir * (a.v - b.v)) || a.i - b.i))
    .map(x => x.e);
}

/* Buy-and-hold, rendered into the ranking at the position the sort key gives it — under
 * the Standard ranking, below every rule that cleared a criterion it could not.
 *
 * It is not a candidate and is deliberately absent from `edge_standard.csv` — scoring the
 * benchmark as one of the things being selected would add it to the trial count and let it
 * win its own comparison. But leaving it off the page entirely made the reader hold the
 * benchmark in their head while scanning 25 rows, which is exactly the arithmetic people
 * get wrong. So it is drawn, from the `bench_*` figures every scored row already carries,
 * and marked as the bar rather than a competitor: no verdict, no link, muted throughout.
 *
 * Only the columns that are genuinely the benchmark's own are filled. `t`, `vs random` and
 * `vs constant` are left blank rather than set to zero: an exposure-matched control at 100%
 * long IS buy-and-hold, so those comparisons are degenerate, not passed. Same for the
 * standard — it is the bar, so it does not clear it.
 */
const benchRow = (bench, cols, sh) => {
  if (bench == null) return "";
  /* Under the Standard ranking the benchmark has no count to be placed by, so it falls to
   * the bottom of every sheet — nothing has ever cleared zero criteria. Last place on a
   * leaderboard reads as "worst", and on this repo that is the one conclusion the page
   * must not imply by accident: the whole finding is that nothing beats holding. So the
   * row says it is not in the ranking rather than leaving its position to speak. */
  const unranked = sh.ranked_on === "edge_passed"
    ? ` <span class="chip mut" title="The six criteria measure a rule AGAINST buy-and-hold, so the benchmark cannot clear them and has no count to be ranked by. Its position here is not a score.">not ranked</span>`
    : "";
  const cells = cols.map((c, i) => i === 0
    ? `<td class="l">Buy &amp; hold <span class="chip mut">benchmark</span>${unranked}</td>`
    : c.bh ? c.bh(bench, sh) : `<td class="flat">—</td>`).join("");
  return `<tr class="bench-row">${cells}</tr>`;
};

/* Must stay in step with the phone breakpoint in `app.css` — that is where the first
 * column is frozen, and the lead columns are only worth moving because they land against
 * a name that stays put. */
const NARROW = matchMedia("(max-width:760px)");

const lbCols = () => {
  if (!NARROW.matches) return LB_COLS;
  const [name, ...rest] = LB_COLS;
  return [name, ...rest.filter(c => c.lead), ...rest.filter(c => !c.lead)];
};

/* Two painted regions with the filter row parked between them: `bt-head` is the sheet's
 * summary and the caveats that qualify it, `bt-body` is the ranking itself. Split only so
 * the filters can sit against the table they change; both are rewritten together on every
 * click, and neither owns the buttons. */
function paintBacktest() {
  // The board switch is the first thing this asks, because the two boards share the page
  // and the two painted regions and nothing else.
  if (bf.board === "conv") return paintConversions();
  const head = document.getElementById("bt-head");
  const host = document.getElementById("bt-body");
  const grp = D.backtest[bf.cls], sh = sheetOf(bf.cls, bf.tf);
  if (!sh) {
    head.innerHTML = "";
    host.innerHTML = `<div class="note">No walk-forward sheet for ${esc(grp.label)} at
      ${bf.tf}. Run <code>python walkforward.py --class ${CLASS_ARG[bf.cls]} --tf ${bf.tf}</code>
      in <code>walk-forward optimization/</code>.</div>`;
    return;
  }
  const best = sh.rows[0], cols = lbCols();
  /* Buy-and-hold does not depend on the rule, so its row is taken off the first scored row
   * rather than recomputed — one benchmark for the whole sheet. */
  const benchEdge = (sh.rows.find(r => r.edge) || {}).edge ?? null;
  head.innerHTML = `
  <div class="strip">
    ${/* Both counts report what the TABLE rests on, not what the sheet knows about.
        * `grp.n` is every symbol in the universe list and `sh.n_rules` every candidate
        * ranked; the scored columns ran on neither. On us_stocks 1d the standard scored
        * 614 of 751 names — the quarantined impostors and the names without enough
        * post-2000 history are gone — and 89 of 416 strategies. Advertising only the
        * larger number made the evidence look broader than it is, which is the one
        * direction a header must never round. */""}
    <div class="stat"><span class="k">Universe</span>
      <span class="v">${sh.n_assets_scored != null ? sh.n_assets_scored : grp.n}</span>
      <span class="s">${sh.n_assets_scored != null && sh.n_assets_scored !== grp.n
        ? `scored, of ${grp.n} in ${esc(grp.label)}` : esc(grp.label)}</span></div>
    <div class="stat"><span class="k">Out-of-sample</span><span class="v">${sh.years.toFixed(1)}y</span>
      <span class="s">per asset · ${sh.folds} walk-forward folds</span></div>
    ${/* The book's span is a DIFFERENT number and it sits here so the two money columns
        * are never read against one shared header. The per-asset figure above is each
        * name's own out-of-sample bars — a membership spell, ~12y on us_stocks — while the
        * book runs the whole out-of-sample calendar, ~23.6y. That is why the same
        * buy-and-hold appears twice on the table at very different sizes. */""}
    ${sh.book_bench ? `<div class="stat"><span class="k">Book span</span>
      <span class="v">${fmtNum(sh.book_bench.years, 1)}y</span>
      <span class="s">${sh.book_bench.n_names} names held as one account</span></div>` : ""}
    <div class="stat"><span class="k">Strategies</span>
      <span class="v">${sh.n_scored != null ? sh.n_scored : sh.n_rules}</span>
      <span class="s">${sh.n_scored != null && sh.n_scored !== sh.n_rules
        ? `scored, of ${sh.n_rules} ranked` : ""}</span></div>
    ${/* ΔSharpe of the BOOK, not IR across assets. IR compares a part-time rule with a
        * full-time benchmark and so pays for exposure; and it was the one card here still
        * quoting a per-asset statistic above a table that is now entirely account-level. */""}
    <div class="stat"><span class="k">Best strategy</span>
      <span class="v ${sign(best.book && best.book.dsharpe)}">${
        fmtIR(best.book && best.book.dsharpe)}</span>
      <span class="s">${esc(stemName(best.rule))} · &Delta;Sharpe as a book</span></div>
    <div class="stat"><span class="k">Time invested</span>
      <span class="v">${pctOr(best.book ? best.book.exposure : best.long_frac)}</span>
      <span class="s">of bars, by the book — read this first</span></div>
    ${/* The BOOK where there is one, the median asset only as a fallback.
        *
        * This card sits directly above the leaderboard and is the figure a reader quotes,
        * so it has to be the account-level one. On the median-asset basis it read "$35k vs
        * $26k held", which is true of a typical single stock and roughly an order of
        * magnitude below what holding the actual universe returned over the same study —
        * a headline that disagrees with the column beneath it by that much is worse than
        * no headline. The sub-line names the basis either way. */""}
    ${best.book ? `<div class="stat"><span class="k">$10k became</span>
      <span class="v">${fmtMoney(best.book.wealth)}</span>
      <span class="s">as a book, vs ${fmtMoney(sh.book_bench && sh.book_bench.wealth)} held${
        best.book.cm_excess_cagr == null ? ""
          : ` · ${fmtPct(best.book.cm_excess_cagr * 100, 2)}/yr at equal risk`}</span></div>`
    : `<div class="stat"><span class="k">$10k became</span>
      <span class="v">${fmtMoney(grew(best.net_pct))}</span>
      <span class="s">median asset · vs ${fmtMoney(grew(best.bh_pct))} held · ${
        fmtDelta(pnlDelta(best.net_pct, best.bh_pct))}${
        pnlRatio(best.net_pct, best.bh_pct) == null ? ""
          : " · " + fmtRatio(pnlRatio(best.net_pct, best.bh_pct)) + " the profit"}</span></div>`}
    <div class="stat"><span class="k">Luck threshold</span><span class="v">+${sh.noise_ceiling}</span>
      <span class="s">best of ${sh.n_rules} worthless strategies</span></div>
  </div>`;

  host.innerHTML = `
  <section class="sec">
    <div class="sec-head"><h2>Leaderboard</h2>
      <span class="sec-note" id="lb-note"></span></div>
    ${/* The `powered: false` banner used to sit here — a paragraph above the table on
        * every underpowered sheet, which is five of the eight. It said the standard cannot
        * resolve an edge on this few folds, which is true and is still said: the fact moved
        * into the `Standard` column's own `doc`, WITH the sheet's fold count, so it is
        * asked for rather than read past. Nothing was dropped; see that column. */""}
    ${/* Where a hovered header answers itself. Absolutely positioned over the top of the
        * ranking rather than pushed into the flow above it: a block that opens on hover and
        * moves the table down moves the header out from under the cursor, which closes it
        * again. */""}
    <div id="lb-doc" class="coldoc" hidden></div>
    <div class="tbl-wrap"><table>
      <thead><tr>${cols.map(c =>
        `<th${c.l ? ' class="l"' : ""}${c.doc || c.sv
          ? ` data-doc="${LB_COLS.indexOf(c)}"` : ""}>${c.h}</th>`).join("")}</tr></thead>
      ${/* A row the standard has not scored prints em-dashes rather than being dropped.
          * The sweep universe and the scored universe can differ, and hiding the gap would
          * read as "everything here was judged" — each cell handles its own null edge. */""}
      <tbody id="lb-body"></tbody>
    </table></div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Universe</h2>
      <span class="sec-note">${esc(grp.label)}</span></div>
    <p class="universe">${grp.universe.map(esc).join(" · ")}</p>
  </section>`;

  /* Re-sorting rewrites the body and nothing else. The header cells survive, so the
   * explanation does not blink out from under the cursor that just clicked, and the
   * horizontal scroll position of a sixteen-column table is not thrown away. */
  const paintRows = () => {
    const body = host.querySelector("#lb-body");
    body.innerHTML = lbOrder(sh, benchEdge, lbSort).map(e => e.bench
      ? benchRow(benchEdge, cols, sh)
      // `sh` is passed as a second argument for the columns whose comparison value is a
      // property of the SHEET rather than of the row — the book's passive drawdown is one
      // portfolio for the whole table, so every row would otherwise re-derive it or, as
      // Max DD did, quietly show a different measurement instead.
      : `<tr data-go="#/backtest/${bf.cls}/${bf.tf}/${slug(e.row.rule)}">${
          cols.map(c => c.cell(e.row, sh)).join("")}</tr>`).join("");
    host.querySelectorAll("th[data-doc]").forEach(th => {
      const on = lbSort && lbSort.i === Number(th.dataset.doc);
      th.classList.toggle("sort-desc", !!on && lbSort.dir < 0);
      th.classList.toggle("sort-asc", !!on && lbSort.dir > 0);
    });
    const by = lbSort ? LB_COLS[lbSort.i].h : null;
    // The basis is named rather than assumed, and BOTH keys are named. It has changed
    // three times — ΔSharpe, raw Sharpe, the book's risk-matched excess — and each time a
    // hardcoded caption survived the change and described the previous one. It is now the
    // standard's count with the old basis demoted to the tiebreak, which is a distinction
    // a reader cannot recover from the rows: six integer tiers look like no ordering at
    // all until the caption says what is ordering inside them.
    const tie = sh.ranked_tiebreak === "book_cm_excess_cagr"
      ? "book vs B&amp;H" : "Sharpe";
    const basis = sh.ranked_on === "edge_passed"
      ? `ranked on Standard, ties on ${tie}`
      : sh.ranked_on === "book_cm_excess_cagr"
      ? "ranked on book vs B&amp;H, risk-matched" : "ranked on Sharpe";
    const picked = sh.ranked_on === "edge_passed" ? `Standard, then ${tie}`
      : sh.ranked_on === "book_cm_excess_cagr" ? "book vs B&amp;H" : "Sharpe";
    host.querySelector("#lb-note").innerHTML =
      `top ${sh.rows.length} of ${sh.n_rules} · ${sh.n_shown_pairs} of them pairs · ${by
        ? `picked on ${picked}, re-ordered by ${by} — <b>not</b> the best ${
            sh.rows.length} by ${by}`
        : basis}${sh.n_flat_dropped
        ? ` · ${sh.n_flat_dropped} rule${sh.n_flat_dropped === 1 ? "" : "s"} that never
            opened a position removed` : ""} · tap a row for its detail`;
    bindGo(body);
  };
  paintRows();
  bindGo(host);
  bindColHeaders(host, { sh, grp, bench: benchEdge?.bench_wealth ?? null }, i => {
    // First click puts the best value at the top — descending for every figure here,
    // ascending for the two text columns. Second flips it, third gives the sheet back the
    // order it was delivered in, which is the only one the note can call a ranking.
    const first = LB_COLS[i].text ? 1 : -1;
    lbSort = !lbSort || lbSort.i !== i ? { i, dir: first }
      : lbSort.dir === first ? { i, dir: -first } : null;
    paintRows();
  });
}

/* ========================= THE CONVERTED-STRATEGY BOARD =========================
 * The second leaderboard on this page. Thirteen third-party rules — eight TradingView
 * Pine scripts, four freqtrade strategies and one pair of notebooks — run through the same
 * `portfolio_wf.py` stage, read through the same `_book_record`, ranked on the same key:
 * the standard's own count, ties on the book's risk-matched excess CAGR.
 *
 * It is a separate board rather than extra rows on the house one because it has a
 * different timeframe axis (1d down to 1m, where the research catalogue runs 1d and 4h),
 * three facets no house rule carries, and its own pre-registered trial family. The reasons
 * are set out in `payload.conversion_sheets`; the consequence here is that only the
 * filter strip and the column list differ, and everything that decides what a number MEANS
 * is shared code.
 *
 * There is no detail page. `run_book.sh` publishes `book_curves_*.json` for the house
 * sheets only, so these rules have no equity series on disk — and a row that navigates to
 * a stub is worse than a row that says it does not navigate. The note under the table says
 * so rather than leaving a dead click to be discovered. */

const CONV = () => D.conversions || { groups: {}, timeframes: [], roster: [], totals: {} };
const convGroup = () => CONV().groups[bf.cls];
const convSheetOf = (cls, tf) => {
  const g = CONV().groups[cls];
  return g ? g.sheets.find(s => s.timeframe === tf) : null;
};
const convTfPills = () => (CONV().timeframes || ["1d"]).map(t => [t, t]);
const convClassPills = () => Object.keys(CONV().groups)
  .map(k => [k, CLASS_LABEL[k] || k]);

/* The three facets, as chips beside the name.
 *
 * They are chips and never part of the name because each of them is the SAME strategy
 * measured a different way, and the reader's first question on this board is how one rule
 * did — not how four labels that share a stem did. `reverses` is the load-bearing one: as
 * published, eight of the thirteen flip long-to-short instead of selling to cash, and on
 * this repo's benchmark that single property dominates everything else the rule does. */
const convChips = r => [
  r.short ? `<span class="chip warn" title="As published: a short signal REVERSES the position rather than selling to cash. The benchmark's drift is then paid twice over every downtrend the rule is wrong about.">reverses</span>` : "",
  r.short_off ? `<span class="chip mut" title="The same signal with the short half removed: it sells to cash instead of flipping. Its reversing twin is on this table too.">short off</span>` : "",
  r.ha ? `<span class="chip mut" title="The signal is computed on Heikin-Ashi candles; the money still settles on real closes. A chart platform fills at the synthetic close, which is an average of four prices nobody could transact at — that difference alone accounts for most published HA results.">Heikin-Ashi</span>` : "",
  r.eod === "flat" ? `<span class="chip mut" title="Positions are closed at the session bell instead of being carried overnight.">flat at the close</span>` : "",
].filter(Boolean).join(" ");

/* Twelve columns, all of them the BOOK, all of them shared renderers with the house
 * leaderboard. Fewer than the sixteen next door because four of those are per-asset
 * columns off `edge_standard.csv`, which never scored this family outside CME futures —
 * and a column that is an em-dash on every row of every sheet is not a column. */
const CONV_COLS = [
  { h: "Strategy", l: true, lead: true,
    cell: r => `<td class="l">${esc(r.base)} ${convChips(r)}</td>`,
    doc: `The rule, and what was done to it. Chips carry the three facets this family has
      and the house catalogue does not.
      <br><br><b>reverses</b> / <b>short off</b> — the eight rules that came from Pine have
      a short side, because <code>strategy.entry(short)</code> flips the position rather
      than closing it to cash; both versions of each are on this table. Across 256 matched
      pairs on these sheets, switching the short side off improved the result <b>253</b>
      times, by a median of <b>16.3</b> percentage points a year, so quoting the reversing
      cell alone measures the short leg and not the signal. <b>A row with neither chip has
      no short side at all</b> — the five freqtrade and notebook rules sell to cash and
      never had one, so there is nothing to switch off and nothing to compare against.
      <br><br><b>Heikin-Ashi</b> — the signal runs on synthetic candles while the money
      settles on real closes. An HA close is <code>(O+H+L+C)/4</code>, an average nobody
      trades at; chart platforms fill at it by default, which is enough on its own to make
      most HA strategies look profitable. A published HA result and one from this board are
      not the same measurement.
      <br><br><b>flat at the close</b> — the intraday variant that does not carry a
      position overnight. Both variants are ranked together so the comparison is on the
      page rather than in somebody's head.`,
    sv: r => r.base, text: true },
  { h: "Long %", cell: r =>
      `<td class="${bookExposure(r) != null && bookExposure(r) > 0.9 ? "loss" : ""}">${
        pctOr(bookExposure(r))}</td>`,
    bh: () => `<td class="flat">100%</td>`,
    doc: `Share of bars the book holds a position. <b>Read it before any money column.</b>
      Anything above 90% is flagged: at that point the rule is approximately buy-and-hold
      and scores near the benchmark for that reason rather than through skill.
      <br><br>It matters more on this board than on the house one. Seven of the thirteen
      hold a position more than 90% of the time as published, four of them above 98%, and
      those seven are the rows at the bottom of the daily sheets. A rule that is always
      invested cannot add anything by timing — it can only be wrong about direction.`,
    sv: r => bookExposure(r), bsv: () => 1 },
  { h: "Sharpe", lead: true,
    cell: r => vsCell(r.book && r.book.sharpe, r.book && r.book.sharpe_bench, fmtSharpe,
      (a, b) => a > b, "the same universe held passively, over the same bars"),
    bh: (b, sh) => `<td class="flat">${fmtNum(
      sh.book_bench && sh.book_bench.sharpe, 3)}</td>`,
    doc: `The book's return per unit of volatility, coloured against the same universe held
      passively over the same bars — hover a cell for that value. Raw Sharpe largely
      rewards time in the market, so a level means little without the benchmark beside it.
      <br><br><b>Measured at the optimistic fill</b>, like every figure on this page: the
      signal is computed from a bar's own close and filled at that close. The fill-timing
      checks below the table price the honest end of the range.`,
    sv: r => r.book && r.book.sharpe,
    bsv: (b, sh) => sh.book_bench && sh.book_bench.sharpe },
  { h: "Max DD", cell: (r, sh) => bookDdCell(r, sh && sh.book_bench && sh.book_bench.dd),
    bh: (b, sh) => `<td class="flat">${sh.book_bench && sh.book_bench.dd != null
      ? fmtNum(sh.book_bench.dd, 1) + "%" : "—"}</td>`,
    doc: ({ sh }) => `The worst peak-to-trough fall of the book — one account holding every
      name at once — against the same universe held passively${
        sh.book_bench && sh.book_bench.dd != null
          ? `, which fell <b>${fmtNum(sh.book_bench.dd, 1)}%</b> over these bars` : ""}.
      Not the drawdown of a typical single name, which is roughly twice as deep: names
      fall on different days, so a book of them falls far less than any of its parts.`,
    sv: r => r.book && r.book.dd, bsv: (b, sh) => sh.book_bench && sh.book_bench.dd },
  { h: "t", cell: r => bookNum(r, r.book && r.book.t, v => fmtSigned(v, 2)),
    doc: ({ sh }) => `The t-statistic of the per-fold edge — the mean fold-to-fold
      advantage over its own scatter, across ${sh.rows[0] && sh.rows[0].book
        && sh.rows[0].book.n_folds ? `<b>${sh.rows[0].book.n_folds}</b>` : "this sheet's"}
      walk-forward folds. Across <b>time</b> and never across assets: a book is every name
      at once and cannot borrow significance from breadth.
      <br><br><b>An em-dash means too few folds to compute it</b>, which is most of the
      intraday sheets — they cover about six years, and a 3-year in-sample window with a
      1-year step leaves four folds. That is a statement that the sheet cannot answer the
      question, not that the answer was no. The money columns are unaffected: what the
      account did is a measurement and needs no power.`,
    sv: r => r.book && r.book.t },
  { h: "vs random", cell: r => bookNum(r, r.book && r.book.vs_random, fmtIR),
    doc: `Sharpe above an exposure-matched <b>coin flip</b> — a book that goes in and out
      of the market at random, at this rule's own rate, backtested rather than modelled.
      <br><br>It is the control that matters most on this board. On the ETF, commodity and
      futures daily sheets a random rule beat the risk-matched benchmark by more than any
      converted strategy did, so a small positive in the money columns is being earned by
      <i>being out of the market some of the time</i> and not by the signal. A rule has to
      clear the coin flip before its excess means anything.
      <br><br><b>An em-dash means the run that wrote this sheet had no random books in its
      panel.</b> <code>vs_random</code> is a second pass over the <code>RANDOM_*</code>
      rows, interpolated at each rule's own exposure, so a run scoped to a rule list cannot
      compute it and the cell is left blank rather than filled with a zero. The daily
      crypto and ETF sheets are in that position; the minute sheets are not.`,
    sv: r => r.book && r.book.vs_random },
  { h: "Trades/asset", cell: r => tradesCell(r.book && {
      trades: r.book.trades_per_asset }),
    doc: `Positions opened on a typical name, out-of-sample. Not good or bad on its own —
      it is what makes the profit factor beside it readable, and on the minute sheets it is
      what makes the cost column readable too.`,
    sv: r => r.book && r.book.trades_per_asset },
  { h: "Profit factor", cell: r => pfCell(r.book, bookExposure(r)),
    doc: `Gross winnings ÷ gross losses per closed trade; 1.00 is break-even. Scored
      against 1.00 and not against the benchmark, which never closes a trade and so has
      none. Greyed above 90% exposure, where the rule barely closes anything.`,
    sv: r => r.book && r.book.profit_factor },
  { h: "$10k / book", lead: true, cell: r => bookWealthCell(r.book),
    bh: (b, sh) => `<td class="flat">${fmtMoney(sh.book_bench && sh.book_bench.wealth)}</td>`,
    doc: ({ sh }) => {
      const bb = sh.book_bench;
      if (!bb) return `No book run covers this sheet.`;
      return `What $10,000 became in <b>one account holding the whole universe</b> —
        ${bb.n_names} names, equal-weighted, rebalanced every bar — over
        <b>${fmtNum(bb.years, 1)} years</b> (${esc(bb.start)} to ${esc(bb.end)}). Holding
        the same universe passively returned <b>${fmtMoney(bb.wealth)}</b>${bb.index_wealth
          ? `, against ${fmtMoney(bb.index_wealth)} for ${esc(bb.index_symbol)} over the
        same bars` : ""}.
        <br><br><b>Coloured on raw money — did the account end with more than holding.</b>
        The next column is coloured on the risk-matched comparison, and rows routinely have
        one without the other: a rule invested half the time can clear holding per unit of
        risk and still finish with far less money, because it was only ever exposed to half
        the market. Read them together.`;
    },
    sv: r => r.book && r.book.wealth, bsv: (b, sh) => sh.book_bench && sh.book_bench.wealth },
  { h: "book vs B&amp;H", lead: true,
    cell: r => numCell(r.book, r.book && r.book.cm_excess_cagr != null
      ? r.book.cm_excess_cagr * 100 : null, v => fmtPct(v, 2)),
    bh: () => `<td class="flat">+0.00%</td>`,
    doc: `<b>The tiebreak this table is ordered by</b>, inside each tier of the Standard
      column. Annual return of the book above the same universe held passively, after the
      passive side has been scaled <i>down</i> with cash to the rule's own volatility —
      never levered up, so no margin and no borrow. A rule that beats holding only by
      taking more risk scores +0.00% here, which is the honest answer.
      <br><br>On the minute sheets this column is dominated by cost rather than by signal.
      The median annual cost of running one of these rules is about 6% of the account at
      5-minute bars and 29% at 1-minute bars on US stocks — and on 1-minute crypto, where
      the fee is per trade and the rules trade every bar, it is <b>2,725%</b>. At that point
      the account is a rounding error and the sign of the signal stops mattering.`,
    sv: r => r.book && r.book.cm_excess_cagr, bsv: () => 0 },
  { h: "fees", cell: r => bookNum(r, r.book && r.book.headroom, v => fmtNum(v, 1) + "x"),
    doc: `Cost headroom: how many times the modelled commission and spread could rise
      before the book stops beating the same basket held at its own volatility.
      <b>0.0x means it already does not</b>, at the real cost.
      <br><br>Exact arithmetic rather than seven re-runs — every cost term is linear in its
      own rate, so the account at 5x the schedule is its zero-cost book minus five times
      its measured drag. The <i>Trading venue</i> check under this table is the same
      question asked with real exchange schedules instead of a multiplier, and it moves two
      of the three crypto results from a win to a loss.`,
    sv: r => r.book && r.book.headroom },
  /* The `Standard` column is deliberately absent, and its absence is why this board ranks
   * on money instead. Dropped on request, and the request was right: almost every sheet
   * here is UNDERPOWERED -- 6 folds on crypto daily and about 4 on the minute sheets,
   * against the 20 the thresholds were calibrated on -- so the six-criteria count was
   * mostly reporting how much history a class has, and it ordered a rule earning
   * +30 pp/yr BELOW one earning +9 for clearing a gate neither had the evidence to claim.
   * The verdict is still computed by `portfolio_wf._standard` and still on every row of
   * the payload; it is simply not rendered and not sorted on. It remains the primary key
   * on the house board next door, whose sheets are long enough for it to mean something. */
];

let convSort = null;

/* Same shape as `lbOrder`, on this board's columns. The benchmark is spliced in at the
 * rank its own count earns, which under the Standard key is below every rule that cleared
 * a criterion — and it carries the same `not ranked` chip, for the same reason: last place
 * on a leaderboard reads as "worst", and on this repo the whole finding is the opposite. */
function convOrder(sh, bench, sort) {
  const rows = sh.rows.map(row => ({ row }));
  if (!sort) {
    /* Spliced in at the rank its own figure earns, and that figure has to be the column
     * the table is sorted by. Buy-and-hold's risk-matched excess over itself is zero by
     * construction, so it lands above every rule that lost to it and below every rule
     * that beat it -- which is the line this whole board is about, drawn in the ranking
     * rather than left for the reader to find. */
    const below = sh.rows.findIndex(r => r.book && r.book.cm_excess_cagr != null
      && r.book.cm_excess_cagr < 0);
    if (bench) rows.splice(below < 0 ? rows.length : below, 0, { bench: true });
    return rows;
  }
  const c = CONV_COLS[sort.i];
  if (bench) rows.push({ bench: true });
  const val = e => {
    const v = e.bench ? (c.bsv ? c.bsv(bench, sh) : null) : c.sv(e.row);
    return v == null || (typeof v === "number" && !isFinite(v)) ? null : v;
  };
  return rows.map((e, i) => ({ e, i, v: val(e) }))
    .sort((a, b) => (a.v == null) - (b.v == null)
      || (a.v == null ? a.i - b.i
        : (typeof a.v === "string" ? sort.dir * a.v.localeCompare(b.v)
                                   : sort.dir * (a.v - b.v)) || a.i - b.i))
    .map(x => x.e);
}

const convBenchRow = (bench, cols, sh) => {
  if (bench == null) return "";
  const cells = cols.map((c, i) => i === 0
    ? `<td class="l">Buy &amp; hold <span class="chip mut" title="The same universe held passively, scaled down with cash to each rule's own volatility. Everything above this line beat it at equal risk; everything below lost to it.">benchmark</span></td>`
    : c.bh ? c.bh(bench, sh) : `<td class="flat">—</td>`).join("");
  return `<tr class="bench-row">${cells}</tr>`;
};

const convCols = () => {
  if (!NARROW.matches) return CONV_COLS;
  const [name, ...rest] = CONV_COLS;
  return [name, ...rest.filter(c => c.lead), ...rest.filter(c => !c.lead)];
};

/* One sensitivity, as its own small table: the same rules re-run with one assumption
 * changed. Deliberately NOT merged into the ranking — a rule at Coinbase fees and the same
 * rule at Binance fees is one strategy with two prices, not two candidates, and putting
 * both on the leaderboard would let a rule occupy two rows for having been priced twice. */
const convCheckTable = c => `
  <div class="conv-check">
    <h3>${esc(c.title)}</h3>
    <p class="mut">${esc(c.note)}</p>
    <div class="tbl-wrap"><table>
      <thead><tr><th class="l">Strategy</th>${c.cols.map(h =>
        `<th>${esc(h)}</th>`).join("")}</tr></thead>
      <tbody>${c.rows.map(r => `<tr>
        <td class="l">${esc(r.base)}${r.short
          ? ` <span class="chip warn">reverses</span>` : ""}${r.short_off
          ? ` <span class="chip mut">short off</span>` : ""}${r.ha
          ? ` <span class="chip mut">Heikin-Ashi</span>` : ""}</td>
        ${r.cells.map(v => v == null || v.excess == null
          ? `<td class="flat">—</td>`
          : `<td class="${sign(v.excess)}" title="${esc(
              v.t == null ? "no t on this run" : `t = ${fmtSigned(v.t, 2)}`)}">${
              fmtPct(v.excess * 100, 2)}${v.t == null ? ""
                : ` <span class="mut">t ${fmtSigned(v.t, 2)}</span>`}</td>`).join("")}
      </tr>`).join("")}</tbody>
    </table></div>
  </div>`;

/* What choosing a rule cost, which is the one thing a leaderboard structurally cannot show.
 * Every row above is the view from the end. This is the same family re-scored so the rule
 * is re-picked on each in-sample window and traded through the next — the only version of
 * these results a person could have actually held. */
const convSelectionPanel = s => {
  if (!s) return "";
  const picks = (s.picks || []).map(p => `<tr>
      <td class="l">${esc(String(p.fold).slice(0, 7))}</td>
      <td class="l">${esc(p.rule)}</td>
      <td class="${sign(p.is_excess)}">${p.is_excess == null ? "—"
        : fmtPct(p.is_excess * 100, 1)}</td></tr>`).join("");
  return `
  <section class="sec">
    <div class="sec-head"><h2>What choosing cost</h2>
      <span class="sec-note">${s.n_candidates} candidates · ${s.n_folds} folds ·
        ${s.n_switches} switch${s.n_switches === 1 ? "" : "es"}</span></div>
    <p class="mut">Every row on the leaderboard is the view from the end: it names the rule
      that turned out best. This is the same family scored the only way a person could have
      traded it — re-pick the leader on each three-year in-sample window, hold it through
      the next year, repeat. The gap between the two is what hindsight was worth.</p>
    <div class="strip">
      <div class="stat"><span class="k">Best rule, chosen after the fact</span>
        <span class="v gain">${fmtPct((s.best_fixed_excess || 0) * 100, 1)}</span>
        <span class="s">${esc(s.best_fixed)} · per year at equal risk</span></div>
      <div class="stat"><span class="k">Choosing it as you went</span>
        <span class="v ${sign(s.is1_excess)}">${fmtPct((s.is1_excess || 0) * 100, 1)}</span>
        <span class="s">t = ${fmtSigned(s.is1_t, 2)} — not significant</span></div>
      <div class="stat"><span class="k">Cost of hindsight</span>
        <span class="v loss">${fmtPct((s.selection_cost || 0) * 100, 1)}</span>
        <span class="s">per year, and it is most of the result</span></div>
      <div class="stat"><span class="k">Verdict</span>
        <span class="v">${esc(s.verdict || "—")}</span>
        <span class="s">${s.n_candidates} candidates over ${s.n_folds} folds cannot be
          separated</span></div>
    </div>
    ${picks ? `<div class="tbl-wrap"><table>
      <thead><tr><th class="l">Window ending</th><th class="l">Rule it picked</th>
        <th>Its in-sample edge</th></tr></thead>
      <tbody>${picks}</tbody></table></div>
      <p class="mut">The edge the leader showed shrinks every window, and by the last one
        the selection reaches for a random control — which is what a rule that fitted a
        period rather than found an edge looks like from the inside.</p>` : ""}
  </section>`;
};

const convRosterSection = () => {
  const roster = CONV().roster || [];
  if (!roster.length) return "";
  return `
  <section class="sec">
    <div class="sec-head"><h2>The thirteen</h2>
      <span class="sec-note">as supplied, 2026-08-18</span></div>
    <div class="tbl-wrap"><table>
      <thead><tr><th class="l">Strategy</th><th class="l">Came from</th>
        <th class="l">Short side</th><th class="l">What it does</th></tr></thead>
      <tbody>${roster.map(r => `<tr>
        <td class="l">${esc(r.name)}</td>
        <td class="l mut">${esc(r.origin)}</td>
        <td class="l mut">${r.reverses ? "reverses — scored both ways"
          : "none — sells to cash"}</td>
        <td class="l">${esc(r.blurb)}</td></tr>`).join("")}</tbody>
    </table></div>
    <p class="mut">Five more files arrived in the same archive and are deliberately not
      here. Two are 1-minute ES and NQ scripts whose entry sizes are index points rather
      than percentages and which need an intraday session clock and tick-filled trailing
      stops. One is gated on a 12-minute closing price requested with look-ahead switched
      on, so every trade in it is conditioned on a price that had not printed — removing
      the leak yields a different strategy nobody has tested. Two are freqtrade bots with
      no signal exit at all, whose every exit comes from a hundred-branch custom rule
      reading tick-level trade state. A plausible rewrite of any of them would put a number
      on this board under a name that did not earn it.</p>
  </section>`;
};

function paintConversions() {
  const head = document.getElementById("bt-head");
  const host = document.getElementById("bt-body");
  const c = CONV();
  const g = convGroup(), sh = convSheetOf(bf.cls, bf.tf);
  const t = c.totals || {};
  const bb = sh && sh.book_bench;
  /* Every tile is about THIS sheet, with the board-wide figure demoted to the sub-line.
   * The other way round — five totals that never move as you click through twenty-one
   * sheets — reads as a banner rather than as a header, and a header that does not respond
   * to the filters above the table trains a reader to stop looking at it. */
  head.innerHTML = `
  <div class="strip">
    <div class="stat"><span class="k">Strategies</span>
      <span class="v">${t.strategies || 0}</span>
      <span class="s">supplied as Pine, freqtrade and notebooks</span></div>
    <div class="stat"><span class="k">Tests on this sheet</span>
      <span class="v">${sh ? sh.n_rules : "—"}</span>
      <span class="s">of ${t.cells || 0} across ${t.sheets || 0} sheets</span></div>
    ${bb ? `<div class="stat"><span class="k">Universe</span>
      <span class="v">${bb.n_names}</span>
      <span class="s">names as one account · ${fmtNum(bb.years, 1)}y out-of-sample${
        bb.start ? ` · ${esc(bb.start)} to ${esc(bb.end)}` : ""}</span></div>` : ""}
    <div class="stat"><span class="k">Beat the benchmark</span>
      <span class="v ${sh && sh.n_beat ? "gain" : ""}">${sh ? sh.n_beat : "—"}</span>
      <span class="s">risk-matched · ${t.beat || 0} of ${t.cells || 0} board-wide, ${
        t.strong || 0} of those convincingly</span></div>
    ${bb ? `<div class="stat"><span class="k">$10k held</span>
      <span class="v">${fmtMoney(bb.wealth)}</span>
      <span class="s">the bar every row is measured against${bb.index_wealth
        ? ` · ${esc(bb.index_symbol)} ${fmtMoney(bb.index_wealth)}` : ""}</span></div>` : ""}
    ${sh ? `<div class="stat"><span class="k">Charts</span>
      <span class="v">${sh.rows.filter(r => r.curve).length}</span>
      <span class="s">${sh.rows.filter(r => r.curve).length === sh.rows.length
        ? "every row opens" : `of ${sh.rows.length} rows · the rest are being re-scored`}</span></div>` : ""}
  </div>`;

  if (!sh) {
    host.innerHTML = `<div class="note">No converted-strategy sheet for
      <b>${esc(CLASS_LABEL[bf.cls] || bf.cls)}</b> at ${esc(bf.tf)}.${
      bf.cls === "futures" ? ` The futures class is <b>daily only</b> — the vendor's
      hourly CME archive collapses whole sessions before 2013, so there are no intraday
      bars to run these on.` : ""}</div>${convRosterSection()}`;
    return;
  }

  const bench = sh.book_bench, cols = convCols();
  host.innerHTML = `
  <section class="sec">
    <div class="sec-head"><h2>Converted strategies</h2>
      <span class="sec-note" id="lb-note"></span></div>
    <div id="lb-doc" class="coldoc" hidden></div>
    <div class="tbl-wrap"><table>
      <thead><tr>${cols.map(c2 =>
        `<th${c2.l ? ' class="l"' : ""}${c2.doc || c2.sv
          ? ` data-doc="${CONV_COLS.indexOf(c2)}"` : ""}>${c2.h}</th>`).join("")}</tr></thead>
      <tbody id="lb-body"></tbody>
    </table></div>
  </section>

  ${(sh.checks || []).length ? `
  <section class="sec">
    <div class="sec-head"><h2>Does it survive the assumption?</h2>
      <span class="sec-note">same rules, one thing changed</span></div>
    ${sh.checks.map(convCheckTable).join("")}
  </section>` : ""}

  ${convSelectionPanel(sh.selection)}

  ${convRosterSection()}`;

  const paintRows = () => {
    const body = host.querySelector("#lb-body");
    body.innerHTML = convOrder(sh, bench, convSort).map(e => e.bench
      ? convBenchRow(bench, cols, sh)
      : `<tr data-go="#/backtest/conv/${bf.cls}/${sh.timeframe}/${
          slug(e.row.key || e.row.rule)}">${
          cols.map(c2 => c2.cell(e.row, sh)).join("")}</tr>`).join("");
    host.querySelectorAll("th[data-doc]").forEach(th => {
      const on = convSort && convSort.i === Number(th.dataset.doc);
      th.classList.toggle("sort-desc", !!on && convSort.dir < 0);
      th.classList.toggle("sort-asc", !!on && convSort.dir > 0);
    });
    const by = convSort ? CONV_COLS[convSort.i].h : null;
    /* Same caption contract as the house board: name the basis rather than assume it, and
     * say plainly when a click has re-ordered the rows the ranking already selected rather
     * than fetched the best rows by that column. */
    host.querySelector("#lb-note").innerHTML =
      `${sh.rows.length === sh.n_rules ? `all ${sh.n_rules}` :
        `top ${sh.rows.length} of ${sh.n_rules}`} cells · ${sh.n_beat} beat buy &amp; hold
       at equal risk · ${by
        ? `picked on book vs B&amp;H, re-ordered by ${by} — <b>not</b> the best ${
           sh.rows.length} by ${by}`
        : "ranked on book vs B&amp;H, risk-matched"} · ${
        bench ? `${bench.n_names} names, ${fmtNum(bench.years, 1)}y` : ""} · tap a row for
       its detail${sh.rows.some(r => !r.curve)
        ? ` · ${sh.rows.filter(r => !r.curve).length} of them have no chart yet`
        : ""}`;
  };
  paintRows();
  bindGo(host);
  bindColHeaders(host, { sh, grp: g, bench: bench && bench.wealth }, i => {
    const first = CONV_COLS[i].text ? 1 : -1;
    convSort = !convSort || convSort.i !== i ? { i, dir: first }
      : convSort.dir === first ? { i, dir: -first } : null;
    paintRows();
  }, CONV_COLS);
}

/* A column explains itself when it is dwelt on, and sorts the ranking on click. Two
 * behaviours on one target, but they answer the two things a reader does with a header they
 * do not recognise — ask what it is, then ask who wins on it.
 *
 * The pause in front of the explanation is the load-bearing part. A popover that opens the
 * instant the cursor crosses a header fires on the way to a different one, so reading down
 * a sixteen-column table sets off a flicker of panels nobody asked for. Three seconds is
 * long enough that appearing means it was wanted.
 *
 * A phone has no hover at all, so the same explanation is on press-and-hold — and the tap
 * that ends it is swallowed, because a long press is not a click that took a while. */
const DOC_DWELL_MS = 3000;   // pointer: how long a header must be held under the cursor
const DOC_HOLD_MS = 500;     // touch: how long a finger must stay down

function bindColHeaders(host, ctx, onSort, cols = LB_COLS) {
  const panel = host.querySelector("#lb-doc");
  const sec = panel.closest(".sec");
  const show = th => {
    const c = cols[Number(th.dataset.doc)];
    if (!c.doc) return;
    panel.innerHTML = `<div class="coldoc-h">${c.h}</div>
      <p>${typeof c.doc === "function" ? c.doc(ctx) : c.doc}</p>`;
    panel.hidden = false;
    // Hung under the header it explains and clamped to the table's own box, which on a wide
    // screen is wider than the section the panel lives in. Measured after it is visible,
    // because a hidden element has no width to clamp against.
    const sr = sec.getBoundingClientRect();
    const tr = th.getBoundingClientRect();
    const box = (sec.querySelector(".tbl-scroll") || sec).getBoundingClientRect();
    // Width is set rather than left to the layout: an absolutely positioned box
    // shrink-to-fits the space between its `left` and the section's right edge, so the
    // explanation of the last column would come out half the width of the first one's.
    const w = Math.min(520, box.width);
    panel.style.width = `${w}px`;
    panel.style.top = `${tr.bottom - sr.top + 8}px`;
    panel.style.left = `${Math.max(box.left - sr.left,
      Math.min(tr.left - sr.left, box.right - sr.left - w))}px`;
  };
  let timer = null, held = false;
  const cancel = () => { clearTimeout(timer); timer = null; };
  const hide = () => { cancel(); panel.hidden = true; };
  const dwell = th => { cancel(); timer = setTimeout(() => show(th), DOC_DWELL_MS); };

  host.querySelectorAll("th[data-doc]").forEach(th => {
    th.onpointerenter = e => { if (e.pointerType === "mouse") dwell(th); };
    th.onpointerleave = e => { if (e.pointerType === "mouse") hide(); };
    th.onpointerdown = e => {
      if (e.pointerType === "mouse") return;
      held = false;
      cancel();
      timer = setTimeout(() => { held = true; show(th); }, DOC_HOLD_MS);
    };
    // A finger that lifts, or slides off, before the hold is up wanted the sort.
    th.onpointerup = th.onpointercancel = e => { if (e.pointerType !== "mouse") cancel(); };
    th.onclick = () => {
      if (held) { held = false; return; }
      onSort(Number(th.dataset.doc));
      // The cursor has not gone anywhere, so the dwell starts again rather than waiting for
      // the reader to leave and come back before the column will explain itself.
      dwell(th);
    };
  });

  // A press anywhere else puts the explanation away. On a phone there is no pointerleave to
  // do it, so the panel would otherwise sit over the ranking until the next long press.
  host.addEventListener("pointerdown", e => {
    if (!e.target.closest("th[data-doc]")) hide();
  }, true);
}

/* ---------- curve loading ----------
 * Fetched per sheet on first use and kept, so switching between rules on one sheet does
 * not re-download 1.5 MB each time. A failure is reported rather than swallowed: an empty
 * chart area with no explanation reads as "this rule has no data", which is a different
 * and wrong claim. */
const curveCache = {};
async function loadCurves(key) {
  if (key in curveCache) return curveCache[key];
  const entry = D.curves && D.curves[key];
  if (!entry) return (curveCache[key] = null);
  try {
    const res = await fetch(entry.file, { cache: "no-cache" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return (curveCache[key] = await res.json());
  } catch (e) {
    curveCache[key] = { __error: String(e && e.message || e) };
    return curveCache[key];
  }
}

const METRIC_ROWS = [
  ["sharpe", "Sharpe", "Return per unit of total volatility. Above 1 is good; it says nothing about beating the benchmark.", 2],
  ["sortino", "Sortino", "Sharpe counting only downside volatility, so upside swings are not penalised.", 2],
  ["calmar", "Calmar", "Annual return divided by the worst peak-to-trough fall. How much pain each unit of return cost.", 2],
  ["max_dd_pct", "Max drawdown", "Worst fall from a high-water mark. The number that decides whether a strategy is actually holdable.", 1, "%"],
  ["vol_pct", "Volatility", "Annualised standard deviation of returns.", 1, "%"],
  ["cagr_pct", "CAGR", "Compounded annual growth.", 2, "%"],
  ["profit_factor", "Profit factor", "Gross winnings ÷ gross losses across trades. Above 1 means the wins outweigh the losses.", 2],
  ["win_rate_pct", "Win rate", "Share of trades that closed positive. A low win rate is fine if the wins are large.", 1, "%"],
  ["trades", "Trades", "A trade is one held position, entry to exit.", 0],
  ["avg_win_pct", "Average win", "Mean return of a winning trade.", 2, "%"],
  ["avg_loss_pct", "Average loss", "Mean return of a losing trade.", 2, "%"],
  ["exposure_pct", "Time in market", "Share of bars holding any position. Read this before any return figure.", 1, "%"],
];

const mval = (m, key, dp, suffix) => {
  const v = m && m[key];
  if (v == null) return "—";
  return Number(v).toFixed(dp) + (suffix || "");
};

/* Which strategy the stored curve is. The book stage builds long/flat only, so this is
 * "long" for everything it writes; the key is kept because the file carries it and a
 * long/short book is a plausible thing to add later. */
const curveSide = c => (c && c.side) || "long";

/* The comparison instruments stored with a curve, as a list, whatever shape the file is in.
 *
 * `portfolio_wf.py` emits `indexes: [{symbol, curve, metrics}, ...]` — benchmark first,
 * then any extra (QQQ beside SPY on the US stock sheets). Files written by the old
 * `curves.py` carry a single `index` / `index_metrics` / `index_symbol` trio, and reading
 * both costs two lines. Absent means no index is cached for the class, which is a real
 * state — crypto and commodities can hit it. */
const curveIndexes = c => Array.isArray(c && c.indexes) ? c.indexes
  : (c && c.index ? [{ symbol: c.index_symbol, curve: c.index, metrics: c.index_metrics }]
                  : []);

/* Two is what `equityChart` has distinct strokes for, and three lines is already the most a
 * reader can follow on a log scale at this width. Extras are dropped from the CHART only —
 * the metrics table below it still carries every one of them as a column. */
const CHART_INDEXES = 2;

/* `r` is the leaderboard row, and it is here only to be CONTRADICTED in print.
 *
 * This table and the strip at the top of the page report the same-named quantities off
 * two different measurements, and until they said so the page simply looked wrong: `ibs`
 * on us_stocks 1d carries Sharpe 1.251 and a -23.0% drawdown here while its own row says
 * 0.552 and -50.4%. Neither is a mistake. This section is the EQUAL-WEIGHT BOOK — one
 * portfolio holding every name at once, which is what `curves.py` builds — and the row is
 * the MEDIAN ASSET, one of N independent single-name backtests at risk-matched size.
 *
 * The gap is diversification and it is the whole argument of `portfolio_wf.py`: idiosyncratic
 * noise cancels in a book and a median cannot see it, so a median across per-symbol
 * backtests describes a thing nobody owns and is biased low. Printing both without naming
 * either is what made them look like a bug.
 */
function metricsSection(m, matched, r) {
  if (!m) return "";
  /* One column per benchmark, each HELD AT THE STRATEGY'S OWN VOLATILITY — the same
   * sizing as the chart above, because a table on a different basis from the picture over
   * it is the mistake this page spent a day removing. The basket is one of these columns
   * now rather than a special case: at equal risk it is just another thing you could have
   * held instead.
   *
   * What matching does and does not move is worth knowing while reading this: with idle
   * cash earning nothing, scaling a benchmark by `w` scales its mean and its standard
   * deviation together, so **Sharpe and Sortino are unchanged** from the full-size
   * instrument. Volatility, drawdown, CAGR and terminal wealth all move, and volatility
   * lands on the strategy's own by construction — that is the point, not a coincidence. */
  const ix = (matched || []).filter(i => i && i.metrics);
  const names = ix.map(i => `${esc(i.label)}${i.weight == null ? ""
    : ` <span class="mut">${fmtNum(i.weight * 100, 0)}%</span>`}`);
  return `
  <section class="sec">
    <div class="sec-head"><h2>Performance metrics</h2>
      <span class="sec-note">the book against every benchmark <b>at its own volatility</b>,
        same window</span></div>
    ${/* Gated on the per-asset ROWS, not on `asset_n`: a pair carries the count (it is the
        * sheet's universe size) but ships no per-symbol table, so keying on `asset_n` would
        * point a reader at an "Asset by asset" section that is not on the page. */""}
    ${r && r.asset_n && r.per_asset && r.per_asset.length
      ? `<div class="note">One portfolio holding all ${r.asset_n} names
    at once, at 1&times; size. These are the same numbers as the strip at the top of this
    page and the same as the row on the leaderboard &mdash; one book, measured once. The
    per-name breakdown is the <em>Asset by asset</em> table below, and those are ${r.asset_n}
    separate single-name backtests: they do not add up to this, because a book
    diversifies and a list of backtests cannot.</div>` : ""}
    <div class="tbl-wrap"><table>
      <thead><tr><th class="l">Metric</th><th>Strategy</th>${
        names.map(n => `<th>${n}</th>`).join("")}
        <th class="l">What it means</th></tr></thead>
      <tbody>${METRIC_ROWS.map(([k, name, help, dp, sfx]) => `
        <tr><td class="l">${name}</td>
          <td>${mval(m, k, dp, sfx)}</td>${
          ix.map(i => `<td>${k in i.metrics ? mval(i.metrics, k, dp, sfx) : "—"}</td>`).join("")}
          <td class="l" style="white-space:normal;color:var(--muted);font-size:12.5px">${help}</td>
        </tr>`).join("")}</tbody>
      <caption>Every benchmark column is that instrument <b>held at the strategy's own
      volatility</b>, the rest in cash — so the volatility row is the same across the table
      by construction, and drawdown, CAGR and total return are directly comparable. Scaled
      down, never levered up.
      ${ix.length ? `<br><br><b>Sharpe and Sortino are unchanged by the matching</b> and
      are the full-size instrument's own: with idle cash earning nothing, holding less of
      something divides its return and its volatility by the same number. If a benchmark's
      Sharpe beats the strategy's here, it beat it at any size.` : ""}
      <br><br>Trade-level statistics (profit factor, win rate, average win and loss) have no
      buy-and-hold counterpart — holding is a single trade that is still open, so its win rate
      is either 100% or 0% and its profit factor has no denominator. None of these replace
      the verdict: a strategy can carry a better Sharpe than every column here and still fail
      the standard, which is exactly what the best rows on this sheet do.</caption>
    </table></div>
  </section>`;
}

/* ---------- what the strategy actually does ----------
 *
 * The single most common complaint about this page was that it named a strategy and then
 * showed fifteen numbers about it without ever saying what it did. `payload.strategy_logic`
 * ships prose for all 262 names — hand-written for the published catalogue, derived from
 * the indicator family for the TA-Lib rules.
 *
 * A pair has no entry of its own and never will: `MAXINDEX~MININDEX|or` is not a strategy
 * anyone wrote down, it is two rules and an operator. So it resolves each leg separately
 * and explains the operator, which is the only honest reading of that row. */
const OP_PROSE = {
  and:  "Long only when BOTH legs are long — the strictest operator, and the one that spends the least time invested.",
  or:   "Long when EITHER leg is long. This is the operator that spends the MOST time invested, which on a rising benchmark is most of why `or` rows top equity leaderboards.",
  vote: "Long on the majority of the two legs, ties resolved to flat.",
  gate: "The first leg decides direction; the second may only veto it.",
};

function legsOf(rule) { return String(rule).split("~").map(s => s.split("|")[0].trim()); }

function renderLogic(text) {
  /* The blocks are "Heading\n    indented body". A heading is a short unindented line;
   * anything else is body. Guessing wrong only costs a bold, never content. */
  const lines = String(text).replace(/\r/g, "").split("\n");
  let html = "", open = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    const isHead = !/^\s/.test(raw) && line.length < 60 && !/[.:]$/.test(line);
    if (isHead) {
      if (open) html += "</p>";
      html += `<h4 class="logic-h">${esc(line)}</h4><p class="logic-p">`;
      open = true;
    } else {
      html += (open ? " " : "<p class=\"logic-p\">") + esc(line);
      open = true;
    }
  }
  return html + (open ? "</p>" : "");
}

function logicSection(r) {
  const L = D.logic || {};
  const direct = L[r.rule] || L[stemName(r.rule)];
  if (direct) {
    const prov = [direct.source, direct.family].filter(Boolean).join(" · ");
    return `<section class="sec"><div class="sec-head"><h2>How it works</h2>
      ${prov ? `<span class="sec-note">${esc(prov)}</span>` : ""}</div>
      <div class="logic">${renderLogic(direct.logic || "")}
      ${direct.note ? `<p class="logic-p mut"><b>Note.</b> ${esc(direct.note)}</p>` : ""}</div>
    </section>`;
  }
  if (r.kind !== "pair") return "";
  const op = (r.op || "").toLowerCase();
  const legs = legsOf(r.rule).map(n => ({ name: n, e: L[n] })).filter(x => x.e);
  if (!legs.length) return "";
  return `<section class="sec"><div class="sec-head"><h2>How it works</h2>
    <span class="sec-note">two rules joined by <code>${esc(op)}</code></span></div>
    <div class="logic">
      ${OP_PROSE[op] ? `<p class="logic-p"><b>The operator.</b> ${esc(OP_PROSE[op])}</p>` : ""}
      ${legs.map(l => `<h4 class="logic-h">${esc(l.name)}</h4>
         <div class="logic-leg">${renderLogic(l.e.logic || "")}</div>`).join("")}
    </div></section>`;
}

/* The off-side note is gone (2026-08-13), with the measurement it explained.
 *
 * The page used to print `edge_standard`'s verdict — which picks whichever of long/flat and
 * long/short scored better on a typical name — beside diagnostics that are long/flat
 * always, so on a short-endorsed rule one row was half one strategy and half another.
 * Now every figure on this page, the verdict included, is the long/flat BOOK, so there is
 * no second side in play and nothing to disclaim. `payload._drop_offside_diagnostics` still
 * blanks the per-asset columns it always did; nothing rendered reads them.
 */
/* ---------------------------------------------------------------- the paper switch
 *
 * DESIGN PREVIEW. It renders the real shape and the real numbers and is deliberately not
 * connected to anything: clicking it says so. The point is to agree the wording and the
 * placement before the desk starts moving a hundred thousand dollars on a click.
 *
 * The name counts are constants here and will come from `catalog.json` once this is
 * wired — the desk publishes its universe there already. They are the DESK's rosters, not
 * the research universe: `us_stocks` is the live top 100, where the research scored 216
 * names over their whole history. */
/* Filled from `catalog.json` and `/v1/house/strategies` on load. Null until then, which
 * is why the switch renders nothing rather than a guess: a name count on the face of this
 * control is a promise about what a click will do, and a stale one is a lie. */
let BOOK = null;          // {capital, timeframes, names:{cls:n}, benchmark:{cls:sym}}
let PROMOTED = null;      // {"<cls>-<tf>-<rule>": registration}

async function loadBookState() {
  try {
    const [cat, mine] = await Promise.all([
      fetch("/catalog.json", { cache: "no-store", credentials: "same-origin" }),
      fetch("/v1/house/strategies", { cache: "no-store", credentials: "same-origin" }),
    ]);
    if (!cat.ok) return;                       // not served by the API: no switch at all
    BOOK = (await cat.json()).book || null;
    PROMOTED = {};
    if (mine.ok) for (const s of await mine.json()) PROMOTED[s.name] = s;
    render();
  } catch { /* offline build, or the one-file dist: leave the switch out */ }
}

/* The board's tab key is not the engine's class name — this page says `stocks` and `etf`
 * where the desk says `us_stocks` and `us_etfs`. Every lookup and every request has to be
 * in the DESK's spelling, and the map is published in the catalog rather than restated
 * here, because it is defined in `dash_config.GROUPS` and would drift if copied. */
const deskClass = cls => ((BOOK && BOOK.class_map) || {})[cls] || cls;

const bookName = (cls, tf, rule) => `${deskClass(cls)}-${tf}-${rule.toLowerCase()}`;

function bookSwitch(r, cls, tf, isPair) {
  /* A pair is rebuilt from two legs by `signals.position_for_row`, which needs the
   * leaderboard row. A live strategy holds only a label, so there is nothing for it to
   * reconstruct — shown and explained rather than hidden, because the whole top of the
   * crypto 1d board is pairs. */
  if (isPair) {
    return `<div class="promote blocked">
      <div class="promote-top"><span class="promote-label">Not tradable live</span>
        <span class="sw off"></span></div>
      <div class="promote-sub">A pair is rebuilt from two legs and has no single
        definition a live strategy can hold.</div>
      <div class="promote-state bad">unavailable</div></div>`;
  }
  if (!BOOK) return "";                       // not behind the API, or not loaded yet
  if (!(BOOK.timeframes || ["1d"]).includes(tf)) {
    return `<div class="promote blocked">
      <div class="promote-top"><span class="promote-label">1d only, for now</span>
        <span class="sw off"></span></div>
      <div class="promote-sub">The desk runs daily books first. 4h follows once the
        daily one has been watched.</div>
      <div class="promote-state">unavailable</div></div>`;
  }

  const rule = stemName(r.rule);
  const reg = (PROMOTED || {})[bookName(cls, tf, rule)];
  const n = (BOOK.names || {})[deskClass(cls)] || 0;
  const slice = n ? BOOK.capital / n : 0;
  /* No names means the desk cannot hold this class — a book of nothing. Say so instead of
   * offering a switch that would be refused. */
  if (!n) {
    return `<div class="promote blocked">
      <div class="promote-top"><span class="promote-label">Not on the desk</span>
        <span class="sw off"></span></div>
      <div class="promote-sub">The desk carries no ${esc(cls)} names right now, so there
        is nothing for a book to hold.</div>
      <div class="promote-state bad">unavailable</div></div>`;
  }

  /* `want` is what was asked for and `state` is what the desk has done. They disagree for
   * a while by design — asking while the desk is down leaves want=live, state=pending —
   * and the switch shows the DESK's answer, because that is the one that is true. */
  if (reg && reg.want !== "retired" && reg.state !== "retired") {
    const live = reg.state === "live";
    const bad = reg.state === "rejected";
    const cls_ = live ? "live" : (bad ? "bad" : "wait");
    const word = bad ? "rejected"
      : live ? "live"
      : (reg.want === "paused" ? "pausing" : "queued — the desk applies it within 30s");
    return `<div class="promote ${live ? "on" : ""}" id="book-switch"
                 role="button" tabindex="0" data-off="1">
      <div class="promote-top">
        <span class="promote-label">${live ? "Paper trading" : "Starting"}</span>
        <span class="sw ${live ? "on" : "wait"}"></span></div>
      ${bookLine(reg)}
      <div class="promote-sub">${bad ? esc(reg.reason || "the desk refused it")
        : `${n} names · <a href="#/paper">see the desk →</a>`}</div>
      <div class="promote-state ${cls_}">${esc(word)}</div>
    </div>`;
  }

  const label = cls === "us_stocks"
    ? `the <b>${n}</b> stocks in the top 100 today`
    : `all <b>${n}</b> names the desk carries`;
  return `<div class="promote" id="book-switch" role="button" tabindex="0">
    <div class="promote-top"><span class="promote-label">Paper trade this</span>
      <span class="sw off"></span></div>
    <div class="promote-sub">One book of <b>${fmtMoney(BOOK.capital)}</b> running
      <code>${esc(rule)}</code> at <b>${esc(tf)}</b> across ${label} —
      <b>${fmtMoney(slice)}</b> a name. A name the rule is out of holds cash.</div>
    <div class="promote-state">off</div>
  </div>`;
}

/* "$100,000 → $104,231" once the desk has marked it. Absent before the first bar, rather
 * than showing the opening balance as if it were a result. */
function bookLine(reg) {
  // `D.strategies` is the live desk, refreshed by the `live.json` poll — so this figure
  // moves on its own without the switch asking for anything.
  const sys = (D.strategies || []).find(
    s => s.rule === reg.rule && s.cls === reg.cls && s.tf === reg.tf);
  if (!sys || sys.equity == null) return "";
  const pct = sys.paper_pnl_pct;
  return `<div class="book-line">${fmtMoney(reg.capital)} →
    <b class="${pct >= 0 ? "gain" : "loss"}">${fmtMoney(sys.equity)}</b>
    <span class="promote-sub" style="margin:0">${pct >= 0 ? "+" : ""}${fmtNum(pct, 2)}%${
      sys.held != null ? ` · ${sys.held} of ${sys.names} held` : ""}</span></div>`;
}

function bindBookSwitch() {
  const el = document.getElementById("book-switch");
  if (!el || el.dataset.busy) return;
  const m = (location.hash || "").match(/^#\/backtest\/([^/]+)\/([^/]+)\/(.+)$/);
  if (!m) return;
  const [, cls, tf, ruleSlug] = m;
  const sh = sheetOf(cls, tf);
  const row = sh && sh.rows.find(x => slug(x.rule) === ruleSlug);
  if (!row) return;
  const rule = stemName(row.rule);
  const turningOff = el.dataset.off === "1";

  /* No confirm dialog. The switch is reversible in one click, the record is kept whichever
   * way it goes, and the card already states the size and the roster before it is touched
   * — a modal repeating what is on screen is a click to dismiss, not a safeguard. What
   * replaces it is that the switch reports the DESK's answer rather than the click's, so
   * nothing reads as done until it is. */
  const go = async () => {
    el.dataset.busy = "1";
    const state = el.querySelector(".promote-state");
    state.textContent = turningOff ? "stopping…" : "starting…";
    try {
      const reg = (PROMOTED || {})[bookName(cls, tf, rule)];
      const res = turningOff
        ? await fetch(`/v1/house/strategies/${encodeURIComponent(reg.strategy_id)}`,
                      { method: "DELETE", credentials: "same-origin" })
        : await fetch("/v1/house/strategies", {
            method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ cls: deskClass(cls), tf, rule }) });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        state.textContent = body.detail || `failed (${res.status})`;
        state.classList.add("bad");
        delete el.dataset.busy;
        return;
      }
      await loadBookState();          // re-render from the desk's answer, not the click
    } catch (e) {
      state.textContent = "could not reach the desk";
      state.classList.add("bad");
      delete el.dataset.busy;
    }
  };

  el.addEventListener("click", go);
  el.addEventListener("keydown", ev => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); }
  });
}

function backtestDetail(cls, tf, ruleSlug) {
  const grp = D.backtest[cls], sh = sheetOf(cls, tf);
  const r = sh && sh.rows.find(x => slug(x.rule) === ruleSlug);
  if (!r) return (location.hash = "#/backtest");
  /* A pair has no asset-by-asset rows — `combo_wf.py` records leg-correlation diagnostics
   * instead — so breadth is the only per-asset figure it can show, and it comes off the
   * leaderboard row rather than off a table that does not exist. */
  const isPair = r.kind === "pair";
  /* Breadth comes off `asset_pos`/`asset_n`, computed over the full asset list in
   * `payload._asset_stats`, which is the same list rendered below. It is not counted here
   * because `scored` is the honest denominator — a rule with no per-asset source shows "—"
   * rather than "0 / 751", which read as a measured zero and was how `ibs` looked. */
  const nAssets = r.asset_n != null ? r.asset_n : grp.n;
  /* The strip is the BOOK now, so it reads one record. `{}` rather than a guard on every
   * line: a sheet with no book run prints em-dashes and the note under the strip does not
   * render, which is the same behaviour the money columns already have. */
  const bk = r.book || {};
  const wins = isPair ? Math.round((r.ir_hit_rate || 0) * grp.n) : r.asset_pos;
  const hasBreadth = wins != null;
  /* Two excesses, and which one the table SORTS on has to be the one its header names.
   * `xpnl` is the money gap in percentage points and `$10k became − buy & hold` is a
   * strictly increasing function of it, so sorting on either gives the same order; `xcagr`
   * is the annual rate and stays a column. Both fall back to recomputing the difference,
   * for payloads written before the keys existed. */
  const xc = p => (p.xcagr != null ? p.xcagr
    : (p.net_cagr != null && p.bh_cagr != null ? p.net_cagr - p.bh_cagr : null));
  const xp = p => (p.xpnl != null ? p.xpnl
    : (p.net_pct != null && p.bh_pct != null ? p.net_pct - p.bh_pct : null));
  const sorted = [...r.per_asset].sort((a, b) =>
    (xp(a) == null) - (xp(b) == null) || (xp(b) || 0) - (xp(a) || 0));
  /* How far apart the out-of-sample spans are, measured rather than asserted: the caption
   * warns that a money ranking is partly a ranking of holding period, and on a sheet where
   * every name ran the same length — the ETFs at 4h — that warning is simply untrue. */
  const spans = sorted.map(p => p.years).filter(v => v != null && v > 0);
  const spanRatio = spans.length ? Math.max(...spans) / Math.min(...spans) : 1;

  app.innerHTML = `
  <a class="back" href="#/backtest">← backtest</a>
  <div class="hero">
   <div class="hero-row">
    <div class="hero-left">
    <div class="d-head"><span class="d-name">${esc(stemName(r.rule))}</span>
      ${opLabel(r.op) ? `<span class="chip mut">${esc(opLabel(r.op))}</span>` : ""}
      <span class="chip mut">${tf}</span><span class="chip mut">${esc(grp.label)}</span></div>
    <p class="lede">${isPair
      ? `Two rules joined by <code>${esc(opLabel(r.op))}</code>, walked forward`
      : "Walk-forward"} out-of-sample, ${sh.folds} folds${
      bk.years ? `, held as one book of ${bk.n_names || grp.n} names over
      <b>${fmtNum(bk.years, 1)} years</b>` : ` over ${sh.years.toFixed(1)} years`}.${
      bk.years ? ` Each name is also scored on its own below, over its own
      ${sh.years.toFixed(1)}-year median spell.` : ""}</p>
    </div>
    ${bookSwitch(r, cls, tf, isPair)}
   </div>
  </div>

  <div class="strip">
    <div class="stat"><span class="k">&Delta;Sharpe</span>
      <span class="v ${sign(bk.dsharpe)}">${fmtIR(bk.dsharpe)}</span>
      <span class="s">book vs the same universe held</span></div>
    <div class="stat"><span class="k">Time invested</span>
      <span class="v">${pctOr(bk.exposure)}</span>
      <span class="s">of bars, by the book${bk.exposure != null && bk.exposure > 0.9
        ? " — this is nearly buy-and-hold" : ""}</span></div>
    <div class="stat"><span class="k">t-statistic</span>
      <span class="v ${sign(bk.t)}">${bk.t == null ? "—" : bk.t.toFixed(2)}</span>
      <span class="s">across ${bk.n_folds || "the"} folds${
        bk.standard && bk.standard.t_bar
          ? ` · needs ${fmtNum(bk.standard.t_bar, 1)} after multiplicity`
          : " · needs 2.0"}</span></div>
    <div class="stat"><span class="k">$10k became</span>
      <span class="v">${bk.wealth == null ? "—" : fmtMoney(bk.wealth)}</span>
      <span class="s">${bk.wealth == null
        ? "no book run covers this sheet"
        : `the book · vs ${fmtMoney(bk.bench_wealth)} held · ${
            fmtDelta(bk.wealth - bk.bench_wealth)}`}</span></div>
    <div class="stat"><span class="k">Return / yr</span>
      <span class="v">${bk.cagr == null ? "—" : fmtCagr(bk.cagr * 100)}</span>
      <span class="s">${bk.cagr == null ? "—"
        : `the book · holding made ${sh.book_bench && sh.book_bench.cagr != null
            ? fmtCagr(sh.book_bench.cagr * 100) : "—"}`}</span></div>
    <div class="stat"><span class="k">Max drawdown</span>
      <span class="v ${bk.dd == null ? "" : "loss"}">${bk.dd == null ? "—"
        : fmtNum(bk.dd, 1) + "%"}</span>
      <span class="s">${sh.book_bench && sh.book_bench.dd != null
        ? `holding fell ${fmtNum(sh.book_bench.dd, 1)}%` : "worst fall of the account"}</span></div>
    <div class="stat"><span class="k">Standard</span>
      <span class="v">${edgeCount(bk.standard)}</span>
      <span class="s">criteria cleared on the book — hover for which</span></div>
  </div>
  ${bk.wealth == null ? "" : `<p class="sec-note">Every figure in this strip is
  <b>the book</b>: one account holding ${bk.n_names || nAssets} names at once over
  ${fmtNum(bk.years, 1)} out-of-sample years, idle capital earning nothing, which is the
  account the chart below draws and the same numbers as this rule's row on the
  leaderboard. Breadth
  (${hasBreadth ? `${wins} of ${nAssets} names positive` : "not available on this sheet"})
  and the per-name table further down are per asset by construction — a book has no
  breadth, it has one equity curve.</p>`}

  ${logicSection(r)}

  ${/* A pair gets the same chart and the same metrics table as a single rule. It did not
      * until 2026-08-15, and the reason it did not is gone: the note here named `curves.py`,
      * which built its own portfolio and only ever stitched single rules. `run_book.sh`
      * replaced it and scores every label on the sheet — `book_curves_*.json` carries the
      * pairs, `copy_curves` publishes them, and the page was refusing to draw files that
      * were sitting in `web/curves/`. On `crypto 1d` that was 23 of the 30 rows, so most of
      * that leaderboard led to a page with no chart and no metrics while `stocks 1d`, which
      * ships no pairs at all, looked complete. What a pair still has no source for is the
      * per-asset table below, and that is a different fact with its own note. */""}
  <div id="curve-host"><p class="sec-note">Loading equity curves…</p></div>

  ${isPair ? `<div class="note"><b>Pairs have no asset-by-asset page.</b> The pair sweep
  records leg-correlation diagnostics rather than per-symbol rows, so the breadth figure above
  — ${pctOr(r.ir_hit_rate)} of ${grp.n} assets positive — is the whole of what this sheet knows
  about where it worked and where it did not. Its two legs each have their own page and were
  each ranked on this same leaderboard.</div>` : `
  <section class="sec">
    <div class="sec-head"><h2>Asset by asset</h2>
      <span class="sec-note">all ${sorted.length} name${sorted.length === 1 ? "" : "s"},
        ranked by P&amp;L vs buy &amp; hold</span></div>
    <div class="tbl-wrap"><table>
      <thead><tr><th class="l">Asset</th><th>Years</th><th>IR vs buy &amp; hold</th>
        <th>$10k became</th><th>Buy &amp; hold</th><th>P&amp;L vs B&amp;H</th>
        <th>CAGR</th><th>B&amp;H CAGR</th><th>vs B&amp;H / yr</th>
        <th class="l">Verdict</th></tr></thead>
      <tbody>${sorted.map(p => {
        const delta = pnlDelta(p.net_pct, p.bh_pct);
        return `
        <tr><td class="l">${esc(p.symbol)}</td>
          <td>${p.years == null ? "—" : p.years.toFixed(1)}</td>
          <td class="${sign(p.ir)}">${fmtIR(p.ir)}</td>
          <td>${fmtMoney(grew(p.net_pct))}</td>
          <td>${fmtMoney(grew(p.bh_pct))}</td>
          <td class="${sign(delta)}">${fmtDelta(delta)}</td>
          <td>${fmtCagr(p.net_cagr)}</td>
          <td>${fmtCagr(p.bh_cagr)}</td>
          <td class="${sign(xc(p))}">${fmtCagrDelta(xc(p))}</td>
          <td class="l"><span class="chip ${p.ir > 0 ? "run" : "halt"}">${p.ir > 0 ? "beat" : "lost"}</span></td>
        </tr>`; }).join("")}</tbody>
      <caption>Sorted by <b>P&amp;L vs B&amp;H</b> — what $10,000 under this rule finished
      with, minus what the same $10,000 held finished with — best to worst.
      <b>Every name the rule was run on is here</b>, so this is the whole population and not
      a selection: what you count in it is the rule's own, and nothing has been set
      aside.${r.asset_unranked
        ? ` ${r.asset_unranked} of them ${r.asset_unranked === 1 ? "has" : "have"} no return
      on one side of the comparison and ${r.asset_unranked === 1 ? "sits" : "sit"} at the
      bottom printing em-dashes.` : ""}${spanRatio > 1.5
        ? ` Read the order with <b>Years</b> in view: span differs by asset — by a factor of
      ${spanRatio.toFixed(0)} on this sheet — so a long-held name can out-earn a short-held
      one at a far worse annual rate. That is why <b>vs B&amp;H / yr</b> is on the row, and
      why it will not agree with this ranking.` : ""}${
        hasBreadth ? ` The breadth gate asks for 70% of assets positive; this rule manages
      ${(wins / nAssets * 100).toFixed(0)}%.` : ""} Verdict follows the IR, so it can
      disagree with the money in <b>both</b> directions: an asset can out-earn buy-and-hold
      and still read “lost” for the risk it took, and one can earn <i>less</i> and read
      “beat” for taking much less risk to get there. Positions are unlevered — 1x, cash
      when flat — so the money columns are what the capital itself earned.</caption>
    </table></div>
  </section>`}`;

  paintCurves(cls, tf, r);
}

/* Fills the chart region once the sheet's curve file arrives. Split from
 * `backtestDetail` so the table and metrics render immediately and the chart appears when
 * ready, rather than the whole page waiting on the fetch. */
async function paintCurves(cls, tf, r) {
  const host = document.getElementById("curve-host");
  if (!host) return;
  const data = await loadCurves(`${cls}_${tf}`);
  if (document.getElementById("curve-host") !== host) return;   // navigated away

  if (!data || data.__error) {
    host.innerHTML = `<p class="sec-note">Equity curves unavailable${
      data && data.__error ? ` (${esc(data.__error)})` : ""}. They are written by the book
      run: <code>./run_book.sh</code> in <code>walk-forward optimization/</code>
      (or <code>python portfolio_wf.py --class ${CLASS_ARG[cls] || cls} --tf ${tf} --pit
      --curves</code> for this sheet alone), then rebuild the dashboard.</p>`;
    return;
  }
  const c = data[r.rule];
  if (!c) {
    /* The curve file is written by the same run that scores the book, so a rule with a
     * book column and no curve means the JSON is older than the CSV beside it. Say that,
     * rather than the old text about a top-N cut, which no longer exists. */
    host.innerHTML = `<p class="sec-note">No curve stored for ${esc(r.rule)}. This sheet's
      curve file carries ${Object.keys(data).length} rules and every rule on the board is
      supposed to be one of them, so it predates ${esc(r.rule)}. Rebuild it by re-running
      the book stage with <code>--curves</code> (<code>./run_book.sh</code> in
      <code>walk-forward optimization/</code>).</p>`;
    return;
  }

  const idxs = curveIndexes(c);
  const drawn = idxs.filter(i => i && i.curve && i.curve.length).slice(0, CHART_INDEXES);
  const names = drawn.map(i => esc(i.symbol || "index"));
  const eq = equitySection(c, r, drawn, names);

  host.innerHTML = `
  <section class="sec">
    <div class="sec-head"><h2>Cumulative P&amp;L</h2>
      <span class="sec-note">the book &mdash; one account, ${c.n_assets ?? "—"} names,
        equal-weight${c.pit ? ", point-in-time members only" : ""}${
        eq.matched.length ? ` &middot; vs ${eq.matched.map(l => esc(l.label)).join(" and ")}
        <b>at equal risk</b>` : ""}
        &middot; <b>${
        curveSide(c) === "short" ? "long/short" : "long/flat"}</b></span></div>
    ${curveSide(c) === "short" ? `<div class="note"><b>This is the long/short version of the
    rule.</b> "Stay out" is turned into "sell it" (<code>2p&minus;1</code>), so it is in the
    market on every bar and pays borrow on the short leg. That is the side
    <code>edge_standard.csv</code> scored for this rule, and the chart has to show the same
    strategy the verdict was computed on. The long/flat version is a different strategy with
    a different exposure and is not what the row above reports.</div>` : ""}
    ${eq.html}
  </section>

  ${metricsSection(c.metrics, eq.all, r)}`;
}

/* ---------------------------------------------------------------- one converted rule
 *
 * The house detail page carries an asset-by-asset table off `wf_per_asset_*`, and this
 * one deliberately does not: `portfolio_wf.py` records the book and its curve, not
 * per-symbol backtests, so there are no rows to draw and an empty table would imply the
 * measurement exists.
 *
 * What it carries instead is the thing this family has and the house catalogue does not —
 * **the same signal measured the other ways round**. Every rule here is one of two, four
 * or eight cells that differ only in whether the short side reverses, whether the signal
 * runs on synthetic candles, and whether the position is carried overnight. Those are the
 * comparisons the whole conversion batch exists to make, and putting them on the page is
 * what stops a reader quoting one cell as "the strategy". */

const convGroupOf = cls => (D.conversions || { groups: {} }).groups[cls];
const convRoster = name => ((D.conversions || {}).roster || [])
  .find(x => x.name === name);

/* The facets, in the order they are argued about, as a readable phrase rather than chips —
 * on a detail page there is room to say it in words. */
const convFacetWords = r => {
  const parts = [];
  if (r.short) parts.push("<b>reversing</b> on a short signal, as published");
  else if (r.short_off) parts.push("with the <b>short side switched off</b>");
  if (r.ha) parts.push("signal computed on <b>Heikin-Ashi</b> candles");
  if (r.chart) parts.push("window lengths read as <b>Pine bar counts</b>");
  if (r.eod === "flat") parts.push("<b>flat at the close</b>, never held overnight");
  else if (r.eod === "hold") parts.push("positions <b>carried overnight</b>");
  return parts;
};

/* Every other cell of the same base strategy on this sheet, so the facets can be compared
 * instead of asserted. Ranked by the same key the board is. */
const convSiblings = (sh, r) => sh.rows.filter(
  x => x.base === r.base && (x.key || x.rule) !== (r.key || r.rule));

const convSibRow = (sh, x) => `
  <tr data-go="#/backtest/conv/${bf.cls}/${sh.timeframe}/${slug(x.key || x.rule)}">
    <td class="l">${convChips(x) || '<span class="mut">as published, long only</span>'}</td>
    <td>${pctOr(bookExposure(x))}</td>
    <td>${fmtNum(x.book.sharpe, 3)}</td>
    <td class="${sign(x.book.cm_excess_cagr)}">${x.book.cm_excess_cagr == null ? "—"
      : fmtPct(x.book.cm_excess_cagr * 100, 2)}</td>
    <td>${fmtMoney(x.book.wealth)}</td></tr>`;

function convDetail(cls, tf, ruleSlug) {
  const g = convGroupOf(cls);
  const sh = g && g.sheets.find(x => x.timeframe === tf);
  const r = sh && sh.rows.find(x => slug(x.key || x.rule) === ruleSlug);
  if (!r) return (location.hash = "#/backtest/conversions");
  bf.board = "conv"; bf.cls = cls; bf.tf = tf;

  const bk = r.book || {}, bb = sh.book_bench || {};
  const meta = convRoster(r.base) || {};
  const sibs = convSiblings(sh, r);
  const facets = convFacetWords(r);

  app.innerHTML = `
  <a class="back" href="#/backtest/conversions">← converted strategies</a>
  <div class="hero">
    <div class="d-head"><span class="d-name">${esc(r.base)}</span>
      ${convChips(r)}
      <span class="chip mut">${esc(tf)}</span>
      <span class="chip mut">${esc(CLASS_LABEL[cls] || cls)}</span></div>
    <p class="lede">${meta.blurb ? esc(meta.blurb) + " " : ""}${meta.origin
      ? `Converted from <b>${esc(meta.origin)}</b>. ` : ""}${facets.length
      ? `Scored here ${facets.join(", ")}. ` : ""}Held as one account of
      <b>${bb.n_names || bk.n_names}</b> names, equal-weighted and rebalanced every bar,
      over <b>${fmtNum(bb.years || bk.years, 1)} years</b>${bb.start
      ? ` (${esc(bb.start)} to ${esc(bb.end)})` : ""}.</p>
  </div>

  <div class="strip">
    <div class="stat"><span class="k">$10k became</span>
      <span class="v ${bk.wealth > bb.wealth ? "gain" : "loss"}">${fmtMoney(bk.wealth)}</span>
      <span class="s">vs ${fmtMoney(bb.wealth)} holding the same names</span></div>
    <div class="stat"><span class="k">vs buy &amp; hold</span>
      <span class="v ${sign(bk.cm_excess_cagr)}">${bk.cm_excess_cagr == null ? "—"
        : fmtPct(bk.cm_excess_cagr * 100, 2)}</span>
      <span class="s">per year, at equal risk</span></div>
    <div class="stat"><span class="k">Time in market</span>
      <span class="v">${pctOr(bk.exposure)}</span>
      <span class="s">read this before the money</span></div>
    <div class="stat"><span class="k">Sharpe</span>
      <span class="v ${bk.sharpe > bk.sharpe_bench ? "gain" : "loss"}">${
        fmtNum(bk.sharpe, 3)}</span>
      <span class="s">vs ${fmtNum(bk.sharpe_bench, 3)} held</span></div>
    <div class="stat"><span class="k">Max drawdown</span>
      <span class="v ${bk.dd > bb.dd ? "gain" : "loss"}">${fmtNum(bk.dd, 1)}%</span>
      <span class="s">vs ${fmtNum(bb.dd, 1)}% held</span></div>
    <div class="stat"><span class="k">Cost headroom</span>
      <span class="v ${bk.headroom > 1 ? "" : "loss"}">${bk.headroom == null ? "—"
        : fmtNum(bk.headroom, 1) + "x"}</span>
      <span class="s">${bk.headroom ? "the fee schedule, before the edge dies"
        : "it already loses at the real cost"}</span></div>
  </div>

  <section class="sec">
    <div class="sec-head"><h2>Cumulative P&amp;L</h2>
      <span class="sec-note">the book, against the same universe at equal risk</span></div>
    <div id="curve-host"><p class="sec-note">Loading…</p></div>
  </section>

  ${sibs.length ? `
  <section class="sec">
    <div class="sec-head"><h2>The same signal, measured the other ways</h2>
      <span class="sec-note">${sibs.length} other cell${sibs.length === 1 ? "" : "s"} of
        <code>${esc(r.base)}</code> on this sheet</span></div>
    <p class="mut">These differ from the row above in the facets only — same signal, same
      bars, same fills, same benchmark. That is the comparison this batch exists to make,
      and it is the reason no single cell can be quoted as &ldquo;the strategy&rdquo;.</p>
    <div class="tbl-wrap"><table>
      <thead><tr><th class="l">Measured</th><th>Long %</th><th>Sharpe</th>
        <th>vs B&amp;H</th><th>$10k / book</th></tr></thead>
      <tbody>${sibs.map(x => convSibRow(sh, x)).join("")}</tbody>
    </table></div>
  </section>` : ""}

  <div id="conv-metrics"></div>`;

  bindGo(app);
  paintConvCurve(cls, tf, r);
}

/* Split from `convDetail` for the same reason `paintCurves` is split from
 * `backtestDetail`: the numbers render immediately and the chart arrives when its sheet
 * does, rather than the whole page waiting on a fetch. */
async function paintConvCurve(cls, tf, r) {
  const host = document.getElementById("curve-host");
  if (!host) return;
  const data = await loadCurves(`conv_${cls}_${tf}`);
  if (document.getElementById("curve-host") !== host) return;   // navigated away

  const ck = r.key || r.rule;
  if (!data || data.__error || !data[ck]) {
    /* Not a fault, and worth saying which of the two it is. The daily sheets have curves;
     * the minute sheets are being re-scored, and that re-score is measured in hours
     * because its cost is set by bar count -- the runs that produced those CSVs took 36
     * hours between them. */
    host.innerHTML = `<p class="sec-note">No equity series stored for this cell yet.
      Curves are written by the same run that scores the sheet, so they arrive together:
      <code>./run_convert_curves.sh ${esc(tf)}</code> in
      <code>walk-forward optimization/</code>, then rebuild the dashboard.${
      data && data.__error ? ` <span class="mut">(${esc(data.__error)})</span>` : ""}</p>`;
    return;
  }

  const c = data[ck];
  const idxs = curveIndexes(c);
  const drawn = idxs.filter(i => i && i.curve && i.curve.length).slice(0, CHART_INDEXES);
  const eq = equitySection(c, r, drawn, drawn.map(i => esc(i.symbol || "index")),
    " There is no name-by-name table on this board: this stage records the book and its"
    + " curve, not per-symbol backtests, so breadth is not something these sheets know.");
  host.innerHTML = eq.html;
  const mhost = document.getElementById("conv-metrics");
  if (mhost) mhost.innerHTML = metricsSection(c.metrics, eq.all, r);
}

/* ---------- "there is a newer build than the one you are looking at" ----------
 *
 * A single-page app never navigates, so a tab left open renders whatever it loaded until
 * somebody reloads it. On the machine that runs the build that is a mild annoyance; on a
 * second device it is a trap, because the page looks live — the clock in the masthead is
 * from the build, not from now — and a stale chart is indistinguishable from a wrong one.
 * An afternoon went into investigating exactly that.
 *
 * The check needs no new endpoint and no server restart: `index.html` already carries a
 * content hash of `data.js` in its `?v=` stamp, written by the build. This tab knows the
 * stamp it booted with (it is in its own script tag), so fetching that small file and
 * comparing is enough. Revalidated rather than re-downloaded — `no-cache` on the HTML
 * makes it a 304 in the common case.
 *
 * It never reloads on its own. Someone reading a detail page should not have it vanish;
 * the bar states the fact and the reload is a click.
 */
const BUILD_POLL_MS = 60000;
const buildStamp = html => (/data\.js\?v=([A-Za-z0-9]+)/.exec(html || "") || [])[1] || null;

function ownBuildStamp() {
  const tag = document.querySelector('script[src^="data.js"]');
  return tag ? buildStamp(tag.getAttribute("src") || "") : null;
}

function showUpdateBar() {
  const bar = document.getElementById("update-bar");
  if (!bar || bar.dataset.shown) return;
  bar.dataset.shown = "1";
  bar.hidden = false;
  bar.innerHTML = `<span>A newer build is on the server — this page is showing the one it
    loaded${D.generated_at ? ` at ${esc(D.generated_at)}` : ""}.</span>
    <button type="button" id="update-reload">Reload</button>`;
  const btn = document.getElementById("update-reload");
  if (btn) btn.addEventListener("click", () => location.reload());
}

function watchForNewBuild() {
  const mine = ownBuildStamp();
  // No stamp means the single-file build (everything inlined, nothing to poll) or a page
  // served some other way. Either way there is nothing this can usefully do.
  if (!mine) return;
  setInterval(async () => {
    try {
      const res = await fetch("index.html", { cache: "no-cache" });
      if (!res.ok) return;
      const theirs = buildStamp(await res.text());
      if (theirs && theirs !== mine) showUpdateBar();
    } catch (e) { /* offline, or the server went away; ask again next tick */ }
  }, BUILD_POLL_MS);
}

/* ---------- live refresh ----------
 * `data.js` is a build artefact: it is whatever `build_web_data.py` last wrote. The node,
 * meanwhile, revalues every open position on its own timer and mirrors the result to
 * `live.json` beside this page. Without polling that file the P&L on screen is frozen at
 * build time, which is exactly what it looked like — the marking was working and the page
 * was not listening.
 *
 * Only the paper section refreshes. Backtest figures come from multi-year sweeps and do
 * not change while you watch them.
 */
const LIVE_EVERY_MS = 20000;
let liveTimer = null, liveFailures = 0;

function applyLive(state) {
  if (!state || !Array.isArray(state.strategies)) return false;
  // `group` is assigned at build time from the universe lists, and the node knows nothing
  // about it — so carry it across by symbol rather than losing the grouping on refresh.
  const groupBySymbol = {};
  D.strategies.forEach(s => { if (s.group) groupBySymbol[s.symbol] = s.group; });
  state.strategies.forEach(s => {
    /* Falls back to the system's own class, which is now a group key for three of the
     * five legs — `crypto`, `us_etfs`, `commodities`. Before, everything that was not
     * crypto became "etf", so a live-refreshed commodity system moved itself into the
     * SPY/SOXL/TQQQ group between the page load and the first tick. */
    /* A book holds a whole class and has no symbol to file under, so the symbol-carried
     * grouping above cannot reach it and it would fall through to the `etf` default —
     * which is labelled "SPY · SOXL · TQQQ". `payload.py` assigns the same group at build
     * time; this is the live-refresh half of it. */
    if (!s.group) s.group = (s.kind === "book") ? "book"
      : (groupBySymbol[s.symbol]
         || ((D.paper_groups || []).some(g => g.key === s.cls) ? s.cls : "etf"));
  });
  D.strategies = state.strategies;
  /* Who is looking, carried from the API's per-account cut. Without these the "Whose"
   * filter cannot tell mine from anybody else's and would show an empty board. They are
   * absent from the offline build, where there is only ever one reader. */
  if (state.account != null) D.account = state.account;
  if (state.house != null) D.house = state.house;
  if (state.is_admin != null) D.is_admin = state.is_admin;
  if (state.feed) D.feed = state.feed;
  if (state.venue) D.venue = state.venue;
  D.generated_at = state.generated_at || D.generated_at;
  const el = document.getElementById("m-time");
  if (el) el.textContent = D.generated_at;
  return true;
}

async function pollLive() {
  try {
    const res = await fetch("live.json", { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    if (applyLive(await res.json())) {
      liveFailures = 0;
      if ((location.hash || "#/paper").startsWith("#/paper")) repaintPaper();
    }
  } catch (e) {
    // A missing live.json just means the node is not running; the page keeps the build's
    // numbers and stops asking rather than logging a failure every 20 seconds forever.
    if (++liveFailures >= 3 && liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  }
}

/* Repaint without moving anything the reader is looking at.
 *
 * Two views tick. The master list rewrites `#paper-body`; a system's own page rewrites
 * `#sys-body`, which is why the hero and the banners sit outside it. Neither has any
 * accordion state left to preserve — the list is links now — but the system page carries a
 * nine-column holdings table that can be scrolled sideways on a laptop, and rebuilding it
 * twice a second would snap it back to column one every time. So the horizontal scroll of
 * every table is captured and put back, the same way `scrollY` already was. */
function repaintPaper() {
  if (location.hash.startsWith("#/paper/sys/")) {
    const host = document.getElementById("sys-body");
    if (!host) return;
    // Vertical as well as horizontal: the fills list scrolls inside its own box, and a
    // reader three hundred rows down it would otherwise be thrown back to the newest fill
    // on every tick.
    const at = [...host.querySelectorAll(".tbl-wrap")]
      .map(w => [w.scrollLeft, w.scrollTop]);
    const y = window.scrollY;
    paintSystem();
    [...host.querySelectorAll(".tbl-wrap")].forEach((w, i) => {
      if (!at[i]) return;
      if (at[i][0]) w.scrollLeft = at[i][0];
      if (at[i][1]) w.scrollTop = at[i][1];
    });
    window.scrollTo(0, y);
    return;
  }
  const host = document.getElementById("paper-body");
  if (!host || location.hash.startsWith("#/paper/")) return;
  const y = window.scrollY;
  // `false`: reuse the frozen ranking. A tick repaint must not re-sort the list — see the
  // note on `orderSystems`.
  paintPaper(false);
  window.scrollTo(0, y);
}

function startLive() {
  if (liveTimer) return;
  liveTimer = setInterval(pollLive, LIVE_EVERY_MS);
  pollLive();
}

/* ---------- tick stream ----------
 * The node holds a WebSocket open to Twelve Data and pushes a compact delta whenever an
 * instrument prints. Polling `live.json` every 20s still runs underneath as the safety
 * net — it is what recovers the page if the socket stalls or the node restarts — but when
 * the stream is up the numbers move on the tick rather than on a timer.
 *
 * The delta carries only what changes (id, P&L, equity, mark, units, state, fill count).
 * The full document is ~300 kB and pushing it twice a second would make the browser
 * re-parse everything to move a few figures.
 */
/* Same origin, and the scheme follows the page: an HTTPS page must use `wss://` or the
 * browser blocks the connection outright. Hard-coding a host and port worked on localhost
 * and would have failed silently the moment the desk was shared through a tunnel. */
const WS_URL = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
let ws = null, wsRetry = 1, wsSeen = 0;

function applyTicks(msg) {
  if (!msg || !Array.isArray(msg.rows)) return;
  const byId = {};
  D.strategies.forEach(s => { byId[s.id] = s; });
  msg.rows.forEach(r => {
    const s = byId[r.id];
    if (!s) return;
    if (r.pnl != null) s.paper_pnl_pct = r.pnl;
    if (r.eq != null) s.equity = r.eq;
    if (r.px != null) s.mark_price = r.px;
    if (r.u != null) s.units = r.u, s.position_units = r.u;
    if (r.st) s.state = r.st;
    if (r.n != null) s.paper_trades = r.n;
  });
  wsSeen = msg.ticks || wsSeen;
  const el = document.getElementById("m-time");
  if (el) el.textContent = `${msg.t} · ${msg.upstream} · ${wsSeen.toLocaleString()} ticks`;
  if ((location.hash || "#/paper").startsWith("#/paper")) repaintPaper();
}

function connectTicks() {
  try { ws = new WebSocket(WS_URL); } catch (e) { return; }
  ws.onopen = () => { wsRetry = 1; };
  ws.onmessage = e => {
    try {
      const msg = JSON.parse(e.data);
      // Two shapes reach this socket. The node's local stream sends a compact delta with
      // `rows`; the shared server pushes the whole `live.json`, which is what a remote
      // viewer gets. Both end in the same repaint.
      if (Array.isArray(msg.rows)) applyTicks(msg);
      else if (Array.isArray(msg.strategies) && applyLive(msg)) {
        const el = document.getElementById("m-time");
        if (el) el.textContent = msg.generated_at || "";
        if ((location.hash || "#/paper").startsWith("#/paper")) repaintPaper();
      }
    } catch (_) {}
  };
  ws.onclose = () => {
    ws = null;
    // Exponential backoff, capped. A closed socket is normal — the node restarts, the
    // laptop sleeps — and the poller keeps the page correct in the meantime.
    setTimeout(connectTicks, Math.min(wsRetry *= 2, 30) * 1000);
  };
  ws.onerror = () => { if (ws) ws.close(); };
}

/* ================================ router ================================ */
const NAV = [["#/paper", "Paper trading"], ["#/backtest", "Backtest"]];

/* The manager console lives in `paper api/`, not here, so it exists only when this page is
 * being served BY that process. The single-file `dist/dashboard.html` and the loopback
 * `serve.py` have no such route, and a dead link on either is worse than no link at all.
 *
 * So it is discovered rather than assumed: `/auth/me` answers 200 only behind the API's
 * login, which is exactly the condition under which `/desk` is reachable. One request, on
 * load, and the link appears or it does not. */
let DESK_LINK = false;

async function findDesk() {
  try {
    const res = await fetch("/auth/me", {cache: "no-store", credentials: "same-origin"});
    DESK_LINK = res.ok;
  } catch { DESK_LINK = false; }
  if (DESK_LINK) render();
}

function render() {
  const h = location.hash || "#/paper";
  let m;
  /* Before the generic `#/paper/<id>`, and it has to stay there: that pattern is `(.+)`,
   * so it swallows `sys/...` whole and hands `paperDetail` an id no strategy has, which
   * bounces straight back to the master list. A strategy id carries no "/", so the two
   * cannot otherwise collide. */
  if ((m = h.match(/^#\/paper\/sys\/([^/]+)\/([^/]+)\/(.+)$/)))
    paperSystem(decodeURIComponent(m[1]), decodeURIComponent(m[2]), m[3]);
  else if ((m = h.match(/^#\/paper\/(.+)$/))) paperDetail(m[1]);
  // Before the three-segment detail pattern would matter, and before the bare prefix:
  // the converted board is a state of the backtest page, not a page of its own, so it sets
  // the switch and falls through to the same master.
  else if (h === "#/backtest/conversions") { bf.board = "conv"; backtestMaster(); }
  // Ahead of the generic three-segment detail pattern, which would otherwise match
  // `conv/<cls>/<tf>` and hand `backtestDetail` a class that does not exist.
  else if ((m = h.match(/^#\/backtest\/conv\/([^/]+)\/([^/]+)\/(.+)$/)))
    convDetail(decodeURIComponent(m[1]), decodeURIComponent(m[2]), m[3]);
  else if ((m = h.match(/^#\/backtest\/([^/]+)\/([^/]+)\/(.+)$/))) backtestDetail(m[1], m[2], m[3]);
  else if (h.startsWith("#/backtest")) backtestMaster();
  else paperMaster();

  const active = h.startsWith("#/backtest") ? "#/backtest" : "#/paper";
  $("#nav").innerHTML = NAV.map(([href, label]) =>
    `<a class="nav-link ${href === active ? "on" : ""}" href="${href}">${label}</a>`).join("")
    // Not hash routes: they are different pages served by a different process, so they are
    // real navigations and never get the `on` state. Both appear together, in the same
    // order they carry over there, so the row of links is the same row on every page.
    + (DESK_LINK ? `<a class="nav-link" href="/desk">My desk</a>
                    <a class="nav-link" href="/desk/docs">API</a>` : "");

  bindGo(app);
  bindBookSwitch();          // no-op on any view that does not render one
  window.scrollTo(0, 0);
}
addEventListener("hashchange", render);

/* Crossing the phone breakpoint changes the leaderboard's column order, so the view has to
 * be rebuilt — in practice this fires when a phone is rotated. The scroll position is put
 * back afterwards because `render` ends at the top of the page, and being thrown there for
 * turning the phone sideways reads as a crash rather than a relayout. */
NARROW.addEventListener("change", () => {
  const y = window.scrollY;
  render();
  window.scrollTo(0, y);
});

render();
findDesk();          // adds "My desk" to the nav, but only where that page exists
loadBookState();     // ...and the paper-trade switch, where the API is serving this page
startLive();
connectTicks();
watchForNewBuild();
