/* The board's formatters, ported from `../Stockhunt Dashboard/web/app.js` unchanged.
 *
 * They are copied rather than re-derived because the two boards have to print the same
 * characters: the same number of decimals, the same typographic minus, the same em-dash
 * for a missing figure. A "cleaner" `Intl.NumberFormat` here would give one board an
 * ASCII hyphen and a thousands separator the other does not have, and the difference
 * would show up as two screenshots of one sheet that disagree.
 *
 * Two conventions run through all of it and both are load-bearing:
 *
 *   * `null` prints an em-dash and never a zero. A row the standard never scored is a
 *     gap in the evidence, not a measured nothing, and the whole page rests on that
 *     distinction.
 *   * the minus sign is U+2212, not a hyphen. `toFixed` emits a hyphen, which sits at a
 *     different height and width and makes a column of numbers look misaligned against
 *     one formatted next to it.
 */

/** Every formatter takes the same three states, because every figure on the board has
 *  them: measured, absent from this row, or absent from this sheet. */
type N = number | null | undefined;

export const fmtPct = (v: N, d = 2) =>
  v == null ? "—" : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d) + "%";
export const fmtIR = (v: N) =>
  v == null ? "—" : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(3);
/** Annualised growth, printed unsigned: 17.51% reads as a rate, +17.51% reads as a gain. */
export const fmtCagr = (v: N, d = 1) => (v == null ? "—" : v.toFixed(d) + "%");
export const fmtNum = (v: N, d = 1) => (v == null ? "—" : Number(v).toFixed(d));
export const fmtSigned = (v: N, d = 2) =>
  v == null ? "—" : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d);
export const fmtSharpe = (v: N) => fmtNum(v, 3);
export const fmtDD = (v: N) => fmtNum(v, 1) + "%";
/** Exposure as whole percent. Its own formatter because `Long %` is the one column a
 *  reader is told to look at before any money column, and a decimal place there invites
 *  a precision the measurement does not have. */
export const pctOr = (v: N) => (v == null ? "—" : (v * 100).toFixed(0) + "%");

/* ---------- P&L: what a fixed stake became ----------
 * A percentage return over 41 years is unreadable (+74,735%) and a percentage-point gap
 * against the benchmark is worse (−89,644 points, which is not a quantity that means
 * anything). The same result as money — $10k became $7.5M against $16.4M holding — is
 * immediately legible.
 */
export const STAKE = 10000;
export const grew = (pct: N) => (pct == null ? null : STAKE * (1 + pct / 100));
export const fmtMoney = (v: N) => {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return "$" + (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return "$" + (v / 1e3).toFixed(0) + "k";
  return "$" + v.toFixed(0);
};

/* P&L against the benchmark, as money.
 *
 * The first version of this divided the rule's profit by the benchmark's, and it was wrong
 * in exactly the case that matters most: a benchmark that LOST money over the window sends
 * the denominator negative, so a rule that made money was rendered as a negative multiple
 * in red beside a verdict of "beat". A ratio to a negative base carries no meaning and
 * flips its own sign. The difference in final value has no such hole. The multiple is kept
 * only where the benchmark did make money, and is suppressed where it did not.
 */
export const pnlDelta = (net: N, bh: N) =>
  net == null || bh == null ? null : grew(net)! - grew(bh)!;
export const fmtDelta = (v: N) =>
  v == null ? "—" : (v >= 0 ? "+" : "−") + fmtMoney(Math.abs(v));
export const pnlRatio = (net: N, bh: N) => {
  if (net == null || bh == null) return null;
  const b = grew(bh)! - STAKE;
  return b <= 0 ? null : (grew(net)! - STAKE) / b; // undefined against a losing base
};
export const fmtRatio = (v: N) =>
  v == null ? "—" : (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2)) + "×";

/** Colour means one thing on this site — gained or lost — so this is the only place a
 *  class is chosen from a number's sign. `flat` for zero AND for missing: neither is a
 *  gain and neither is a loss. */
export const sign = (v: N) => (v != null && v > 0 ? "gain" : v != null && v < 0 ? "loss" : "flat");

/** React escapes text for us; this exists for the column `doc`s, which are HTML strings
 *  carrying <b>, <code> and <br> and are rendered with `dangerouslySetInnerHTML`. Any
 *  value interpolated into one comes from the API, so it goes through here first — the
 *  same rule the vanilla board follows. */
export const esc = (s: unknown) =>
  String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c] as string);

export const clip = (t: unknown, n: number) =>
  String(t).length > n ? String(t).slice(0, n - 1) + "…" : String(t);

/* A pair is two rules joined by an operator (`or`, `and`, `vote`, `gate`) and carries that
 * operator inside its own name — `HT_TRENDMODE~MAXINDEX|or`. The table prints the stem and
 * shows the operator as a chip, which is also what marks the row as a pair rather than a
 * single rule; there is no separate type column, because the two are not separate lists. */
export const stemName = (r: unknown) => String(r).split("|")[0];
/* `nan` and `None` are what a single rule's empty operator column becomes after the two
 * sweeps are concatenated — neither is an operator and neither may reach a chip. */
export const opLabel = (o: unknown) =>
  !o || o === "nan" || o === "None" ? "" : String(o);
