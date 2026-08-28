"use client";

/* THE TOGGLE — and the whole of it is the difference between what was ASKED FOR and what
 * the DESK HAS DONE.
 *
 * `want` is written by the API; `state` is written by the desk, which is a separate process
 * reading the same ledger. They genuinely disagree while it catches up, and that
 * disagreement is information rather than a bug to paper over — so this control shows both
 * and never pretends the click landed.
 *
 * The switch position is `want`. It does not move optimistically and it does not move on
 * hover: the knob sits mid-travel (`.sw.wait`) exactly while the two disagree, which is the
 * one thing on screen that says "asked for, not yet done".
 *
 * WHY THE HEARTBEAT IS HERE. `want <> state` says precisely the same thing whether the desk
 * read the row a second ago or has been down since Tuesday. `paper api/web/desk.html`
 * solved this first and this follows it exactly: `/v1/desk` separates in-flight from nobody
 * is home, and there is a fourth state — never beaten — which is NOT the same claim as
 * "down" and must not be dressed up as one, because a desk older than the heartbeat runs
 * perfectly and reports nothing.
 *
 * So this page prints no promise it cannot keep. It never says "the desk will apply this on
 * its next pass"; it says what is true now, and it keeps watching.
 */

import { useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { fmtMoney } from "@/lib/format";
import {
  fmtAgo,
  isRetired,
  mergePortfolio,
  portfolioApi,
  settlementOf,
  useDeskPulse,
  type Portfolio,
} from "@/lib/portfolio";

/** The console's own loop: watch until the desk has actually done it, then say what it did.
 *  Twelve looks at 400ms converges on the first or second in the ordinary case; when it does
 *  not, the reason is on screen rather than implied by a row that never moves. */
const SETTLE_TRIES = 12;
const SETTLE_MS = 400;

export interface PortfolioToggleProps {
  p: Portfolio;
  /** False where the ledger will refuse the write — the house's portfolios are readable by
   *  everybody and writable by their owner. Null means NOT KNOWN YET, in which case the
   *  control is offered and the API's own refusal is what answers. */
  canWrite: boolean | null;
}

export function PortfolioToggle({ p, canWrite }: PortfolioToggleProps) {
  const { pulse, error: pulseError } = useDeskPulse();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);

  const want = String(p.want ?? "");
  const state = String(p.state ?? "");
  const retired = isRetired(p);
  const settle = settlementOf(want, state, pulse);
  const unsettled = settle.kind !== "settled";

  /* A control appears only where it does something.
   *
   * Pause and resume are meaningless once the desk is finished with a row — it will never
   * look at it again — and offering them produces the one disagreement that can NEVER
   * converge: pausing a retired portfolio leaves want='paused', state='retired', two fields
   * pointing at each other forever with no pass that could reconcile them. */
  const act = want === "paused" ? "resume" : "pause";

  async function onClick() {
    if (busy || retired || canWrite === false) return;
    setBusy(true);
    setErr(null);
    try {
      const row = act === "pause"
        ? await portfolioApi.pause(p.portfolio_id)
        : await portfolioApi.resume(p.portfolio_id);
      if (!alive.current) return;
      mergePortfolio(row);
      // Then WATCH. The write set `want`; only the desk sets `state`, and the sentence under
      // the switch is recomputed from the two on every look.
      for (let i = 0; i < SETTLE_TRIES; i++) {
        await new Promise((r) => setTimeout(r, SETTLE_MS));
        if (!alive.current) return;
        try {
          const fresh = await portfolioApi.one(p.portfolio_id);
          mergePortfolio(fresh);
          if (String(fresh.want) === String(fresh.state)) break;
        } catch {
          break; // the sentence under the switch already says the desk cannot be reached
        }
      }
    } catch (e) {
      if (alive.current) setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  const swClass = [
    want === "live" ? "on" : "off",
    unsettled && !retired ? "wait" : "",
  ].filter(Boolean).join(" ");

  const stateClass =
    settle.kind === "settled" && want === "live"
      ? "live"
      : settle.kind === "failing" || settle.kind === "stopped" || settle.kind === "never"
        ? "bad"
        : settle.kind === "inflight" || settle.kind === "unknown"
          ? "wait"
          : "";

  return (
    <div
      className={`promote${retired || canWrite === false || busy ? " blocked" : ""}`}
      role={retired || canWrite === false ? undefined : "button"}
      tabIndex={retired || canWrite === false ? undefined : 0}
      aria-label={retired ? "retired" : `${act} paper trading for ${p.name}`}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
    >
      <div className="promote-top">
        <span className="promote-label">
          {retired ? "Retired" : want === "live" ? "Paper trading" : "Paused"}
        </span>
        <span className={`sw ${retired ? "off" : swClass}`} aria-hidden="true" />
      </div>

      <p className="promote-sub">
        {retired ? (
          <>
            This portfolio was retired. Its legs stopped trading and their fills and equity
            curves are kept — <b>a forward record you can erase is not a record</b>.
          </>
        ) : canWrite === false ? (
          <>
            The house owns this one, so it is readable here and changed by its owner. What it
            holds, what it did and why it changed are all on this page either way.
          </>
        ) : (
          <>
            One switch for the whole basket: <b>{act === "pause" ? "pause" : "resume"}</b>{" "}
            cascades to every leg in one transaction, because half a basket switched off is a
            position nobody chose to hold. {fmtMoney(p.capital)} split equally across them.
          </>
        )}
      </p>

      {/* BOTH FIELDS, always, under their own names. A single "status" here would be the
          page choosing which of the two to believe. */}
      <p className="promote-state">
        asked <b>{want || "—"}</b> · desk <b>{state || "—"}</b>
        {pulse?.seconds_ago != null && <> · last pass {fmtAgo(pulse.seconds_ago)} ago</>}
      </p>
      <p className={`promote-state ${stateClass}`} style={{ textTransform: "none", letterSpacing: 0 }}>
        {busy ? "writing it down…" : settle.text}
      </p>

      {pulseError && !pulse && (
        <p className="promote-state bad" style={{ textTransform: "none", letterSpacing: 0 }}>
          The desk&apos;s heartbeat could not be read ({pulseError}), so whether a pending
          change is in flight cannot be told from here.
        </p>
      )}
      {err && (
        <p className="promote-state bad" style={{ textTransform: "none", letterSpacing: 0 }}>
          {err}
        </p>
      )}
    </div>
  );
}
