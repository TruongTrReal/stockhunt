import type { Metadata } from "next";
import Link from "next/link";
import "./board.css";

/* `board.css` is COPIED from `../Stockhunt Dashboard/web/app.css` by the `prebuild` and
 * `predev` scripts, and is gitignored. It is not a fork: that stylesheet is 730 hand-tuned
 * lines carrying an argument about colour — green and red mean gained and lost, the six
 * series hues deliberately contain neither, and the order of those six is a CVD safety
 * mechanism rather than decoration. A second copy would drift from it silently and take
 * that argument with it. One source of truth, copied at build time.
 */

export const metadata: Metadata = {
  title: "Stockhunt",
  description: "Walk-forward research and paper trading",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="top">
          <div className="top-in">
            <span className="brand">
              <b>stockhunt</b>
            </span>
            <nav className="nav">
              {/* `next/link` and not a bare <a>: it applies `basePath` for us. A hand
                  written href would be right at the root and 404 at /next/, which is
                  exactly where this app is mounted while it is being built. */}
              <Link className="on" href="/">
                Research
              </Link>
            </nav>
          </div>
        </header>

        <div className="page">
          <main>{children}</main>

          {/* Verbatim from the vanilla shell. It is a compliance line, not copy: the two
              kinds of figure on this site are produced by different machinery and must
              never be read as one number. */}
          <footer>
            Research figures are walk-forward out-of-sample results; paper-trading figures
            are simulated fills on live market data. The two are reported separately and
            never summed. Past performance, simulated or otherwise, is not indicative of
            future results. Nothing here is an offer to sell or a solicitation to buy any
            security, and nothing here is investment advice.
          </footer>
        </div>
      </body>
    </html>
  );
}
