import { existsSync, readdirSync, readFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { describe, expect, it } from "vitest"

import { DEFAULT_CLASS_NAME, MERMAID_LANG } from "../src/lib/mermaid-integration.js"
import { SITE_BASE } from "../src/lib/repo.js"

/**
 * Diagrams are rendered at BUILD time, and this suite reads the built bytes to prove it.
 *
 * A client-rendered diagram is absent from the raw `.md` twin, absent from all three llms bundles, and
 * absent from any fetch that runs no JavaScript. It ships the densest thing on the page to the audience
 * that already has the prose and withholds it from the audience that cannot re-derive it. So the
 * assertions here are about what a client receives without running anything.
 *
 * The three surfaces carry three different things, and each is asserted separately because they are
 * produced by different mechanisms:
 *
 *   HTML          the rendered SVG, inline, no diagram runtime
 *   `.md` twin    the Mermaid fence verbatim, because the twin is built from the entry's source
 *   llms bundles  the figure's caption, because they are built by flattening rendered HTML back to
 *                 Markdown and an inline `<svg>` survives that round trip as nothing at all
 *
 * Every quantity is derived from the build. The corpus carries a diagram today and will carry more when
 * the generated tree lands, so a written-down count would report the growth as a defect.
 */

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const dist = join(root, "dist")
const segment = SITE_BASE.endsWith("/") ? SITE_BASE : `${SITE_BASE}/`

const readDist = (relative: string): string => {
  const path = join(dist, relative)
  if (!existsSync(path)) throw new Error(`\`dist/${relative}\` is absent — build before running this suite`)
  return readFileSync(path, "utf8")
}

/** A fence opener whose language is `mermaid`, at the start of a line. */
const fencePattern = new RegExp(`^ {0,3}(?:\`{3,}|~{3,})[ \\t]*${MERMAID_LANG}(?=[ \\t]|$)`, "gm")

/** The fence's info string past the language: the caption the figure carries. */
const captionPattern = new RegExp(`^ {0,3}(?:\`{3,}|~{3,})[ \\t]*${MERMAID_LANG}[ \\t]+(.+?)[ \\t]*$`, "gm")

/** Every emitted raw twin, as paths relative to `dist/`. */
const twins = (directory: string, prefix = ""): ReadonlyArray<string> =>
  readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    if (entry.name.startsWith("_") || entry.name === "pagefind") return []
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return twins(path, `${prefix}${entry.name}/`)
    return entry.name.endsWith(".md") ? [`${prefix}${entry.name}`] : []
  })

/** The twins that carry a diagram, with how many each carries. Derived, never listed. */
const diagramPages = (): ReadonlyArray<{ twin: string; page: string; fences: number }> =>
  twins(dist)
    .map((twin) => {
      const body = readFileSync(join(dist, twin), "utf8")
      return {
        twin,
        page: join(twin.slice(0, -".md".length), "index.html"),
        fences: [...body.matchAll(fencePattern)].length
      }
    })
    .filter((entry) => entry.fences > 0)

describe("build-time Mermaid", () => {
  const pages = diagramPages()

  it("has at least one diagram in the corpus, or every case here is vacuous", () => {
    expect(pages.length).toBeGreaterThan(0)
  })

  it("renders one figure into the HTML for every fence the twin carries", () => {
    for (const { twin, page, fences } of pages) {
      const html = readDist(page)
      const figures = html.split(`class="${DEFAULT_CLASS_NAME}"`).length - 1
      expect(figures, `${page}: ${fences} fence(s) in ${twin}, ${figures} figure(s) rendered`).toBe(
        fences
      )
      // An SVG element inside the figure, not a placeholder waiting for a script.
      const figureAt = html.indexOf(`class="${DEFAULT_CLASS_NAME}"`)
      expect(html.slice(figureAt, figureAt + 400)).toContain("<svg")
    }
  })

  it("ships no diagram runtime to the browser", () => {
    /*
     * The named check rather than a blanket "no script": Starlight ships its own island scripts, and a
     * gate that fires on the framework's correct output gets deleted instead of fixed.
     */
    for (const { page } of pages) {
      const html = readDist(page)
      expect(html).not.toMatch(/<script[^>]*src="[^"]*mermaid/i)
      expect(html).not.toMatch(/mermaid\.initialize/i)
      expect(html).not.toMatch(/cdn\.jsdelivr\.net[^"]*mermaid/i)
    }
  })

  it("requests no third-party font from the rendered SVG", () => {
    /*
     * `beautiful-mermaid` writes `@import url('https://fonts.googleapis.com/…')` into the SVG's `<style>`
     * unconditionally and offers no option to suppress it, so the integration strips it. Left in, every
     * page carrying a diagram makes a third-party request that is invisible in the site's configuration.
     */
    for (const { page } of pages) {
      expect(readDist(page)).not.toContain("fonts.googleapis.com")
    }
  })

  it("keeps the fence verbatim in the raw twin, which is the form an agent can read", () => {
    for (const { twin, fences } of pages) {
      const body = readDist(twin)
      expect([...body.matchAll(fencePattern)].length).toBe(fences)
      // The diagram body too, not just the opener: a stripped fence leaves the language behind.
      expect(body).toMatch(/^(?:flowchart|graph|sequenceDiagram|stateDiagram|classDiagram|erDiagram)/m)
    }
  })

  it("carries every caption into both llms bundles, the only part of a diagram that reaches them", () => {
    const captions = pages.flatMap(({ twin }) =>
      [...readDist(twin).matchAll(captionPattern)].map((match) => match[1] ?? "")
    )
    expect(captions.length, "no diagram carries a caption, so the bundles describe none").toBeGreaterThan(0)
    for (const surface of ["llms-full.txt", "llms-small.txt"]) {
      const bundle = readDist(surface)
      for (const caption of captions) {
        expect(bundle.includes(caption), `${surface} lost the caption "${caption}"`).toBe(true)
      }
    }
    // The twin carries it as the fence's info string, which is where it was authored.
    for (const { twin } of pages) {
      for (const caption of [...readDist(twin).matchAll(captionPattern)].map((m) => m[1] ?? "")) {
        expect(readDist(twin)).toContain(caption)
      }
    }
  })

  it("renders a diagram on a page under the site's base segment, not only at the root", () => {
    /*
     * Not a base assertion about the diagram itself — the SVG carries no URL — but a guard that the pages
     * being inspected are the ones the site actually serves. A `dist/` tree read at the wrong prefix would
     * make every case above pass over files nobody fetches.
     */
    for (const { page } of pages) {
      const html = readDist(page)
      const canonical = /<link rel="canonical" href="([^"]+)"/.exec(html)?.[1] ?? ""
      expect(new URL(canonical).pathname.startsWith(segment), `${page}: ${canonical}`).toBe(true)
    }
  })
})
