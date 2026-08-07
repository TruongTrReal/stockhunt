"""Render the tracking dashboard from `results/dashboard_data.json`.

Generated, never hand-edited: refreshing the board is `build_dashboard_data.py` then this,
then republish to the same artifact URL. The page is a snapshot by necessity — a published
claude.ai artifact has no runtime capability that can reach Twelve Data, and its CSP blocks
external hosts, so nothing in the page can fetch on its own.
"""

from __future__ import annotations

import json

import fwd_config

OUT = fwd_config.RESULTS_DIR / "dashboard.html"

CSS = """
:root{
  color-scheme: light dark;
  --ground:#F4F6F5; --surface:#FFFFFF; --surface2:#EDF0EE;
  --ink:#131719; --ink2:#394246; --muted:#616B70; --rule:#E1E5E3; --rule2:#CBD2CE;
  --accent:#9A6B10; --accent-bg:#F7EFDD;
  --pos:#14664A; --pos-bg:#E2F0EA; --neg:#A32E24; --neg-bg:#F8E7E4;
  --mono:ui-monospace,"Cascadia Mono",Consolas,"SF Mono",Menlo,monospace;
  --sans:ui-sans-serif,system-ui,"Segoe UI Variable Text","Segoe UI",-apple-system,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --ground:#0C1013; --surface:#141A1D; --surface2:#1B2226;
  --ink:#E3E8E9; --ink2:#BCC5C8; --muted:#8A949A; --rule:#232A2E; --rule2:#313A3F;
  --accent:#D6A249; --accent-bg:#241C0C;
  --pos:#4FBE8C; --pos-bg:#10261D; --neg:#ED7D70; --neg-bg:#2B1613;
}}
:root[data-theme="dark"]{
  --ground:#0C1013; --surface:#141A1D; --surface2:#1B2226;
  --ink:#E3E8E9; --ink2:#BCC5C8; --muted:#8A949A; --rule:#232A2E; --rule2:#313A3F;
  --accent:#D6A249; --accent-bg:#241C0C;
  --pos:#4FBE8C; --pos-bg:#10261D; --neg:#ED7D70; --neg-bg:#2B1613;
}
:root[data-theme="light"]{
  --ground:#F4F6F5; --surface:#FFFFFF; --surface2:#EDF0EE;
  --ink:#131719; --ink2:#394246; --muted:#616B70; --rule:#E1E5E3; --rule2:#CBD2CE;
  --accent:#9A6B10; --accent-bg:#F7EFDD;
  --pos:#14664A; --pos-bg:#E2F0EA; --neg:#A32E24; --neg-bg:#F8E7E4;
}
body{background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased;}
.board{max-width:1080px;margin:0 auto;padding:40px 22px 80px;
  display:flex;flex-direction:column;gap:34px;}
p,li{color:var(--ink2);max-width:74ch;}
strong{color:var(--ink);font-weight:650;}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.15em;
  text-transform:uppercase;color:var(--accent);}
h1{font-size:clamp(25px,3.6vw,34px);line-height:1.12;letter-spacing:-.022em;
  font-weight:700;text-wrap:balance;}
h2{font-size:18px;font-weight:670;letter-spacing:-.012em;}
.head{display:flex;flex-direction:column;gap:11px;}
.meta{font-family:var(--mono);font-size:12px;color:var(--muted);
  display:flex;flex-wrap:wrap;gap:5px 20px;border-top:1px solid var(--rule);padding-top:11px;}

.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(184px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);border-radius:5px;overflow:hidden;}
.cell{background:var(--surface);padding:15px 17px;display:flex;flex-direction:column;gap:5px;}
.cell .k{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted);}
.cell .v{font-family:var(--mono);font-size:23px;font-weight:640;
  font-variant-numeric:tabular-nums;color:var(--ink);line-height:1.1;}
.cell .s{font-size:12.5px;color:var(--muted);line-height:1.35;}

section{display:flex;flex-direction:column;gap:15px;}
.sechead{display:flex;align-items:baseline;gap:14px;
  border-bottom:1.5px solid var(--ink);padding-bottom:7px;}
.sechead .tag{margin-left:auto;font-family:var(--mono);font-size:10.5px;
  letter-spacing:.11em;text-transform:uppercase;color:var(--muted);white-space:nowrap;}

.scroll{overflow-x:auto;border:1px solid var(--rule);border-radius:5px;background:var(--surface);}
table{border-collapse:collapse;width:100%;font-size:13.5px;}
th,td{padding:8px 13px;text-align:left;border-bottom:1px solid var(--rule);white-space:nowrap;}
thead th{font-family:var(--mono);font-size:10.5px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--muted);font-weight:600;
  background:var(--surface2);border-bottom:1px solid var(--rule2);}
tbody tr:last-child td{border-bottom:none;}
td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;}
td.m{font-family:var(--mono);font-size:12.5px;color:var(--ink);}
tr.hi td{background:var(--accent-bg);}
caption{caption-side:bottom;text-align:left;padding:9px 13px;font-size:12.5px;
  color:var(--muted);border-top:1px solid var(--rule);}
.neg{color:var(--neg);} .pos{color:var(--pos);}

.chip{display:inline-flex;align-items:center;font-family:var(--mono);font-size:10.5px;
  font-weight:600;letter-spacing:.06em;text-transform:uppercase;padding:2px 7px;
  border-radius:3px;white-space:nowrap;}
.chip.ok{background:var(--pos-bg);color:var(--pos);}
.chip.no{background:var(--neg-bg);color:var(--neg);}
.chip.wait{background:var(--accent-bg);color:var(--accent);}
.chip.mut{background:var(--surface2);color:var(--muted);}

.note{background:var(--surface);border:1px solid var(--rule);border-left:3px solid var(--accent);
  border-radius:5px;padding:14px 17px;}
.note p{margin:0;font-size:13.5px;}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:15px;}
.card{background:var(--surface);border:1px solid var(--rule);border-radius:5px;
  padding:15px 17px;display:flex;flex-direction:column;gap:9px;}
.card h3{font-size:14.5px;font-weight:650;color:var(--ink);}
.card p{font-size:13px;margin:0;}
ul{padding-left:19px;display:flex;flex-direction:column;gap:5px;}
li::marker{color:var(--muted);}
footer{border-top:1px solid var(--rule);padding-top:15px;font-size:12.5px;color:var(--muted);}
code{font-family:var(--mono);font-size:.88em;background:var(--surface2);
  padding:1px 5px;border-radius:3px;color:var(--ink);}
a{color:var(--accent);}
a:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
"""

JS = """
const D = window.__DASH__;
const f = (v, d = 3) => (v === null || v === undefined || Number.isNaN(v))
  ? '\\u2014' : (v >= 0 ? '+' : '\\u2212') + Math.abs(v).toFixed(d);
const pct = (v, d = 1) => (v === null || v === undefined)
  ? '\\u2014' : (v >= 0 ? '+' : '\\u2212') + Math.abs(v * 100).toFixed(d) + '%';
const cls = v => v === null || v === undefined ? '' : (v > 0 ? 'pos' : 'neg');
const el = (t, h) => { const n = document.createElement(t); n.innerHTML = h; return n; };

function table(host, cols, rows, cap) {
  const w = el('div', ''); w.className = 'scroll';
  const t = document.createElement('table');
  t.innerHTML = '<thead><tr>' + cols.map(c => `<th>${c}</th>`).join('') + '</tr></thead>'
    + '<tbody>' + rows.map(r => `<tr class="${r.hi ? 'hi' : ''}">`
      + r.cells.map(c => `<td class="${c.c || ''}">${c.v}</td>`).join('') + '</tr>').join('')
    + '</tbody>' + (cap ? `<caption>${cap}</caption>` : '');
  w.appendChild(t); host.appendChild(w);
}

// ---- research sheets
{
  const host = document.getElementById('sheets');
  const rows = D.research.map(r => ({
    hi: r.sheet === 'us_stocks_1d',
    cells: [
      { v: r.sheet, c: 'm' },
      { v: r.folds, c: 'n' },
      { v: r.oos_years.toFixed(1), c: 'n' },
      { v: r.best_rule || '\\u2014', c: 'm' },
      { v: `<span class="${cls(r.best_ir)}">${f(r.best_ir)}</span>`, c: 'n' },
      { v: `<span class="${cls(r.is1_ir)}">${f(r.is1_ir)}</span>`, c: 'n' },
      { v: r.ranking_stability === null ? '\\u2014' : r.ranking_stability.toFixed(2), c: 'n' },
      { v: `<span class="chip ${r.gates_cleared ? 'ok' : 'no'}">${r.gates_cleared} of 4</span>` },
    ],
  }));
  table(host, ['Sheet', 'Folds', 'OOS yrs', 'Best fixed rule', 'Best IR', 'IS#1 IR',
    'Rank stab.', 'Gates'],
    rows,
    'IS#1 is the honest number: the rule re-picked on each in-sample window, which is what '
    + 'you could actually have traded. Highlighted row is the only sheet with enough history '
    + 'to settle the question.');
}

// ---- gate power
{
  const host = document.getElementById('power');
  const rows = D.gate_power.map(g => ({
    hi: g.coherent,
    cells: [
      { v: g.sheet, c: 'm' },
      { v: g.oos_years.toFixed(1), c: 'n' },
      { v: g.t_gate_implies.toFixed(2), c: 'n' },
      { v: g.ceiling_5.toFixed(2), c: 'n' },
      { v: g.ceiling_96.toFixed(2), c: 'n' },
      { v: g.ceiling_327.toFixed(2), c: 'n' },
      { v: `<span class="chip ${g.coherent ? 'ok' : 'no'}">${g.coherent ? 'testable' : 'underpowered'}</span>` },
    ],
  }));
  table(host, ['Sheet', 'OOS yrs', 't-gate needs IR', 'Luck @5', 'Luck @96', 'Luck @327', 'Verdict'],
    rows,
    'The IR gate is 0.50. "Luck @N" is the score the best of N worthless rules reaches by '
    + 'chance. Where that exceeds 0.50 the gate cannot be passed meaningfully, however good a '
    + 'result looks.');
}

// ---- ETFs
{
  const host = document.getElementById('etf');
  D.etf.forEach(e => {
    const c = el('div', ''); c.className = 'card';
    c.appendChild(el('h3', `SOXL &amp; TQQQ \\u2014 ${e.timeframe}`));
    c.appendChild(el('p',
      `Best of ${e.n_rules} rules: <strong>${e.best_rule}</strong> at `
      + `<span class="${cls(e.best_ir)}">${f(e.best_ir)}</span> IR. `
      + `Re-picked each fold: <span class="${cls(e.is1_ir)}">${f(e.is1_ir)}</span>. `
      + `<span class="chip ${e.gates_cleared ? 'ok' : 'no'}">${e.gates_cleared} of 4 gates</span>`));
    const rows = e.buyhold.map(b => ({
      cells: [{ v: b.symbol, c: 'm' }, { v: pct(b.cagr), c: 'n' },
      { v: b.sharpe.toFixed(2), c: 'n' },
      { v: `<span class="neg">${pct(b.max_drawdown)}</span>`, c: 'n' }],
    }));
    table(c, ['Buy &amp; hold', 'CAGR', 'Sharpe', 'Max drawdown'], rows, null);
    host.appendChild(c);
  });
}

// ---- prereg
{
  const host = document.getElementById('prereg');
  const rows = D.prereg.map(p => ({
    cells: [
      { v: p.rule, c: 'm' },
      { v: `<span class="${cls(p.ir)}">${f(p.ir)}</span>`, c: 'n' },
      { v: p.prior },
      { v: p.contaminated ? '<span class="chip mut">seen before</span>' : '<span class="chip ok">clean</span>' },
    ],
  }));
  table(host, ['Published rule', 'IR', 'Source', 'Status'], rows,
    'Five rules fixed in advance, no free parameters, no selection \\u2014 so the luck '
    + 'threshold is only +0.15 on this sheet. All five still lose to buy-and-hold.');
}

// ---- parity
{
  const host = document.getElementById('parity');
  const rows = D.parity.map(p => ({
    hi: p.min_window >= D.pipeline.warmup_measured,
    cells: [{ v: p.rule, c: 'm' }, { v: p.min_window, c: 'n' }],
  }));
  table(host, ['Rule', 'Bars of history needed'], rows,
    `The live strategy carries ${D.pipeline.warmup_default} bars. Anything needing more than `
    + 'that would trade a different signal from the one tested, with nothing in the logs to say so.');
}

// ---- prices
{
  const host = document.getElementById('prices');
  const rows = D.prices.map(p => ({
    cells: p.error
      ? [{ v: p.symbol, c: 'm' }, { v: '\\u2014', c: 'n' }, { v: '\\u2014', c: 'n' }, { v: p.error }]
      : [{ v: p.symbol, c: 'm' },
      { v: p.close.toLocaleString(undefined, { minimumFractionDigits: 2 }), c: 'n' },
      { v: `<span class="${p.change_pct >= 0 ? 'pos' : 'neg'}">${f(p.change_pct, 2)}%</span>`, c: 'n' },
      { v: p.bar_date, c: 'm' }],
  }));
  table(host, ['Instrument', 'Last close', 'Change', 'Bar date'], rows,
    'Last fully closed daily bar at build time. The still-forming bar is deliberately excluded.');
}
"""


def build() -> None:
    data = json.loads(
        (fwd_config.RESULTS_DIR / "dashboard_data.json").read_text(encoding="utf-8"))
    p = data["pipeline"]
    stamp = data["generated_at"].replace("T", " ").replace("+00:00", " UTC")

    best_ir = max((r["best_ir"] for r in data["research"] if r["best_ir"] is not None),
                  default=None)
    total_rankable = sum(r["n_rankable"] for r in data["research"])
    coherent = [g["sheet"] for g in data["gate_power"] if g["coherent"]]

    html = f"""<title>Forward Test Tracker — StockHunt</title>
<style>{CSS}</style>
<div class="board">

  <header class="head">
    <div class="eyebrow">StockHunt · forward test · status board</div>
    <h1>Pipeline is built and running. No strategy is deployed, because none has earned it.</h1>
    <p>Backtest, paper and live all run through one Nautilus code path. The research behind it
    has now tested {total_rankable:,} rule-sheet combinations across twelve sheets, walk-forward,
    and <strong>not one has cleared the acceptance gates</strong>. This board tracks both halves:
    what the plumbing is doing, and what the evidence says.</p>
    <div class="meta">
      <span>Generated {stamp}</span>
      <span>Snapshot, not a live feed — refresh below</span>
      <span>Timeframes {" · ".join(p["timeframes"])}</span>
    </div>
  </header>

  <div class="strip">
    <div class="cell"><span class="k">Strategies deployed</span><span class="v">0</span>
      <span class="s">Nothing has cleared the gates</span></div>
    <div class="cell"><span class="k">Gates cleared</span><span class="v">0 of 4</span>
      <span class="s">On every sheet, every rule</span></div>
    <div class="cell"><span class="k">Best result anywhere</span>
      <span class="v neg">{best_ir:+.3f}</span>
      <span class="s">Information ratio vs buy-and-hold; 0 means a tie</span></div>
    <div class="cell"><span class="k">Statistically testable</span>
      <span class="v">{len(coherent)} of {len(data["gate_power"])}</span>
      <span class="s">{", ".join(coherent) or "none"} — the rest lack history</span></div>
  </div>

  <div class="note"><p><strong>How to read the headline number.</strong> Everything is scored as
  information ratio against simply buying and holding the same asset. Zero means matching
  buy-and-hold; positive means beating it. Every number on this board is negative, so the honest
  summary is that no tested rule has beaten holding the asset after real costs.</p></div>

  <section>
    <div class="sechead"><h2>Forward-test pipeline</h2><span class="tag">what is wired up</span></div>
    <div class="grid2">
      <div class="card"><h3>Market data <span class="chip ok">verified</span></h3>
        <p>Twelve Data, Pro plan. Bars are polled shortly after each close rather than streamed —
        at 1d and 4h a strategy decides once or twice a day, so a tick feed would be thousands of
        messages to discard. The still-forming bar is dropped on every read; acting on it would be
        trading a close that has not happened.</p></div>
      <div class="card"><h3>Warm-up parity <span class="chip ok">measured</span></h3>
        <p>A live strategy recomputes indicators over a rolling buffer; the backtest sees the whole
        history. For recursive indicators those differ. Measured worst case is
        <strong>{p["warmup_measured"]} bars</strong>; the strategy now carries
        {p["warmup_default"]}. The previous default of 750 was too small and would have traded a
        silently different signal.</p></div>
      <div class="card"><h3>Simulated fills <span class="chip wait">not yet exercised</span></h3>
        <p>Nautilus <code>SandboxExecutionClient</code>, funded 100,000 USD, filling from bars. The
        node starts, warms up 1,500 bars and subscribes cleanly, but no order has been placed yet —
        a daily bar closes once a day. Proving the order path is the next step.</p></div>
      <div class="card"><h3>Path to live <span class="chip mut">one line</span></h3>
        <p>Going live swaps the execution client for Binance or IBKR. Strategy, data client and the
        signal layer are untouched, which is what makes backtest, paper and live the same system
        rather than three that drift apart.</p></div>
    </div>
  </section>

  <section>
    <div class="sechead"><h2>Research verdict by sheet</h2><span class="tag">walk-forward, after costs</span></div>
    <div id="sheets"></div>
  </section>

  <section>
    <div class="sechead"><h2>Can the question even be settled?</h2><span class="tag">statistical power</span></div>
    <p>Searching many rules and reporting the best is an extreme-value problem: the winner of a big
    search scores well by luck alone. Where that luck threshold sits above the acceptance gate, the
    sheet cannot produce provable evidence no matter what is run on it. Only daily US equities —
    after the history fix that took it from 24 to 54 folds — clears that bar.</p>
    <div id="power"></div>
  </section>

  <section>
    <div class="sechead"><h2>Leveraged ETFs</h2><span class="tag">SOXL &amp; TQQQ, as requested</span></div>
    <p>The concern with a 3x daily-reset ETF is volatility decay making buy-and-hold a weak
    benchmark, which would let a rule look good simply by sitting out. Measured, that did not
    happen: both ETFs score the same risk-adjusted return as SPY. What the leverage bought was a
    <strong>90% drawdown</strong>. No indicator improved on holding them.</p>
    <div class="grid2" id="etf"></div>
  </section>

  <section>
    <div class="sechead"><h2>Published strategies, tested honestly</h2><span class="tag">five, fixed in advance</span></div>
    <p>Rather than searching harder, this tests a small set of well-known published rules with no
    parameters to tune and no selection — which drops the luck threshold from +0.36 to +0.15 on
    53 years of daily data. It is the most statistically powerful test in the project.</p>
    <div id="prereg"></div>
  </section>

  <section>
    <div class="sechead"><h2>Live signal parity</h2><span class="tag">the bug this caught</span></div>
    <div id="parity"></div>
  </section>

  <section>
    <div class="sechead"><h2>Market snapshot</h2><span class="tag">at build time</span></div>
    <div id="prices"></div>
  </section>

  <footer>
    Refresh this board by running <code>python build_dashboard_data.py</code> then
    <code>python build_dashboard.py</code> in <code>forward test/</code>, and republishing to the
    same URL. A published page cannot fetch market data itself — there is no runtime capability
    for it and the page&rsquo;s content policy blocks outside hosts — so freshness comes from
    regenerating, which can be put on a schedule.
  </footer>
</div>
<script>window.__DASH__ = {json.dumps(data, separators=(",", ":"))};</script>
<script>{JS}</script>
"""
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT}  ({len(html):,} bytes)")


if __name__ == "__main__":
    build()
