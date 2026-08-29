"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/* The masthead, and it is duplicated on purpose — this copy, `paper api/web/desk.html` and
 * `docs.html` are three self-contained pages that must stay DIMENSIONALLY identical. Two of
 * the four destinations below are served by a different process, so these are real
 * navigations between processes: any difference in the rail, the gap or the font size shows
 * up as the nav jumping when you switch pages.
 *
 * The active link recolours and underlines and NEVER changes weight. Bolding it re-measures
 * the text and shifts every link after it, which is the same jump one line up.
 */

/* Order is the vanilla board's, and the last two leave this app entirely.
 *
 * `next/link` applies `basePath` for us, which is right for the two routes this export
 * owns and WRONG for the two it does not: `/desk` and `/desk/docs` are `paper api`'s own
 * pages, so they are bare anchors and must stay bare. A `next/link` there would resolve to
 * `/next/desk` and 404. */
const IN_APP = [
  ["/paper", "Paper trading"],
  /* Its own entry rather than a link buried on the desk, because a portfolio is now the
     unit the desk is organised around: `/paper` lists what is RUNNING, and this lists what
     exists, including the baskets nobody has switched on. */
  ["/portfolio", "Portfolios"],
  ["/", "Research"],
] as const;

const ELSEWHERE = [
  ["/desk", "Your strategies"],
  ["/desk/docs", "API"],
] as const;

export function Nav() {
  // A server component cannot know the path, and the layout has to stay a server component
  // to export `metadata` — so the active state lives in this one small client island rather
  // than turning the whole shell into one.
  const path = usePathname() ?? "/";
  const active = path.startsWith("/paper")
    ? "/paper"
    : path.startsWith("/portfolio")
      ? "/portfolio"
      : "/";

  return (
    <nav className="nav">
      {IN_APP.map(([href, label]) => (
        <Link
          key={href}
          className={`nav-link${href === active ? " on" : ""}`}
          href={href}
        >
          {label}
        </Link>
      ))}
      {ELSEWHERE.map(([href, label]) => (
        <a key={href} className="nav-link" href={href}>
          {label}
        </a>
      ))}
    </nav>
  );
}
