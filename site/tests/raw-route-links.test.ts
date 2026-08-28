import { readdirSync, readFileSync, statSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { describe, expect, it } from "vitest"

import { SITE_BASE } from "../src/lib/repo.js"

/**
 * The raw `.md` routes are the agent surface, and their links have to resolve THERE rather than only in
 * the HTML twin.
 *
 * This is the defect class the file exists for, and it is invisible from every angle a build reports on.
 * A site served from a base segment has TWO link rewriters: one for the rendered tree, one for the raw
 * twins, built from different inputs — the rendered tree from the compiled page, each twin from the
 * page's Markdown source. So a link comes out correct on whichever surface its producer touched and
 * wrong on the other, and nothing fails. Hundreds of links can point at `/internals/…` instead of
 * `/product/internals/…`, every one a 404 for the agent following it, while the HTML is correct
 * throughout and the link validator reports every internal link valid.
 *
 * The seam runs both ways. A loader that prefixes the base into its own bodies produces raw routes that
 * are right and HTML that is doubled: `/product/product/reference/…`. That direction hides behind the
 * validator exclusion a generated tier needs for an unrelated structural reason — a validator can only
 * judge a target whose headings it recorded in its own Markdown pass, and an injected page has no source
 * file for that pass.
 *
 * So: a passing link validator is not evidence about this surface. These assertions read the built bytes.
 */

/* =================================================================================================
 * CONFIG — the whole site-specific surface of this file.
 * ================================================================================================= */

const CONFIG = {
  /** Built output, relative to the package root. */
  dist: "dist",
  /**
   * The base the build ran with, IMPORTED from the module `astro.config.ts` reads it from.
   *
   * Not an environment variable with a `/` default: that default is the one value at which every
   * assertion below goes vacuous, so a suite run without the variable set would report a green pass over
   * output it never inspected.
   */
  base: SITE_BASE,
  /**
   * Path prefixes inside the built output that are not raw twins. `.md` files under these are assets or
   * fixtures rather than pages, and holding them to the link contract reports a defect that is not one.
   */
  ignore: ["_astro/", "pagefind/"]
} as const

const distDir = join(dirname(dirname(fileURLToPath(import.meta.url))), CONFIG.dist)
const segment = CONFIG.base.endsWith("/") ? CONFIG.base : `${CONFIG.base}/`

/** Every raw Markdown route the build emitted. */
const rawRoutes = (directory: string): ReadonlyArray<string> =>
  readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return rawRoutes(path)
    if (!entry.name.endsWith(".md")) return []
    const relative = path.slice(distDir.length + 1)
    return CONFIG.ignore.some((prefix) => relative.startsWith(prefix)) ? [] : [path]
  })

/**
 * Every root-relative Markdown link target in a body.
 *
 * `\/(?!\/)` requires exactly one leading slash, so a protocol-relative target is excluded here and
 * caught by its own case below. Without that lookahead a `//host/path` reads as a root-relative link and
 * the case that exists to find it never sees it.
 */
const rootRelativeTargets = (body: string): ReadonlyArray<string> =>
  [...body.matchAll(/\]\((\/(?!\/)[^)]*)\)/g)].map(([, target]) => target as string)

const isFile = (path: string): boolean => {
  try {
    return statSync(path).isFile()
  } catch {
    return false
  }
}

/* =================================================================================================
 * ================================================================================================= */

describe("the raw Markdown routes", () => {
  const routes = rawRoutes(distDir)

  it("exist, so the rest of this file is not vacuously true", () => {
    /*
     * A floor of one rather than a number. The count belongs to the build, and the strong form of this
     * assertion — a twin for every built page — lives in `agent-surface.test.ts`, where the page list is
     * already derived. Writing a number here would make adding a page look like a broken test.
     */
    expect(routes.length).toBeGreaterThan(0)
  })

  it("carries the base segment on every root-relative link", () => {
    const offenders = routes.flatMap((file) =>
      rootRelativeTargets(readFileSync(file, "utf8"))
        .filter((target) => !target.startsWith(segment))
        .map((target) => `${file.slice(distDir.length)} -> ${target}`)
    )
    expect(offenders).toEqual([])
  })

  it("carries it exactly once, and never as a host", () => {
    /*
     * The assertion CHANGES SHAPE with the base rather than going vacuous at one of them.
     *
     * At a non-root base the failure is a doubled segment, `/product/product/…`. At the root base there
     * is no segment to double and the analogous defect is a protocol-relative `//path`: a URL naming a
     * HOST rather than a path, which is exactly what concatenating an empty base onto a leading slash
     * produces. Both forms parse, resolve to nothing, and look right in the source.
     */
    const doubled = routes.flatMap((file) => {
      const body = readFileSync(file, "utf8")
      const offenders =
        segment === "/"
          ? [...body.matchAll(/\]\((\/\/[^)]*)\)/g)].map(([, target]) => target as string)
          : rootRelativeTargets(body).filter((target) =>
              target.startsWith(`${segment}${segment.slice(1)}`)
            )
      return offenders.map((target) => `${file.slice(distDir.length)} -> ${target}`)
    })
    expect(doubled).toEqual([])
  })

  it("points every internal link at something that was actually built", () => {
    const missing = routes.flatMap((file) =>
      rootRelativeTargets(readFileSync(file, "utf8"))
        .filter((target) => target.startsWith(segment) && !target.includes("#"))
        .map((target) => target.slice(segment.length))
        .filter((path) => !(isFile(join(distDir, path)) || isFile(join(distDir, path, "index.html"))))
        .map((path) => `${file.slice(distDir.length)} -> ${segment}${path}`)
    )
    expect(missing).toEqual([])
  })

  it("links twins to twins, so an agent following a link stays on the Markdown surface", () => {
    /*
     * A twin whose links all point at rendered pages walks the agent back into the HTML on the first
     * hop, which spends the tokens the twin was fetched to avoid. A link to a page route is not a defect
     * on its own — a twin legitimately references pages — so this asserts the surface is REACHABLE from
     * itself rather than that every link is a twin: at least one link per corpus resolves to another
     * `.md` route. Without this the whole surface can be a set of islands and every case above passes.
     */
    const twinLinks = routes.flatMap((file) =>
      rootRelativeTargets(readFileSync(file, "utf8")).filter((target) => target.endsWith(".md"))
    )
    expect(twinLinks.length).toBeGreaterThan(0)
    for (const target of twinLinks) {
      expect(isFile(join(distDir, target.slice(segment.length))), `${target} resolves to nothing`).toBe(
        true
      )
    }
  })
})
