"use client";

/* TICK SOME ROWS, THEN SEE WHAT THEY DO TOGETHER — before committing to anything.
 *
 * The preview is the important half of this control, not a confirmation step after it. The
 * moment somebody is choosing rules is exactly the moment they need to know whether their
 * picks are one bet in five costumes, and `POST /v1/portfolios/preview` blends legs that do
 * not exist yet precisely so that question can be asked without creating anything. Making
 * them create the basket first to find out would be the wrong order, and it would leave a
 * ledger full of baskets somebody made to look at.
 *
 * It reuses the leaderboard's EXISTING selection — the ticked boxes that draw lines on the
 * chart — rather than adding a second set of checkboxes. Two selections in one table is two
 * things to keep in step and one of them will be wrong. The consequence is that a basket
 * built here holds at most six legs, which is the chart palette's ceiling; the API accepts
 * up to twenty-five, and a larger one is built by following a sheet instead.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError } from "@/lib/api";
import { LegCorrelation } from "@/components/LegCorrelation";
import { LegTable } from "@/components/LegTable";
import { PortfolioChart } from "@/components/PortfolioChart";
import {
  isRetired,
  legKey,
  legsOf,
  portfolioApi,
  readCorr,
  usePortfolios,
  usePreview,
  type LegRef,
} from "@/lib/portfolio";

export interface AddToPortfolioProps {
  cls: string;
  tf: string;
  /** The ticked rules, in the order they were picked. */
  rules: string[];
  onClose: () => void;
}

export function AddToPortfolio({ cls, tf, rules, onClose }: AddToPortfolioProps) {
  const router = useRouter();
  const { list } = usePortfolios();
  const { blend, loading, error, run } = usePreview();
  const [name, setName] = useState("");
  const [into, setInto] = useState("");
  const [showLegs, setShowLegs] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const picked: LegRef[] = rules.map((rule) => ({ cls, tf, rule }));

  /* Baskets that can still be added to. A retired one is a record rather than a holding, so
     merging into it is not a thing anybody means. */
  const targets = list.filter((p) => !isRetired(p));
  const target = targets.find((p) => p.portfolio_id === into) ?? null;

  /* The legs actually being blended: the ticked ones alone, or the chosen basket's plus the
     ticked ones with duplicates removed. Merging is previewed even though it cannot yet be
     committed (see the note below the buttons) — seeing what a rule does TO a basket you
     already hold is most of why anybody would add one to it. */
  const merged: LegRef[] = target
    ? (() => {
        const seen = new Set<string>();
        const out: LegRef[] = [];
        for (const l of [...legsOf(target), ...picked]) {
          const k = legKey(l);
          if (seen.has(k)) continue;
          seen.add(k);
          out.push({ cls: l.cls, tf: l.tf, rule: l.rule });
        }
        return out;
      })()
    : picked;

  // Re-blended whenever the picks or the target change, which is what makes this a preview
  // rather than a form: the answer is always about what is currently selected.
  const key = merged.map(legKey).join(",");
  useEffect(() => {
    if (merged.length) run(merged);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const corr = readCorr(blend?.corr ?? []);

  async function create() {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      const row = await portfolioApi.create({
        name: name.trim(),
        kind: "manual",
        legs: picked,
      });
      router.push(`/portfolio/detail/?id=${encodeURIComponent(row.portfolio_id)}`);
    } catch (e) {
      // The API's own words: `_check_leg` names the rule it would not take and why — on the
      // board but untradable, or collapsed into an equivalent under another name — and a
      // duplicate name comes back as a conflict. All of it is something to act on.
      setErr(e instanceof ApiError ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <section className="sec" id="pf-preview">
      <div className="sec-head">
        <h2>{target ? `${target.name}, with these added` : "These, held together"}</h2>
        <span className="sec-note">
          {merged.length} leg{merged.length === 1 ? "" : "s"} · one pot split equally,
          rebalanced monthly · nothing is written until you say so
        </span>
      </div>

      <div className="pf-preview">
        {loading && <p className="sec-note busy-note">Blending them…</p>}

        {error && !loading && (
          <div className="note">
            <b>These cannot be blended.</b> {error}
          </div>
        )}

        {blend && blend.ok && !loading && (
          <>
            <PortfolioChart
              blend={blend}
              label={target ? target.name : "this basket"}
              showLegs={showLegs}
              onShowLegs={setShowLegs}
            />

            {/* THE CORRELATION IS ABOVE THE MONEY on purpose. Somebody assembling a basket
                off one sheet is assembling five rules that trade the same universe on the
                same bars, and whether that is five bets or one decides whether the combined
                curve above means anything. */}
            <LegCorrelation blend={blend} />

            <LegTable
              legs={blend.legs}
              capital={blend.capital}
              years={blend.years}
              corrOf={(i) => corr.perLeg[i] ?? null}
            />
          </>
        )}

        {/* RANKING IS NOT PASSING, and here more than anywhere: this control is reached by
            ticking the top of a leaderboard. */}
        <div className="note">
          <b>Picking the top of a sheet does not pick five good rules.</b> Nothing here
          clears the acceptance standard, so these are the least-bad candidates on it — and
          combining rules that each fail a gate does not produce one that passes.
        </div>

        <div className="pf-form">
          <span className="f-label">Add to</span>
          <select
            className="fsel"
            value={into}
            onChange={(e) => setInto(e.target.value)}
            aria-label="an existing portfolio"
          >
            <option value="">a new portfolio</option>
            {targets.map((p) => (
              <option key={p.portfolio_id} value={p.portfolio_id}>
                {p.name}
              </option>
            ))}
          </select>

          {!target && (
            <>
              <input
                className="search"
                type="text"
                placeholder="Name it…"
                maxLength={64}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              <button
                type="button"
                className="btn"
                disabled={busy || !name.trim() || !picked.length}
                onClick={create}
              >
                {busy ? "Creating…" : `Create with ${picked.length} leg${picked.length === 1 ? "" : "s"}`}
              </button>
            </>
          )}

          <button type="button" className="pill" onClick={onClose}>
            Close
          </button>
        </div>

        {target && (
          /* AN HONEST DEAD END rather than a button that does nothing. `/v1/portfolios` can
             create a basket and toggle one; it has no route that adds a leg to a basket that
             already exists, so this page cannot commit the merge above however it is
             dressed. What it CAN do is show the reader what the merge would look like, which
             is the half that needed the engine anyway. */
          <p className="sec-note" style={{ maxWidth: "72ch" }}>
            The blend above is what <b>{target.name}</b> would become with these added.
            Committing it is not something this page can do: the portfolio API creates a
            basket and switches one on or off, and has no route that adds a leg to one that
            already exists. Until it has, the way to hold this combination is to create it as
            a new portfolio.
          </p>
        )}

        {err && <div className="note">{err}</div>}

        <p className="sec-note" style={{ maxWidth: "72ch" }}>
          The preview writes nothing and needs no permission. A created portfolio comes back{" "}
          <b>pending</b>, like every other registration on this desk — the API writes it down
          and the desk picks it up on its next control tick, so a portfolio existing is never
          the same claim as a portfolio trading.
        </p>
      </div>
    </section>
  );
}
