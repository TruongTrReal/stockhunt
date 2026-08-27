/* Copy the board stylesheet in, rather than fork it.
 *
 * `../Stockhunt Dashboard/web/app.css` is the one definition of how this project looks,
 * and it carries an argument in its comments: colour means gained or lost, the six series
 * hues deliberately contain neither, and their ORDER is a colour-vision safety mechanism.
 * A second hand-maintained copy would drift from that and take the argument with it.
 *
 * It cannot simply be imported: the folder name has a space and sits outside this app's
 * root, which is the same constraint the repo's CLAUDE.md describes for its Python
 * folders. So it is copied on every dev start and every build, and the copy is gitignored
 * so nobody edits the one that is not the source.
 */
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "..", "..", "Stockhunt Dashboard", "web", "app.css");
const dst = resolve(here, "..", "app", "board.css");
mkdirSync(dirname(dst), { recursive: true });
copyFileSync(src, dst);
console.log(`board.css <- ${src}`);
