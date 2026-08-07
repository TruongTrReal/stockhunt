/* Three sections, hash-routed, so any of them can be bookmarked and sent on its own:
 *   #/paper                     every strategy running in the sandbox
 *   #/paper/<id>                one strategy's live paper progress
 *   #/backtest                  research leaderboards, 20 mega-caps and 10 crypto pairs
 *   #/backtest/<cls>/<tf>/<rule>  one rule, broken down asset by asset
 *   #/method                    the four gates and how to read an IR
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
const fmtNum = (v, d = 1) => v == null ? "—" : Number(v).toFixed(d);

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
const gatePips = g => { const n = g.filter(Boolean).length;
  return `<span class="gates${n ? "" : " none"}" title="Information ratio, Breadth, Headroom, t-statistic">${n}/4</span>`; };

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

  const colors = ["var(--ink)", "var(--muted)"];
  const lines = sets.map((s, k) => `
    <polyline points="${s.map((v, i) => `${x(i)},${y(v)}`).join(" ")}" fill="none"
      stroke="${colors[k]}" stroke-width="${k ? 1.1 : 1.7}"
      ${k ? 'stroke-dasharray="4 3"' : ""} vector-effect="non-scaling-stroke"/>`).join("");

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

/* One asset's thumbnail: the rule against holding that same asset. Same log treatment, no
 * axis furniture — at this size a gridline is noise and the numbers sit underneath. */
function miniChart(a, b, w = 300, h = 62) {
  const all = [...a, ...b].filter(v => v > 0);
  if (all.length < 2) return "";
  const lo = Math.log10(Math.min(...all)), hi = Math.log10(Math.max(...all));
  const span = (hi - lo) || 1;
  const pts = s => s.map((v, i) =>
    `${(i / Math.max(s.length - 1, 1)) * w},${h - ((Math.log10(Math.max(v, 1e-9)) - lo) / span) * (h - 6) - 3}`).join(" ");
  const base = h - ((Math.log10(100) - lo) / span) * (h - 6) - 3;
  return `<svg class="mini" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    ${base > 0 && base < h ? `<line x1="0" x2="${w}" y1="${base}" y2="${base}"
      stroke="var(--hair)" stroke-width="1" vector-effect="non-scaling-stroke"/>` : ""}
    <polyline points="${pts(b)}" fill="none" stroke="var(--muted)" stroke-width="1"
      stroke-dasharray="3 2" vector-effect="non-scaling-stroke"/>
    <polyline points="${pts(a)}" fill="none" stroke="var(--ink)" stroke-width="1.5"
      vector-effect="non-scaling-stroke"/></svg>`;
}

function lineChart(sets, labels) {
  const w = 640, h = 172, pad = { l: 6, r: 6, t: 10, b: 16 };
  const all = sets.flat(), lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1;
  const n = Math.max(...sets.map(s => s.length));
  const x = i => pad.l + (i / (n - 1)) * (w - pad.l - pad.r);
  const y = v => pad.t + (1 - (v - lo) / span) * (h - pad.t - pad.b);
  const colors = ["var(--ink)", "var(--muted)"];
  const grid = [0, 1].map(f => {
    const gy = pad.t + f * (h - pad.t - pad.b);
    return `<line x1="${pad.l}" x2="${w - pad.r}" y1="${gy}" y2="${gy}"
      stroke="var(--hair)" stroke-width="1"/>`; }).join("");
  const lines = sets.map((s, k) =>
    `<polyline points="${s.map((v, i) => `${x(i)},${y(v)}`).join(" ")}" fill="none"
      stroke="${colors[k]}" stroke-width="${k ? 1.2 : 1.8}"
      ${k ? 'stroke-dasharray="4 3"' : ""} vector-effect="non-scaling-stroke"/>`).join("");
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
      role="img" aria-label="${labels.join(" versus ")}">${grid}
    <line x1="${pad.l}" x2="${w - pad.r}" y1="${y(100)}" y2="${y(100)}"
      stroke="var(--hair-2)" stroke-width="1"/>${lines}</svg>
    <div class="legend">${labels.map((l, k) =>
      `<span><i class="sw" style="background:${colors[k]}"></i>${esc(l)}</span>`).join("")}</div>`;
}

function bindGo(root) {
  root.querySelectorAll("[data-go]").forEach(el =>
    el.onclick = () => { location.hash = el.dataset.go; });
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
let pf = { cls: "all", tf: "all" };

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
      <span class="v ${D.feed.status === "ok" ? "gain" : ""}">${esc(D.feed.status)}</span>
      <span class="s">${esc(D.feed.source)} · ${esc(D.feed.plan)}</span></div>
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

  <div id="paper-strip"></div>

  <div class="filters">
    <span class="f-group"><span class="f-label">Asset</span>
      ${pills([["all", "All"], ["equity", "Equities"], ["crypto", "Crypto"]], pf.cls, "data-cls")}</span>
    <span class="f-group"><span class="f-label">Timeframe</span>
      ${pills([["all", "All"], ["1d", "1d"], ["4h", "4h"]], pf.tf, "data-tf")}</span></div>

  <div id="paper-body"></div>`;

  paintPaper();
  loadPaperCurves().then(c => { if (c) repaintPaper(); });
  document.querySelectorAll("[data-cls]").forEach(b =>
    b.onclick = () => { pf.cls = b.dataset.cls; setActive("data-cls", pf.cls); paintPaper(); });
  document.querySelectorAll("[data-tf]").forEach(b =>
    b.onclick = () => { pf.tf = b.dataset.tf; setActive("data-tf", pf.tf); paintPaper(); });
}

/* The headline figures live in their own container because the tick repaint rewrites only
 * the region it is given. They used to sit outside it, so every number here — desk P&L,
 * mean, fill count, feed status — stayed frozen at page load while the rows underneath
 * updated several times a second. */
function paperStrip() {
  const host = document.getElementById("paper-strip");
  if (!host) return;
  const live = D.strategies.filter(s => s.status === "running").length;
  const mean = D.strategies.length
    ? D.strategies.reduce((a, s) => a + (s.paper_pnl_pct || 0), 0) / D.strategies.length : 0;
  // Summed from the systems' own books, not read off `D.venue`. The tick stream pushes a
  // per-system equity on every print but the venue totals only refresh on the 20-second
  // `live.json` poll, so the percentage moved live while the dollar figure lagged behind
  // it — two views of one number disagreeing on screen. Deriving both from the same array
  // also makes them agree by construction, which is the bug that put a $10,000 phantom
  // loss on the desk when one system had not reported yet.
  const deployed = D.strategies.reduce((a, s) => a + (s.capital || 0), 0) || D.venue.balance;
  const equity = D.strategies.reduce(
    (a, s) => a + (s.equity != null ? s.equity : (s.capital || 0)), 0);
  const pnl = equity - deployed;
  host.innerHTML = `
  <div class="strip">
    <div class="stat"><span class="k">${isReplay() ? "Systems" : "Systems live"}</span>
      <span class="v">${countSystems(D.strategies.filter(s => isReplay() || s.status === "running"))} / ${countSystems(D.strategies)}</span>
      <span class="s">${countSystems(D.strategies.filter(s => s.cls === "crypto"))} crypto ·
        ${countSystems(D.strategies.filter(s => s.cls === "equity"))} equity ·
        ${D.strategies.length} instances over ${new Set(D.strategies.map(s => s.symbol)).size} assets</span></div>
    <div class="stat"><span class="k">${isReplay() ? "Replay P&amp;L, mean" : "Paper P&amp;L, mean"}</span>
      <span class="v ${sign(mean)}">${fmtPct(mean)}</span>
      <span class="s">since ${(D.strategies[0] || {}).since || "—"}</span></div>
    <div class="stat"><span class="k">Total fills</span>
      <span class="v">${D.strategies.reduce((a, s) => a + (s.paper_trades || 0), 0)}</span>
      <span class="s">orders that reached a position</span></div>
    <div class="stat"><span class="k">P&amp;L</span>
      <span class="v ${sign(pnl)}">${(pnl >= 0 ? "+" : "−") + money(Math.abs(pnl))}</span>
      <span class="s ${sign(pnl)}">${fmtPct((equity / deployed - 1) * 100)} on
        ${money(deployed)} deployed</span></div>
    <div class="stat"><span class="k">Data feed</span>
      <span class="v ${D.feed.status === "ok" ? "gain" : ""}">${esc(D.feed.status)}</span>
      <span class="s">${esc(D.feed.source)}</span></div>
  </div>`;
}

function paintPaper() {
  paperStrip();
  const rows = D.strategies.filter(s =>
    (pf.cls === "all" || s.cls === pf.cls) && (pf.tf === "all" || s.tf === pf.tf));
  const host = document.getElementById("paper-body");
  host.innerHTML = `
  <section class="sec">
    <div class="sec-head"><h2>${isReplay() ? "Replayed systems" : "Running systems"}</h2>
      <span class="sec-note">${countSystems(rows)} systems · ${rows.length} deployments · open a system, then a universe</span></div>

    ${groupedStrategies(rows)}
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

/* ---------- grouped, collapsible strategy list ----------
 * 330 strategies as one flat table is unreadable, and the flat list also hid the thing
 * that matters most: the three groups answer three different questions. The mega-caps are
 * the universe the equity rules were ranked on, so that group is the only like-for-like
 * forward test; the ETFs are a transfer onto instruments the study never held; crypto has
 * its own sheet entirely.
 *
 * Built on native <details>, deliberately. The browser owns the open/closed state, so a
 * filter click that rewrites this container does not collapse everything the reader had
 * opened, and it keeps working with the keyboard for free.
 */
/* A *system* is one rule at one horizon on one asset class — the thing the research
 * ranked and the thing you would decide to keep or drop. Running it across 20 mega-caps is
 * deployment, not twenty systems. Counting instances made the headline read 330 when there
 * are 20 distinct systems on the desk, which overstated the breadth of what is being
 * tested by more than an order of magnitude. */
const systemKey = s => `${s.cls}|${s.tf}|${s.rule}`;
const countSystems = list => new Set(list.map(systemKey)).size;

function aggregate(list) {
  const n = list.length;
  const mean = n ? list.reduce((a, s) => a + (s.paper_pnl_pct || 0), 0) / n : 0;
  return {
    n, mean,
    live: list.filter(s => s.status === "running").length,
    fills: list.reduce((a, s) => a + (s.paper_trades || 0), 0),
    open: list.filter(s => s.state && s.state !== "flat").length,
  };
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
 * the log treatment the research charts need would only flatten the detail here. */
function pnlSpark(curve, bench, w = 560, h = 96) {
  const sets = bench && bench.length ? [curve, bench] : [curve];
  const all = sets.flat().filter(v => isFinite(v));
  if (all.length < 2) return "";
  const lo = Math.min(...all, 100), hi = Math.max(...all, 100), span = (hi - lo) || 1;
  const pad = 6;
  const y = v => pad + (1 - (v - lo) / span) * (h - pad * 2);
  const line = (s, k) => `<polyline points="${s.map((v, i) =>
      `${(i / Math.max(s.length - 1, 1)) * w},${y(v)}`).join(" ")}" fill="none"
      stroke="${k ? "var(--muted)" : (curve[curve.length - 1] >= 100 ? "var(--gain)" : "var(--loss)")}"
      stroke-width="${k ? 1 : 1.6}" ${k ? 'stroke-dasharray="3 2"' : ""}
      vector-effect="non-scaling-stroke"/>`;
  return `<svg class="pnl-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
      aria-hidden="true">
    <line x1="0" x2="${w}" y1="${y(100)}" y2="${y(100)}" stroke="var(--hair-2)"
      stroke-width="1" vector-effect="non-scaling-stroke"/>
    ${sets.map((s, k) => line(s, k)).reverse().join("")}</svg>`;
}

function pnlPanel(entry, label, withBench) {
  if (!entry) return `<p class="sec-note">No simulated history for this window.</p>`;
  const d = entry.dates || [];
  return `
  <div class="pnl-wrap">
    <div class="pnl-head">
      <span class="pnl-val num ${sign(entry.pnl_pct)}">${fmtPct(entry.pnl_pct)}</span>
      <span class="pnl-lbl">${label}${withBench && entry.bench_pnl_pct != null
        ? ` · buy &amp; hold ${fmtPct(entry.bench_pnl_pct)}` : ""}</span>
    </div>
    ${pnlSpark(entry.curve, withBench ? entry.bench : null)}
    <div class="pnl-axis"><span>${esc(d[0] || "")}</span><span>${esc(d[d.length - 1] || "")}</span></div>
  </div>`;
}


function groupedStrategies(rows) {
  const groups = (D.paper_groups && D.paper_groups.length ? D.paper_groups
    : [{ key: "crypto", label: "Crypto" }, { key: "megacap", label: "Equities" },
       { key: "etf", label: "ETFs" }]);
  const groupLabel = {};
  groups.forEach(g => { groupLabel[g.key] = g.label; });

  // Systems first. A system is the thing that was researched and the thing you would keep
  // or drop; the assets under it are where it happens to be deployed. Ordered by class,
  // horizon and name rather than by P&L — on a page updating several times a second, a
  // list that re-sorts itself cannot be read.
  const systems = [...new Set(rows.map(systemKey))].sort();

  const blocks = systems.map(key => {
    const mine = rows.filter(s => systemKey(s) === key);
    if (!mine.length) return "";
    const [cls, tf, rule] = key.split("|");
    const a = aggregate(mine);

    // Second level: which universe. An equity system spans the mega-caps and the ETFs, and
    // those carry different evidential weight — one is the universe it was ranked on, the
    // other a transfer onto instruments the research never held. A crypto system has only
    // its own group, and a single-group system still gets the header so the shape of the
    // page does not change between them.
    const inner = groups.map(g => {
      const gs = mine.filter(s => (s.group || "") === g.key)
                     .sort((x, y) => x.symbol.localeCompare(y.symbol));
      if (!gs.length) return "";
      const ga = aggregate(gs);
      return `
      <details class="sym" data-key="grp:${key}|${g.key}">
        <summary>
          <span class="sym-name">${esc(groupLabel[g.key] || g.key)}</span>
          <span class="sym-meta">${gs.length} assets · ${ga.open} with a position ·
            ${ga.fills} fills</span>
          <span class="sym-pnl num ${sign(ga.mean)}">${fmtPct(ga.mean)}</span>
        </summary>
        <div class="tbl-wrap"><table>
          <thead><tr><th class="l">Asset</th><th class="l">State</th><th>Units</th>
            <th>Entry</th><th>Mark</th>
            <th>${isReplay() ? "Replay P&amp;L" : "Paper P&amp;L"}</th>
            <th>Trades</th><th class="l">Status</th></tr></thead>
          <tbody>${gs.map(s => `
            <tr data-go="#/paper/${s.id}">
              <td class="l">${esc(s.symbol)}</td>
              <td class="l">${stateCell(s)}</td>
              <td>${fmtUnits(s.position_units)}</td>
              <td>${s.entry ? s.entry.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—"}</td>
              <td>${s.mark_price ? s.mark_price.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—"}</td>
              <td class="${sign(s.paper_pnl_pct)}">${fmtPct(s.paper_pnl_pct)}</td>
              <td>${s.paper_trades}</td>
              <td class="l">${statusChip(s)}</td></tr>`).join("")}</tbody>
        </table></div>
        ${(() => { const c = pcurves && pcurves[key];
          if (!c || !c.assets) return "";
          return `<div class="minis">${gs.map(s => {
            const a = c.assets[s.symbol];
            if (!a) return "";
            return `<figure class="mini-card">
              <figcaption><span class="mini-sym">${esc(s.symbol)}</span></figcaption>
              ${PC_WINDOWS.map(([w, label]) => { const e = a[w];
                if (!e) return "";
                return `<div class="mini-win">
                  <span class="hist-lbl">${label}</span>
                  ${pnlSpark(e.curve, e.bench, 300, 46)}
                  <span class="hist-nums"><b class="${sign(e.pnl_pct)}">${fmtPct(e.pnl_pct)}</b>
                    <span>hold ${fmtPct(e.bench_pnl_pct)}</span></span>
                </div>`; }).join("")}
            </figure>`; }).join("")}</div>`; })()}
      </details>`;
    }).join("");

    return `
    <details class="grp" data-key="sys:${key}">
      <summary>
        <span class="grp-id"><span class="grp-name">${esc(rule)}</span>
          <span class="grp-meta">${esc(tf)} · ${esc(cls)} · ${a.n} assets</span></span>
        <span class="grp-hist">${PC_WINDOWS.map(([w, label]) => {
          const e = pcurves && pcurves[key] && pcurves[key].system && pcurves[key].system[w];
          if (!e) return `<span class="hist-cell"></span>`;
          const beat = e.pnl_pct - (e.bench_pnl_pct ?? 0);
          return `<span class="hist-cell">
            <span class="hist-lbl">${label}</span>
            ${pnlSpark(e.curve, e.bench, 200, 30)}
            <span class="hist-nums">
              <span class="hist-row"><b class="${sign(e.pnl_pct)}">${fmtPct(e.pnl_pct)}</b>
                <b class="${sign(beat)}">${fmtPct(beat)}</b></span>
              <span class="hist-row hist-sub">hold ${fmtPct(e.bench_pnl_pct)}</span>
            </span>
          </span>`; }).join("")}</span>
        <span class="grp-pnl num ${sign(a.mean)}">${fmtPct(a.mean)}</span>
      </summary>
      <p class="sec-note pnl-caveat">Charts are simulated from history — solid the system,
        dashed the same basket held. The desk is days old; this is how it <em>would</em>
        have traded. Live P&amp;L is the figure on the right.</p>
      <div class="syms">${inner}</div>
    </details>`;
  }).join("");

  return blocks || `<p class="sec-note">Nothing matches this filter.</p>`;
}


function paperDetail(id) {
  const s = D.strategies.find(x => x.id === id);
  if (!s) return (location.hash = "#/paper");
  app.innerHTML = `
  <a class="back" href="#/paper">← ${isReplay() ? "strategy replay" : "paper trading"}</a>
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
    <div class="stat"><span class="k">Entry</span>
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
      ? `<div class="panel">${lineChart([s.paper_curve, s.bench_curve], ["Strategy", "Buy & hold"])}
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
        <th>Price</th><th>P&amp;L</th></tr></thead>
      <tbody>${s.trades.length ? s.trades.map(t => `
        <tr><td class="l">${t.ts}</td>
          <td class="l ${t.side === "BUY" ? "gain" : "loss"}">${t.side}</td>
          <td>${fmtUnits(t.qty)}</td>
          <td>${t.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
          <td class="${sign(t.pnl)}">${t.pnl >= 0 ? "+" : "−"}$${Math.abs(t.pnl)
            .toLocaleString(undefined, { maximumFractionDigits: 0 })}</td></tr>`).join("")
      : `<tr><td class="l" colspan="5" style="color:var(--muted)">No fills yet — still warming up.</td></tr>`}
      </tbody></table></div>
  </section>

  <div class="note">Looking for whether this rule actually works? That is the multi-year
  question — see <a href="#/backtest">the backtest for ${esc(s.rule)}</a>.</div>`;
}

/* ================================ BACKTEST ================================ */
let bf = { cls: "stocks", tf: "1d", kind: "single" };
const sheetOf = (cls, tf) => D.backtest[cls].sheets.find(s => s.timeframe === tf);

function backtestMaster() {
  app.innerHTML = `
  <div class="hero">
    <h1>Backtest results</h1>
    <p class="lede">Every TA-Lib rule run independently on each asset, walk-forward: parameters
    re-picked on each in-sample window and applied to the next. Scored as information ratio
    against buy-and-hold on the same asset — zero means matching it, positive means beating it.</p>
  </div>

  <div class="filters">
    <span class="f-group"><span class="f-label">Universe</span>
      ${pills([["stocks", "Top 20 US stocks"], ["crypto", "Top 10 crypto"]], bf.cls, "data-bcls")}</span>
    <span class="f-group"><span class="f-label">Timeframe</span>
      ${pills([["1d", "1d"], ["4h", "4h"]], bf.tf, "data-btf")}</span>
    <span class="f-group"><span class="f-label">Rules</span>
      ${pills([["single", "Single"], ["combo", "Combinations"]], bf.kind, "data-bkind")}</span></div>

  <div id="bt-body"></div>`;

  paintBacktest();
  document.querySelectorAll("[data-bcls]").forEach(b =>
    b.onclick = () => { bf.cls = b.dataset.bcls; setActive("data-bcls", bf.cls); paintBacktest(); });
  document.querySelectorAll("[data-btf]").forEach(b =>
    b.onclick = () => { bf.tf = b.dataset.btf; setActive("data-btf", bf.tf); paintBacktest(); });
  document.querySelectorAll("[data-bkind]").forEach(b =>
    b.onclick = () => { bf.kind = b.dataset.bkind; setActive("data-bkind", bf.kind); paintBacktest(); });
}

/* Pairs of rules joined by an operator (`or`, `and`, `vote`, `gate`).
 *
 * These sort ABOVE every single rule on equities — the best 1d combination scores IR
 * -0.057 against -0.224 for the best single — and that is the single most misleading
 * number in the whole study. `corr(IR, long_frac)` is +0.88 on this sheet: the ranking is
 * very largely a ranking of time spent invested, `or` wins because it is the operator
 * that spends the most, and `MININDEX~MAXINDEX|or` is long 100% of the time, which is to
 * say it is buy-and-hold wearing a rule's name. IR against buy-and-hold approaches zero
 * from below as a strategy approaches always-long, and zero is the ceiling, not a win.
 *
 * So exposure sits immediately beside the IR, and the correlation is stated above the
 * table. Crypto behaves differently (correlation near zero), which is why it is computed
 * per sheet rather than asserted once. */
/* `HT_TRENDMODE~MAXINDEX|or` carries its operator in the name; the table gives that its
 * own column, so strip it here rather than print it twice. */
const comboName = r => String(r).split("|")[0];
/* `IS#1[combo]` — the re-selected-each-fold pseudo-rule — has no single operator, and the
 * CSV therefore carries a NaN that stringifies to the literal "nan". */
const opLabel = o => !o || o === "nan" || o === "None" ? "—" : o;

function paintCombos(host, grp, sh) {
  if (!sh.combos || !sh.combos.length) {
    host.innerHTML = `<div class="note">No walk-forward combinations have been scored for
      this sheet yet. Run <code>python combo_wf.py --tf ${sh.timeframe}</code> in
      <code>backtest master/</code>.</div>`;
    return;
  }
  const best = sh.combos[0], bestSingle = sh.rows[0];
  const corr = sh.combo_corr;
  const drivenByExposure = corr != null && corr > 0.5;

  host.innerHTML = `
  <div class="strip">
    <div class="stat"><span class="k">Combinations</span><span class="v">${sh.n_combos}</span>
      <span class="s">pairs joined by or / and / vote / gate</span></div>
    <div class="stat"><span class="k">Best combination</span>
      <span class="v ${sign(best.ir_net)}">${fmtIR(best.ir_net)}</span>
      <span class="s">${esc(comboName(best.rule))}</span></div>
    <div class="stat"><span class="k">Time invested</span>
      <span class="v">${best.long_frac == null ? "—" : (best.long_frac * 100).toFixed(0) + "%"}</span>
      <span class="s">of bars, long</span></div>
    <div class="stat"><span class="k">Best single</span>
      <span class="v ${sign(bestSingle.ir_net)}">${fmtIR(bestSingle.ir_net)}</span>
      <span class="s">${esc(bestSingle.rule)}</span></div>
    <div class="stat"><span class="k">Cleared all gates</span>
      <span class="v">${sh.combos.filter(r => r.gates.every(Boolean)).length}</span>
      <span class="s">of ${sh.combos.length} shown</span></div>
  </div>

  <div class="note">${drivenByExposure
    ? `<b>Read the exposure column before the IR column.</b> Across all ${sh.n_combos}
       combinations on this sheet, IR and time-invested correlate at <b>+${corr.toFixed(2)}</b> —
       so this is largely a ranking of how often a rule is in the market, not of skill. The
       best one here is long <b>${(best.long_frac * 100).toFixed(0)}%</b> of the time. IR against
       buy-and-hold rises toward zero as a strategy approaches always-long, and zero is the
       ceiling, not a win: a combination that beats every single rule this way has not found
       an edge, it has found its way back to buy-and-hold.`
    : `On this sheet IR and time-invested correlate at <b>${corr == null ? "—" : corr.toFixed(2)}</b>,
       so the ranking is not simply a ranking of exposure — unlike the equity sheets, where it
       is. Exposure is still shown beside the IR so the two can be read together.`}</div>

  <div class="note"><b>These are walk-forward numbers, and they are lower than the older
  single-split ones.</b> An earlier report showed a handful of combinations clearing 2 of 4
  gates — <code>MININDEX or HT_DCPHASE</code> at IR +0.069 on stocks, <code>MAXINDEX gate
  MA_50</code> at +0.153 on crypto. Two things about that. The gates they cleared were
  breadth and cost headroom, never IR and never the t-statistic: across all 1,104 candidates
  in that run, those two passed <b>zero</b> times. And under walk-forward the same pairs do
  not survive — <code>MAXINDEX~TRIMA_50|gate</code>, the nearest relative that still exists,
  falls from +0.153 to −0.196, while the other is not even in the candidate set because its
  leg never earned a place on in-sample rank. The single split chose its winners with the
  test period already visible; that is what walk-forward exists to price.</div>

  <section class="sec">
    <div class="sec-head"><h2>Combination leaderboard</h2>
      <span class="sec-note">top ${sh.combos.length} of ${sh.n_combos} · luck threshold +${sh.combo_ceiling}</span></div>
    <div class="tbl-wrap"><table>
      <thead><tr><th class="l">Combination</th><th class="l">Op</th>
        <th>Long %</th><th>IR vs buy &amp; hold</th>
        <th>$10k became</th><th>P&amp;L vs B&amp;H</th>
        <th>CAGR</th><th>Breadth</th><th>t-stat</th><th class="l">Gates</th></tr></thead>
      <tbody>${sh.combos.map(r => {
        const delta = pnlDelta(r.net_pct, r.bh_pct);
        return `
        <tr><td class="l">${esc(comboName(r.rule))}</td>
          <td class="l">${esc(opLabel(r.op))}</td>
          <td class="${r.long_frac != null && r.long_frac > 0.9 ? "loss" : ""}">${
            r.long_frac == null ? "—" : (r.long_frac * 100).toFixed(0) + "%"}</td>
          <td class="${sign(r.ir_net)}">${fmtIR(r.ir_net)}</td>
          <td>${fmtMoney(grew(r.net_pct))}</td>
          <td class="${sign(delta)}">${fmtDelta(delta)}</td>
          <td>${fmtCagr(r.net_cagr)}</td>
          <td>${r.ir_hit_rate == null ? "—" : (r.ir_hit_rate * 100).toFixed(0) + "%"}</td>
          <td class="${sign(r.t_stat)}">${r.t_stat == null ? "—" : r.t_stat.toFixed(2)}</td>
          <td class="l">${gatePips(r.gates)}</td></tr>`; }).join("")}</tbody>
      <caption><b>Long %</b> is the share of bars the combination holds a long position;
      anything above 90% is flagged, because at that point it is approximately buy-and-hold
      and its IR is approaching zero for that reason rather than through skill. Operators:
      <code>or</code> takes a position if either leg does (the most exposed),
      <code>and</code> only when both agree, <code>vote</code> by majority,
      <code>gate</code> uses one leg as a filter on the other. Combinations have no
      asset-by-asset page — the sweep records their leg-correlation diagnostics rather than
      per-asset rows.</caption>
    </table></div>
  </section>`;
}

function paintBacktest() {
  const host = document.getElementById("bt-body");
  const grp = D.backtest[bf.cls], sh = sheetOf(bf.cls, bf.tf);
  if (bf.kind === "combo") { paintCombos(host, grp, sh); bindGo(host); return; }
  const best = sh.rows[0];
  const passing = sh.rows.filter(r => r.gates.every(Boolean)).length;
  host.innerHTML = `
  <div class="strip">
    <div class="stat"><span class="k">Universe</span><span class="v">${grp.n}</span>
      <span class="s">${esc(grp.label)}</span></div>
    <div class="stat"><span class="k">Out-of-sample</span><span class="v">${sh.years.toFixed(1)}y</span>
      <span class="s">${sh.folds} walk-forward folds</span></div>
    <div class="stat"><span class="k">Best rule</span>
      <span class="v ${sign(best.ir_net)}">${fmtIR(best.ir_net)}</span>
      <span class="s">${esc(best.rule)} · IR vs buy &amp; hold</span></div>
    <div class="stat"><span class="k">$10k became</span>
      <span class="v">${fmtMoney(grew(best.net_pct))}</span>
      <span class="s">vs ${fmtMoney(grew(best.bh_pct))} held · ${fmtDelta(pnlDelta(best.net_pct, best.bh_pct))}${
        pnlRatio(best.net_pct, best.bh_pct) == null ? ""
          : " · " + fmtRatio(pnlRatio(best.net_pct, best.bh_pct)) + " the profit"}</span></div>
    <div class="stat"><span class="k">Its return</span>
      <span class="v">${fmtCagr(best.net_cagr)}</span>
      <span class="s">a year, vs ${fmtCagr(best.bh_cagr)} holding</span></div>
    <div class="stat"><span class="k">Luck threshold</span><span class="v">+${sh.noise_ceiling}</span>
      <span class="s">best of ${sh.n_rules} worthless rules</span></div>
    <div class="stat"><span class="k">Cleared all gates</span><span class="v">${passing}</span>
      <span class="s">of ${sh.rows.length} rules shown</span></div>
  </div>

  <div class="note">The best rule here scores <b class="${sign(best.ir_net)}">${fmtIR(best.ir_net)}</b>,
  ${best.ir_net < sh.noise_ceiling ? `below the <b>+${sh.noise_ceiling}</b> that the best of
  ${sh.n_rules} worthless rules would reach by chance. Nothing on this sheet is distinguishable
  from luck.` : `above the <b>+${sh.noise_ceiling}</b> luck threshold for ${sh.n_rules} rules —
  worth a second look.`}
  ${best.cagr_gap != null && best.cagr_gap > 0 && best.ir_net <= 0 ? `<br><br>Note the two
  figures disagree: it returned <b>${fmtCagr(best.net_cagr)}</b> a year against buy-and-hold's
  <b>${fmtCagr(best.bh_cagr)}</b>, yet still scores negative. That is what a losing rule looks
  like in a rising market — more return bought with more risk. The IR is the one to trust.` : ""}</div>

  <section class="sec">
    <div class="sec-head"><h2>Leaderboard</h2>
      <span class="sec-note">tap a rule for its asset-by-asset breakdown</span></div>
    <div class="tbl-wrap"><table>
      <thead><tr><th class="l">Rule</th><th>IR vs buy &amp; hold</th>
        <th>$10k became</th><th>P&amp;L vs B&amp;H</th>
        <th>CAGR</th><th>vs B&amp;H</th>
        <th>Breadth</th><th>t-stat</th><th class="l">Gates</th></tr></thead>
      <tbody>${sh.rows.map(r => {
        const delta = pnlDelta(r.net_pct, r.bh_pct);
        return `
        <tr data-go="#/backtest/${bf.cls}/${bf.tf}/${slug(r.rule)}">
          <td class="l">${esc(r.rule)}</td>
          <td class="${sign(r.ir_net)}">${fmtIR(r.ir_net)}</td>
          <td>${fmtMoney(grew(r.net_pct))}</td>
          <td class="${sign(delta)}">${fmtDelta(delta)}</td>
          <td>${fmtCagr(r.net_cagr)}</td>
          <td class="${sign(r.cagr_gap)}">${r.cagr_gap == null ? "—"
            : (r.cagr_gap >= 0 ? "+" : "−") + Math.abs(r.cagr_gap).toFixed(2) + " pp"}</td>
          <td>${(r.ir_hit_rate * 100).toFixed(0)}%</td>
          <td class="${sign(r.t_stat)}">${r.t_stat.toFixed(2)}</td>
          <td class="l">${gatePips(r.gates)}</td></tr>`; }).join("")}</tbody>
      <caption>Benchmark for this whole sheet: <b>$10,000 held becomes
      ${fmtMoney(grew(best.bh_pct))}</b>, ${fmtCagr(best.bh_cagr)} a year over
      ${sh.years.toFixed(1)} out-of-sample years. It is stated once here rather than repeated
      down a column, because it is the same figure on every row.
      <b>$10k became</b> is the rule's P&amp;L on the same stake and <b>P&amp;L vs B&amp;H</b>
      is how much more or less money that is than holding. <b>CAGR</b> and <b>vs B&amp;H</b>
      say the same thing as an annual rate and a gap in points. Read all of it beside the IR
      and never alone — money made rewards being in the market, so a rule can earn more and
      still score negative because it took more risk to do it. Breadth is the share of the
      ${grp.n} assets on which the rule has a positive IR; a high IR carried by one name is a
      fitted result.</caption>
    </table></div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Universe</h2>
      <span class="sec-note">${esc(grp.label)}</span></div>
    <p class="universe">${grp.universe.map(esc).join(" · ")}</p>
  </section>`;
  bindGo(host);
}

/* ---------- curve loading ----------
 * Fetched per sheet on first use and kept, so switching between rules on one sheet does
 * not re-download 1.5 MB each time. A failure is reported rather than swallowed: an empty
 * chart area with no explanation reads as "this rule has no data", which is a different
 * and wrong claim. */
const curveCache = {};
async function loadCurves(cls, tf) {
  const key = `${cls}_${tf}`;
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

function metricsSection(m, bm) {
  if (!m) return "";
  return `
  <section class="sec">
    <div class="sec-head"><h2>Performance metrics</h2>
      <span class="sec-note">strategy against buy &amp; hold, same window</span></div>
    <div class="tbl-wrap"><table>
      <thead><tr><th class="l">Metric</th><th>Strategy</th><th>Buy &amp; hold</th>
        <th class="l">What it means</th></tr></thead>
      <tbody>${METRIC_ROWS.map(([k, name, help, dp, sfx]) => `
        <tr><td class="l">${name}</td>
          <td>${mval(m, k, dp, sfx)}</td>
          <td>${bm && k in bm ? mval(bm, k, dp, sfx) : "—"}</td>
          <td class="l" style="white-space:normal;color:var(--muted);font-size:12.5px">${help}</td>
        </tr>`).join("")}</tbody>
      <caption>Trade-level statistics (profit factor, win rate, average win and loss) have no
      buy-and-hold counterpart — holding is a single trade that is still open, so its win rate
      is either 100% or 0% and its profit factor is undefined. Sharpe, Calmar and drawdown are
      computed identically for both and are directly comparable. None of these replace the
      IR: a strategy can carry a better Sharpe than the benchmark and still lose to it, which
      is exactly what most rows on this sheet do.</caption>
    </table></div>
  </section>`;
}

function backtestDetail(cls, tf, ruleSlug) {
  const grp = D.backtest[cls], sh = sheetOf(cls, tf);
  const r = sh && sh.rows.find(x => slug(x.rule) === ruleSlug);
  if (!r) return (location.hash = "#/backtest");
  const wins = r.per_asset.filter(p => p.ir > 0).length;
  const sorted = [...r.per_asset].sort((a, b) => b.ir - a.ir);

  app.innerHTML = `
  <a class="back" href="#/backtest">← backtest</a>
  <div class="hero">
    <div class="d-head"><span class="d-name">${esc(r.rule)}</span>
      <span class="chip mut">${tf}</span><span class="chip mut">${esc(grp.label)}</span></div>
    <p class="lede">Walk-forward out-of-sample over ${sh.years.toFixed(1)} years,
    ${sh.folds} folds, run independently on all ${grp.n} assets.</p>
  </div>

  <div class="strip">
    <div class="stat"><span class="k">IR vs buy &amp; hold</span>
      <span class="v ${sign(r.ir_net)}">${fmtIR(r.ir_net)}</span>
      <span class="s">mean across ${grp.n} assets</span></div>
    <div class="stat"><span class="k">Breadth</span>
      <span class="v">${wins} / ${grp.n}</span>
      <span class="s">assets with positive IR</span></div>
    <div class="stat"><span class="k">t-statistic</span>
      <span class="v ${sign(r.t_stat)}">${r.t_stat.toFixed(2)}</span>
      <span class="s">needs 2.0 to pass</span></div>
    <div class="stat"><span class="k">$10k became</span>
      <span class="v">${fmtMoney(grew(r.net_pct))}</span>
      <span class="s">vs ${fmtMoney(grew(r.bh_pct))} held · ${fmtDelta(pnlDelta(r.net_pct, r.bh_pct))}${
        pnlRatio(r.net_pct, r.bh_pct) == null ? ""
          : " · " + fmtRatio(pnlRatio(r.net_pct, r.bh_pct)) + " the profit"}</span></div>
    <div class="stat"><span class="k">Return / yr</span><span class="v">${fmtCagr(r.net_cagr)}</span>
      <span class="s">buy &amp; hold made ${fmtCagr(r.bh_cagr)}</span></div>
    <div class="stat"><span class="k">Gates</span><span class="v">${gatePips(r.gates)}</span>
      <span class="s">${r.gates.filter(Boolean).length} of 4 passed</span></div>
  </div>

  <div id="curve-host"><p class="sec-note">Loading equity curves…</p></div>

  <section class="sec">
    <div class="sec-head"><h2>Asset by asset</h2>
      <span class="sec-note">where it works and where it does not</span></div>
    <div class="tbl-wrap"><table>
      <thead><tr><th class="l">Asset</th><th>Years</th><th>IR vs buy &amp; hold</th>
        <th>$10k became</th><th>Buy &amp; hold</th><th>P&amp;L vs B&amp;H</th>
        <th>CAGR</th><th>B&amp;H CAGR</th>
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
          <td class="l"><span class="chip ${p.ir > 0 ? "run" : "halt"}">${p.ir > 0 ? "beat" : "lost"}</span></td>
        </tr>`; }).join("")}</tbody>
      <caption>Sorted by IR, best to worst. <b>Years</b> differs by asset and the money
      columns are not comparable across rows because of it — a 53-year holding period turns a
      modest rate into a large number all on its own, which is why CAGR sits beside it. The
      breadth gate asks for 70% of assets positive; this rule manages
      ${(wins / grp.n * 100).toFixed(0)}%. Verdict follows the IR, so an asset can show more
      P&amp;L than buy-and-hold and still read “lost” — it beat the price but not the risk
      taken to do it.</caption>
    </table></div>
  </section>

  <div id="asset-charts"></div>`;

  paintCurves(cls, tf, r);
}

/* Fills the two async regions once the sheet's curve file arrives. Split from
 * `backtestDetail` so the table and metrics render immediately and the charts appear when
 * ready, rather than the whole page waiting on a 1.5 MB fetch. */
async function paintCurves(cls, tf, r) {
  const host = document.getElementById("curve-host");
  const assetHost = document.getElementById("asset-charts");
  if (!host) return;
  const data = await loadCurves(cls, tf);
  if (document.getElementById("curve-host") !== host) return;   // navigated away

  if (!data || data.__error) {
    host.innerHTML = `<p class="sec-note">Equity curves unavailable${
      data && data.__error ? ` (${esc(data.__error)})` : ""}. Generate them with
      <code>python curves.py --class ${cls === "stocks" ? "us_stocks" : "crypto"} --tf ${tf}</code>
      in <code>backtest master/</code>, then re-run <code>build_web_data.py</code>.</p>`;
    return;
  }
  const c = data[r.rule];
  if (!c) {
    host.innerHTML = `<p class="sec-note">No curve stored for ${esc(r.rule)} — curves are
      generated for the top ${Object.keys(data).length} rules on each sheet.</p>`;
    return;
  }

  host.innerHTML = `
  <section class="sec">
    <div class="sec-head"><h2>Cumulative P&amp;L</h2>
      <span class="sec-note">equal-weight basket of all ${c.assets.length} assets</span></div>
    <div class="panel">${equityChart([c.curve, c.bench], [esc(r.rule), "Buy & hold"], c.dates)}
      <p class="sec-note">Both lines start at 100 and cover the same out-of-sample bars.
      <b>This basket is not the same measure as the leaderboard figure.</b> The leaderboard
      averages each asset's result; this rebalances an equal-weight basket every bar, which is
      itself a strategy and earns a diversification premium. On this sheet that shows up in
      the <em>benchmark</em>: ${fmtCagr(r.bh_cagr)} a year as an average asset,
      ${mval(c.bench_metrics, "cagr_pct", 2, "%")} as a rebalanced basket. Compare the two
      lines to each other, not to the table. The per-asset charts below carry no such effect
      and tie exactly to the table above.</p></div>
  </section>

  ${metricsSection(c.metrics, c.bench_metrics)}`;

  if (!assetHost) return;
  assetHost.innerHTML = `
  <section class="sec">
    <div class="sec-head"><h2>Per asset: strategy against holding that asset</h2>
      <span class="sec-note">solid = rule · dashed = buy &amp; hold · log scale, growth of 100</span></div>
    <div class="minis">${c.assets.map(a => {
      const m = a.metrics || {}, bm = a.bench_metrics || {};
      const delta = (m.total_pct == null || bm.total_pct == null)
        ? null : grew(m.total_pct) - grew(bm.total_pct);
      return `
      <figure class="mini-card">
        <figcaption><span class="mini-sym">${esc(a.symbol)}</span>
          <span class="mini-ir ${sign(a.ir)}">${fmtIR(a.ir)}</span></figcaption>
        ${miniChart(a.curve, a.bench)}
        <div class="mini-foot">
          <span class="mini-grew">$10k → <b class="${sign(m.total_pct)}">${
            fmtMoney(grew(m.total_pct))}</b></span>
          <span>vs hold <b class="${sign(delta)}">${fmtDelta(delta)}</b></span>
          <span>CAGR ${fmtCagr(m.cagr_pct)} vs ${fmtCagr(bm.cagr_pct)}</span>
          <span>DD ${mval(m, "max_dd_pct", 0, "%")} vs ${mval(bm, "max_dd_pct", 0, "%")}</span>
          <span>${m.trades == null ? "—" : m.trades + " trades"} · ${
            mval(m, "win_rate_pct", 0, "% won")}</span>
        </div>
      </figure>`; }).join("")}</div>
    <p class="sec-note"><b>A rising line with a red number is not a contradiction.</b>
    <em>$10k →</em> is what the rule itself made, so it is green whenever the solid line
    ends above where it started. <em>vs hold</em> is the gap to the dashed line, and it goes
    red whenever holding the asset would have made more — which is most of them, since the
    dashed line sits above the solid one in nearly every panel here. A rule can multiply
    your money many times over and still be the wrong thing to have done.</p>
    <p class="sec-note">Sorted by IR. Each panel is one asset over its own out-of-sample
    window, so the horizontal spans differ — META has 13.6 years where JNJ has 53.6, and a
    longer line is more history rather than more success.</p>
  </section>`;
}

/* ================================ METHOD ================================ */
function methodView() {
  const r = D.research;
  app.innerHTML = `
  <div class="hero"><h1>How to read this</h1>
    <p class="lede">${esc(r.note)}</p></div>

  <section class="sec">
    <div class="sec-head"><h2>The four gates</h2>
      <span class="sec-note">all four, or it is not an edge</span></div>
    <div class="panel">${r.gates.map(g => `
      <div class="gdoc"><span class="gbadge">${g.k}</span>
        <div><div class="gname">${esc(g.name)}</div>
          <p class="gask">${esc(g.ask)}</p>
          <span class="gtgt">target ${esc(g.target)}</span></div></div>`).join("")}</div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Why information ratio, not profit</h2></div>
    <div class="panel">
      <p class="gask">Profit rewards being in the market, not being right. In a rising market
      a rule that is simply invested more often earns more with no predictive skill at all.
      Information ratio measures the average lead over buy-and-hold divided by how much that
      lead wobbles — so doing nothing different scores exactly zero, and losing to
      buy-and-hold scores negative.</p>
      <p class="gask">It also makes timeframes and asset classes comparable, which raw
      dollars never are.</p>
    </div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Why searching harder does not help</h2></div>
    <div class="panel">
      <p class="gask">Test enough rules and the best one looks good by luck alone. Each
      leaderboard quotes that luck threshold. Only two things actually move the gates:
      <b>more history</b>, and <b>lower turnover</b>. Trying more indicators raises the bar
      rather than clearing it.</p>
    </div>
  </section>`;
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
    if (!s.group) s.group = groupBySymbol[s.symbol] || (s.cls === "crypto" ? "crypto" : "etf");
  });
  D.strategies = state.strategies;
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

/* Repaint without losing what the reader has open. The accordion state lives in the DOM
 * (native <details>), so it is captured by key, the container is rewritten, and the same
 * keys are reopened — and the scroll position never moves because nothing above changes. */
function repaintPaper() {
  const host = document.getElementById("paper-body");
  if (!host || location.hash.startsWith("#/paper/")) return;
  const open = new Set([...host.querySelectorAll("details[data-key]")]
    .filter(d => d.open).map(d => d.dataset.key));
  const y = window.scrollY;
  paintPaper();
  host.querySelectorAll("details[data-key]").forEach(d => {
    if (open.has(d.dataset.key)) d.open = true;
  });
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
const NAV = [["#/paper", "Paper trading"], ["#/backtest", "Backtest"], ["#/method", "Method"]];

function render() {
  const h = location.hash || "#/paper";
  let m;
  if ((m = h.match(/^#\/paper\/(.+)$/))) paperDetail(m[1]);
  else if ((m = h.match(/^#\/backtest\/([^/]+)\/([^/]+)\/(.+)$/))) backtestDetail(m[1], m[2], m[3]);
  else if (h.startsWith("#/backtest")) backtestMaster();
  else if (h.startsWith("#/method")) methodView();
  else paperMaster();

  const active = h.startsWith("#/backtest") ? "#/backtest"
    : h.startsWith("#/method") ? "#/method" : "#/paper";
  $("#nav").innerHTML = NAV.map(([href, label]) =>
    `<a class="nav-link ${href === active ? "on" : ""}" href="${href}">${label}</a>`).join("");

  bindGo(app);
  window.scrollTo(0, 0);
}
addEventListener("hashchange", render);
render();
startLive();
connectTicks();
