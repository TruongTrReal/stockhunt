"use client";

/* /portfolio — every basket the reader can see.
 *
 * A portfolio is a named set of strategy legs with ONE pot of money, one combined curve and
 * one switch. This page is the index of them and nothing more: what it is, how many legs,
 * how much money, whether it is trading, and the shape of what it did. Everything that needs
 * a paragraph is on the detail page, because a list that explains each row is not a list.
 *
 * TWO KINDS, and the difference matters enough to be a chip on every row. A `manual`
 * portfolio holds rules somebody picked; nothing re-checks them. A `follow` portfolio tracks
 * one leaderboard sheet's top few, re-checked daily — so its holdings move under it and its
 * curve is the record of several different baskets. The membership log on the detail page is
 * the only thing that says which.
 *
 * The house's portfolios are readable by everybody and changed by their owner, which is the
 * same rule `/v1/house/strategies` follows: being able to read the house book is most of why
 * a member would trust the desk.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { CurveSpark } from "@/components/PortfolioChart";
import { fmtMoney, fmtDate } from "@/lib/format";
import {
  isRetired,
  legCount,
  usePortfolios,
  useSparks,
  type Portfolio,
} from "@/lib/portfolio";
import { useLive } from "@/lib/live";

const detailHref = (id: string) => `/portfolio/detail/?id=${encodeURIComponent(id)}`;

/** `want` is what was asked for and `state` is what the desk has done. On a LIST the two
 *  are shown as one word plus a marker, because a row is a place to see that something is
 *  outstanding rather than to read why — the detail page's switch carries the sentence. */
function StateCell({ p }: { p: Portfolio }) {
  const want = String(p.want ?? "");
  const state = String(p.state ?? "");
  if (isRetired(p)) return <span className="pf-state">retired</span>;
  if (want && state && want === state)
    return <span className={`pf-state ${want === "live" ? "live" : ""}`}>{state}</span>;
  return (
    <span className="pf-state wait" title={`asked ${want || "—"}, desk says ${state || "—"}`}>
      {want || "—"} → {state || "—"}
    </span>
  );
}

function Row({ p, spark }: { p: Portfolio; spark: ReturnType<ReturnType<typeof useSparks>> }) {
  const router = useRouter();
  const href = detailHref(p.portfolio_id);
  const n = legCount(p);
  const source = p.source_cls && p.source_tf ? `${p.source_cls} ${p.source_tf}` : null;

  return (
    <div className="grp">
      <div
        className="grp-row pf-row"
        role="link"
        tabIndex={0}
        aria-label={`${p.name} — open this portfolio`}
        onClick={() => router.push(href)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            router.push(href);
          }
        }}
      >
        <span className="grp-id">
          <span className="grp-name">{p.name}</span>
          <span className="grp-meta">
            {/* Legs print an em-dash and never a zero where the listing did not carry them:
                "no legs" and "not told" are different facts about a basket. */}
            {n == null ? "— legs" : `${n} leg${n === 1 ? "" : "s"}`} ·{" "}
            {fmtMoney(p.capital)} · rebalanced {p.rebalance ?? "monthly"}
            {p.inception ? ` · since ${fmtDate(p.inception)}` : ""}
          </span>
        </span>

        <span className="chip mut">
          {p.kind === "follow" ? (source ? `follows ${source}` : "follows a sheet") : "hand-picked"}
        </span>

        <span className="grp-meta">
          {p.kind === "follow" && p.top_n ? `top ${p.top_n}, re-checked daily` : ""}
        </span>

        <span className="grp-live">
          {spark === undefined ? (
            <span className="hist-lbl busy-note">blending…</span>
          ) : spark === null ? (
            <span className="hist-lbl">no combined curve</span>
          ) : (
            <CurveSpark portfolio={spark.portfolio} bench={spark.bench} w={150} h={30} />
          )}
        </span>

        <StateCell p={p} />
      </div>
    </div>
  );
}

export default function PortfolioListPage() {
  const { list, ready, error } = usePortfolios();
  const { doc } = useLive();
  const sparkOf = useSparks(list.map((p) => p.portfolio_id));

  const house = String(doc?.house ?? "00");
  // Retired last, then the house's, then by name. A retired basket is a record rather than a
  // holding, so it does not belong among the things that are running.
  const rows = [...list].sort(
    (a, b) =>
      Number(isRetired(a)) - Number(isRetired(b)) ||
      Number(a.account !== house) - Number(b.account !== house) ||
      a.name.localeCompare(b.name),
  );

  const live = rows.filter((p) => !isRetired(p) && p.state === "live").length;
  const asked = rows.filter((p) => !isRetired(p) && p.want !== p.state).length;

  return (
    <>
      <div className="hero">
        <h1>Portfolios</h1>
        <p className="lede">
          A portfolio is a basket of strategies with one pot of money, split equally across
          its legs and rebalanced back to equal weight every month. One combined curve, one
          switch. The only question worth asking about a basket is what its members do to
          each other — five rules picked off one leaderboard trade the same universe, so they
          can be five names for one bet — and that is the number the detail page opens with.
        </p>
      </div>

      {/* RANKING IS NOT PASSING, and this is the first place it has to be said. Nothing on
          any sheet clears this repo's acceptance gates; a sheet's top five are the least-bad
          five, and combining five rules that each fail a gate does not produce one that
          passes. */}
      <div className="note">
        <b>A top-ranked basket is not a good basket.</b> Nothing on any leaderboard here
        clears the acceptance standard, so a portfolio that follows a sheet holds the
        least-bad rules on it rather than five that work. Combining them does not fix that —
        see <Link href="/">Research</Link> for what each leg was actually scored at.
      </div>

      {!ready && !list.length && <div className="note busy-note">Reading the ledger…</div>}

      {error && ready && (
        <div className="note">
          The portfolio list could not be read — {error}. Anything below is the last answer
          this page got.
        </div>
      )}

      {ready && !rows.length && !error && (
        <div className="note">
          No portfolios yet. Tick strategies on the <Link href="/">leaderboard</Link>, look at
          what they do together, and create one from the selection — the preview blends legs
          that do not exist yet, so nothing has to be committed to see it.
        </div>
      )}

      {rows.length > 0 && (
        <>
          <p className="deskline">
            <span>
              <b>{rows.length}</b> portfolio{rows.length === 1 ? "" : "s"} visible
            </span>
            <span>
              <b>{live}</b> the desk is running
            </span>
            {asked > 0 && (
              <span>
                <b>{asked}</b> waiting on the desk
              </span>
            )}
          </p>

          <section className="sec">
            <div className="sec-head">
              <h2>Baskets</h2>
              <span className="sec-note">
                the curve is the research blend, not the desk&apos;s record · tap one for its
                legs, its correlations and its switch
              </span>
            </div>
            {rows.map((p) => (
              <Row key={p.portfolio_id} p={p} spark={sparkOf(p.portfolio_id)} />
            ))}
          </section>
        </>
      )}

      <p className="sec-note" style={{ maxWidth: "72ch" }}>
        The curves above are walk-forward research over each leg&apos;s whole history. What
        these baskets have done since the desk picked them up is a different measurement, on{" "}
        <Link href="/paper/">Paper trading</Link>, and the two are never added together.
      </p>
    </>
  );
}
