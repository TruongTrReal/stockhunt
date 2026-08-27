"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, type Row, type Sheet, type SheetRef } from "@/lib/api";

/* THE WHOLE POINT OF THIS PAGE: every ranked rule is reachable, not the top 30.
 *
 * The old board baked its rows into `data.js`, so depth cost bytes on every load — and 94%
 * of a row's bytes are the asset-by-asset table underneath it, which only the detail page
 * ever draws. ~500 rows a sheet carried that way is 319 MB. Paging the API instead means
 * one page is one request and the depth is free.
 *
 * `n_ranked` is the last page's index and `n_rules` is NOT: `n_rules` counts the whole
 * candidate population including the rows dropped before ranking — never scored by the
 * standard, no book, never opened a position, closet trackers. Paging on `n_rules` would
 * offer pages that are structurally empty.
 */

const PAGE_SIZE = 50;

const CLASS_LABEL: Record<string, string> = {
  us_stocks: "Top 100 US stocks",
  us_etfs: "ETFs",
  crypto: "Crypto",
  commodities: "Commodities",
  cme_futures: "CME futures",
};

const pct = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(dp)}%`;
const num = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : v.toFixed(dp);

/** Colour carries one meaning on this site: gained or lost. Nothing else gets it. */
function Money({ v }: { v: number | null | undefined }) {
  if (v === null || v === undefined || Number.isNaN(v)) return <span className="mut">—</span>;
  return <span style={{ color: v >= 0 ? "var(--gain)" : "var(--loss)" }}>{pct(v)}</span>;
}

export default function ResearchPage() {
  const [sheets, setSheets] = useState<SheetRef[] | null>(null);
  const [cls, setCls] = useState("us_stocks");
  const [tf, setTf] = useState("1d");
  const [page, setPage] = useState(0);
  const [sheet, setSheet] = useState<Sheet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.sheets().then(setSheets).catch((e) => setError(String(e.message ?? e)));
  }, []);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    api
      .leaderboard(cls, tf, page * PAGE_SIZE, PAGE_SIZE)
      .then((s) => {
        if (live) setSheet(s);
      })
      .catch((e: ApiError) => {
        // A 404 is "this sheet was never scored", which is a real and expected state on a
        // timeframe a class has no run for. It is not an error worth a red box.
        if (live) {
          setSheet(null);
          setError(e.status === 404 ? null : e.message);
        }
      })
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [cls, tf, page]);

  const classes = useMemo(
    () => Array.from(new Set((sheets ?? []).map((s) => s.cls))),
    [sheets],
  );
  const timeframes = useMemo(
    () => Array.from(new Set((sheets ?? []).filter((s) => s.cls === cls).map((s) => s.tf))),
    [sheets, cls],
  );

  const pick = useCallback((next: string, which: "cls" | "tf") => {
    // A filter change invalidates the page number: page 7 of a 500-row sheet is nowhere on
    // a 60-row one, and landing on an empty page reads as "no results" rather than "you
    // were deep in the last one".
    setPage(0);
    (which === "cls" ? setCls : setTf)(next);
  }, []);

  const lastPage = sheet ? Math.max(0, Math.ceil(sheet.n_ranked / PAGE_SIZE) - 1) : 0;

  return (
    <>
      <div className="hero">
        <h1>Research</h1>
        <p className="lede">
          Every strategy run independently on each asset, walk-forward: parameters re-picked
          on each in-sample window and applied to the next. Scored as information ratio
          against buy-and-hold on the same asset — zero means matching it, positive means
          beating it. Single rules, pairs of rules and the strategies converted from outside
          this catalogue are ranked in one list; only the asset class separates them,
          because only the asset class changes the prices, the costs and the benchmark.
        </p>
      </div>

      <div className="filters wide">
        <span className="f-group">
          <span className="f-label">Asset class</span>
          <select className="fsel" value={cls} onChange={(e) => pick(e.target.value, "cls")}>
            {classes.map((c) => (
              <option key={c} value={c}>
                {CLASS_LABEL[c] ?? c}
              </option>
            ))}
          </select>
        </span>
        <span className="f-group">
          <span className="f-label">Timeframe</span>
          <select className="fsel" value={tf} onChange={(e) => pick(e.target.value, "tf")}>
            {timeframes.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </span>
      </div>

      {error && <div className="note">{error}</div>}

      {!error && !sheet && !loading && (
        <div className="note">
          No scored sheet for <b>{CLASS_LABEL[cls] ?? cls}</b> at {tf}. The verdict stage
          has not run on this cell.
        </div>
      )}

      {sheet && (
        <section className="sec">
          <div className="sec-head">
            <h2>Leaderboard</h2>
            <span className="sec-note">
              {sheet.n_ranked.toLocaleString()} ranked of {sheet.n_rules.toLocaleString()}{" "}
              candidates · {sheet.folds ?? "—"} folds · {num(sheet.years, 1)}y · ranked on{" "}
              {sheet.ranked_on}, ties on {sheet.ranked_tiebreak}
              {sheet.noise_ceiling != null && <> · luck threshold +{sheet.noise_ceiling}</>}
            </span>
          </div>

          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th className="l">#</th>
                  <th className="l">Rule</th>
                  <th>n/6</th>
                  <th>Verdict</th>
                  <th>Book vs B&amp;H</th>
                  <th>IR</th>
                  <th>t</th>
                  <th>Long %</th>
                  <th>Turnover</th>
                  <th>CAGR</th>
                  <th>B&amp;H</th>
                </tr>
              </thead>
              <tbody>
                {sheet.rows.map((r: Row, i: number) => (
                  <tr key={`${r.rule}-${i}`}>
                    <td className="l mut">{sheet.offset + i + 1}</td>
                    <td className="l">
                      {r.rule}
                      {r.kind === "pair" && <span className="chip">pair</span>}
                    </td>
                    <td>{r.edge?.passed ?? "—"}</td>
                    <td className="mut">{r.edge?.verdict ?? "—"}</td>
                    <td>
                      <Money v={r.book?.cm_excess_cagr} />
                    </td>
                    <td>{num(r.ir_net)}</td>
                    <td>{num(r.t_stat)}</td>
                    <td>{pct(r.long_frac, 0)}</td>
                    <td>{num(r.turnover, 1)}</td>
                    <td>{pct(r.net_cagr)}</td>
                    <td className="mut">{pct(r.bh_cagr)}</td>
                  </tr>
                ))}
                {loading && sheet.rows.length === 0 && (
                  <tr>
                    <td colSpan={11} className="mut">
                      Loading…
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="lb-tools" style={{ marginTop: 18 }}>
            <button className="pill" disabled={page === 0} onClick={() => setPage(0)}>
              ‹‹ first
            </button>
            <button className="pill" disabled={page === 0} onClick={() => setPage(page - 1)}>
              ‹ prev
            </button>
            <span className="sec-note">
              rows {sheet.offset + 1}–{sheet.offset + sheet.rows.length} of{" "}
              {sheet.n_ranked.toLocaleString()} · page {page + 1} of {lastPage + 1}
            </span>
            <button
              className="pill"
              disabled={page >= lastPage}
              onClick={() => setPage(page + 1)}
            >
              next ›
            </button>
            <button
              className="pill"
              disabled={page >= lastPage}
              onClick={() => setPage(lastPage)}
            >
              last ››
            </button>
          </div>
        </section>
      )}
    </>
  );
}
