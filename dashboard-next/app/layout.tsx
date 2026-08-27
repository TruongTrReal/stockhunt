import type { Metadata } from "next";
import "./board.css";
import "./busy.css";
import { Nav } from "@/components/Nav";

/* `board.css` is COPIED from `../Stockhunt Dashboard/web/app.css` by the `prebuild` and
 * `predev` scripts, and is gitignored. It is not a fork: that stylesheet is 730 hand-tuned
 * lines carrying an argument about colour — green and red mean gained and lost, the six
 * series hues deliberately contain neither, and the order of those six is a CVD safety
 * mechanism rather than decoration. A second copy would drift from it silently and take
 * that argument with it. One source of truth, copied at build time.
 *
 * `busy.css` is the opposite: a short file this app owns, holding the states the vanilla
 * board has no use for because it never waits for anything.
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
            {/* A client island, because the active link depends on the path and this
                layout must stay a server component to export `metadata`. */}
            <Nav />
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
