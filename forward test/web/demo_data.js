/* DEMO DATA — layout review only. Nothing here is a real result.
 *
 * Shapes match what `build_dashboard_data.py` will emit, so wiring the real feed is a
 * swap of this file for a generated one, not a rewrite of the views.
 *
 * Every strategy carries two histories that must never be conflated:
 *   backtest : walk-forward out-of-sample, the research result
 *   paper    : live simulated fills since the node went up
 * They are different periods and different sample sizes; the detail view shows them
 * side by side but never adds them together.
 */
window.DEMO = true;

function walk(n, drift, vol, seed) {
  let s = seed, out = [100];
  const rnd = () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648 - 0.5;
  for (let i = 1; i < n; i++) out.push(+(out[i - 1] * (1 + drift + rnd() * vol)).toFixed(3));
  return out;
}

/* Assigned straight onto `window` — a top-level `const D` here would collide with
   app.js's own `const D`, since classic scripts share one global scope. */
window.DASH = (function () {
  const strategies = [
    { id: "soxl-1d-sma200", symbol: "SOXL", cls: "equity", tf: "1d", rule: "SMA_200",
      state: "long", status: "running", since: "2026-07-14", days: 23,
      paper_pnl_pct: 3.42, paper_trades: 4, position_units: 712, entry: 128.40,
      bt_ir: -0.072, bt_gates: [0, 0, 0, 0], bt_years: 13.5, bt_folds: 14,
      turnover: 2.1, note: "Price above its 200-day average, so long. Boring by design." },
    { id: "tqqq-1d-sma200", symbol: "TQQQ", cls: "equity", tf: "1d", rule: "SMA_200",
      state: "long", status: "running", since: "2026-07-14", days: 23,
      paper_pnl_pct: 1.88, paper_trades: 3, position_units: 1291, entry: 71.10,
      bt_ir: -0.094, bt_gates: [0, 0, 0, 0], bt_years: 13.5, bt_folds: 14,
      turnover: 2.0, note: "Same rule, second leveraged ETF." },
    { id: "spy-1d-sma200", symbol: "SPY", cls: "equity", tf: "1d", rule: "SMA_200",
      state: "long", status: "running", since: "2026-07-14", days: 23,
      paper_pnl_pct: 0.71, paper_trades: 2, position_units: 128, entry: 762.30,
      bt_ir: -0.031, bt_gates: [0, 0, 0, 0], bt_years: 26.6, bt_folds: 24,
      turnover: 1.6, note: "Unlevered control for the two ETFs." },
    { id: "soxl-4h-ema200", symbol: "SOXL", cls: "equity", tf: "4h", rule: "EMA_200",
      state: "flat", status: "running", since: "2026-07-14", days: 23,
      paper_pnl_pct: -1.24, paper_trades: 11, position_units: 0, entry: null,
      bt_ir: -0.181, bt_gates: [0, 0, 0, 0], bt_years: 3.4, bt_folds: 4,
      turnover: 9.4, note: "Faster horizon, more whipsaw. Currently out of the market." },
    { id: "btc-1d-sma200", symbol: "BTC/USD", cls: "crypto", tf: "1d", rule: "SMA_200",
      state: "long", status: "running", since: "2026-07-14", days: 23,
      paper_pnl_pct: 2.15, paper_trades: 2, position_units: 1.482, entry: 63180.00,
      bt_ir: -0.048, bt_gates: [0, 0, 0, 0], bt_years: 5.9, bt_folds: 6,
      turnover: 1.9, note: "Crypto trades 24/7, so no session flattening." },
    { id: "eth-1d-sma200", symbol: "ETH/USD", cls: "crypto", tf: "1d", rule: "SMA_200",
      state: "short", status: "running", since: "2026-07-14", days: 23,
      paper_pnl_pct: -0.62, paper_trades: 5, position_units: -48.9, entry: 1944.00,
      bt_ir: -0.112, bt_gates: [0, 0, 0, 0], bt_years: 5.9, bt_folds: 6,
      turnover: 3.3, note: "Short leg enabled here to exercise the borrow path." },
    { id: "btc-4h-donchian", symbol: "BTC/USD", cls: "crypto", tf: "4h", rule: "DONCHIAN_55",
      state: "long", status: "warming", since: "2026-08-05", days: 1,
      paper_pnl_pct: 0.0, paper_trades: 0, position_units: 0, entry: null,
      bt_ir: -0.180, bt_gates: [0, 0, 0, 0], bt_years: 3.6, bt_folds: 4,
      turnover: 6.8, note: "Still filling its 1,500-bar warm-up buffer." },
    { id: "sol-4h-sma200", symbol: "SOL/USD", cls: "crypto", tf: "4h", rule: "SMA_200",
      state: "flat", status: "halted", since: "2026-07-28", days: 9,
      paper_pnl_pct: -2.90, paper_trades: 18, position_units: 0, entry: null,
      bt_ir: -0.264, bt_gates: [0, 0, 0, 0], bt_years: 3.6, bt_folds: 4,
      turnover: 14.2, note: "Halted: turnover ran 7x the backtest estimate." },
  ];

  strategies.forEach((s, i) => {
    s.paper_curve = walk(s.days + 1, s.paper_pnl_pct / 100 / Math.max(s.days, 1), 0.012, 7 + i * 13);
    s.bench_curve = walk(s.days + 1, 0.0012, 0.011, 91 + i * 7);
    s.bt_curve = walk(140, 0.0009, 0.02, 31 + i * 11);
    s.bt_bench = walk(140, 0.0011, 0.018, 53 + i * 5);
    s.folds = Array.from({ length: Math.min(s.bt_folds, 8) }, (_, k) => ({
      fold: k, oos_start: `${2018 + k}-01-02`,
      is_ir: +(0.6 - k * 0.07 + (k % 3) * 0.12).toFixed(2),
      oos_ir: +(-0.1 - (k % 4) * 0.13 + (k % 2) * 0.2).toFixed(2),
    }));
    s.trades = Array.from({ length: s.paper_trades }, (_, k) => ({
      ts: `2026-0${7 + (k > 4 ? 1 : 0)}-${String(14 + k * 2).padStart(2, "0")} 20:00`,
      side: k % 2 === 0 ? "BUY" : "SELL",
      qty: +(s.position_units ? Math.abs(s.position_units) / (k + 2) : 100 + k * 7).toFixed(3),
      price: +(100 + k * 3.2).toFixed(2),
      pnl: +((k % 3 === 0 ? 1 : -1) * (40 + k * 18)).toFixed(2),
    }));
  });

  return {
    generated_at: "2026-08-06 03:40 UTC",
    feed: { source: "Twelve Data", plan: "pro", status: "ok", last_bar: "2026-08-05" },
    venue: { name: "Nautilus sandbox", balance: 100000, equity: 100842.15 },
    strategies,
    research: {
      sheets_tested: 12, configs_tested: 2934, gates_cleared: 0,
      best_ir: -0.048, testable_sheets: 1, total_sheets: 12,
      gates: [
        { k: "I", name: "Information ratio", target: "0.50 – 1.00",
          ask: "Does it beat buy-and-hold, per unit of tracking error?" },
        { k: "B", name: "Breadth", target: "70 – 80%",
          ask: "Does it work on most assets, or is one name carrying it?" },
        { k: "H", name: "Cost headroom", target: "3 – 5x",
          ask: "Does the edge survive several times the real fee schedule?" },
        { k: "T", name: "t-statistic", target: "2 – 3",
          ask: "Is the sample long enough for the result to mean anything?" },
      ],
      note: "Across every sheet and every rule tested walk-forward, none has cleared all four gates. The strategies below run to prove the pipeline, not because they are expected to make money.",
    },
    backtest: buildBacktest(),
  };
})();

/* ---- backtest section: the real universes, 20 mega-caps and 10 crypto pairs ----
 * Separate from paper trading on purpose. This is years of walk-forward out-of-sample
 * history; the paper run is weeks of simulated fills. Mixing them on one screen invites
 * exactly the wrong conclusion, that a few good paper days validate a rule the research
 * says is negative.
 */
function buildBacktest() {
  const stocks = ["AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "JPM", "JNJ",
    "XOM", "UNH", "V", "PG", "HD", "MA", "CVX", "ABBV", "PEP", "KO", "WMT"];
  const crypto = ["BTC/USD", "ETH/USD", "XRP/USD", "BNB/USD", "SOL/USD", "DOGE/USD",
    "ADA/USD", "TRX/USD", "AVAX/USD", "LINK/USD"];
  const rules = ["MAXINDEX", "HT_TRENDMODE", "MININDEX", "CDLDOJI", "EMA_1000",
    "SMA_200", "LINEARREG_INTERCEPT_200", "TRIMA_200", "DONCHIAN_55", "RSI",
    "MACD", "ADX"];

  const mk = (universe, tf, seedBase, years, folds) => {
    let s = seedBase;
    const rnd = () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648;
    const rows = rules.map((rule, i) => {
      const ir = +(-0.06 - i * 0.045 - rnd() * 0.05).toFixed(3);
      const per = universe.map(sym => ({
        symbol: sym,
        ir: +(ir + (rnd() - 0.45) * 0.55).toFixed(3),
        bh_cagr: +(0.05 + rnd() * 0.35).toFixed(3),
      }));
      const hit = per.filter(p => p.ir > 0).length / per.length;
      return {
        rule, ir_net: ir, ir_hit_rate: +hit.toFixed(2),
        headroom: 0, t_stat: +(ir * Math.sqrt(years)).toFixed(2),
        turnover: +(1.5 + rnd() * 12).toFixed(1),
        gates: [0, hit >= 0.7 ? 1 : 0, 0, 0],
        per_asset: per,
        curve: walk(120, 0.0007 + rnd() * 0.0004, 0.019, 17 + i * 9),
        bench: walk(120, 0.0011, 0.017, 61 + i * 3),
      };
    });
    return { timeframe: tf, years, folds, universe, rows,
      noise_ceiling: +(2.0 / Math.sqrt(years) * 1.35).toFixed(2) };
  };

  return {
    stocks: {
      label: "Top 20 US mega-caps", n: 20, universe: stocks,
      sheets: [mk(stocks, "1d", 3, 41.0, 54), mk(stocks, "4h", 29, 3.9, 5)],
    },
    crypto: {
      label: "Top 10 crypto by market cap", n: 10, universe: crypto,
      sheets: [mk(crypto, "1d", 71, 5.9, 6), mk(crypto, "4h", 113, 3.6, 4)],
    },
  };
}
