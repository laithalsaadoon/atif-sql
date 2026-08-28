import { readdirSync, readFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { describe, expect, it } from "vitest"

import { braceOffenders, maskedBody } from "../gates/brace-gate.mjs"

/**
 * The NEGATIVE CONTROL for the brace gate, plus the scan it performs over the published corpus.
 *
 * `gates/brace-gate.mjs` reports an empty finding set, and an empty finding set is what a broken scanner
 * reports too. "The corpus is clean" and "the check is broken" are the same green without a case that
 * proves the scanner fires — so the poison here is a synthetic input to the same function the gate calls,
 * which means the control cannot rot away from the gate it verifies.
 *
 * The escape-hatch case matters as much as the poison: without it the pair is satisfied by a scanner that
 * refuses every brace, including the ones the build accepts, and the fix it would demand is to rewrite a
 * REST path that documents an API correctly.
 */

const scanner = braceOffenders as (markdown: string) => ReadonlyArray<{
  line: number
  column: number
  text: string
}>
const mask = maskedBody as (markdown: string) => string

/** The published content corpus: exactly the pages that reach the raw-twin builder's MDX parser. */
const content = join(dirname(dirname(fileURLToPath(import.meta.url))), "src", "content", "docs")

const markdownUnder = (root: string): ReadonlyArray<string> =>
  readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const path = join(root, entry.name)
    if (entry.isDirectory()) return entry.name.startsWith(".") ? [] : markdownUnder(path)
    return /\.mdx?$/.test(entry.name) ? [path] : []
  })

describe("the brace gate", () => {
  it("finds a bare brace, and names the line it sits on", () => {
    const poisoned = ["# Title", "", "## GET /v1/exec/{id}", "", "Body."].join("\n")
    const found = scanner(poisoned)
    expect(found.length).toBe(1)
    expect(found[0]?.line).toBe(3)
    expect(found[0]?.text).toContain("{id}")
  })

  it("accepts the same path inside backticks, which is the only legal repair", () => {
    const repaired = ["# Title", "", "## GET `/v1/exec/{id}`", "", "Body."].join("\n")
    expect(scanner(repaired)).toEqual([])
  })

  it("accepts a brace inside a fence, which the MDX parser never enters", () => {
    const fenced = ["# Title", "", "```json", '{ "id": 1 }', "```", "", "Body."].join("\n")
    expect(scanner(fenced)).toEqual([])
  })

  it("accepts a mermaid decision node, so a diagram is not a build failure", () => {
    const diagram = ["```mermaid", "flowchart TD", "  a{Decide} --> b[Do]", "```"].join("\n")
    expect(scanner(diagram)).toEqual([])
  })

  it("ignores frontmatter, which reaches a different parser entirely", () => {
    const stamped = ['---', 'title: "A {braced} title"', "---", "", "Body."].join("\n")
    expect(scanner(stamped)).toEqual([])
  })

  it("keeps every newline while masking, so a reported line number is the file's line number", () => {
    const source = ["# Title", "", "```txt", "{ masked }", "```", "", "Tail {here}"].join("\n")
    expect(mask(source).split("\n").length).toBe(source.split("\n").length)
    expect(scanner(source)[0]?.line).toBe(7)
  })

  it("passes over every page the site publishes", () => {
    const pages = markdownUnder(content)
    expect(pages.length, "no published page was scanned, so this case is vacuous").toBeGreaterThan(0)
    const offenders = pages.flatMap((page) =>
      scanner(readFileSync(page, "utf8")).map(
        (finding) => `${page.slice(content.length + 1)}:${finding.line}  ${finding.text}`
      )
    )
    expect(offenders).toEqual([])
  })
})
