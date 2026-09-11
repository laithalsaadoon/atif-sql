/**
 * The repository the docs tree cites, and the commit its permalinks are anchored to.
 *
 * This is the ONE place a commit is named. `citation-links.ts` turns every ccu `path:LOC` citation
 * into a link under `PERMALINK_COMMIT`, and a line anchor is only meaningful against an immutable
 * ref: pinned to a branch, `#L412` keeps resolving and starts pointing at whatever moved into line
 * 412. So the value is a full SHA.
 *
 * Override at build time, which is what CI does:
 *
 *     ATIF_SQL_DOCS_COMMIT=$(git rev-parse HEAD) pnpm build
 *
 * On a merge-commit checkout prefer the SHA the runner exports for the commit under review
 * (`GITHUB_SHA` on a `push`, `github.event.pull_request.head.sha` on a `pull_request`) over
 * re-resolving `HEAD`, which names the ephemeral merge commit instead.
 */

import { execFileSync } from "node:child_process"

/** Repository web root, no trailing slash. */
export const REPO_URL = "https://github.com/laithalsaadoon/atif-sql"

/**
 * The commit every citation permalink is pinned to.
 *
 * A short SHA resolves on GitHub but is not what `git ls-tree` is handed, so the presence gate in
 * `citation-links.ts` needs the full form, which is why the fallback resolves `HEAD` rather than
 * naming a SHA in source. A literal here is wrong the moment anything lands after it: it either
 * anchors every line to a stale tree, or — once history is rewritten and the SHA no longer
 * resolves — fails the build on every citation at once. Resolving `HEAD` costs nothing that
 * `citation-links.ts` does not already pay, since its presence gate shells out to `git ls-tree`,
 * so a build without a repository is impossible either way.
 */
export const PERMALINK_COMMIT =
  process.env.ATIF_SQL_DOCS_COMMIT ??
  execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim()

/** The deployed origin, with NO base segment. This is what `Astro.site` holds. */
export const SITE_ORIGIN = "https://laithalsaadoon.github.io"

/**
 * The base segment, leading and trailing slash included.
 *
 * A GitHub Pages PROJECT site is served from a path segment rather than a domain root, which is what
 * splits the rendered tree and the raw twins into two link-rewriting problems. `base-raw-links.ts`
 * closes the twin half; every URL helper takes this value rather than concatenating it.
 */
export const SITE_BASE = "/atif-sql/"
