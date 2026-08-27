"use client";

/* A column explains itself when it is dwelt on, and sorts the ranking on click. Two
 * behaviours on one target, but they answer the two things a reader does with a header they
 * do not recognise — ask what it is, then ask who wins on it.
 *
 * The pause in front of the explanation is the load-bearing part. A popover that opens the
 * instant the cursor crosses a header fires on the way to a different one, so reading down
 * a nineteen-column table sets off a flicker of panels nobody asked for. Three seconds is
 * long enough that appearing means it was wanted.
 *
 * A phone has no hover at all, so the same explanation is on press-and-hold — and the tap
 * that ends it is SWALLOWED, because a long press is not a click that took a while.
 * Sorting the table out from under a reader who was asking what a column meant is the
 * whole failure this guards against.
 *
 * Ported from `bindColHeaders` in `../Stockhunt Dashboard/web/app.js`. It takes the column
 * list rather than closing over `LB_COLS` for the same reason it did there: a column added
 * without a `doc` is the one column nobody can ask about, and the list is what makes that
 * checkable in one place.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { DocCtx, LbCol } from "@/lib/columns";

const DOC_DWELL_MS = 3000; // pointer: how long a header must be held under the cursor
const DOC_HOLD_MS = 500; // touch: how long a finger must stay down

interface Placed {
  i: number;
  top: number;
  left: number;
  width: number;
}

/** `ctx` is null until the sheet lands: the sheet-dependent `doc`s read its folds, its
 *  universe and its benchmark, and there is no header to dwell on before then anyway. */
export function useColumnDocs(cols: LbCol[], ctx: DocCtx | null, onSort: (i: number) => void) {
  /* `hostRef` is the region a press outside a header dismisses from, `secRef` is what the
   * panel is positioned against (`.sec` is the `position:relative` box) and `boxRef` is
   * the table's own scroll box, which on a wide screen is wider than the section. */
  const hostRef = useRef<HTMLDivElement | null>(null);
  const secRef = useRef<HTMLElement | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const held = useRef(false);
  const [placed, setPlaced] = useState<Placed | null>(null);

  const cancel = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  }, []);
  const hide = useCallback(() => {
    cancel();
    setPlaced(null);
  }, [cancel]);

  const show = useCallback((th: HTMLElement, i: number) => {
    const sec = secRef.current;
    if (!sec || !cols[i]?.doc) return;
    // Hung under the header it explains and clamped to the table's own box. The width is
    // SET rather than left to the layout: an absolutely positioned box shrink-to-fits the
    // space between its `left` and the section's right edge, so without this the last
    // column's explanation came out 112px wide against the first column's 515.
    const sr = sec.getBoundingClientRect();
    const tr = th.getBoundingClientRect();
    const box = (boxRef.current ?? sec).getBoundingClientRect();
    const w = Math.min(520, box.width);
    setPlaced({
      i,
      width: w,
      top: tr.bottom - sr.top + 8,
      left: Math.max(box.left - sr.left, Math.min(tr.left - sr.left, box.right - sr.left - w)),
    });
  }, [cols]);

  const dwell = useCallback((th: HTMLElement, i: number) => {
    cancel();
    timer.current = setTimeout(() => show(th, i), DOC_DWELL_MS);
  }, [cancel, show]);

  // A press anywhere else puts the explanation away. On a phone there is no pointerleave
  // to do it, so the panel would otherwise sit over the ranking until the next long press.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const onDown = (e: PointerEvent) => {
      if (!(e.target as HTMLElement | null)?.closest?.("th[data-doc]")) hide();
    };
    host.addEventListener("pointerdown", onDown, true);
    return () => host.removeEventListener("pointerdown", onDown, true);
  }, [hide]);

  // A sheet change or a re-sort re-renders the header row; a panel left open would be
  // explaining a column the cursor is no longer on.
  useEffect(() => cancel, [cancel]);

  /** Spread onto a `<th>`. `i` indexes the FULL column list, so hiding a column never
   *  renumbers an explanation — the same contract `data-doc` carried in the vanilla DOM,
   *  and the attribute is kept because the dismiss check reads it. */
  const thProps = (i: number) => ({
    "data-doc": i,
    onPointerEnter: (e: React.PointerEvent<HTMLElement>) => {
      if (e.pointerType === "mouse") dwell(e.currentTarget, i);
    },
    onPointerLeave: (e: React.PointerEvent<HTMLElement>) => {
      if (e.pointerType === "mouse") hide();
    },
    onPointerDown: (e: React.PointerEvent<HTMLElement>) => {
      if (e.pointerType === "mouse") return;
      held.current = false;
      cancel();
      const th = e.currentTarget;
      timer.current = setTimeout(() => {
        held.current = true;
        show(th, i);
      }, DOC_HOLD_MS);
    },
    // A finger that lifts, or slides off, before the hold is up wanted the sort.
    onPointerUp: (e: React.PointerEvent<HTMLElement>) => {
      if (e.pointerType !== "mouse") cancel();
    },
    onPointerCancel: (e: React.PointerEvent<HTMLElement>) => {
      if (e.pointerType !== "mouse") cancel();
    },
    onClick: (e: React.MouseEvent<HTMLElement>) => {
      if (held.current) {
        held.current = false;
        return;
      }
      onSort(i);
      // The cursor has not gone anywhere, so the dwell starts again rather than waiting
      // for the reader to leave and come back before the column will explain itself.
      dwell(e.currentTarget, i);
    },
  });

  const col = placed && ctx ? cols[placed.i] : null;
  const panel = (
    /* Absolutely positioned over the top of the ranking rather than pushed into the flow
     * above it: a block that opens on hover and moves the table down moves the header out
     * from under the cursor, which closes it again — a loop. `pointer-events:none` comes
     * from `.coldoc` for the same reason. */
    <div
      className="coldoc"
      hidden={!placed}
      style={placed ? { top: placed.top, left: placed.left, width: placed.width } : undefined}
    >
      {col ? (
        <>
          <div className="coldoc-h">{col.h}</div>
          {/* The `doc`s are prose with <b>, <i>, <code> and <br> in them, carried across
              from the vanilla board verbatim. Every value interpolated into one is escaped
              where it is built (`lib/format.esc`); nothing here is reader input. */}
          <p
            dangerouslySetInnerHTML={{
              __html: typeof col.doc === "function" ? col.doc(ctx!) : col.doc,
            }}
          />
        </>
      ) : null}
    </div>
  );

  return { hostRef, secRef, boxRef, panel, thProps };
}
