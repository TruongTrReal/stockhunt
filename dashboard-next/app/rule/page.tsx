"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { api, type ApiError, type Curve, type RuleDetail } from "@/lib/api";
import { EquityChart } from "@/components/EquityChart";

/* THE ROUTE IS A QUERY STRING, NOT A PATH SEGMENT, and that is forced rather than chosen.
 *
 * `output: "export"` pre-renders every route at build time, so a dynamic segment needs
 * `generateStaticParams` — which here would mean enumerating ~500 rules across 19 sheets,
 * from an API that requires a session, at build time on a box that may not be able to
 * reach it. A query string is read in the browser and needs no pre-rendering at all, so
 * one static page serves every rule.
 *
 * The label itself is why this matters more than it looks: a pair is `LEG_A~LEG_B|op` and
 * an overlay is `ha:chart:ibs@buy=0.3`. Those survive a query parameter untouched; as a
 * path they meet URL normalisation, and the dashboard has already had one bug from
 * splitting a pair's name on its own operator.
 */

const pct = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${v.toFixed(dp)}%`;
const num = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : v.toFixed(dp);

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="sec-note">{label}</div>
      <div style={{ font: "20px var(--mono)", color: "var(--ink)" }}>{children}</div>
    </div>
  );
}

function RuleView() {
  const q = useSearchParams();
  const cls = q.get("cls") ?? "";
  const tf = q.get("tf") ?? "";
  const rule = q.get("rule") ?? "";

  const [curve, setCurve] = useState<Curve | null>(null);
  const [detail, setDetail] = useState<RuleDetail | null>(null);
  const [curveErr, setCurveErr] = useState<string | null>(null);
  const [detailErr, setDetailErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!cls || !tf || !rule) return;
    let live = true;
    setLoading(true);
    // Fetched in parallel and failing INDEPENDENTLY. A rule can legitimately have a curve
    // and no per-asset rows (a pair records leg diagnostics instead of per-symbol
    // backtests) or rows and no curve (its sheet was booked without `--curves`). Treating
    // either absence as a failed page would hide the half that did arrive.
    Promise.allSettled([api.curve(cls, tf, rule), api.rule(cls, tf, rule)]).then(
      ([c, d]) => {
        if (!live) return;
        if (c.status === "fulfilled") setCurve(c.value);
        else setCurveErr((c.reason as ApiError)?.message ?? "no curve");
        if (d.status === "fulfilled") setDetail(d.value);
        else setDetailErr((d.reason as ApiError)?.message ?? "no per-asset rows");
        setLoading(false);
      },
    );
    return () => {
      live = false;
    };
  }, [cls, tf, rule]);

  if (!cls || !tf || !rule) {
    return <div className="note">No rule named. Open one from the leaderboard.</div>;
  }

  const m = curve?.metrics ?? {};
  const bm = curve?.bench_metrics ?? {};

  return (
    <>
      <div className="hero">
        <Link className="back" href="/">
          ← leaderboard
        </Link>
        <h1 style={{ fontFamily: "var(--mono)", fontSize: 26 }}>{rule}</h1>
        <p className="lede">
          {cls} · {tf} · the book that held this rule across the whole universe,
          equal-weighted and rebalanced every bar, on the walk-forward out-of-sample span.
        </p>
      </div>

      {loading && <div className="note">Loading…</div>}

      {curve && (
        <section className="sec">
          <div className="sec-head">
            <h2>Equity</h2>
            <span className="sec-note">
              {curve.n_assets ?? "—"} names · {curve.dates?.length ?? 0} bars
            </span>
          </div>
          <EquityChart
            dates={curve.dates}
            curve={curve.curve}
            bench={curve.bench}
            ruleLabel={rule}
          />
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit,minmax(120px,1fr))",
              gap: 18,
              marginTop: 18,
            }}
          >
            <Stat label="Total">{pct(m.total_pct, 1)}</Stat>
            <Stat label="CAGR">{pct(m.cagr_pct)}</Stat>
            <Stat label="Sharpe">{num(m.sharpe)}</Stat>
            <Stat label="Sortino">{num(m.sortino)}</Stat>
            <Stat label="Max drawdown">{pct(m.max_dd_pct, 1)}</Stat>
            <Stat label="Calmar">{num(m.calmar)}</Stat>
            <Stat label="Bench CAGR">{pct(bm.cagr_pct)}</Stat>
            <Stat label="Bench max DD">{pct(bm.max_dd_pct, 1)}</Stat>
          </div>
        </section>
      )}

      {curveErr && !curve && (
        <div className="note">
          No equity series for this rule on {cls}/{tf}. {curveErr}
        </div>
      )}

      <section className="sec">
        <div className="sec-head">
          <h2>Asset by asset</h2>
          <span className="sec-note">
            {detail
              ? `${detail.rows.length} names · risk-matched, from the same run the verdict came from`
              : "—"}
          </span>
        </div>

        {detailErr && !detail && (
          <div className="note">
            {/* Named precisely, because the two reasons mean different things: a pair has
                no per-symbol backtest at all, and a missing sheet is work not yet done. */}
            No asset-by-asset table for this rule. A pair records leg-correlation
            diagnostics rather than per-symbol rows, so this is expected on one.
          </div>
        )}

        {detail && (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th className="l">Symbol</th>
                  <th>IR</th>
                  <th>Years</th>
                  <th>CAGR</th>
                  <th>B&amp;H CAGR</th>
                  <th>Total</th>
                  <th>B&amp;H total</th>
                </tr>
              </thead>
              <tbody>
                {detail.rows.map((r) => (
                  <tr key={r.symbol}>
                    <td className="l">{r.symbol}</td>
                    <td>{num(r.ir)}</td>
                    <td className="mut">{num(r.years, 1)}</td>
                    <td>{pct(r.net_cagr)}</td>
                    <td className="mut">{pct(r.bh_cagr)}</td>
                    <td>{pct(r.net_pct, 1)}</td>
                    <td className="mut">{pct(r.bh_pct, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

export default function RulePage() {
  // `useSearchParams` suspends during prerender, and a static export prerenders every
  // route. Without this boundary the build fails outright rather than at runtime.
  return (
    <Suspense fallback={<div className="note">Loading…</div>}>
      <RuleView />
    </Suspense>
  );
}
