"use client";

/* /paper/strategy/?id= — ONE DEPLOYMENT of a system: one rule, on one instrument.
 *
 * An account id is `00:cls-tf-rule`, which survives a query parameter untouched — the same
 * reason the rule label does one route over. The vanilla reaches this by `#/paper/<id>`,
 * a pattern that has to sit AFTER `#/paper/sys/...` in its router or it swallows it whole;
 * separate files have no such ordering problem, which is one collision this port cannot
 * reintroduce.
 *
 * THE BACK LINK GOES UP TO THE SYSTEM, not to the list. This page is reached from a
 * system's holdings table, and a back link that skips the page you came from is a link to
 * somewhere else.
 */

import { Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { PnlLive } from "@/components/PnlChart";
import { ReplayBanner } from "@/app/paper/page";
import { systemHref } from "@/app/paper/page";
import {
  cash,
  fmtPct,
  fmtUnits,
  isReplay,
  price,
  prettyNote,
  sign,
  statusChip,
  strategiesOf,
  useLive,
  type Fill,
} from "@/lib/live";

function StrategyView() {
  const q = useSearchParams();
  const id = q.get("id") ?? "";
  const { doc, ready } = useLive();
  const s = strategiesOf(doc).find((x) => x.id === id);
  const replay = isReplay(doc);

  if (!ready) return <div className="note busy-note">Reading the desk…</div>;
  if (!s) {
    return (
      <div className="note">
        No deployment with id <b>{id}</b> is on the desk — it may have been retired.{" "}
        <Link href="/paper/">Back to paper trading</Link>.
      </div>
    );
  }

  const [chip, label] = statusChip(s, replay);
  const trades = (s.trades as Fill[] | undefined) || [];

  return (
    <>
      <Link className="back" href={systemHref(s.cls || "", s.tf || "", s.rule || "")}>
        ← {s.rule}
      </Link>
      <div className="hero">
        <div className="d-head">
          <span className="d-name">
            {s.symbol} · {s.rule}
          </span>
          <span className="chip mut">{s.tf}</span>
          <span className={`chip ${chip}`}>{label}</span>
        </div>
        <p className="lede">{prettyNote(s.note)}</p>
      </div>

      <ReplayBanner doc={doc} />

      <div className="strip">
        <div className="stat">
          <span className="k">{replay ? "Replay P&L" : "Paper P&L"}</span>
          <span className={`v ${sign(s.paper_pnl_pct)}`}>{fmtPct(s.paper_pnl_pct)}</span>
          <span className="s">
            over {s.days} days{replay ? " of history" : " live"}
          </span>
        </div>
        <div className="stat">
          <span className="k">Position</span>
          <span className="v">
            <span className={`pos-${s.state}`}>{s.state}</span>
          </span>
          <span className="s">
            {s.position_units ? `${fmtUnits(s.position_units)} units` : "no exposure"}
          </span>
        </div>
        <div className="stat">
          <span className="k">Avg cost</span>
          <span className="v">{price(s.entry)}</span>
          <span className="s">{s.paper_trades} fills</span>
        </div>
        <div className="stat">
          <span className="k">Turnover / yr</span>
          <span className="v">{s.turnover == null ? "—" : s.turnover.toFixed(1)}</span>
          <span className="s">watch for drift vs backtest</span>
        </div>
      </div>

      <section className="sec">
        <div className="sec-head">
          <h2>{replay ? "Replayed progress" : "Live progress"}</h2>
          <span className="sec-note">{s.days} days of simulated fills</span>
        </div>
        {s.paper_curve && s.paper_curve.length > 1 ? (
          <div className="panel sys-live">
            {/* Baseline at 0, and cut at `curve_breaks`: this series is cumulative P&L in
                PERCENT, not an index. A chart drawn against 100 puts its own reference off
                the top of a plot that runs either side of zero. */}
            <PnlLive
              curve={s.paper_curve}
              bench={null}
              breaks={s.curve_breaks}
              w={1200}
              h={220}
            />
            <div className="legend">
              <span>
                <i
                  className="sw"
                  style={{
                    background:
                      (s.paper_pnl_pct || 0) >= 0 ? "var(--gain)" : "var(--loss)",
                  }}
                />
                Cumulative P&amp;L
              </span>
            </div>
            <p className="sec-note">
              <span
                className="explains"
                title={
                  replay
                    ? "Historical bars, so this is long enough to look at — but it is the "
                      + "same period the research already scored, not new evidence. Its job "
                      + "here is to prove bars arrive, signals compute and orders fill."
                    : "Its job is to prove bars arrive, signals compute and orders fill. "
                      + "Whether the rule works is the multi-year question, answered by the "
                      + "walk-forward run and not by days of fills."
                }
              >
                {replay ? "the period the research already scored" : "too short to judge the rule"}
              </span>
            </p>
          </div>
        ) : (
          <p className="sec-note">No curve yet — this strategy has not completed a bar.</p>
        )}
      </section>

      <section className="sec">
        <div className="sec-head">
          <h2>Fills</h2>
          <span className="sec-note">newest last</span>
        </div>
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th className="l">Time</th>
                <th className="l">Side</th>
                <th>Qty</th>
                <th>Price</th>
                <th>Realised P&amp;L</th>
                <th>Book P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {trades.length ? (
                trades.map((t, i) => (
                  <tr key={`${t.ts}|${i}`}>
                    <td className="l">{t.ts}</td>
                    <td className={`l ${t.side === "BUY" ? "gain" : "loss"}`}>{t.side}</td>
                    <td>{fmtUnits(t.qty)}</td>
                    <td>{price(t.price)}</td>
                    {/* Two different questions, both on the table. `realised` is what THIS
                        fill closed against what the closed part cost, and is null when it
                        opened or added; `pnl` is the whole book's mark at that instant. */}
                    <td
                      className={t.realised == null ? "" : sign(t.realised)}
                      title={
                        t.realised == null
                          ? "this fill opened or added — it closed nothing"
                          : undefined
                      }
                    >
                      {cash(t.realised)}
                    </td>
                    <td className={`book-pnl ${sign(t.pnl)}`}>{cash(t.pnl)}</td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td className="l" colSpan={6} style={{ color: "var(--muted)" }}>
                    No fills yet — still warming up.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <p className="sec-note">
        <Link href="/">the backtest for {s.rule}</Link> is the multi-year question
      </p>
    </>
  );
}

export default function StrategyPage() {
  return (
    <Suspense fallback={<div className="note busy-note">Reading the desk…</div>}>
      <StrategyView />
    </Suspense>
  );
}
