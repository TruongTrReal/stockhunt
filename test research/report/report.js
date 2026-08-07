(function () {
  "use strict";

  var ALL_DATA = JSON.parse(document.getElementById("report-data").textContent);
  var TIMEFRAMES = ["1D", "1H", "5m"];
  var TIMEFRAME_TLDR = {
    "1D": "Daily bars. Positions can be held for weeks, months, or years - there is no forced exit at the end of a day. 10.5 years of history, all 501 S&P 500 stocks.",
    "1H": "Hourly bars. Positions can be held across days or weeks, same as 1D - no forced exit at day's end, just finer-grained entries/exits than daily bars. ~2 years of history (Yahoo's hourly data limit), all ~500 S&P 500 stocks.",
    "5m": "5-minute bars. Every position IS force-closed at the last bar of each trading day - zero overnight exposure, re-entered fresh the next morning if the rule still signals. Much shorter lookback than 1H/1D: Yahoo only serves 60 days of 5-minute history. All ~500 S&P 500 stocks. Treat results here as a small, noisy sample - not a settled verdict.",
  };
  var currentTimeframe = "1D";
  var DATA = ALL_DATA[currentTimeframe];
  // 8 hues is the full validated categorical set (dataviz palette) for a
  // line chart's adjacent-pair colorblind-safety gate - going past 8 would
  // mean unvalidated colors, so the overlay chart caps there even though
  // the scoreboard lists more.
  var SERIES_COLORS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)", "var(--s7)", "var(--s8)"];
  var TOP_N_SCOREBOARD = 10;
  var TOP_N_CHART = 8;

  // ---------------------------------------------------------------- utils
  function fmtMoneyCompact(n) {
    var sign = n < 0 ? "-" : "";
    var abs = Math.abs(n);
    if (abs >= 1e6) return sign + "$" + (abs / 1e6).toFixed(2) + "M";
    if (abs >= 1e3) return sign + "$" + (abs / 1e3).toFixed(1) + "K";
    return sign + "$" + abs.toFixed(0);
  }
  function fmtMoneyFull(n) {
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
    var chips = [
      ["Window", m.start_date + " → " + m.end_date],
      ["Universe", m.n_tickers + " stocks"],
      ["Capital", "$" + m.capital_per_ticker.toLocaleString() + "/stock"],
      ["Indicators", m.n_indicators + " (" + m.n_generic_fallback + " generic-fallback)"],
    ];
    var wrap = document.getElementById("meta-chips");
    wrap.innerHTML = "";
    chips.forEach(function (c) {
      var span = el("span");
      span.innerHTML = c[0] + ": <b>" + c[1] + "</b>";
      wrap.appendChild(span);
    });

    document.getElementById("eyebrow-count").textContent =
      m.n_indicators + " indicators · " + m.n_tickers + " stocks · one rule each · " + currentTimeframe;
    document.getElementById("leaderboard-title").textContent =
      "Full leaderboard — all " + m.n_indicators + " indicators (" + currentTimeframe + ")";
    document.getElementById("scoreboard-title").textContent = "Top " + TOP_N_SCOREBOARD + " by total PnL";
    document.getElementById("chart-title").textContent =
      "Cumulative PnL — top " + TOP_N_CHART + " of " + TOP_N_SCOREBOARD + " vs. buy & hold";
  }

  // ---------------------------------------------------------------- timeframe switcher
  function renderTimeframeBar() {
    var bar = document.getElementById("timeframe-bar");
    bar.innerHTML = "";
    TIMEFRAMES.forEach(function (tf) {
      if (!ALL_DATA[tf]) return;
      var tab = el("button", "timeframe-tab" + (tf === currentTimeframe ? " active" : ""), tf);
      tab.type = "button";
      makeActionable(tab, function () { switchTimeframe(tf); });
      attachTooltip(tab, tf, TIMEFRAME_TLDR[tf]);
      bar.appendChild(tab);
    });
  }

  function switchTimeframe(tf) {
    if (tf === currentTimeframe || !ALL_DATA[tf]) return;
    currentTimeframe = tf;
    DATA = ALL_DATA[tf];
    overlayHidden = {};
    selectedIndicator = rankedRows()[0].indicator;
    renderTimeframeBar();
    renderAll();
  }

  // ---------------------------------------------------------------- chart engine
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

  function drawLineChart(container, seriesList, opts) {
    opts = opts || {};
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

    seriesList = seriesList.map(function (s) { return Object.assign({}, s, { points: ffillPoints(s.points) }); });
    var visible = seriesList.filter(function (s) { return s.visible !== false; });
    var allPoints = [];
    visible.forEach(function (s) { allPoints = allPoints.concat(s.points); });
    if (!allPoints.length) { container.innerHTML = ""; return; }

    var n = seriesList[0].points.length;
    var yMin = Math.min(0, Math.min.apply(null, allPoints.map(function (p) { return p[1]; })));
    var yMax = Math.max.apply(null, allPoints.map(function (p) { return p[1]; }));
    var pad = (yMax - yMin) * 0.08 || 1;
    yMin -= pad; yMax += pad;

    function xAt(i) { return marginL + (n <= 1 ? 0 : (i / (n - 1)) * innerW); }
    function yAt(v) { return marginT + innerH - ((v - yMin) / (yMax - yMin)) * innerH; }

    var svgns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(svgns, "svg");
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", height);
    svg.classList.add("chart");

    // gridlines + y labels
    var yTicks = niceTicks(yMin, yMax, 5);
    yTicks.forEach(function (t) {
      var y = yAt(t);
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
      label.textContent = fmtMoneyCompact(t);
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
      path.setAttribute("stroke-width", "2");
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
          "<b class='tnum'>" + fmtMoneyCompact(s.points[i][1]) + "</b></div>";
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
      badges.innerHTML = "<span class='badge'>PF " + fmtPF(row.profit_factor, row.n_trades) + "</span><span class='badge'>WR " + fmtPct(row.trade_win_rate, 0) + "</span>" +
        (row.generic_fallback ? "<span class='badge fallback'>fallback rule</span>" : "");
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
  function renderOverlayChart() {
    var top = rankedRows().slice(0, TOP_N_CHART);
    var series = top.map(function (row, i) {
      return {
        key: row.indicator, label: row.indicator, color: SERIES_COLORS[i % SERIES_COLORS.length],
        points: DATA.curves[row.indicator], visible: !overlayHidden[row.indicator],
      };
    });
    series.push({
      key: "__benchmark", label: "Buy & Hold (all " + DATA.meta.n_tickers + " stocks)", color: "var(--text-muted)", dashed: true,
      points: DATA.benchmark_curve, visible: !overlayHidden.__benchmark,
    });
    drawLineChart(document.getElementById("overlay-chart-wrap"), series, { height: 340 });

    var legend = document.getElementById("overlay-legend");
    legend.innerHTML = "";
    series.forEach(function (s) {
      var item = el("div", "legend-item" + (s.visible ? "" : " off"));
      var swatch = el("span", "legend-swatch" + (s.dashed ? " dashed" : ""));
      if (!s.dashed) swatch.style.background = s.color;
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
  var COLUMNS = [
    { key: "rank", label: "Rank", fmt: function (v) { return v; }, cls: "rank-cell" },
    { key: "indicator", label: "Indicator", fmt: function (v) { return v; }, cls: "name-cell", isName: true },
    { key: "total_pnl_dollars", label: "Total PnL", fmt: fmtMoneyCompact, colorize: true },
    { key: "avg_cagr", label: "CAGR", fmt: function (v) { return fmtPct(v, 1); }, colorize: true },
    { key: "avg_sharpe", label: "Sharpe", fmt: function (v) { return fmtNum(v, 2); }, colorize: true },
    { key: "profit_factor", label: "PF", fmt: function (v, row) { return fmtPF(v, row.n_trades); } },
    { key: "trade_win_rate", label: "Win Rate", fmt: function (v) { return fmtPct(v, 0); } },
    { key: "avg_max_drawdown", label: "Max DD", fmt: function (v) { return fmtPct(v, 0); }, alwaysLoss: true },
    { key: "n_trades", label: "Trades", fmt: function (v) { return v.toLocaleString(); } },
    { key: "avg_holding_days", label: "Avg Hold", fmt: function (v) { return fmtDuration(v); } },
  ];
  var sortState = { key: "total_pnl_dollars", dir: -1 };

  function renderLeaderboardHead() {
    var tr = document.getElementById("leaderboard-head");
    tr.innerHTML = "";
    COLUMNS.forEach(function (col) {
      var th = el("th", sortState.key === col.key ? "sorted" : "", col.label + (sortState.key === col.key ? (sortState.dir === -1 ? " ↓" : " ↑") : ""));
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
        if (col.isName) {
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

    var stats = [
      ["Total PnL", fmtMoneyCompact(row.total_pnl_dollars), gainClass(row.total_pnl_dollars)],
      ["CAGR", fmtPct(row.avg_cagr, 1), ""],
      ["Sharpe", fmtNum(row.avg_sharpe, 2), ""],
      ["Profit Factor", fmtPF(row.profit_factor, row.n_trades), ""],
      ["Win Rate", fmtPct(row.trade_win_rate, 0), ""],
      ["Max Drawdown", fmtPct(row.avg_max_drawdown, 0), "loss"],
      ["Trades", row.n_trades.toLocaleString(), ""],
    ];
    var grid = el("div", "stat-grid");
    stats.forEach(function (s) {
      var tile = el("div", "stat-tile");
      tile.innerHTML = "<div class='stat-label'>" + s[0] + "</div><div class='stat-value tnum " + s[2] + "'>" + s[1] + "</div>";
      grid.appendChild(tile);
    });
    card.appendChild(grid);

    var chartWrap = el("div", "chart-wrap");
    card.appendChild(chartWrap);
    drawLineChart(chartWrap, [{ key: row.indicator, label: row.indicator, color: "var(--accent)", points: DATA.curves[row.indicator], area: true }], { height: 260 });

    // trade history
    var tradesForIndicator = DATA.trades[row.indicator];
    if (!tradesForIndicator) {
      card.appendChild(el("div", "no-trades", "Trade history is only computed for the top 10 indicators by total PnL — not available for this one. Its aggregate stats above are still computed across all " + row.n_tickers + " stocks."));
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
      tickerStatsHolder.appendChild(el("div", "ticker-stat-label", ticker + " specifically — not the " + DATA.meta.n_tickers + "-stock aggregate above"));
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
    renderScoreboard();
    renderOverlayChart();
    renderLeaderboardHead();
    renderLeaderboardBody();
    renderDetail();
  }

  renderTimeframeBar();
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
