(function () {
  "use strict";

  var PAYLOAD = JSON.parse(document.getElementById("report-data").textContent);
  // Panels are keyed "<class>|<timeframe>|<cost_bps>" — a flat key rather than a
  // nested object so a missing combination is one lookup miss instead of three.
  var ALL_DATA = PAYLOAD.panels;
  var CLASSES = PAYLOAD.classes;          // [{key, label, n_assets, cost_grid, headline_cost, tldr}]
  var TIMEFRAMES = PAYLOAD.timeframes;    // ["1d","4h","2h","1h","15m","5m","1m"]
  var TIMEFRAME_TLDR = PAYLOAD.timeframe_tldr;
  var GATES = PAYLOAD.gates;              // [{key, label, letter, target}]

  // Leaderboard rows arrive as arrays against PAYLOAD.row_fields, and descriptions from
  // one shared table — with ~1,300 rows across 52 panels, repeating the key names and
  // the tldr text per row tripled the file. Hydrate once, here, so nothing downstream
  // has to know the wire format.
  (function hydrate() {
    var fields = PAYLOAD.row_fields;
    if (!fields) return;                  // demo payloads ship plain objects
    var tldr = PAYLOAD.tldr || {};
    Object.keys(ALL_DATA).forEach(function (key) {
      ALL_DATA[key].leaderboard = ALL_DATA[key].leaderboard.map(function (arr) {
        var row = {};
        for (var i = 0; i < fields.length; i++) row[fields[i]] = arr[i];
        row.tldr = tldr[row.indicator] || "";
        return row;
      });
    });
  })();

  // A synthetic payload must announce itself. Fabricated numbers in a page that
  // looks exactly like the real report is the single easiest way for a layout
  // check to get quoted as a result.
  if (PAYLOAD.demo) {
    var warn = document.createElement("div");
    warn.style.cssText = "background:var(--loss);color:#fff;padding:10px 20px;" +
      "font-family:ui-monospace,Consolas,monospace;font-size:13px;font-weight:600;" +
      "text-align:center;letter-spacing:0.02em;";
    warn.textContent = "LAYOUT CHECK - every number on this page is synthetic. " +
      "No backtest has been run. Do not quote anything here.";
    document.body.insertBefore(warn, document.body.firstChild);
  }

  var currentClass = CLASSES[0].key;
  var currentTimeframe = TIMEFRAMES[0];
  var currentCost = classSpec().headline_cost;

  function classSpec(key) {
    key = key || currentClass;
    for (var i = 0; i < CLASSES.length; i++) if (CLASSES[i].key === key) return CLASSES[i];
    return CLASSES[0];
  }
  function panelKey(cls, tf, cost) { return cls + "|" + tf + "|" + cost; }
  function panelFor(cls, tf, cost) { return ALL_DATA[panelKey(cls, tf, cost)]; }

  var DATA = panelFor(currentClass, currentTimeframe, currentCost);
  // 8 hues is the full validated categorical set (dataviz palette) for a
  // line chart's adjacent-pair colorblind-safety gate - going past 8 would
  // mean unvalidated colors, so the overlay chart caps there even though
  // the scoreboard lists more.
  var SERIES_COLORS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)", "var(--s7)", "var(--s8)"];
  var TOP_N_SCOREBOARD = 10;
  var TOP_N_CHART = 8;

  // ---------------------------------------------------------------- utils
  // Must survive two things the equity sheets never produced: a null (the value was
  // non-finite and got sanitised out — compounding overflows float64 on 3.3M-bar
  // series) and magnitudes far past "M". Rendering null as "$0" would claim a rule
  // made nothing when the truth is that its equity is unrepresentable, and a bare
  // "e+21M" suffix breaks the card grid.
  function fmtMoneyCompact(n) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    if (!isFinite(n)) return n > 0 ? "overflow" : "-overflow";
    var sign = n < 0 ? "-" : "";
    var abs = Math.abs(n);
    if (abs >= 1e15) return sign + "$" + abs.toExponential(1).replace("e+", "e");
    if (abs >= 1e12) return sign + "$" + (abs / 1e12).toFixed(2) + "T";
    if (abs >= 1e9) return sign + "$" + (abs / 1e9).toFixed(2) + "B";
    if (abs >= 1e6) return sign + "$" + (abs / 1e6).toFixed(2) + "M";
    if (abs >= 1e3) return sign + "$" + (abs / 1e3).toFixed(1) + "K";
    return sign + "$" + abs.toFixed(0);
  }
  function fmtMoneyFull(n) {
    if (n === null || n === undefined || isNaN(n) || !isFinite(n)) return "—";
    return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
  }
  // holding_days is actual elapsed calendar time (exit timestamp - entry
  // timestamp), not a bar count - so it needs an adaptive unit: a value of
  // 0.27 means ~6.4 hours (typical for an EOD-flattened intraday trade),
  // not "0 days". Never assume "d" is the right unit without checking size.
  function fmtDuration(days) {
    if (days === null || days === undefined || isNaN(days)) return "—";
    if (days >= 365) return (days / 365).toFixed(1) + "y";
    if (days >= 1) return days.toFixed(1) + "d";
    if (days * 24 >= 1) return (days * 24).toFixed(1) + "h";
    return Math.max(1, Math.round(days * 24 * 60)) + "m";
  }
  function fmtPct(x, dp) {
    if (x === null || x === undefined || isNaN(x)) return "—";
    return (x * 100).toFixed(dp === undefined ? 1 : dp) + "%";
  }
  function fmtNum(x, dp) {
    if (x === null || x === undefined || isNaN(x)) return "—";
    return x.toFixed(dp === undefined ? 2 : dp);
  }
  function fmtPF(x, nTrades) {
    if (x === null || x === undefined) return nTrades > 0 ? "∞" : "—";
    if (isNaN(x)) return "—";
    return x.toFixed(2);
  }
  function gainClass(n) { return n > 0 ? "gain" : n < 0 ? "loss" : ""; }
  function fmtX(x) {
    if (x === null || x === undefined || isNaN(x)) return "—";
    return x.toFixed(1) + "×";
  }

  // ---------------------------------------------------------------- gates
  // A row carries a boolean per gate under `gate_<key>`, precomputed by the
  // builder at the panel's own cost level — the browser never re-derives a
  // pass/fail, so the page and the CSVs can't disagree.
  function gatesPassed(row) {
    var n = 0;
    GATES.forEach(function (g) { if (row["gate_" + g.key]) n++; });
    return n;
  }
  function gatesNode(row) {
    var wrap = el("span", "gates");
    GATES.forEach(function (g) {
      var ok = row["gate_" + g.key];
      var pip = el("span", "gate-pip " + (row.is_baseline ? "none" : ok ? "pass" : "fail"), g.letter);
      pip.title = g.label + " — target " + g.target + (row.is_baseline ? "" : (ok ? " · pass" : " · fail"));
      wrap.appendChild(pip);
    });
    return wrap;
  }
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }
  // ---------------------------------------------------------------- tooltip (tldr text, generic)
  var tldrTip = el("div", "tldr-tooltip");
  document.body.appendChild(tldrTip);
  function attachTooltip(node, titleText, bodyText) {
    node.addEventListener("mouseenter", function () {
      tldrTip.innerHTML = "<div class='tldr-name'>" + titleText + "</div><div class='tldr-body'>" + bodyText + "</div>";
      tldrTip.style.display = "block";
      var rect = node.getBoundingClientRect();
      var top = rect.bottom + 8;
      var left = Math.min(rect.left, window.innerWidth - 320);
      tldrTip.style.top = top + "px";
      tldrTip.style.left = Math.max(8, left) + "px";
    });
    node.addEventListener("mouseleave", function () { tldrTip.style.display = "none"; });
    node.addEventListener("focus", function () { node.dispatchEvent(new Event("mouseenter")); });
    node.addEventListener("blur", function () { tldrTip.style.display = "none"; });
  }
  function attachTldr(node, indicatorName) {
    var row = findRow(indicatorName);
    if (!row) return;
    attachTooltip(node, indicatorName, row.tldr);
  }

  function makeActionable(node, handler) {
    node.setAttribute("tabindex", "0");
    node.setAttribute("role", "button");
    node.addEventListener("click", handler);
    node.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); handler(); }
    });
  }

  // ---------------------------------------------------------------- meta
  function renderMeta() {
    var m = DATA.meta;
    var cs = classSpec();
    var chips = [
      ["Window", m.start_date + " → " + m.end_date],
      ["Universe", m.n_tickers + " " + cs.noun],
      ["Bars", m.n_bars.toLocaleString() + "/asset"],
      ["Capital", "$" + m.capital_per_ticker.toLocaleString() + "/asset"],
      ["Fees", ((cs.cost_labels && cs.cost_labels[currentCost]) || currentCost)
        + (currentCost === cs.headline_cost ? " (headline)" : "")],
      ["Candidates", m.n_indicators.toLocaleString()],
    ];
    var wrap = document.getElementById("meta-chips");
    wrap.innerHTML = "";
    chips.forEach(function (c) {
      var span = el("span");
      span.innerHTML = c[0] + ": <b>" + c[1] + "</b>";
      wrap.appendChild(span);
    });

    document.getElementById("eyebrow-count").textContent =
      m.n_indicators.toLocaleString() + " candidates · " + m.n_tickers + " " + cs.noun +
      " · " + cs.label.toLowerCase() + " · " + currentTimeframe + " · "
      + ((cs.cost_labels && cs.cost_labels[currentCost]) || currentCost);
    // Say plainly when the page is showing a subset. A leaderboard labelled "full"
    // that silently drops 80% of the combos is the kind of thing that gets quoted.
    var truncated = m.n_combos_total && m.n_combos_shown < m.n_combos_total;
    document.getElementById("leaderboard-title").textContent =
      (truncated ? "Leaderboard — " : "Full leaderboard — ") + cs.label + ", "
      + currentTimeframe + " @ "
      + ((cs.cost_labels && cs.cost_labels[currentCost]) || currentCost) + " fees";
    var note = document.querySelector("#leaderboard-section .section-note");
    if (note) {
      note.textContent = truncated
        ? "all " + m.n_singles + " single rules + top " + m.n_combos_shown
          + " of " + m.n_combos_total.toLocaleString()
          + " combos by IR · full set in results/combo_summary_*.csv"
        : "click any column to sort · click a row for detail";
    }
    // Ranked on IR, not PnL — the payload is sorted by ir_net and the cards follow it.
    // Labelling these "top by PnL" (as the S&P-only report did) would misdescribe them:
    // the highest-PnL rule is usually just the most long-biased one.
    document.getElementById("scoreboard-title").textContent =
      "Top " + TOP_N_SCOREBOARD + " by information ratio";
    document.getElementById("chart-title").textContent =
      "Cumulative PnL — top " + TOP_N_CHART + " by IR vs. buy & hold";
    document.getElementById("chart-note").textContent =
      "portfolio-level, $" + m.capital_per_ticker.toLocaleString() +
      "/asset, summed across " + m.n_tickers + " " + cs.noun;
  }

  // A worked example of the PnL-vs-IR divergence, computed from the panel currently on
  // screen rather than written into the prose — so it can never contradict the table
  // above it after a re-run, and it adapts as the reader changes class/timeframe/fees.
  function renderWhyIR() {
    var host = document.getElementById("why-ir-live");
    if (!host) return;
    var rows = rankedRows();
    if (!rows.length) { host.innerHTML = ""; return; }

    var byPnl = rows.slice().sort(function (a, b) {
      return (b.total_pnl_dollars || -Infinity) - (a.total_pnl_dollars || -Infinity);
    })[0];
    var byIR = rows.slice().sort(function (a, b) {
      return (b.ir_net === null ? -Infinity : b.ir_net) - (a.ir_net === null ? -Infinity : a.ir_net);
    })[0];
    var base = baselineRow();
    var cs = classSpec();

    var same = byPnl.indicator === byIR.indicator;
    var html = "<div class='why-ir'><div class='hd'>worked example &mdash; this panel</div>";
    html += "<table><tbody>";
    html += "<tr><td class='k'>Richest by dollars</td><td><b>" + byPnl.indicator + "</b></td>" +
      "<td class='num'>" + fmtMoneyCompact(byPnl.total_pnl_dollars) + "</td>" +
      "<td class='num " + gainClass(byPnl.ir_net) + "'>IR " + fmtNum(byPnl.ir_net, 2) + "</td>" +
      "<td class='num'>works on " + fmtPct(byPnl.ir_hit_rate, 0) + " of " + cs.noun + "</td></tr>";
    if (!same) {
      html += "<tr><td class='k'>Best by IR</td><td><b>" + byIR.indicator + "</b></td>" +
        "<td class='num'>" + fmtMoneyCompact(byIR.total_pnl_dollars) + "</td>" +
        "<td class='num " + gainClass(byIR.ir_net) + "'>IR " + fmtNum(byIR.ir_net, 2) + "</td>" +
        "<td class='num'>works on " + fmtPct(byIR.ir_hit_rate, 0) + " of " + cs.noun + "</td></tr>";
    }
    if (base) {
      html += "<tr><td class='k'>Buy &amp; hold (free)</td><td><b>" + base.indicator + "</b></td>" +
        "<td class='num'>" + fmtMoneyCompact(base.total_pnl_dollars) + "</td>" +
        "<td class='num'>IR 0.00</td><td class='num'>&mdash;</td></tr>";
    }
    html += "</tbody></table>";

    // The punchline only holds when dollars and IR actually disagree; say whichever is true.
    var beatsOnDollars = base && byPnl.total_pnl_dollars > base.total_pnl_dollars;
    var msg;
    if (beatsOnDollars && byPnl.ir_net !== null && byPnl.ir_net < 0) {
      msg = "The dollar leader <b>beats buy-and-hold on profit and still loses on IR</b>. "
        + "Pooled dollars are carried by whichever asset trended hardest; the rule only "
        + "helps on " + fmtPct(byPnl.ir_hit_rate, 0) + " of the " + cs.noun
        + ". Rank it by profit and it is the best in the study &mdash; rank it by IR and "
        + "it is a long-bias proxy for the benchmark.";
    } else if (byPnl.ir_net !== null && byPnl.ir_net < 0) {
      msg = "The dollar leader still has a <b>negative IR</b>: it made money because the "
        + "market did, not because the rule did. Holding was better and free.";
    } else {
      msg = "Here the two orderings broadly agree &mdash; but they need not, which is why "
        + "the ranking is fixed to IR rather than to whichever number flatters.";
    }
    html += "<p style='margin:10px 0 0;font-size:13.5px'>" + msg + "</p></div>";
    host.innerHTML = html;
  }

  // Prose for each gate, keyed by the same `key` the pass/fail booleans use. The target
  // strings are NOT written here — they come from the payload, so the explanation and
  // the logic that decides green/red cannot drift apart.
  var GATE_DOC = {
    ir: {
      ask: "Does it actually beat buy-and-hold, after real fees, on data it was never " +
           "selected on?",
      why: "This is the edge itself; the other three only qualify it. Measured out of " +
           "sample, so a rule cannot pass by having been chosen for the same period it " +
           "is scored on.",
      fail: "Negative means it lost to simply holding the asset. Zero means it did " +
            "nothing the benchmark did not already do for free.",
      bug: "Above ~2.0 on a multi-year sample is nearly always look-ahead or an " +
           "accounting error, not a discovery.",
    },
    breadth: {
      ask: "On what share of the assets is the rule's IR positive?",
      why: "A strong average carried by one or two names is a fitted result, not a " +
           "broad one. On this data the dollar leader helps on under half the stocks " +
           "while topping the profit column — breadth is what exposes that.",
      fail: "Below ~55% the average is a coin flip dressed up as a strategy.",
      bug: "100% is a warning, not a triumph. A real edge is uneven across assets; " +
           "perfect breadth usually means a leak affecting every asset identically.",
    },
    headroom: {
      ask: "How many multiples of the REAL fee schedule does the edge survive?",
      why: "An edge that dies at 1.2x what you pay is not tradeable: one worse fill, a " +
           "little slippage or a fee-tier change and it is gone. Headroom is the margin " +
           "between the edge and the friction.",
      fail: "Below 1.0 it is already unprofitable at real cost. Exactly 0.0 means it " +
            "was never profitable even before fees, so no cost could have saved it.",
      bug: "The clearest case in this study: a 1-minute crypto rule with genuine gross " +
           "signal scores 0.017x — it would need trading to be ~60x cheaper than the " +
           "cheapest venue available.",
    },
    t: {
      ask: "Given how much history exists, is this distinguishable from luck?",
      why: "t = IR x sqrt(years). Significance depends on the LENGTH of the sample, not " +
           "how finely it is chopped — so the same six years at one minute gives 390x " +
           "the bars and exactly the same t.",
      fail: "Below 2 the result is inside what chance produces, however good the IR " +
            "looks in isolation.",
      bug: "Above ~6 on a sample this short is a red flag, not a triumph.",
    },
  };

  function renderGatesExplained() {
    var host = document.getElementById("gates-explained");
    if (!host) return;
    var html = "";
    GATES.forEach(function (g) {
      var doc = GATE_DOC[g.key] || {};
      html += "<div class='gate-doc'><div class='badge-lg'>" + g.letter + "</div><div>" +
        "<div class='gname'>" + g.label + "</div>" +
        "<div class='gask'>" + (doc.ask || "") + "</div><dl>" +
        "<dt>target</dt><dd class='tgt'>" + g.target + "</dd>" +
        "<dt>why it exists</dt><dd>" + (doc.why || "") + "</dd>" +
        "<dt>what a failure means</dt><dd>" + (doc.fail || "") + "</dd>" +
        "<dt>too good to be true</dt><dd class='bug'>" + (doc.bug || "") + "</dd>" +
        "</dl></div></div>";
    });
    // The fifth check is supplementary — it is not one of the four, and it is only
    // defined for a candidate that is ahead of the benchmark in the first place.
    html += "<div class='gate-doc'><div class='badge-lg' style='background:var(--fallback)'>+</div><div>" +
      "<div class='gname'>Leave-one-out (supplementary)</div>" +
      "<div class='gask'>If you delete the single best asset, how much of the IR " +
      "survives?</div><dl>" +
      "<dt>target</dt><dd class='tgt'>keep &gt;80% of the IR</dd>" +
      "<dt>why it exists</dt><dd>Breadth counts how many assets help; this measures " +
      "whether one of them is carrying the whole result. A rule that loses a third of " +
      "its IR when one name is removed is a bet on that name.</dd>" +
      "<dt>when it is blank</dt><dd>Undefined whenever the IR is negative — retention " +
      "is a fraction of the edge, and there is no edge to erode. Shown as n/a rather " +
      "than a misleading number.</dd></dl></div></div>";
    host.innerHTML = html;
  }

  // ---------------------------------------------------------------- selector bars
  function renderClassBar() {
    var bar = document.getElementById("class-bar");
    bar.innerHTML = "";
    CLASSES.forEach(function (cs) {
      var tab = el("button", "class-tab" + (cs.key === currentClass ? " active" : ""),
        cs.label + " <span class='class-count'>" + cs.n_assets + "</span>");
      tab.type = "button";
      makeActionable(tab, function () { switchClass(cs.key); });
      attachTooltip(tab, cs.label, cs.tldr);
      bar.appendChild(tab);
    });
  }

  function renderTimeframeBar() {
    var bar = document.getElementById("timeframe-bar");
    bar.innerHTML = "";
    TIMEFRAMES.forEach(function (tf) {
      if (!panelFor(currentClass, tf, currentCost)) return;
      var tab = el("button", "timeframe-tab" + (tf === currentTimeframe ? " active" : ""), tf);
      tab.type = "button";
      makeActionable(tab, function () { switchTimeframe(tf); });
      attachTooltip(tab, tf, TIMEFRAME_TLDR[tf]);
      bar.appendChild(tab);
    });
  }

  // Cost levels differ per class — crypto is charged on a taker-fee reality that
  // would be absurd for equities — so this bar is rebuilt whenever class changes.
  function renderCostBar() {
    var bar = document.getElementById("cost-bar");
    var cs = classSpec();
    bar.innerHTML = "";
    bar.appendChild(el("span", "cost-label", "fees"));
    cs.cost_grid.forEach(function (c) {
      var label = (cs.cost_labels && cs.cost_labels[c]) || c;
      var tab = el("button", "cost-tab" + (c === currentCost ? " active" : "") +
        (c === cs.headline_cost ? " headline" : ""), label);
      tab.type = "button";
      // Each scenario is a real fee schedule, not a round number: the tooltip carries
      // what it is actually made of.
      if (cs.cost_notes && cs.cost_notes[c]) attachTooltip(tab, label, cs.cost_notes[c]);
      makeActionable(tab, function () { switchCost(c); });
      bar.appendChild(tab);
    });
    bar.appendChild(el("span", "cost-label",
      "commission + half-spread per side, sell-side regulatory fees, short borrow while "
      + "short · gross is not evidence"));
  }

  // Any switch can land on a combination that was never built (a class/timeframe
  // with too little history). Fall back to something that exists rather than
  // rendering a blank page off an undefined panel.
  function resolvePanel() {
    var cs = classSpec();
    if (panelFor(currentClass, currentTimeframe, currentCost)) return;
    if (cs.cost_grid.indexOf(currentCost) === -1) currentCost = cs.headline_cost;
    if (!panelFor(currentClass, currentTimeframe, currentCost)) {
      for (var i = 0; i < TIMEFRAMES.length; i++) {
        if (panelFor(currentClass, TIMEFRAMES[i], currentCost)) { currentTimeframe = TIMEFRAMES[i]; return; }
      }
    }
  }

  function reload() {
    resolvePanel();
    DATA = panelFor(currentClass, currentTimeframe, currentCost);
    overlayHidden = {};
    selectedIndicator = rankedRows()[0].indicator;
    renderClassBar();
    renderTimeframeBar();
    renderCostBar();
    renderAll();
  }

  function switchClass(key) {
    if (key === currentClass) return;
    currentClass = key;
    currentCost = classSpec().headline_cost;
    reload();
  }
  function switchTimeframe(tf) {
    if (tf === currentTimeframe) return;
    currentTimeframe = tf;
    reload();
  }
  function switchCost(c) {
    if (c === currentCost) return;
    currentCost = c;
    reload();
  }

  // ---------------------------------------------------------------- chart engine
  // ---------------------------------------------------------------- scales
  // Cumulative PnL spans 26 years of compounding and, on the 1-minute sheets, ranges
  // from $1.2M to $7.7e24 in a single chart. On a linear axis that squashes two
  // decades into a flat line and hides the benchmark entirely. Signed log fixes it and,
  // unlike a plain log axis, survives the negative PnL that most rules produce:
  //     t(v) = sign(v) * log10(1 + |v|)
  function symlog(v) {
    return (v < 0 ? -1 : 1) * Math.log10(1 + Math.abs(v));
  }

  // Ticks chosen in dollar space (0, +/-1K, 10K, 100K, ...) and then transformed, so the
  // labels stay readable money rather than log units.
  function symlogTicks(min, max) {
    var hi = Math.max(Math.abs(min), Math.abs(max));
    var top = Math.floor(Math.log10(Math.max(hi, 1e3)));
    // On the 1-minute sheets the range runs from $1K to $1e27 — every decade would be
    // ~25 labels stacked on top of each other. Step the decades so at most ~9 land.
    var step = Math.max(1, Math.ceil((top - 3 + 1) / 9));
    var out = [0];
    for (var e = 3; e <= top; e += step) {
      var v = Math.pow(10, e);
      if (v <= max * 1.0001) out.push(v);
      if (-v >= min * 1.0001) out.push(-v);
    }
    return out.sort(function (a, b) { return a - b; });
  }

  function niceTicks(min, max, count) {
    var range = max - min || 1;
    var step = Math.pow(10, Math.floor(Math.log10(range / count)));
    var err = (range / count) / step;
    if (err >= 7.5) step *= 10;
    else if (err >= 3.5) step *= 5;
    else if (err >= 1.5) step *= 2;
    var start = Math.ceil(min / step) * step;
    var ticks = [];
    for (var v = start; v <= max; v += step) ticks.push(v);
    return ticks;
  }

  // Defensive: null/missing points (e.g. a data gap) must never reach the
  // arithmetic below as-is - JS coerces `null - x` to a number via 0, which
  // silently renders as a fake crash to zero. Carry the last known value
  // forward instead (0 before any value has been seen).
  function ffillPoints(points) {
    var last = 0;
    return points.map(function (p) {
      if (p[1] === null || p[1] === undefined || isNaN(p[1])) return [p[0], last];
      last = p[1];
      return p;
    });
  }

  // Curves are cumulative excess return over buy-and-hold, in percentage points — the
  // same difference series the IR is computed on — so they are labelled as such, not as
  // dollars.
  function fmtPP(v) {
    if (v === null || v === undefined || isNaN(v) || !isFinite(v)) return "—";
    var a = Math.abs(v);
    var dp = a >= 100 ? 0 : a >= 10 ? 1 : 2;
    return (v > 0 ? "+" : "") + v.toFixed(dp) + "pp";
  }

  function drawLineChart(container, seriesList, opts) {
    opts = opts || {};
    var valueFmt = opts.valueFmt || fmtMoneyCompact;
    var width = opts.width || container.clientWidth || 900;
    var height = opts.height || 320;
    // Narrow (phone) containers get smaller margins/type and fewer x-axis
    // labels - 5 date labels at the desktop font size will collide with
    // each other once the plot area drops much below ~300px wide.
    var isNarrow = width < 480;
    var marginL = isNarrow ? 52 : 66, marginR = 10, marginT = 14, marginB = 24;
    var axisFontSize = isNarrow ? 9 : 10.5;
    var innerW = width - marginL - marginR;
    var innerH = height - marginT - marginB;

    // A series can legitimately have no points: curves are retained only for the top
    // slice of each panel, and on the 1-minute sheets some are dropped outright because
    // compounding over 3.3M bars overflows float64. Drop those rather than letting
    // ffillPoints(undefined) throw and take the whole page down.
    seriesList = seriesList
      .filter(function (s) { return Array.isArray(s.points) && s.points.length > 1; })
      .map(function (s) { return Object.assign({}, s, { points: ffillPoints(s.points) }); });
    if (!seriesList.length) {
      container.innerHTML = "<div class='no-trades'>No excess-return curve available " +
        "for this selection.</div>";
      return;
    }
    var visible = seriesList.filter(function (s) { return s.visible !== false; });
    var allPoints = [];
    visible.forEach(function (s) { allPoints = allPoints.concat(s.points); });
    if (!allPoints.length) { container.innerHTML = ""; return; }

    var n = seriesList[0].points.length;
    var yMin = Math.min(0, Math.min.apply(null, allPoints.map(function (p) { return p[1]; })));
    var yMax = Math.max.apply(null, allPoints.map(function (p) { return p[1]; }));

    var useLog = opts.log === true;
    var tMin, tMax;
    if (useLog) {
      tMin = symlog(yMin); tMax = symlog(yMax);
      var lpad = (tMax - tMin) * 0.06 || 1;
      tMin -= lpad; tMax += lpad;
    } else {
      var pad = (yMax - yMin) * 0.08 || 1;
      yMin -= pad; yMax += pad;
      tMin = yMin; tMax = yMax;
    }

    function xAt(i) { return marginL + (n <= 1 ? 0 : (i / (n - 1)) * innerW); }
    function yAt(v) {
      var t = useLog ? symlog(v) : v;
      return marginT + innerH - ((t - tMin) / (tMax - tMin)) * innerH;
    }

    var svgns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(svgns, "svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", height);
    svg.classList.add("chart");

    // gridlines + y labels
    var yTicks = useLog ? symlogTicks(yMin, yMax) : niceTicks(yMin, yMax, 5);
    yTicks.forEach(function (t) {
      var y = yAt(t);
      if (!isFinite(y) || y < marginT - 1 || y > marginT + innerH + 1) return;
      var line = document.createElementNS(svgns, "line");
      line.setAttribute("x1", marginL); line.setAttribute("x2", width - marginR);
      line.setAttribute("y1", y); line.setAttribute("y2", y);
      line.setAttribute("stroke", "var(--border)");
      line.setAttribute("stroke-width", Math.abs(t) < 1e-6 ? 1.4 : 1);
      svg.appendChild(line);

      var label = document.createElementNS(svgns, "text");
      label.setAttribute("x", marginL - 8); label.setAttribute("y", y + 3);
      label.setAttribute("text-anchor", "end"); label.setAttribute("font-size", axisFontSize);
      label.setAttribute("fill", "var(--text-muted)");
      label.textContent = valueFmt(t);
      svg.appendChild(label);
    });

    // x labels: 5 across the date range on a normal-width chart, only 3
    // (start/middle/end) once narrow, so labels can't overlap each other.
    var dateIdxTicks = isNarrow
      ? [0, Math.floor((n - 1) * 0.5), n - 1]
      : [0, Math.floor((n - 1) * 0.25), Math.floor((n - 1) * 0.5), Math.floor((n - 1) * 0.75), n - 1];
    dateIdxTicks.forEach(function (i) {
      var label = document.createElementNS(svgns, "text");
      label.setAttribute("x", xAt(i)); label.setAttribute("y", height - 6);
      label.setAttribute("text-anchor", i === 0 ? "start" : i === n - 1 ? "end" : "middle");
      label.setAttribute("font-size", axisFontSize); label.setAttribute("fill", "var(--text-muted)");
      label.textContent = seriesList[0].points[i][0].slice(0, 7);
      svg.appendChild(label);
    });

    // series lines
    visible.forEach(function (s) {
      var d = s.points.map(function (p, i) { return (i === 0 ? "M" : "L") + xAt(i).toFixed(1) + "," + yAt(p[1]).toFixed(1); }).join(" ");
      if (s.area) {
        var areaD = d + " L" + xAt(n - 1).toFixed(1) + "," + yAt(yMin).toFixed(1) + " L" + xAt(0).toFixed(1) + "," + yAt(yMin).toFixed(1) + " Z";
        var area = document.createElementNS(svgns, "path");
        area.setAttribute("d", areaD);
        area.setAttribute("fill", s.color);
        area.setAttribute("opacity", "0.10");
        svg.appendChild(area);
      }
      var path = document.createElementNS(svgns, "path");
      path.setAttribute("d", d);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", s.color);
      path.setAttribute("stroke-width", String(s.width || 2));
      if (s.dashed) path.setAttribute("stroke-dasharray", "5,4");
      path.setAttribute("stroke-linejoin", "round");
      svg.appendChild(path);

      var last = s.points[n - 1];
      var dot = document.createElementNS(svgns, "circle");
      dot.setAttribute("cx", xAt(n - 1)); dot.setAttribute("cy", yAt(last[1]));
      dot.setAttribute("r", 3.5); dot.setAttribute("fill", s.color);
      svg.appendChild(dot);
    });

    // hover crosshair + tooltip
    var hoverLine = document.createElementNS(svgns, "line");
    hoverLine.setAttribute("y1", marginT); hoverLine.setAttribute("y2", marginT + innerH);
    hoverLine.setAttribute("stroke", "var(--text-muted)"); hoverLine.setAttribute("stroke-width", "1");
    hoverLine.setAttribute("opacity", "0"); hoverLine.setAttribute("stroke-dasharray", "3,3");
    svg.appendChild(hoverLine);

    var hitRect = document.createElementNS(svgns, "rect");
    hitRect.setAttribute("x", marginL); hitRect.setAttribute("y", marginT);
    hitRect.setAttribute("width", innerW); hitRect.setAttribute("height", innerH);
    hitRect.setAttribute("fill", "transparent");
    svg.appendChild(hitRect);

    container.innerHTML = "";
    container.appendChild(svg);

    var tooltip = container.__tooltip;
    if (!tooltip) {
      tooltip = el("div", "chart-tooltip");
      container.style.position = "relative";
      container.appendChild(tooltip);
      container.__tooltip = tooltip;
    }

    hitRect.addEventListener("mousemove", function (ev) {
      var rect = svg.getBoundingClientRect();
      var scaleX = width / rect.width;
      var mx = (ev.clientX - rect.left) * scaleX;
      var i = Math.round(((mx - marginL) / innerW) * (n - 1));
      i = Math.max(0, Math.min(n - 1, i));
      hoverLine.setAttribute("x1", xAt(i)); hoverLine.setAttribute("x2", xAt(i));
      hoverLine.setAttribute("opacity", "1");

      var rows = visible.map(function (s) {
        return "<div style='display:flex;justify-content:space-between;gap:14px;'>" +
          "<span><span style='display:inline-block;width:8px;height:8px;border-radius:50%;background:" + s.color + ";margin-right:6px;'></span>" + s.label + "</span>" +
          "<b class='tnum'>" + valueFmt(s.points[i][1]) + "</b></div>";
      }).join("");
      tooltip.innerHTML = "<div style='color:var(--text-muted);margin-bottom:4px;'>" + s_points_date(visible, i) + "</div>" + rows;
      tooltip.style.display = "block";
      var left = ev.clientX - rect.left + 14;
      if (left + 160 > rect.width) left = ev.clientX - rect.left - 174;
      tooltip.style.left = left + "px";
      tooltip.style.top = (ev.clientY - rect.top - 10) + "px";
    });
    hitRect.addEventListener("mouseleave", function () {
      hoverLine.setAttribute("opacity", "0");
      tooltip.style.display = "none";
    });

    function s_points_date(vis, i) { return vis.length ? vis[0].points[i][0] : ""; }
  }

  function drawSparkline(container, points, color) {
    if (!Array.isArray(points) || points.length < 2) { container.innerHTML = ""; return; }
    points = ffillPoints(points);
    var width = 200, height = 28;
    var svgns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(svgns, "svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("width", "100%"); svg.setAttribute("height", height);
    svg.classList.add("score-spark");
    var vals = points.map(function (p) { return p[1]; });
    var yMin = Math.min(0, Math.min.apply(null, vals)), yMax = Math.max.apply(null, vals);
    var pad = (yMax - yMin) * 0.1 || 1; yMin -= pad; yMax += pad;
    var n = points.length;
    function xAt(i) { return (n <= 1 ? 0 : (i / (n - 1)) * width); }
    function yAt(v) { return height - ((v - yMin) / (yMax - yMin)) * height; }
    var d = points.map(function (p, i) { return (i === 0 ? "M" : "L") + xAt(i).toFixed(1) + "," + yAt(p[1]).toFixed(1); }).join(" ");
    var path = document.createElementNS(svgns, "path");
    path.setAttribute("d", d); path.setAttribute("fill", "none");
    path.setAttribute("stroke", color); path.setAttribute("stroke-width", "1.6");
    svg.appendChild(path);
    container.innerHTML = "";
    container.appendChild(svg);
  }

  // ---------------------------------------------------------------- scoreboard
  function rankedRows() { return DATA.leaderboard.filter(function (r) { return !r.is_baseline; }); }
  function baselineRow() { return DATA.leaderboard.find(function (r) { return r.is_baseline; }); }

  var selectedIndicator = rankedRows()[0].indicator;

  function renderScoreboard() {
    var wrap = document.getElementById("scoreboard");
    wrap.innerHTML = "";
    rankedRows().slice(0, TOP_N_SCOREBOARD).forEach(function (row, i) {
      var card = el("div", "score-card" + (row.indicator === selectedIndicator ? " active" : ""));
      card.dataset.indicator = row.indicator;
      card.innerHTML =
        "<div class='score-rank'>RANK " + row.rank + "</div>" +
        "<div class='score-name'>" + row.indicator + "</div>" +
        "<div class='score-pnl tnum " + gainClass(row.total_pnl_dollars) + "'>" + fmtMoneyCompact(row.total_pnl_dollars) + "</div>";
      var spark = el("div");
      card.appendChild(spark);
      var badges = el("div", "score-badges");
      badges.innerHTML = "<span class='badge'>IR " + fmtNum(row.ir_net, 2) + "</span>" +
        "<span class='badge'>t " + fmtNum(row.t_stat, 2) + "</span>" +
        (row.generic_fallback ? "<span class='badge fallback'>fallback rule</span>" : "");
      badges.appendChild(gatesNode(row));
      card.appendChild(badges);
      makeActionable(card, function () { selectIndicator(row.indicator); });
      attachTldr(card.querySelector(".score-name"), row.indicator);
      wrap.appendChild(card);
      drawSparkline(spark, DATA.curves[row.indicator], SERIES_COLORS[i % SERIES_COLORS.length]);
    });
    renderBaselineCard();
  }

  function renderBaselineCard() {
    var b = baselineRow();
    var card = document.getElementById("baseline-card");
    card.innerHTML =
      "<span class='baseline-label'>Buy &amp; hold baseline (not ranked)</span>" +
      "<div class='baseline-metrics'>" +
      "<span>PnL <b class='tnum " + gainClass(b.total_pnl_dollars) + "'>" + fmtMoneyCompact(b.total_pnl_dollars) + "</b></span>" +
      "<span>CAGR <b class='tnum'>" + fmtPct(b.avg_cagr, 1) + "</b></span>" +
      "<span>Sharpe <b class='tnum'>" + fmtNum(b.avg_sharpe, 2) + "</b></span>" +
      "<span>Max DD <b class='tnum loss'>" + fmtPct(b.avg_max_drawdown, 0) + "</b></span>" +
      "</div>";
    makeActionable(card, function () { selectIndicator(b.indicator); });
  }

  // ---------------------------------------------------------------- overlay chart
  var overlayHidden = {};
  // Linear by default, as the original report was. Log is available from the toggle and
  // is the only readable option on the 1-minute sheets, where the range runs from $1M to
  // $1e24 — but it is opt-in, not imposed.
  var overlayLog = false;

  function renderScaleToggle() {
    var head = document.querySelector("#chart-section .section-head");
    var existing = document.getElementById("scale-toggle");
    if (existing) existing.remove();
    var btn = el("button", "cost-tab" + (overlayLog ? " active" : ""),
      overlayLog ? "log $" : "linear $");
    btn.id = "scale-toggle";
    btn.type = "button";
    btn.title = "Cumulative PnL spans many orders of magnitude; log keeps the early "
      + "years and the benchmark visible. Linear shows true dollar proportions.";
    makeActionable(btn, function () { overlayLog = !overlayLog; renderOverlayChart(); });
    head.appendChild(btn);
  }

  function renderOverlayChart() {
    var top = rankedRows().slice(0, TOP_N_CHART);
    var series = top.map(function (row, i) {
      return {
        key: row.indicator, label: row.indicator, color: SERIES_COLORS[i % SERIES_COLORS.length],
        points: DATA.curves[row.indicator], visible: !overlayHidden[row.indicator],
      };
    });
    series.push({
      key: "__benchmark", label: "Buy & Hold (all " + DATA.meta.n_tickers + " " + classSpec().noun + ")",
      color: "var(--text-muted)", dashed: true,
      points: DATA.benchmark_curve, visible: !overlayHidden.__benchmark,
    });
    drawLineChart(document.getElementById("overlay-chart-wrap"), series,
                  { height: 340, log: overlayLog });
    renderScaleToggle();

    var legend = document.getElementById("overlay-legend");
    legend.innerHTML = "";
    series.forEach(function (s) {
      var item = el("div", "legend-item" + (s.visible ? "" : " off"));
      var swatch = el("span", "legend-swatch" + (s.dashed ? " dashed" : ""));
      if (s.dashed) swatch.style.borderTopColor = s.color;
      else swatch.style.background = s.color;
      item.appendChild(swatch);
      item.appendChild(document.createTextNode(s.label));
      makeActionable(item, function () {
        overlayHidden[s.key] = !overlayHidden[s.key];
        renderOverlayChart();
      });
      legend.appendChild(item);
    });
  }

  // ---------------------------------------------------------------- leaderboard table
  // The four acceptance gates lead, because they are what decides the question;
  // the PnL/Sharpe columns from the earlier studies stay for diagnosis.
  // Every column carries `help`, shown on hover. Several of these numbers are easy to
  // misread on their own — most of all Total PnL, which is displayed but is NOT what
  // anything is ordered by.
  var COLUMNS = [
    { key: "rank", label: "Rank", fmt: function (v) { return v; }, cls: "rank-cell",
      help: "Position by information ratio, best first. Not by profit." },
    { key: "indicator", label: "Indicator", fmt: function (v) { return v; }, cls: "name-cell", isName: true,
      help: "The rule. Hover the name itself for what it actually trades. Names joined by "
          + "vote / and / or / gate are two-rule combinations, and both legs were "
          + "shortlisted on the training period only." },
    { key: "gates_passed", label: "Gates", isGates: true,
      help: "The four acceptance gates: I = information ratio, B = breadth, H = fee "
          + "headroom, T = t-statistic. Green passed, red failed. A candidate is only an "
          + "edge if all four are green." },
    { key: "ir_net", label: "IR net", fmt: function (v) { return fmtNum(v, 2); }, colorize: true,
      help: "Information ratio against buy-and-hold on the same asset, net of fees, out of "
          + "sample. Average lead divided by how much that lead wobbles. Zero = matched "
          + "the benchmark; negative = lost to it. Target 0.50-1.00. This is the ranking." },
    { key: "ir_hit_rate", label: "Breadth", fmt: function (v) { return fmtPct(v, 0); },
      help: "Share of assets where the rule's IR is positive. A strong average carried by "
          + "one or two names is a fitted result, not a broad one. Target 70-80%. Note "
          + "100% is a warning sign, not a triumph." },
    { key: "headroom", label: "Fee headroom", fmt: fmtX,
      help: "How many multiples of the REAL fee schedule the edge survives. 1.0x means it "
          + "dies at exactly what you would pay; 3.0x means it survives three times that. "
          + "Target 3-5x. Shows 0.0x when the rule is already unprofitable before fees — "
          + "there is no cost it could have survived." },
    { key: "t_stat", label: "t", fmt: function (v) { return fmtNum(v, 2); }, colorize: true,
      help: "t = IR x sqrt(years). The significance of the result given how much history "
          + "exists. Target 2-3. Because it scales with the square root of the SAMPLE "
          + "LENGTH, running the same span at a finer timeframe buys no significance at "
          + "all — 390x the bars, identical sqrt(years)." },
    { key: "total_pnl_dollars", label: "Total PnL", fmt: fmtMoneyCompact, colorize: true,
      help: "Pooled dollars across all assets at $10,000 each. Shown for scale — NOT what "
          + "anything is ranked by. It rewards being in the market rather than being "
          + "right, and it is dominated by whichever single asset trended hardest, so a "
          + "rule can top this column while losing to buy-and-hold on most names. See the "
          + "methodology at the bottom." },
    { key: "avg_cagr", label: "CAGR", fmt: function (v) { return fmtPct(v, 1); }, colorize: true,
      help: "Mean compound annual growth rate across assets. Absolute, so it inherits the "
          + "survivorship bias of the universe — useful for comparing rules to each other, "
          + "not as a forecast." },
    { key: "avg_sharpe", label: "Sharpe", fmt: function (v) { return fmtNum(v, 2); }, colorize: true,
      help: "Return per unit of volatility, measured against CASH — not against the "
          + "benchmark. In a rising market it largely measures how long-biased a rule is, "
          + "which is exactly why this study does not rank on it. IR is Sharpe with a "
          + "rival instead of a mattress." },
    { key: "avg_max_drawdown", label: "Max DD", fmt: function (v) { return fmtPct(v, 0); }, alwaysLoss: true,
      help: "Mean worst peak-to-trough fall across assets. Deeper is worse." },
    { key: "turnover_per_year", label: "Turn/yr", fmt: function (v) { return fmtNum(v, 0); },
      help: "Units of position change per year — the thing fees are charged on. This is "
          + "what decides fee headroom: a rule turning over thousands of times a year "
          + "needs an implausible gross edge to survive any real venue." },
    { key: "n_trades", label: "Trades", fmt: function (v) { return (v || 0).toLocaleString(); },
      help: "Total position changes summed across all assets over the whole sample." },
  ];
  var sortState = { key: "ir_net", dir: -1 };

  function renderLeaderboardHead() {
    var tr = document.getElementById("leaderboard-head");
    tr.innerHTML = "";
    COLUMNS.forEach(function (col) {
      var th = el("th", sortState.key === col.key ? "sorted" : "", col.label + (sortState.key === col.key ? (sortState.dir === -1 ? " ↓" : " ↑") : ""));
      if (col.help) attachTooltip(th, col.label, col.help);
      makeActionable(th, function () {
        if (sortState.key === col.key) sortState.dir *= -1;
        else { sortState.key = col.key; sortState.dir = col.key === "indicator" ? 1 : -1; }
        renderLeaderboardHead();
        renderLeaderboardBody();
      });
      tr.appendChild(th);
    });
  }

  function sortValue(row) {
    var v = row[sortState.key];
    if (v === null || v === undefined) {
      // null profit_factor with trades present means "no losing trades" (infinite, best);
      // null elsewhere (or PF with zero trades) means unknown/undefined - sort last.
      if (sortState.key === "profit_factor" && row.n_trades > 0) return Infinity;
      return -Infinity;
    }
    return v;
  }

  function renderLeaderboardBody() {
    var rows = rankedRows().sort(function (a, b) {
      var av = sortValue(a), bv = sortValue(b);
      if (typeof av === "string") return av.localeCompare(bv) * sortState.dir;
      return (av - bv) * sortState.dir;
    });
    var tbody = document.getElementById("leaderboard-body");
    tbody.innerHTML = "";
    rows.forEach(function (row) {
      var tr = el("tr", row.indicator === selectedIndicator ? "active" : "");
      COLUMNS.forEach(function (col) {
        var v = row[col.key];
        var colorCls = col.alwaysLoss ? "loss" : col.colorize ? gainClass(v) : "";
        var td = el("td", (col.cls || "") + " tnum" + (colorCls ? " " + colorCls : ""));
        if (col.isGates) {
          td.appendChild(gatesNode(row));
        } else if (col.isName) {
          var nameSpan = el("span", "indicator-name-hover", v);
          td.appendChild(nameSpan);
          if (row.generic_fallback) td.insertAdjacentHTML("beforeend", " <span class='badge fallback'>fallback</span>");
          attachTldr(nameSpan, row.indicator);
        } else {
          td.textContent = col.fmt(v, row);
        }
        tr.appendChild(td);
      });
      makeActionable(tr, function () { selectIndicator(row.indicator); });
      tbody.appendChild(tr);
    });
  }

  // ---------------------------------------------------------------- detail panel
  function findRow(name) {
    for (var i = 0; i < DATA.leaderboard.length; i++) if (DATA.leaderboard[i].indicator === name) return DATA.leaderboard[i];
    return null;
  }

  function renderDetail() {
    var row = findRow(selectedIndicator);
    var card = document.getElementById("detail-card");
    card.innerHTML = "";

    var head = el("div", "detail-head");
    var rankBadge = row.is_baseline
      ? "<span class='badge'>baseline — not ranked</span>"
      : "<span class='badge'>PnL Rank " + row.rank + " / " + rankedRows().length + "</span>";
    head.innerHTML = "<span class='detail-name'>" + row.indicator + "</span>" + rankBadge +
      (row.generic_fallback ? "<span class='badge fallback'>generic fallback rule</span>" : "");
    card.appendChild(head);
    attachTldr(head.querySelector(".detail-name"), row.indicator);
    card.appendChild(el("div", "tldr-inline", row.tldr));

    if (row.generic_fallback) {
      card.appendChild(el("div", "fallback-notice", "This indicator has no conventional trading direction — it uses the generic \"above its own trailing SMA\" fallback rule, not a textbook strategy. See methodology below."));
    }

    // The four gates first and on their own row — they are the verdict. Each tile
    // shows the measured value against the target it had to clear, so a fail is
    // legible without cross-referencing the methodology.
    if (!row.is_baseline) {
      var gateStats = [
        ["IR net, OOS", fmtNum(row.ir_net, 2), row.gate_ir, GATES[0].target],
        ["Breadth", fmtPct(row.ir_hit_rate, 0), row.gate_breadth, GATES[1].target],
        ["Cost headroom", fmtX(row.headroom), row.gate_headroom, GATES[2].target],
        ["t = IR×√yrs", fmtNum(row.t_stat, 2), row.gate_t, GATES[3].target],
        // Undefined when the base IR is negative — there is no positive edge for
        // dropping an asset to erode, so say that rather than printing a bare dash.
        [row.ir_net > 0 ? "Leave-one-out" : "Leave-one-out (n/a, IR<0)",
         row.ir_net > 0 ? fmtPct(row.loo_retention, 0) : "n/a",
         row.ir_net > 0 ? row.loo_retention >= 0.8 : false, ">80%"],
      ];
      var gateGrid = el("div", "stat-grid");
      gateGrid.style.gridTemplateColumns = "repeat(5, 1fr)";
      gateStats.forEach(function (s) {
        var tile = el("div", "stat-tile");
        tile.innerHTML = "<div class='stat-label'>" + s[0] + "</div>" +
          "<div class='stat-value tnum " + (s[2] ? "gain" : "loss") + "'>" + s[1] + "</div>" +
          "<div class='stat-label' style='margin:2px 0 0'>target " + s[3] + "</div>";
        gateGrid.appendChild(tile);
      });
      card.appendChild(gateGrid);
    }

    // Aggregate stats only — profit factor and win rate are trade-level and live in the
    // per-asset panel below, where they mean something.
    var stats = [
      ["Total PnL", fmtMoneyCompact(row.total_pnl_dollars), gainClass(row.total_pnl_dollars)],
      ["CAGR", fmtPct(row.avg_cagr, 1), ""],
      ["Sharpe", fmtNum(row.avg_sharpe, 2), ""],
      ["Max Drawdown", fmtPct(row.avg_max_drawdown, 0), "loss"],
      ["Turnover/yr", fmtNum(row.turnover_per_year, 0), ""],
      ["Trades", (row.n_trades || 0).toLocaleString(), ""],
      ["Assets", String(row.n_tickers), ""],
    ];
    var grid = el("div", "stat-grid");
    stats.forEach(function (s) {
      var tile = el("div", "stat-tile");
      tile.innerHTML = "<div class='stat-label'>" + s[0] + "</div><div class='stat-value tnum " + s[2] + "'>" + s[1] + "</div>";
      grid.appendChild(tile);
    });
    card.appendChild(grid);

    // Curves are only retained for the top slice of each panel — 56 panels x 231
    // rows of full curve would be a several-hundred-MB page. Rows below the cut
    // still have every scalar; they just have nothing to plot, and must say so
    // rather than calling drawLineChart with undefined points.
    var ownCurve = DATA.curves[row.indicator];
    if (ownCurve && ownCurve.length > 1) {
      var chartWrap = el("div", "chart-wrap");
      card.appendChild(chartWrap);
      drawLineChart(chartWrap, [{ key: row.indicator, label: row.indicator, color: "var(--accent)", points: ownCurve, area: true }], { height: 260 });
    } else {
      card.appendChild(el("div", "no-trades",
        "No PnL curve retained for this candidate — curves are stored for the top " +
        (DATA.meta.n_curves || "few") + " of " + DATA.meta.n_indicators.toLocaleString() +
        " by IR, to keep the page a single loadable file. Every scalar above is still " +
        "computed across all " + row.n_tickers + " " + classSpec().noun + "."));
    }

    // trade history
    var tradesForIndicator = DATA.trades[row.indicator];
    if (!tradesForIndicator) {
      card.appendChild(el("div", "no-trades", "Trade history is only computed for the top " + TOP_N_SCOREBOARD + " candidates — not available for this one. Its aggregate stats above are still computed across all " + row.n_tickers + " " + classSpec().noun + "."));
      return;
    }
    var tickers = Object.keys(tradesForIndicator);
    var tabs = el("div", "ticker-tabs");
    var tickerStatsHolder = el("div");
    var tickerChartHolder = el("div", "chart-wrap");
    var tableHolder = el("div");
    card.appendChild(tabs);
    card.appendChild(tickerStatsHolder);
    card.appendChild(tickerChartHolder);
    card.appendChild(tableHolder);

    function renderTickerChart(ticker) {
      var curve = tradesForIndicator[ticker].curve;
      if (!curve || curve.length < 2) {
        tickerChartHolder.innerHTML = "<div class='no-trades'>No cumulative PnL curve available for " + ticker + ".</div>";
        return;
      }
      drawLineChart(tickerChartHolder, [{ key: ticker, label: ticker + " cumulative PnL", color: "var(--s1)", points: curve, area: true }], { height: 200 });
    }

    function renderTickerStats(ticker) {
      var s = tradesForIndicator[ticker].stats;
      var tickerStats = [
        ["Total PnL", fmtMoneyCompact(s.total_pnl_dollars), gainClass(s.total_pnl_dollars)],
        ["CAGR", fmtPct(s.cagr, 1), ""],
        ["Sharpe", fmtNum(s.sharpe, 2), ""],
        ["Profit Factor", fmtPF(s.profit_factor, s.n_trades), ""],
        ["Win Rate", fmtPct(s.win_rate, 0), ""],
        ["Avg Win", fmtMoneyCompact(s.avg_win_dollars), "gain"],
        ["Avg Loss", fmtMoneyCompact(s.avg_loss_dollars), "loss"],
        ["Max Drawdown", fmtPct(s.max_drawdown, 0), "loss"],
        ["Trades", s.n_trades.toLocaleString(), ""],
      ];
      var grid = el("div", "stat-grid ticker-stat-grid");
      tickerStats.forEach(function (t) {
        var tile = el("div", "stat-tile");
        tile.innerHTML = "<div class='stat-label'>" + t[0] + "</div><div class='stat-value tnum " + t[2] + "'>" + t[1] + "</div>";
        grid.appendChild(tile);
      });
      tickerStatsHolder.innerHTML = "";
      tickerStatsHolder.appendChild(el("div", "ticker-stat-label", ticker + " specifically — not the " + DATA.meta.n_tickers + "-asset aggregate above"));
      tickerStatsHolder.appendChild(grid);
    }

    function renderTradeTable(ticker) {
      tableHolder.innerHTML = "";
      var trades = tradesForIndicator[ticker].trades;
      var fullCount = tradesForIndicator[ticker].stats.n_trades;
      if (!trades || !trades.length) {
        tableHolder.appendChild(el("div", "no-trades", "No trades for " + ticker + " under this rule."));
        return;
      }
      if (fullCount > trades.length) {
        tableHolder.appendChild(el("div", "no-trades", "Showing the most recent " + trades.length + " of " + fullCount.toLocaleString() + " total trades. The stats above (PF, WR, ...) reflect all " + fullCount.toLocaleString() + " — summing only the rows below will not match them."));
      }
      var table = el("table", "trades");
      table.innerHTML = "<thead><tr><th>Entry</th><th>Exit</th><th>Dir</th><th>Entry $</th><th>Exit $</th><th>PnL %</th><th>PnL $</th><th>Held</th></tr></thead>";
      var tbody = el("tbody");
      trades.slice().reverse().forEach(function (t) {
        var tr = el("tr");
        tr.innerHTML = "<td>" + t.entry_date + "</td><td>" + t.exit_date + "</td><td>" + t.direction + "</td>" +
          "<td class='tnum'>" + t.entry_price.toFixed(2) + "</td><td class='tnum'>" + t.exit_price.toFixed(2) + "</td>" +
          "<td class='tnum " + gainClass(t.pnl_pct) + "'>" + t.pnl_pct.toFixed(1) + "%</td>" +
          "<td class='tnum " + gainClass(t.pnl_dollars) + "'>" + fmtMoneyFull(t.pnl_dollars) + "</td>" +
          "<td class='tnum'>" + fmtDuration(t.holding_days) + "</td>";
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      var scrollWrap = el("div", "trades-scroll");
      scrollWrap.appendChild(table);
      tableHolder.appendChild(scrollWrap);
    }

    tickers.forEach(function (ticker, i) {
      var tab = el("div", "ticker-tab" + (i === 0 ? " active" : ""), ticker);
      makeActionable(tab, function () {
        Array.prototype.forEach.call(tabs.children, function (c) { c.classList.remove("active"); });
        tab.classList.add("active");
        renderTickerStats(ticker);
        renderTickerChart(ticker);
        renderTradeTable(ticker);
      });
      tabs.appendChild(tab);
    });
    if (tickers.length) { renderTickerStats(tickers[0]); renderTickerChart(tickers[0]); renderTradeTable(tickers[0]); }
  }

  function selectIndicator(name) {
    selectedIndicator = name;
    renderScoreboard();
    renderLeaderboardBody();
    renderDetail();
    document.getElementById("detail-section").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------------------------------------------------------------- init
  function renderAll() {
    renderMeta();
    renderWhyIR();
    renderScoreboard();
    renderOverlayChart();
    renderLeaderboardHead();
    renderLeaderboardBody();
    renderDetail();
  }

  renderGatesExplained();
  renderClassBar();
  renderTimeframeBar();
  renderCostBar();
  renderAll();
  // Charts read container.clientWidth once at render time (not reactively),
  // so a resize or phone rotation needs an explicit redraw - otherwise a
  // chart drawn at e.g. desktop width stays too wide after rotating to
  // portrait, forcing horizontal scroll on what should be a responsive page.
  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      renderOverlayChart();
      renderDetail();
    }, 150);
  });
})();
