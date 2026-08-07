"""Splice report/template.html + report/report.js + report_data_combo.json -> report/index.html.

Same assembly as `../test research/src/build_index_html.py`, against this project's
single two-timeframe payload instead of three separate files.

`</` is escaped to `<\\/` inside the JSON blob. That is a no-op for JSON.parse but stops
any string in the data — an indicator name, a tldr — from closing the surrounding
<script> tag early and silently truncating the page.

    python build_combo_index.py
"""

from __future__ import annotations

import json

TEMPLATE = "report/template.html"
REPORT_JS = "report/report.js"
SOURCE = "report_data_combo.json"
OUT = "report/index.html"


def compact(payload: dict) -> None:
    """Shrink the embedded payload without dropping a single row, curve point or trade.

    The artifact host caps a published page at 16MB. Curve values are cumulative dollars
    pooled across 20 tickers, so stored cents are noise — rounding to whole dollars is
    lossless at the resolution the chart draws. Preferred over cutting drill-down
    tickers or dropping a timeframe, both of which lose real content.
    """
    def round_curve(points):
        for p in points:
            p[1] = int(round(p[1]))

    for tf in payload.values():
        for pts in tf.get("curves", {}).values():
            round_curve(pts)
        round_curve(tf.get("benchmark_curve", []))
        for by_ticker in tf.get("trades", {}).values():
            for detail in by_ticker.values():
                round_curve(detail.get("curve", []))


def main() -> None:
    html = open(TEMPLATE, encoding="utf8").read()
    for ph in ("__REPORT_DATA_JSON__", "__REPORT_JS__"):
        n = html.count(ph)
        if n != 1:
            raise SystemExit(f"{TEMPLATE}: expected exactly one {ph}, found {n}")

    with open(SOURCE, encoding="utf8") as f:
        payload = json.load(f)

    for tf, d in payload.items():
        m = d["meta"]
        combos = sum(1 for r in d["leaderboard"] if r.get("combo_size", 1) > 1)
        print(f"  {tf}: {len(d['leaderboard'])} rows ({combos} combos), "
              f"{len(d['curves'])} curves, {len(d['trades'])} drill-downs | "
              f"best excess Sharpe {m['best_excess_sharpe']:+.3f}, "
              f"{m['n_beat_buyhold']} beat buy-&-hold")

    before = len(json.dumps(payload, separators=(",", ":")))
    compact(payload)
    blob = json.dumps(payload, separators=(",", ":"), allow_nan=False).replace("</", r"<\/")
    print(f"\n  payload {before / 2**20:.2f} -> {len(blob) / 2**20:.2f} MiB")

    html = html.replace("__REPORT_JS__", open(REPORT_JS, encoding="utf8").read())
    html = html.replace("__REPORT_DATA_JSON__", blob)
    with open(OUT, "w", encoding="utf8") as f:
        f.write(html)
    print(f"wrote {OUT} ({len(html) / 2**20:.2f} MiB)")


if __name__ == "__main__":
    main()
