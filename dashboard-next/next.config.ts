import type { NextConfig } from "next";

/* A STATIC EXPORT, on purpose, and the reason is operational rather than technical.
 *
 * Every number this app draws comes from `paper api` at request time, behind that
 * process's email-code session. There is nothing for a Node server to render that the
 * browser cannot ask for itself, so shipping one would put a second runtime on the VPS —
 * a second thing to supervise, restart and keep in step with a deploy that is currently
 * `git pull` every five minutes. `output: "export"` produces a directory of files that
 * the FastAPI process already in front of the board can serve, under the same login.
 *
 * The cost is named so it is not discovered: no SSR, no ISR, no route handlers. If this
 * app ever needs a server-rendered page, that is the moment to revisit — not before.
 *
 * `trailingSlash` because the export writes `research/index.html`, and the Python side
 * resolves a URL path to a file. Without it `/research` and `/research/` disagree about
 * which file they mean, and only one of them exists.
 */
// Overridable for a deploy that mounts it elsewhere; `""` serves it at the root.
const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "/next";

const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  // The path `paper api` serves this export from -- `api_board.next_board`, which routes
  // `/next/{path:path}`. THE TWO MUST AGREE, and a mismatch does not fail loudly: Next
  // bakes this prefix into every asset URL at BUILD time, so the wrong value serves a
  // page that returns 200 and renders nothing while every chunk 404s.
  //
  // A LITERAL DEFAULT, not an env file, and that is the whole reason it is here. It lived
  // in `.env.production` for about an hour, until `create-next-app`'s own `.gitignore`
  // rule -- `.env*`, which exists to keep secrets out of git -- turned out to cover it
  // too. The VPS would have cloned the repo without it, built with an empty basePath and
  // served exactly the blank page described above. A public mount path is not a secret
  // and belongs in code beside the comment explaining it.
  basePath: BASE_PATH,
  assetPrefix: BASE_PATH || undefined,
  images: { unoptimized: true },

  /* DEV ONLY. `next dev` serves on :3000 and the API lives on :8000, so the session
   * cookie would be cross-origin and every request would arrive unauthenticated. A
   * rewrite makes the browser see one origin in development, which is what production
   * genuinely is. `rewrites` is ignored by `output: "export"`, so this cannot leak into
   * the built artifact. */
  async rewrites() {
    const api = process.env.NEXT_PUBLIC_API_ORIGIN || "http://127.0.0.1:8000";
    return [
      { source: "/v1/:path*", destination: `${api}/v1/:path*` },
      { source: "/login", destination: `${api}/login` },
      { source: "/otp", destination: `${api}/otp` },
    ];
  },
};

export default nextConfig;
