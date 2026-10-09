import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join } from "node:path"
import { describe, expect, it } from "vitest"

import {
  type MarkdownLinksOptions,
  pageSlug,
  relativeMarkdownLink,
  resolveMarkdownLink,
  rewriteTwin,
  twinOf
} from "../src/lib/markdown-links.js"

/**
 * The relative `.md` link rewrite, on a collection built here, so every branch is pinned without a
 * site build: a sibling, a parent-relative and a root page link become routes, a link to a Markdown
 * file the site does not publish becomes its GitHub blob, and one to nothing stays as written for
 * the links validator and `docs:links` to fail on. The built-site half is `scripts/docs_links.py`.
 */

const collection = mkdtempSync(join(tmpdir(), "markdown-links-"))
for (const page of [
  "README.md",
  "index.md",
  "agents.md",
  "architecture/module-map.md",
  "insights/business-logic.md",
  "insights/contract-map.md"
]) {
  const path = join(collection, page)
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, "---\ntitle: x\n---\n")
}

const tracked = new Set(["docs/CONTRACT.md", "docs/insights/business-logic.md", "README.md"])

const options: MarkdownLinksOptions = {
  collectionRoot: collection,
  repoRoot: "/unused",
  treeDir: "docs",
  authoredDir: "site/authored",
  syncedPaths: new Set([
    "README.md",
    "architecture/module-map.md",
    "insights/business-logic.md",
    "insights/contract-map.md"
  ]),
  repoUrl: "https://github.com/o/r",
  commit: "abc123",
  siteBase: "/atif-sql",
  existsAtCommit: (path) => tracked.has(path)
}

describe("relativeMarkdownLink", () => {
  it("takes a relative path to a .md file and nothing else", () => {
    expect(relativeMarkdownLink("../a/b.md#x")).toEqual({ path: "../a/b.md", hash: "#x" })
    expect(relativeMarkdownLink("b.md")).toEqual({ path: "b.md", hash: "" })
    for (const url of ["/a.md", "//h/a.md", "https://h/a.md", "#a", "a.txt", "mailto:a@b.md", ""]) {
      expect(relativeMarkdownLink(url), url).toBeUndefined()
    }
  })
})

describe("resolveMarkdownLink", () => {
  it("resolves a parent-relative link against the source file, not the URL", () => {
    expect(
      resolveMarkdownLink("insights/business-logic.md", "../architecture/module-map.md#x", options)
    ).toEqual({ kind: "page", slug: "architecture/module-map", hash: "#x" })
  })

  it("resolves a sibling", () => {
    expect(resolveMarkdownLink("insights/business-logic.md", "contract-map.md", options)).toEqual({
      kind: "page",
      slug: "insights/contract-map",
      hash: ""
    })
  })

  it("gives README.md its readme route and index.md the root", () => {
    expect(pageSlug("README.md")).toBe("readme")
    expect(pageSlug("index.md")).toBe("")
    expect(resolveMarkdownLink("agents.md", "index.md", options)).toEqual({
      kind: "page",
      slug: "",
      hash: ""
    })
  })

  it("sends an unpublished tree file to its blob at the commit", () => {
    expect(resolveMarkdownLink("README.md", "CONTRACT.md", options)).toEqual({
      kind: "blob",
      repoPath: "docs/CONTRACT.md",
      hash: ""
    })
  })

  it("reports a link to nothing as missing rather than inventing a target", () => {
    expect(
      resolveMarkdownLink("insights/business-logic.md", "../architecture/gone.md", options)
    ).toEqual({ kind: "missing", path: "architecture/gone.md" })
  })
})

describe("rewriteTwin", () => {
  it("leaves a link that resolves from the twin's own URL exactly as authored", () => {
    const body = "[map](../architecture/module-map.md) [sib](contract-map.md#y)\n"
    const twin = twinOf("/atif-sql", "insights/business-logic")
    const result = rewriteTwin(body, "insights/business-logic.md", twin, options)
    expect(result).toEqual({ body, rewritten: 0 })
  })

  it("rewrites what the twin's URL cannot reach, and nothing inside a fence", () => {
    const body = "[c](CONTRACT.md) [home](index.md)\n```\n[c](CONTRACT.md)\n```\n"
    const result = rewriteTwin(body, "README.md", twinOf("/atif-sql", "readme"), options)
    expect(result.rewritten).toBe(1)
    expect(result.body).toBe(
      "[c](https://github.com/o/r/blob/abc123/docs/CONTRACT.md) [home](index.md)\n" +
        "```\n[c](CONTRACT.md)\n```\n"
    )
    // The twin of README.md is served lowercased, so the authored case no longer reaches it.
    expect(rewriteTwin("[t](README.md)\n", "agents.md", "/atif-sql/agents.md", options)).toEqual({
      body: "[t](/atif-sql/readme.md)\n",
      rewritten: 1
    })
  })

  it("addresses the root twin as index.md, never the dotfile the Pages artifact drops", () => {
    expect(twinOf("/atif-sql", "")).toBe("/atif-sql/index.md")
  })
})
