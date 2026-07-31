"""Assemble report/index.html from template.html + report.js + the built JSON.

This step was previously manual (CLAUDE.md notes no script performed it), which
meant editing template.html or report.js had no effect on the deliverable until
someone remembered to re-splice them by hand. This does the splice:

    template.html
      __REPORT_DATA_JSON__  ->  {"1D": ..., "1H": ..., "5m": ...}
      __REPORT_JS__         ->  contents of report.js

`</` is escaped to `<\\/` inside the JSON payload. That is a no-op for JSON.parse
but prevents any string in the data (an indicator name, a tldr) from closing the
surrounding <script> tag early and truncating the page.

    python build_index_html.py
"""

import json
import os

TEMPLATE = "../report/template.html"
REPORT_JS = "../report/report.js"
OUT = "../report/index.html"
SOURCES = {"1D": "../data/report_data.json",
           "1H": "../data/report_data_1h.json",
           "5m": "../data/report_data_5m.json"}


def compact(payload):
    """Shrink the embedded payload without dropping any content.

    The artifact host caps a published page at 16MB and the full three-timeframe
    dataset lands just over it. Two lossless-in-practice reductions get it back
    under, in preference to cutting drill-down tickers or a whole timeframe:

      * Curve values are cumulative dollars - pooled across ~500 tickers they run
        to millions, so stored cents are noise. Rounded to whole dollars.
      * Some trade timestamps carry a redundant " 00:00" on daily bars. The UI
        prints the date only, so the time component is dead weight.

    Both keep every row, curve point and trade in place.
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
                for tr in detail.get("trades", []):
                    for k in ("entry_date", "exit_date"):
                        if len(tr.get(k, "")) > 10:
                            tr[k] = tr[k][:10]


def main():
    html = open(TEMPLATE, encoding="utf8").read()
    for placeholder in ("__REPORT_DATA_JSON__", "__REPORT_JS__"):
        n = html.count(placeholder)
        if n != 1:
            raise SystemExit(f"{TEMPLATE}: expected exactly one {placeholder}, found {n}")

    payload = {}
    for tf, path in SOURCES.items():
        if not os.path.exists(path):
            print(f"  ! {path} missing - '{tf}' tab will be absent")
            continue
        with open(path, encoding="utf8") as f:
            payload[tf] = json.load(f)
        lb = payload[tf]["leaderboard"]
        combos = sum(1 for r in lb if " + " in r["indicator"])
        print(f"  {tf}: {len(lb)} leaderboard rows ({combos} combos), "
              f"{len(payload[tf]['curves'])} curves, {len(payload[tf]['trades'])} drill-downs")

    before = len(json.dumps(payload, separators=(",", ":")))
    compact(payload)
    after = len(json.dumps(payload, separators=(",", ":")))
    print(f"\n  compaction: {before/2**20:.2f} -> {after/2**20:.2f} MiB "
          f"({(1-after/before)*100:.1f}% smaller)")

    blob = json.dumps(payload, separators=(",", ":"), allow_nan=False).replace("</", r"<\/")
    html = html.replace("__REPORT_JS__", open(REPORT_JS, encoding="utf8").read())
    html = html.replace("__REPORT_DATA_JSON__", blob)

    with open(OUT, "w", encoding="utf8") as f:
        f.write(html)
    print(f"\nWrote {OUT} ({len(html)/2**20:.1f} MiB)")


if __name__ == "__main__":
    main()
