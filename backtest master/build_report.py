"""Assemble report/index.html from template.html + report.js + a payload JSON.

This is the step the earlier studies never scripted — `test research/` treats it as a
manual operation, which is how `index.html` there drifted from its own template. Here it
is one command:

    python build_report.py                    # data/report_payload.json -> report/index.html
    python build_report.py --demo             # synthesise a payload first (layout check)

The payload contract is documented in `payload_schema()` below and is the *only* coupling
between the sweep and the page. Anything the page needs must be computed in Python and
land in that structure — `report.js` never re-derives a metric, so the page and the CSVs
cannot disagree about whether a gate passed.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def _sanitize(obj):
    """Replace non-finite floats with None, recursively. See the note at json.dumps."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj

HERE = Path(__file__).resolve().parent
REPORT_DIR = HERE / "report"
# The payload is a build artifact, so it lives beside the template that consumes it.
# It used to sit in `data/`, which is now the repo-wide price cache and holds no
# per-study output at all.
PAYLOAD_PATH = REPORT_DIR / "report_payload.json"
OUT_PATH = REPORT_DIR / "index.html"

TEMPLATE_PLACEHOLDER = "__REPORT_DATA_JSON__"
JS_PLACEHOLDER = "__REPORT_JS__"


def payload_schema() -> str:
    """The contract, spelled out. Kept next to the assembler so it stays honest."""
    return """
    {
      "classes": [ {key, label, noun, n_assets, cost_grid[], headline_cost, tldr} ],
      "timeframes": ["1d","4h","2h","1h","15m","5m","1m"],
      "timeframe_tldr": {tf: str},
      "gates": [ {key, label, letter, target} ],          # exactly 4, in display order
      "panels": {
        "<class>|<timeframe>|<cost_bps>": {
          "meta": {start_date, end_date, n_tickers, n_bars,
                   capital_per_ticker, n_indicators, n_generic_fallback},
          "leaderboard": [ {
              rank, indicator, is_baseline, generic_fallback, tldr, n_tickers,
              gates_passed, gate_ir, gate_breadth, gate_headroom, gate_t,
              ir_net, ir_hit_rate, headroom, t_stat, loo_retention,
              total_pnl_dollars, avg_cagr, avg_sharpe, profit_factor,
              trade_win_rate, avg_max_drawdown, turnover_per_year,
              n_trades, avg_holding_days
          } ],
          "curves": {indicator: [[date, cum_pnl], ...]},
          "benchmark_curve": [[date, cum_pnl], ...],
          "trades": {indicator: {ticker: {stats:{}, curve:[], trades:[]}}}
        }
      }
    }
    """


def build(payload: dict) -> str:
    template = (REPORT_DIR / "template.html").read_text(encoding="utf-8")
    js = (REPORT_DIR / "report.js").read_text(encoding="utf-8")

    for placeholder, where in ((TEMPLATE_PLACEHOLDER, "data"), (JS_PLACEHOLDER, "script")):
        if placeholder not in template:
            raise SystemExit(f"template.html is missing the {where} placeholder {placeholder}")

    # Nothing here can rely on the file being *served* as UTF-8: it is opened from
    # disk, from a static host, and embedded in an artifact wrapper whose <head> we
    # do not control. A page with no declarable charset that contains a raw U+2192
    # renders as "â†'". So the output is forced to pure ASCII — but the two regions
    # need different escapes, and they are not interchangeable:
    #   * HTML text  -> numeric character references (&#8594;)
    #   * JS source  -> backslash escapes (→); an entity inside a string
    #                   literal would render as the literal text "&#8594;"
    # The template is escaped BEFORE substitution so the JS and payload, already
    # ASCII by then, are not double-escaped.
    template = template.encode("ascii", "xmlcharrefreplace").decode("ascii")
    js = js.encode("ascii", "backslashreplace").decode("ascii")

    # ensure_ascii (the default) escapes non-ASCII in the payload the same way.
    # separators because the payload dominates file size and every space is
    # multiplied by ~50 panels.
    #
    # allow_nan=False is deliberate and load-bearing: NaN/Infinity are not valid JSON
    # and would break JSON.parse in the browser. It has already caught a real case —
    # compounding over 3.3M crypto minute bars overflows float64 to inf. Sanitising
    # first means a legitimate non-finite value becomes null instead of aborting the
    # build, while allow_nan=False still fails loudly if anything slips past.
    blob = json.dumps(_sanitize(payload), separators=(",", ":"), allow_nan=False)
    # A literal "</script>" anywhere in the data would close the tag early and
    # break the page silently — the browser would parse the rest as HTML.
    blob = blob.replace("</", "<\\/")

    html = template.replace(JS_PLACEHOLDER, js).replace(TEMPLATE_PLACEHOLDER, blob)

    non_ascii = {c for c in html if ord(c) > 127}
    if non_ascii:
        raise SystemExit(f"output is not pure ASCII: {sorted(non_ascii)[:10]}")
    return html


def main() -> None:
    if "--demo" in sys.argv:
        import make_demo_payload
        payload = make_demo_payload.build()
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        PAYLOAD_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        print(f"demo payload -> {PAYLOAD_PATH}")
    else:
        if not PAYLOAD_PATH.exists():
            raise SystemExit(
                f"{PAYLOAD_PATH} not found. Run the sweep first, or "
                f"`python build_report.py --demo` to check the layout.")
        payload = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))

    html = build(payload)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    size_mb = OUT_PATH.stat().st_size / 1e6
    print(f"wrote {OUT_PATH} ({size_mb:.1f} MB, {len(payload['panels'])} panels)")


if __name__ == "__main__":
    main()
