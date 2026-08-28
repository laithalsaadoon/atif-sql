import { existsSync, readdirSync, readFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { describe, expect, it } from "vitest"

/*
 * TWO IMPORT SPECIFIERS TO ADJUST when the modules live elsewhere. The constants are IMPORTED rather
 * than restated in `CONFIG`, and the difference matters: a test that hardcodes the note's label or class
 * still passes after someone renames it in the source, which is the rename this probe exists to catch.
 */
import { AGENT_NOTE_CLASS, AGENT_NOTE_LABEL } from "../src/lib/agent-note.js"
import { SITE_BASE, SITE_ORIGIN } from "../src/lib/repo.js"
import {
  DEEP_LINK_BUDGET,
  DEEP_LINK_TARGETS,
  deepLink,
  deepLinks,
  discoveryLinks,
  rawMarkdownUrl,
  referencePrompt,
  siteUrl
} from "../src/lib/agent-surface.js"

/**
 * The dead-button lock, and the checks over the built agent surface.
 *
 * Every case here reads `dist/`. That is the point rather than an inconvenience: the subject of these
 * assertions is the bytes a browser and an agent actually receive, and a check against a component's
 * inputs passes while the emitted href is wrong. Wire this suite behind the build in the task runner so
 * the directory is present in an ordered run.
 *
 * Every quantity is DERIVED from the build. An assertion that four controls ship stops being true the
 * day a fifth is added, and it reports as a defect in the change rather than in the test. The one
 * exception is `CONFIG.verifiedDeepLinks`, whose literals are facts about systems this repo does not
 * control, established by probing them: a literal is the honest form there, and the failure it prevents
 * is a control whose href has drifted from its target's published shape.
 */

/* =================================================================================================
 * CONFIG — the whole site-specific surface of this file.
 * ================================================================================================= */

const CONFIG = {
  /** Built output, relative to the package root. */
  dist: "dist",
  /** Authored content, relative to the package root, as path segments. */
  content: ["src", "content", "docs"],

  /**
   * The deployed origin and the base segment, IMPORTED from the module `astro.config.ts` reads them
   * from, so the suite and the build cannot disagree about which output is being inspected. An
   * environment variable defaulting to `/` would default to the one base at which every base assertion
   * here is vacuous.
   */
  origin: SITE_ORIGIN,
  base: SITE_BASE,

  /**
   * A different NON-ROOT base for the pure-function cases. Base handling asserted at `/` is vacuous,
   * because every consumer of the base is a no-op there, and asserting it at the site's own base would
   * pass on a helper that ignored its argument and hardcoded the value. Keep this different from `base`.
   */
  probeBase: "/product",

  /** The agent page's content-collection entry id. */
  agentPage: "agents",

  /**
   * The floor on how many pages carry an agent note. A convention used once is not a convention.
   *
   * Two is the floor rather than a larger number because the site AUTHORS two pages — the landing page
   * and the agent page — and the rest of the corpus is a generated documentation tree the site publishes
   * without editing. This is a floor on a practice, not a count of the corpus, so it does not go stale
   * as the tree grows.
   */
  minAgentNotePages: 2,

  /** The relations the head discovery block emits, in order. */
  discoveryRels: ["alternate", "index", "llms-full-txt"] as const,

  /** Which of those relations this site invented rather than adopted. */
  inventedRels: ["llms-full-txt"] as const,

  /**
   * The verified deep-link formats, probed 2026-08-12. RE-PROBE AND UPDATE THE DATE when copying this
   * in: a vendor parameter that changed spelling produces a control that opens the assistant with an
   * empty prompt, and nothing on the page looks wrong.
   *
   * Codex is absent because it has no web prompt parameter — confirmed absent rather than merely
   * undocumented — so the missing row is an assertion, not an omission.
   */
  verifiedDeepLinks: [
    ["chatgpt", "https://chatgpt.com/?q="],
    ["claude", "https://claude.ai/new?q="],
    ["claude-code", "https://claude.ai/code?prompt="],
    ["cursor", "https://cursor.com/link/prompt?text="]
  ] as ReadonlyArray<readonly [string, string]>,

  /**
   * Nouns a registry owns. A digit-led quantity in front of one of these on the agent page is a
   * hand-written count, which is right on the day it is typed and wrong from the next commit.
   */
  countedNouns: ["commands?", "tools?", "error codes?", "response types?", "topics?", "pages?"],

  /**
   * Class-name fragments and declarations that remove content from sight while leaving it in the DOM.
   * Used by the cloaking probe, which is the one check in this file that asserts an ABSENCE.
   */
  hidingClasses: ["sr-only", "visually-hidden", "screen-reader", "visuallyhidden", "hidden-text"],
  hidingDeclarations: [
    /display\s*:\s*none/i,
    /visibility\s*:\s*hidden/i,
    /clip\s*:\s*rect/i,
    /clip-path\s*:\s*inset\(\s*(?:100%|50%)/i,
    /font-size\s*:\s*0(?![.\d])/i,
    /opacity\s*:\s*0(?![.\d])/i,
    /(?:^|[;{\s])(?:width|height)\s*:\s*1px/i,
    /(?:left|top)\s*:\s*-\d{4,}px/i,
    /text-indent\s*:\s*-\d{4,}px/i
  ]
} as const

/* =================================================================================================
 * Reading the build.
 * ================================================================================================= */

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const dist = join(root, CONFIG.dist)
const content = join(root, ...CONFIG.content)

/** The base as a segment with a trailing slash, which is what every path comparison below wants. */
const segment = CONFIG.base.endsWith("/") ? CONFIG.base : `${CONFIG.base}/`

/** The context the pure functions are exercised with: a real origin and a non-root base. */
const CONTEXT = { site: new URL(CONFIG.origin), base: CONFIG.probeBase }

/** Same, as the URL prefix everything built from it must start with. */
const PROBE_PREFIX = `${CONFIG.origin}${CONFIG.probeBase}/`

/**
 * The context matching the build being read. Used only where an assertion compares a produced URL against
 * a path on disk; every case that exercises base handling itself uses `CONTEXT`, whose base is non-root.
 */
const BUILT_CONTEXT = { site: new URL(CONFIG.origin), base: CONFIG.base }

const readDist = (relative: string): string => {
  const path = join(dist, relative)
  if (!existsSync(path)) {
    throw new Error(`\`${CONFIG.dist}/${relative}\` is absent — build before running this suite`)
  }
  return readFileSync(path, "utf8")
}

/** A site-absolute path as a path inside `dist/`. */
const served = (path: string): string =>
  path.startsWith(segment) ? path.slice(segment.length) : path.replace(/^\/+/, "")

/** Every page directory the build emitted, as the site-absolute paths a browser would request. */
const builtPages = (): ReadonlyArray<string> => {
  const walk = (directory: string, prefix: string): ReadonlyArray<string> =>
    readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
      if (entry.name.startsWith("_") || entry.name === "pagefind") return []
      if (entry.isDirectory()) return walk(join(directory, entry.name), `${prefix}${entry.name}/`)
      return entry.name === "index.html" ? [prefix] : []
    })
  return walk(dist, segment)
}

/** One built page's HTML. */
const pageHtml = (path: string): string => readDist(join(served(path), "index.html"))

/** Every `href` in a built document. */
const hrefs = (html: string): ReadonlyArray<string> =>
  [...html.matchAll(/href="([^"]*)"/g)].map((match) => match[1] ?? "")

/** Every stylesheet the build emitted. */
const builtStylesheets = (): ReadonlyArray<string> => {
  const walk = (directory: string): ReadonlyArray<string> =>
    readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
      const path = join(directory, entry.name)
      if (entry.isDirectory()) return walk(path)
      return entry.name.endsWith(".css") ? [path] : []
    })
  return walk(dist)
}

/** The authored source of one entry, whichever extension it uses. */
const authoredSource = (page: string): string => {
  const md = join(content, `${page}.md`)
  return readFileSync(existsSync(md) ? md : join(content, `${page}.mdx`), "utf8")
}

/**
 * Every internal link in the agent page's BODY, as the site-absolute paths a browser would request.
 *
 * The body rather than the whole document, because the `<head>`'s machine-surface relations are asserted
 * on their own terms above and would otherwise be counted as navigation.
 */
const agentPageInternalLinks = (): ReadonlyArray<string> => {
  const document = readDist(join(CONFIG.agentPage, "index.html"))
  const body = document.slice(document.indexOf("<body"))
  return hrefs(body).filter((href) => href.startsWith(segment) && !href.startsWith("//"))
}

/* =================================================================================================
 * The base segment. Asserted rather than trusted: it is the input every other case below is read
 * through, so a wrong value here makes the whole suite inspect a path that does not exist.
 * ================================================================================================= */

describe("the base segment the build actually shipped", () => {
  it("is a path segment rather than a domain root, or every base case here is vacuous", () => {
    expect(segment.startsWith("/")).toBe(true)
    expect(segment.endsWith("/")).toBe(true)
    expect(segment).not.toBe("/")
    // A base joined by concatenation somewhere would show up as a doubled or host-shaped prefix.
    expect(segment.startsWith("//")).toBe(false)
  })

  it("prefixes every canonical URL the build emitted", () => {
    const pages = builtPages()
    expect(pages.length).toBeGreaterThan(0)
    for (const path of pages) {
      const canonical = /<link rel="canonical" href="([^"]+)"/.exec(pageHtml(path))?.[1]
      expect(canonical, `${path} has no canonical link`).toBeDefined()
      const url = new URL(canonical ?? "")
      expect(url.origin).toBe(CONFIG.origin)
      expect(url.pathname.startsWith(segment), `${path}: ${canonical} drops the base`).toBe(true)
      // The base appears once. Twice is what string concatenation of base and path produces.
      expect(url.pathname.startsWith(`${segment}${segment.slice(1)}`)).toBe(false)
    }
  })

  it("prefixes the discovery hrefs and the machine surfaces alike", () => {
    for (const surface of ["llms.txt", "llms-full.txt", "llms-small.txt"]) {
      expect(siteUrl(surface, BUILT_CONTEXT).pathname).toBe(`${segment}${surface}`)
      expect(existsSync(join(dist, surface)), `dist/${surface} is absent`).toBe(true)
    }
  })
})

/* =================================================================================================
 * The deep-link format lock.
 * ================================================================================================= */

describe("the deep-link format lock", () => {
  const VERIFIED = new Map(CONFIG.verifiedDeepLinks)

  const page = {
    title: "For agents",
    pageUrl: `${PROBE_PREFIX}${CONFIG.agentPage}/`,
    markdownUrl: `${PROBE_PREFIX}${CONFIG.agentPage}.md`
  }

  it("ships a control for every verified target and for no other", () => {
    expect(DEEP_LINK_TARGETS.map((target) => target.id).sort()).toEqual([...VERIFIED.keys()].sort())
  })

  it("ships no control on a desktop URL scheme, which does nothing without the app installed", () => {
    for (const target of DEEP_LINK_TARGETS) {
      expect(target.endpoint.startsWith("https://")).toBe(true)
      expect(new URL(target.endpoint).hostname).not.toBe("")
    }
  })

  it.each([...VERIFIED])("builds %s's href in its verified format", (id, format) => {
    const target = DEEP_LINK_TARGETS.find((candidate) => candidate.id === id)
    if (target === undefined) throw new Error(`no target \`${id}\``)
    for (const body of ["", "a short body", "x".repeat(50_000)]) {
      const link = deepLink(target, page, body)
      expect(link.href.startsWith(format)).toBe(true)
      // A format prefix with an empty payload behind it is the dead button in its purest form.
      expect(link.href.length).toBeGreaterThan(format.length)
      expect(link.href.length).toBeLessThanOrEqual(DEEP_LINK_BUDGET)
    }
  })

  it("labels every control as opening the target, never as asking it", () => {
    for (const target of DEEP_LINK_TARGETS) {
      expect(target.label.startsWith("Open in ")).toBe(true)
      expect(target.label.toLowerCase()).not.toContain("ask")
    }
  })

  it("respects a vendor ceiling stricter than ours", () => {
    const [first] = DEEP_LINK_TARGETS
    if (first === undefined) throw new Error("the target table is empty")
    const link = deepLink({ ...first, vendorLimit: 400 }, page, "y".repeat(5_000))
    expect(link.href.length).toBeLessThanOrEqual(400)
    expect(link.carriesContent).toBe(false)
  })

  it("carries the page's Markdown when it fits and its URL when it does not", () => {
    /*
     * Read back through `searchParams` rather than `decodeURIComponent`: a query string encodes a space
     * as `+`, which `decodeURIComponent` leaves alone, so decoding by hand compares the payload against
     * a string it can never equal.
     */
    const payload = (link: (typeof short)[number]): string =>
      new URL(link.href).searchParams.get(link.target.parameter) ?? ""

    const short = deepLinks(page, "one small claim")
    expect(short.every((link) => link.carriesContent)).toBe(true)
    for (const link of short) expect(payload(link)).toContain("one small claim")

    const long = deepLinks(page, "z".repeat(50_000))
    expect(long.every((link) => link.carriesContent)).toBe(false)
    // The fallback is a redirection rather than a truncation: the page is still named.
    for (const link of long) {
      expect(payload(link)).toContain(page.markdownUrl)
      expect(payload(link)).not.toContain("zzzz")
    }
  })

  it("refuses to ship a truncated prompt when even the reference form overflows", () => {
    const [first] = DEEP_LINK_TARGETS
    if (first === undefined) throw new Error("the target table is empty")
    expect(() => deepLink({ ...first, vendorLimit: 40 }, page, "body")).toThrow(/over the 40 ceiling/)
  })

  it("names the page in every prompt, so a prefill is never contextless", () => {
    const prompt = referencePrompt(page)
    expect(prompt).toContain(page.title)
    expect(prompt).toContain(page.markdownUrl)
    expect(prompt).toContain(page.pageUrl)
  })
})

/* =================================================================================================
 * Every shipped href, read from dist.
 * ================================================================================================= */

describe("every shipped href in the build", () => {
  const targetPrefixes = DEEP_LINK_TARGETS.map(
    (target) => `${target.endpoint}${target.endpoint.includes("?") ? "&" : "?"}${target.parameter}=`
  )

  const shipped = (): ReadonlyArray<{ page: string; href: string }> =>
    builtPages().flatMap((path) =>
      hrefs(pageHtml(path))
        .filter((href) => targetPrefixes.some((prefix) => href.startsWith(prefix)))
        .map((href) => ({ page: path, href }))
    )

  it("gives every built page one control per target", () => {
    const pages = builtPages()
    expect(pages.length).toBeGreaterThan(0)
    // Derived on both sides: neither the page count nor the target count is written down here.
    expect(shipped()).toHaveLength(pages.length * DEEP_LINK_TARGETS.length)
  })

  it("keeps every shipped href inside the budget", () => {
    for (const { page, href } of shipped()) {
      expect(href.length, `${page} exceeds the budget`).toBeLessThanOrEqual(DEEP_LINK_BUDGET)
    }
  })

  it("carries a payload behind every shipped href", () => {
    for (const { page, href } of shipped()) {
      const prefix = targetPrefixes.find((candidate) => href.startsWith(candidate)) ?? ""
      expect(href.length, `${page} has a control with an empty payload`).toBeGreaterThan(prefix.length)
    }
  })

  it("emits no protocol-relative href anywhere", () => {
    for (const path of builtPages()) {
      for (const href of hrefs(pageHtml(path))) {
        expect(href.startsWith("//"), `${path} emits a protocol-relative href`).toBe(false)
      }
    }
  })
})

/* =================================================================================================
 * The head discovery block.
 * ================================================================================================= */

describe("the head discovery block", () => {
  it("points at this page's raw route, and at both machine surfaces", () => {
    const links = discoveryLinks("guide/install", CONTEXT)
    expect(links.map((link) => link.rel)).toEqual([...CONFIG.discoveryRels])
    for (const link of links) {
      expect(link.type).toBe("text/markdown")
      expect(new URL(link.href).origin).toBe(CONTEXT.site.origin)
      expect(link.href.startsWith(PROBE_PREFIX)).toBe(true)
    }
  })

  it("declares which relations are conventions and which are ours", () => {
    const links = discoveryLinks(CONFIG.agentPage, CONTEXT)
    const invented = links.filter((link) => link.warrant === "invention")
    expect(invented.map((link) => link.rel)).toEqual([...CONFIG.inventedRels])
  })

  it("adds no relation that has no adopters", () => {
    const rels = discoveryLinks(CONFIG.agentPage, CONTEXT).map((link) => link.rel)
    expect(rels).not.toContain("describedby")
  })

  it("is present on every built page, with a target that resolves on disk", () => {
    for (const path of builtPages()) {
      const document = pageHtml(path)
      for (const rel of CONFIG.discoveryRels) {
        const match = new RegExp(`<link rel="${rel}"[^>]*href="([^"]+)"`).exec(document)
        expect(match, `${path} has no rel="${rel}"`).not.toBeNull()
        const href = match?.[1] ?? ""
        expect(href.startsWith("//")).toBe(false)
        const target = served(new URL(href).pathname)
        expect(existsSync(join(dist, target)), `${path}: ${href} resolves to nothing`).toBe(true)
      }
    }
  })
})

/* =================================================================================================
 * The JSON-LD graph.
 * ================================================================================================= */

describe("the JSON-LD graph", () => {
  /** The one `application/ld+json` block on a page, parsed. A parse error IS the failure. */
  const graphOf = (path: string): { "@graph": ReadonlyArray<Record<string, unknown>> } => {
    const blocks = [
      ...pageHtml(path).matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)
    ]
    expect(blocks.length, `${path} has ${blocks.length} ld+json blocks, not one`).toBe(1)
    return JSON.parse(blocks[0]?.[1] ?? "") as {
      "@graph": ReadonlyArray<Record<string, unknown>>
    }
  }

  it("parses on every built page, as exactly one graph", () => {
    const pages = builtPages()
    expect(pages.length).toBeGreaterThan(0)
    for (const path of pages) {
      const graph = graphOf(path)
      expect(graph["@graph"].length).toBeGreaterThan(1)
      expect(graph["@graph"].map((node) => node["@type"])).toContain("TechArticle")
      expect(graph["@graph"].map((node) => node["@type"])).toContain("WebSite")
    }
  })

  it("agrees with the page's own canonical URL, which no human clicks to find out", () => {
    for (const path of builtPages()) {
      const canonical = /<link rel="canonical" href="([^"]+)"/.exec(pageHtml(path))?.[1]
      const urls = graphOf(path)["@graph"].map((node) => node["url"])
      expect(urls, `${path}: no graph node names the canonical URL ${canonical}`).toContain(canonical)
    }
  })

  it("states where this page's Markdown is, so a consumer needs no knowledge of the convention", () => {
    for (const path of builtPages()) {
      const article = graphOf(path)["@graph"].find((node) => node["@type"] === "TechArticle")
      const encoding = article?.["encoding"] as Record<string, unknown> | undefined
      expect(encoding?.["encodingFormat"]).toBe("text/markdown")
      const href = String(encoding?.["contentUrl"] ?? "")
      const id = served(path).replace(/\/$/, "")
      expect(href).toBe(rawMarkdownUrl(id, BUILT_CONTEXT).href)
      expect(existsSync(join(dist, served(new URL(href).pathname))), `${href} is absent`).toBe(true)
    }
  })

  it("claims no search endpoint, because this site's search runs in the browser", () => {
    /*
     * A `SearchAction` is a promise that a GET against a URL template returns results. Starlight's search
     * is Pagefind, which indexes and queries entirely client-side, so no route answers one — and a
     * machine-readable claim that 404s on the first consumer to follow it is worse than an absent field.
     */
    for (const path of builtPages()) {
      const site = graphOf(path)["@graph"].find((node) => node["@type"] === "WebSite")
      expect(site?.["potentialAction"]).toBeUndefined()
    }
  })
})

/* =================================================================================================
 * The raw-Markdown route.
 * ================================================================================================= */

describe("the raw-Markdown route", () => {
  it("maps the site root to the route the raw-twin plugin actually injects", () => {
    // The root entry's id is the empty string or `index`, and both map to `<base>/.md`.
    expect(rawMarkdownUrl("", CONTEXT).pathname).toBe(`${CONFIG.probeBase}/.md`)
    expect(rawMarkdownUrl("index", CONTEXT).pathname).toBe(`${CONFIG.probeBase}/.md`)
    expect(rawMarkdownUrl("guide/install", CONTEXT).pathname).toBe(
      `${CONFIG.probeBase}/guide/install.md`
    )
  })

  it("keeps the base segment out of the origin, however the base is written", () => {
    for (const base of [CONFIG.probeBase, `${CONFIG.probeBase}/`]) {
      const url = siteUrl("llms.txt", { site: CONTEXT.site, base })
      expect(url.href).toBe(`${PROBE_PREFIX}llms.txt`)
      // The bug this forbids: a URL naming a HOST, from a base joined by concatenation.
      expect(url.href.startsWith("//")).toBe(false)
    }
  })

  it("has a twin on disk for every built page", () => {
    for (const path of builtPages()) {
      const id = served(path).replace(/\/$/, "")
      // The twin's location is derived through the module under test, not restated here.
      const twin = served(rawMarkdownUrl(id, BUILT_CONTEXT).pathname)
      expect(existsSync(join(dist, twin)), `${path} has no \`.md\` twin at ${twin}`).toBe(true)
    }
  })
})

/* =================================================================================================
 * The agent note reaches all three surfaces, not one.
 * ================================================================================================= */

describe("the agent note survives into every surface", () => {
  /** Every authored page carrying a note, found rather than listed. */
  const authored = (): ReadonlyArray<string> => {
    const walk = (directory: string, prefix: string): ReadonlyArray<string> =>
      readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
        if (entry.isDirectory()) return walk(join(directory, entry.name), `${prefix}${entry.name}/`)
        if (!/\.mdx?$/.test(entry.name)) return []
        const body = readFileSync(join(directory, entry.name), "utf8")
        if (!body.includes(":::agent")) return []
        return [`${prefix}${entry.name.replace(/\.mdx?$/, "")}`]
      })
    return walk(content, "")
  }

  it("is used on the agent page and where behavior genuinely differs", () => {
    const pages = authored()
    expect(pages).toContain(CONFIG.agentPage)
    expect(pages.length).toBeGreaterThanOrEqual(CONFIG.minAgentNotePages)
  })

  it("opens every block with the label, since only visible text survives the bundle", () => {
    for (const page of authored()) {
      const source = authoredSource(page)
      const blocks = source.split(":::agent").slice(1)
      expect(blocks.length).toBeGreaterThan(0)
      for (const block of blocks) {
        expect(block.trimStart().startsWith(`**${AGENT_NOTE_LABEL}.**`), page).toBe(true)
      }
      // A directive label would be escaped into `:::agent\[…]` on the raw route.
      expect(source).not.toMatch(/:::agent\[/)
    }
  })

  it("renders as a marked block in the HTML, not as a bare div", () => {
    for (const page of authored()) {
      const document = readDist(join(page === "index" ? "" : page, "index.html"))
      const notes = document.split(`class="${AGENT_NOTE_CLASS}"`).length - 1
      expect(notes, `${page} lost its note in the HTML`).toBeGreaterThan(0)
      expect(document).toContain(AGENT_NOTE_LABEL)
    }
  })

  it("passes verbatim into the page's `.md` twin, directive and label both", () => {
    for (const page of authored()) {
      const twin = readDist(`${page === "index" ? "" : page}.md`)
      expect(twin, `${page}.md lost the directive`).toContain(":::agent")
      expect(twin, `${page}.md lost the label`).toContain(`**${AGENT_NOTE_LABEL}.**`)
      expect(twin).not.toContain(":::agent\\[")
    }
  })

  it("reaches `llms-full.txt` with the label intact, once per authored note", () => {
    const bundle = readDist("llms-full.txt")
    const expected = authored().reduce(
      (total, page) => total + authoredSource(page).split(":::agent").length - 1,
      0
    )
    expect(expected).toBeGreaterThan(0)
    expect(bundle.split(`**${AGENT_NOTE_LABEL}.**`).length - 1).toBe(expected)
  })
})

/* =================================================================================================
 * The cloaking probe. The one check here that asserts an absence.
 * ================================================================================================= */

describe("no agent-addressed content is hidden from human readers", () => {
  /**
   * Content served to a machine and hidden from every human is cloaking wearing an accessibility class
   * name. The rule is easy to state in prose and easy to violate by accident — a note that looks noisy
   * gets an `.sr-only` and the diff reads like a styling change — so it is asserted instead. A principle
   * in prose is an opinion; a principle in a test is a rule.
   */

  const hidingSignals = [
    ...CONFIG.hidingClasses,
    'aria-hidden="true"',
    "hidden=",
    "display:none",
    "display: none"
  ]

  it("opens no note inside an element that removes it from sight", () => {
    /*
     * The reading: every note opens with the label as its first visible text, so the element enclosing it
     * opens within a short window before the label. The window is bounded rather than parsed because a
     * regex cannot track nesting; a hiding wrapper further out than this window is caught by the
     * stylesheet case below, which needs no nesting at all.
     */
    for (const path of builtPages()) {
      const document = pageHtml(path)
      let at = document.indexOf(AGENT_NOTE_LABEL)
      while (at !== -1) {
        const window = document.slice(Math.max(0, at - 400), at)
        for (const signal of hidingSignals) {
          expect(window.includes(signal), `${path}: a note is hidden by \`${signal}\``).toBe(false)
        }
        at = document.indexOf(AGENT_NOTE_LABEL, at + 1)
      }
    }
  })

  it("never combines the note class with a hiding class", () => {
    for (const path of builtPages()) {
      for (const attribute of pageHtml(path).matchAll(/class="([^"]*)"/g)) {
        const classes = (attribute[1] ?? "").split(/\s+/)
        if (!classes.includes(AGENT_NOTE_CLASS)) continue
        for (const hiding of CONFIG.hidingClasses) {
          expect(classes.some((name) => name.includes(hiding)), `${path}: ${attribute[1]}`).toBe(false)
        }
      }
    }
  })

  it("ships no stylesheet rule that hides the note class", () => {
    /*
     * The teeth of this probe. A block visible in the HTML and `display:none` in the CSS is the same
     * cloak with the evidence moved one file over, and it is the form that survives a review of the
     * markup. Declaration blocks are read individually, so a rule nested in an at-rule is still checked.
     */
    const sheets = builtStylesheets()
    expect(sheets.length, "no stylesheet was emitted, so this case is vacuous").toBeGreaterThan(0)
    for (const sheet of sheets) {
      const css = readFileSync(sheet, "utf8")
      for (const block of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        const selector = block[1] ?? ""
        const body = block[2] ?? ""
        if (!selector.includes(`.${AGENT_NOTE_CLASS}`)) continue
        for (const declaration of CONFIG.hidingDeclarations) {
          expect(
            declaration.test(body),
            `${sheet}: \`${selector.trim()}\` hides the note with \`${body.trim()}\``
          ).toBe(false)
        }
      }
    }
  })
})

/* =================================================================================================
 * The agent page's own entry points.
 * ================================================================================================= */

describe("the agent page's own entry points", () => {
  it("is the first entry `llms.txt` lists", () => {
    const listed = [...readDist("llms.txt").matchAll(/^- \[[^\]]+\]\(([^)]+)\)/gm)].map(
      (match) => match[1] ?? ""
    )
    expect(listed.length).toBeGreaterThan(1)
    expect(listed[0]).toBe(`${CONFIG.origin}${segment}${CONFIG.agentPage}.md`)
  })

  it("is reachable from the site navigation on every page", () => {
    for (const path of builtPages()) {
      expect(hrefs(pageHtml(path)), `${path} cannot reach the agent page`).toContain(
        `${segment}${CONFIG.agentPage}/`
      )
    }
  })

  it("resolves every internal link it carries to a real file in the build", () => {
    /*
     * Stricter than a link validator, and for a reason a validator cannot get around: a validator can
     * only judge a target whose headings it recorded in its own Markdown pass, and a loader-injected page
     * has no source file for that pass — so every link into a generated tier reports invalid and gets
     * excluded. This resolves against the bytes on disk instead, which needs no source file.
     *
     * Scoped to the body: a link a reader or an agent can follow. The `<head>` carries the
     * machine-surface relations, covered above on their own terms.
     */
    const internal = agentPageInternalLinks()
    expect(internal.length).toBeGreaterThan(0)
    for (const href of internal) {
      const target = served(href.split("#")[0] ?? "")
      const candidates = [target, join(target, "index.html")]
      expect(
        candidates.some((candidate) => existsSync(join(dist, candidate))),
        `${href} resolves to nothing in the build`
      ).toBe(true)
    }
  })

  it("lists every other built page, and each one's twin, in its read-next table", () => {
    /*
     * The read-next table is the one GENERATED region of the agent page, and the invariant is coverage
     * rather than a row count: a page added to the documentation tree has to appear without an edit, and
     * a removed page must not leave a row behind. Both sides are derived — the expected set from the
     * pages the build emitted, the actual set from the emitted HTML — so neither can be satisfied by a
     * number written down here.
     *
     * The page's own row is excluded: a page linking to itself is not a place to go next.
     */
    const internal = new Set(agentPageInternalLinks().map((href) => href.split("#")[0] ?? ""))
    const others = builtPages().filter((path) => path !== `${segment}${CONFIG.agentPage}/`)
    expect(others.length).toBeGreaterThan(0)
    for (const path of others) {
      expect(internal.has(path), `the read-next table omits the page ${path}`).toBe(true)
      const id = served(path).replace(/\/$/, "")
      const twin = rawMarkdownUrl(id, BUILT_CONTEXT).pathname
      expect(internal.has(twin), `the read-next table omits the twin ${twin}`).toBe(true)
    }
  })

  it("carries no unexpanded generator token, which would ship as visible nonsense", () => {
    /*
     * The read-next table is substituted into a copy of the authored page by `scripts/authored-pages.mjs`.
     * The placeholder is a bare token rather than an HTML comment, because the raw-twin builder parses
     * every body through `remark-mdx`, where `<!--` is a parse error. So a substitution that did not run
     * ships as readable text on the page instead of failing the build, and this is what catches it.
     */
    expect(authoredSource(CONFIG.agentPage)).not.toMatch(/GENERATED-[A-Z-]+/)
    expect(readDist(join(CONFIG.agentPage, "index.html"))).not.toMatch(/GENERATED-[A-Z-]+/)
    expect(readDist(`${CONFIG.agentPage}.md`)).not.toMatch(/GENERATED-[A-Z-]+/)
  })

  it("states no count, because a hand-written count is a lie told to the reader who trusts it", () => {
    const source = authoredSource(CONFIG.agentPage)
    const counted = new RegExp(`\\b\\d+\\s+(?:${CONFIG.countedNouns.join("|")})\\b`, "i")
    expect(source).not.toMatch(counted)
  })

  it("writes no brace anchor, which would break its own raw route", () => {
    const source = authoredSource(CONFIG.agentPage)
    expect(source).not.toMatch(/\{\s*#[a-z0-9-]+\s*\}/)
  })

  it("numbers its sections from one, contiguously, in the heading text", () => {
    /*
     * The number lives in the heading TEXT so that every surface agrees on it: the rendered page, the
     * table of contents, the search index, `llms.txt`, and the raw Markdown. The alternative — an
     * explicit `{ #anchor }` — carries a brace into the raw route, where the MDX parser reads it as an
     * expression and one occurrence fails the whole route.
     */
    const headings = authoredSource(CONFIG.agentPage)
      .split("\n")
      .filter((line) => line.startsWith("## "))
    expect(headings.length).toBeGreaterThan(1)
    expect(headings.map((heading) => heading.split(" ")[1])).toEqual(
      headings.map((_, at) => `${at + 1}.`)
    )
  })
})
