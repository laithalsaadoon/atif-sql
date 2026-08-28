/**
 * Build-time Mermaid rendering for Astro 7: an integration that claims the ```mermaid fence at mdast and
 * replaces it with rendered SVG, so no diagram runtime reaches the browser and the diagram is present in
 * a fetch that runs no JavaScript.
 *
 * **Why build time and not the client.** A client-rendered diagram is absent from every agent surface at
 * once: absent from the page's raw Markdown twin, absent from the llms bundles, and absent from a plain
 * `fetch` of the HTML. The diagram is the densest thing on the page and it is the one thing the machine
 * reader cannot see. Rendering at build time is what makes the surface honest.
 *
 * **Why an mdast visitor and not a hast one.** Expressive Code is itself a hast plugin, pushed as a
 * `hastPlugins` entry by its own integration, so it runs after any hast visitor written here and replaces
 * the whole `pre` subtree the visitor just edited. The failure is measured, not theoretical: a hast plugin
 * that sets `tabIndex` on `pre` and on `table` ships a site whose tables carry the attribute and whose
 * code blocks do not. At mdast the fence is still a fence and nothing downstream has claimed it.
 *
 * **Why the renderer is injected.** The renderer is the part with a dependency, a fidelity ceiling and a
 * licence, and it is the part most likely to be swapped. Passing it in keeps this file assertable with a
 * stub renderer and makes the swap one argument rather than a fork.
 */

import { readdir, readFile } from "node:fs/promises"
import { join } from "node:path"

import type { AstroIntegration } from "astro"
import { renderMermaidSVG } from "beautiful-mermaid"
import { defineMdastPlugin, type MdastPluginDefinition } from "satteri"

/** The fence language claimed. */
export const MERMAID_LANG = "mermaid"

/**
 * The class on the emitted `<figure>`.
 *
 * Named once and shared by the plugin and the build-time check, so the marker the check counts cannot
 * drift from the marker the plugin writes.
 */
export const DEFAULT_CLASS_NAME = "mermaid-figure"

/** What a renderer is told about one fence. */
export interface Diagram {
  /** The fence body, verbatim. */
  readonly source: string
  /** The fence's info string past the language, or `undefined`. */
  readonly meta: string | undefined
  /** Zero-based position among the mermaid fences in this document. */
  readonly index: number
  /**
   * A label for the document, for error messages. Never `undefined`: see the `fileURL` note on
   * `mermaidPlugin`. It is a filesystem path when the compile supplies one and a synthetic label
   * otherwise, so it is fit for a message and not for reading a file.
   */
  readonly label: string
}

/** Turns one fence into an SVG string. May be async; the mdast visitor awaits it. */
export type MermaidRenderer = (diagram: Diagram) => string | Promise<string>

export interface MermaidOptions {
  /** Defaults to `beautifulMermaid()`. */
  readonly renderer?: MermaidRenderer
  /** Wrapper class on the emitted `<figure>`, for the CSS that constrains diagram width. */
  readonly className?: string
}

/**
 * The default renderer: `beautiful-mermaid`.
 *
 * Chosen for three properties, each verified against the installed package at version 1.1.3 on
 * 2026-08-26:
 *
 *  1. **Synchronous and browserless.** `renderMermaidSVG(text, options) => string`. It computes its own
 *     text metrics and lays out with `elkjs`, so a build needs no Chromium download, no `playwright`
 *     install step, and no per-diagram browser round trip. A docs build stays a `node` process.
 *  2. **CSS-custom-property output.** The root element carries `style="--bg:…;--fg:…"` and every other
 *     colour is derived from those two with `color-mix()`. Passing the page theme's own variables — `bg:
 *     "var(--sl-color-bg)"`, `fg: "var(--sl-color-text)"` — makes ONE rendered asset track light and dark
 *     without a second render and without a media query inside the SVG.
 *  3. **It throws on input it does not understand** rather than producing an empty drawing, which is what
 *     lets the guard below be a guard.
 *
 * Rendering only reaches CSS variables when the SVG is INLINED in the document. A `var()` inside an SVG
 * referenced as `<img src="diagram.svg">` resolves in the image's own document, where the page's custom
 * properties do not exist, and the diagram renders with no colour at all. That is why this integration
 * returns inline markup and never writes a file plus an `<img>`.
 *
 * Supported diagram types, probed by rendering each one: `graph`/`flowchart` (all four directions),
 * `stateDiagram-v2`, `sequenceDiagram`, `classDiagram`, `erDiagram`, `xychart-beta`. NOT supported, and
 * each throws: `gantt`, `pie`, `mindmap`, `journey`, and every other header — verified 2026-08-28 by
 * building a `gantt` fence, which failed the build naming the header and the expected alternatives. Pass
 * a `renderer` over `mermaid-isomorphic` when a page needs one of those; see the note below it.
 */
export interface BeautifulMermaidOptions {
  /** Background, written into `--bg`. A `var(...)` reference is passed through verbatim. */
  readonly bg?: string
  /** Foreground, written into `--fg`. A `var(...)` reference is passed through verbatim. */
  readonly fg?: string
  /** Canvas padding in px. The package's own default is 40. */
  readonly padding?: number
  /**
   * Whether to keep the webfont `@import` the renderer writes into the SVG's `<style>`.
   *
   * It is stripped by default, and the reason is a property of the package rather than a preference: the
   * SVG carries `@import url('https://fonts.googleapis.com/css2?family=…')` UNCONDITIONALLY, built by
   * interpolating the `font` option into the URL. There is no option that suppresses it — passing
   * `font: "inherit"` requests a Google font literally named `inherit`. So a build-time render ships a
   * third-party font request on every page carrying a diagram, which is a privacy fact and a render-block
   * that is invisible in the integration's configuration. The declaration it feeds is
   * `font-family: '<font>', system-ui, sans-serif`, so with the `@import` removed the text falls back to
   * the system stack and the layout, computed at build time, does not move.
   */
  readonly webfontImport?: boolean
}

const GOOGLE_FONT_IMPORT = /@import\s+url\((['"])https:\/\/fonts\.googleapis\.com[^'"]*\1\);?[ \t]*\n?/g

/**
 * A fence's info string as HTML text content.
 *
 * The caption is author-written text spliced into generated markup, so `<` and `&` are escaped: an
 * unescaped `<` opens an element inside the figure and takes the rest of the caption with it.
 */
const escapeText = (value: string): string =>
  value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")

/**
 * The default renderer, over the STATICALLY imported `beautiful-mermaid`.
 *
 * THE IMPORT IS STATIC, and it has to be. A `code` visitor runs while Astro renders content, which is
 * after the config-time Vite module runner has been torn down, so a dynamic `import()` reached from
 * there throws `Vite module runner has been closed` — measured 2026-08-28 on astro@7.2.9, where every
 * diagram failed that way and Astro's glob loader logged the failure and continued with an empty page.
 * A static import is resolved by Vite while the config is loading, so the function is already in this
 * module's scope by the time a fence needs it. `starlight-base-path` works around the same tear-down
 * with a `Function`-built native import; a static import needs no workaround at all.
 *
 * Reaching a different renderer — `mermaid-isomorphic` for a diagram type this one does not implement —
 * goes through the `renderer` option, from a project that installs it. A specifier this project does not
 * depend on cannot be imported from here either way.
 *
 * `renderMermaidSVG` is the non-deprecated synchronous export. `renderMermaid` is a deprecated alias of
 * the ASYNC one, so importing that name and forgetting to await it stringifies a `Promise` into the page.
 */
export const beautifulMermaid = (options: BeautifulMermaidOptions = {}): MermaidRenderer => {
  return async (diagram) => {
    const svg = renderMermaidSVG(diagram.source, {
      ...(options.bg === undefined ? {} : { bg: options.bg }),
      ...(options.fg === undefined ? {} : { fg: options.fg }),
      ...(options.padding === undefined ? {} : { padding: options.padding }),
      /*
       * The renderer's own background rectangle is suppressed so the diagram sits on the page's
       * background. `--bg` is still written to the root element and is still what every derived colour
       * mixes against, so contrast is preserved.
       */
      transparent: true
    })
    return options.webfontImport === true ? svg : svg.replace(GOOGLE_FONT_IMPORT, "")
  }
}

/**
 * The fidelity route, for a diagram `beautiful-mermaid` does not implement.
 *
 * `mermaid-isomorphic` runs the real `mermaid` package — the full grammar, every diagram type, exact
 * upstream output — in a headless browser, at the cost of `playwright` and a browser download in CI. It
 * is reached through the `renderer` option and NOT through a loader in this file. A specifier this
 * project does not depend on has no working import shape here: a static or literal one fails to resolve
 * while the config loads, and a variable one reaches the render phase, where the module runner is
 * already closed — the measurement recorded on `beautifulMermaid` above.
 *
 * A renderer supplied that way owns one detail this file cannot: `createMermaidRenderer` returns a BATCH
 * renderer resolving to `PromiseSettledResult[]`, so a diagram that failed to render arrives as a
 * REJECTED entry inside a RESOLVED promise. Re-throw it. Returning the settled array's `.value` without
 * checking `.status` is exactly how a broken fence disappears from a green build.
 */

/**
 * The Sätteri mdast plugin. One instance per document, which is what makes `index` per-document.
 *
 * **The `fileURL` trap.** `ctx.fileURL` is `URL | undefined`: it holds the compile's `fileURL` option,
 * and a content-layer loader that synthesizes an entry body does not have to supply one. The obvious
 * guard — `if (!ctx.fileURL) return` — therefore makes the plugin silently skip every loader-injected
 * page, and skipping is invisible: the page builds, the fence renders as a code block, and the only
 * symptom is a diagram-shaped listing on some pages and not others. So `fileURL` is treated as what it
 * is, a label that may be absent, and it gates nothing:
 *
 *   * The MDX branch reads `ctx.sourceFormat`, which is always `"markdown"` or `"mdx"` and never
 *     undefined, rather than testing the filename extension.
 *   * The error label falls back to a synthetic string, so a message still identifies the document.
 *
 * **Why the MDX branch exists.** An mdast `html` node is raw HTML, which MDX has no node for: in an
 * `.mdx` document the SVG has to arrive as JSX. `set:html` on a `Fragment` is Astro's own accessor for
 * that, and it carries the markup as a string attribute, so a brace inside an SVG label is never read as
 * an expression.
 */
export const mermaidPlugin = (options: MermaidOptions = {}): MdastPluginDefinition => {
  const render = options.renderer ?? beautifulMermaid()
  const className = options.className ?? DEFAULT_CLASS_NAME
  let index = 0

  return defineMdastPlugin({
    name: "mermaid",
    async code(node, ctx) {
      if (node.lang !== MERMAID_LANG) return

      const label = ctx.fileURL === undefined ? "<generated entry>" : ctx.fileURL.pathname
      const at = node.position?.start.line
      const where = at === undefined ? label : `${label}:${at}`

      let svg: string
      try {
        svg = await render({
          source: node.value,
          meta: node.meta ?? undefined,
          index: index++,
          label: where
        })
      } catch (cause) {
        /*
         * THROW. NEVER PASS THE FENCE THROUGH.
         *
         * Returning `undefined` here leaves the node alone, and the fence then ships as a code block
         * full of Mermaid source: a build that succeeded, a page that looks plausible, and a diagram the
         * author believes is rendered. That is the single failure this file exists to prevent, and it is
         * worse than a red build because it is not reported anywhere. `beautiful-mermaid` throws
         * `Invalid mermaid header: "gantt". Expected "graph TD", "flowchart LR", "stateDiagram-v2", etc.`
         * for a diagram type it does not implement, and `Empty mermaid diagram` for an empty fence
         * (both probed 2026-08-26), so an unsupported type is a named build failure with a page and a
         * line number attached.
         */
        /*
         * The cause's own message is interpolated rather than left to `cause`. Astro's glob loader
         * catches a render error and logs `error.message` only (`astro/dist/content/loaders/glob.js`,
         * verified 7.2.9), so a cause reached only through the error chain is invisible in a build log —
         * which turns "the renderer does not support this diagram type" into "did not render".
         */
        const reason = cause instanceof Error ? cause.message : String(cause)
        throw new Error(
          `${where}: mermaid diagram ${index - 1} did not render: ${reason}. ` +
            "A fence that cannot be rendered is never passed through as a code block: fix the diagram, " +
            "or supply a renderer that supports its type.",
          { cause }
        )
      }

      if (svg.trim() === "") {
        throw new Error(`${where}: the renderer returned an empty string for diagram ${index - 1}`)
      }

      /*
       * A `<figure>` rather than a bare `<svg>`: the diagram is a figure in the document's own terms, and
       * it gives the width-constraining CSS one stable hook.
       *
       * THE CAPTION IS THE ONLY PART OF A DIAGRAM THAT REACHES THE llms BUNDLES. Those are built by
       * rendering the page to HTML and converting it back to Markdown, and an inline `<svg>` survives
       * that round trip as nothing at all — measured 2026-08-28 on this build, where `llms-full.txt`
       * carried neither the SVG nor the fence. The raw `.md` twin still carries the fence verbatim,
       * because it is built from the entry's source, so an agent fetching the twin reads the diagram as
       * Mermaid. An agent reading a bundle reads the caption or nothing. Write one: the fence's info
       * string past the language becomes the `<figcaption>`.
       */
      const caption = (node.meta ?? "").trim()
      const value =
        `<figure class="${className}">${svg}` +
        (caption === "" ? "" : `<figcaption>${escapeText(caption)}</figcaption>`) +
        "</figure>"

      return ctx.sourceFormat === "mdx"
        ? {
            type: "mdxJsxFlowElement",
            name: "Fragment",
            attributes: [{ type: "mdxJsxAttribute", name: "set:html", value }],
            children: []
          }
        : { type: "html", value }
    }
  })
}

/**
 * The processor a project configured, narrowed structurally.
 *
 * The published option type declares `mdastPlugins` as an array this code has no write access to, and
 * appending is the documented way an integration contributes a plugin. A local structural type is how
 * that append is expressed without asserting through `any`, and it doubles as the identity test below.
 */
interface SatteriProcessor {
  readonly name: string
  readonly options: { mdastPlugins: unknown[] }
}

interface UnifiedProcessor {
  readonly name: string
  readonly options: { remarkPlugins: unknown[] }
}

const isSatteriProcessor = (processor: unknown): processor is SatteriProcessor => {
  if (typeof processor !== "object" || processor === null) return false
  const candidate = processor as { name?: unknown; options?: { mdastPlugins?: unknown } }
  return candidate.name === "satteri" && Array.isArray(candidate.options?.mdastPlugins)
}

const isUnifiedProcessor = (processor: unknown): processor is UnifiedProcessor => {
  if (typeof processor !== "object" || processor === null) return false
  const candidate = processor as { name?: unknown; options?: { remarkPlugins?: unknown } }
  return candidate.name === "unified" && Array.isArray(candidate.options?.remarkPlugins)
}

/**
 * Attaches the plugin to whichever processor the project configured, and refuses the rest.
 *
 * Dispatch is on the processor's own identity rather than on the Astro version, because a project may
 * configure either engine at any version. The push is a factory rather than a plugin instance, so
 * Sätteri calls it once per document and each document gets its own diagram counter.
 *
 * The unified branch throws with its own message instead of falling back to a remark plugin. That is a
 * deliberate absence: a remark implementation is a second code path with a second set of node types and
 * a second failure mode, and shipping one that is never exercised is how the untested path becomes the
 * one that breaks. The message names the fix, which is one line of configuration.
 */
export const attachMermaidPlugin = (processor: unknown, options: MermaidOptions): void => {
  if (isSatteriProcessor(processor)) {
    processor.options.mdastPlugins.push(() => mermaidPlugin(options))
    return
  }
  if (isUnifiedProcessor(processor)) {
    throw new Error(
      "The mermaid integration renders at mdast and needs the Sätteri processor, but " +
        "`markdown.processor` is the unified engine. Set " +
        "`markdown: { processor: satteri() }` in astro.config.ts, which is Astro 7's own default."
    )
  }
  throw new Error(
    "`markdown.processor` is not a processor the mermaid integration recognises. It supports the " +
      "Sätteri engine, identified by `processor.name === \"satteri\"`."
  )
}

/**
 * Adds `mermaid` to the syntax highlighter's exclude list, preserving whatever is configured.
 *
 * Without this the highlighter reaches the fence first. The default exclude list is `["math"]` and
 * nothing else (verified in `@astrojs/internal-helpers@0.10.2`, where `defaultExcludeLanguages =
 * ["math"]`), so a mermaid fence is highlighted as an unknown language before this plugin sees it.
 *
 * Two behaviours of the surrounding machinery make the merge below safe, both verified 2026-08-26:
 *
 *  * The processor tests `excludeLangs.includes(lang) || defaultExcludeLanguages.includes(lang)`, so
 *    `math` stays excluded whatever this function writes. Appending cannot drop it.
 *  * `updateConfig` merges arrays by CONCATENATION and does not re-validate the config, so passing only
 *    the delta appends to a project's own list rather than replacing it. A repeated entry is harmless
 *    because the test is `includes`.
 *
 * The branch on the value's shape is the part that is easy to get wrong. `markdown.syntaxHighlight` is a
 * union of an object, the strings `"shiki"` and `"prism"`, and `false`. When a project wrote
 * `syntaxHighlight: "prism"`, merging an object over a string REPLACES it — so passing `{ excludeLangs:
 * […] }` alone would silently drop `type` and turn highlighting off site-wide. The string case therefore
 * re-states `type` explicitly.
 */
export const excludeMermaidFromHighlighting = (
  current: unknown,
  updateConfig: (config: {
    markdown: { syntaxHighlight: { type?: string; excludeLangs: string[] } }
  }) => unknown
): void => {
  // `false` disables highlighting outright: nothing claims the fence, so there is nothing to exclude.
  if (current === false) return

  if (typeof current === "string") {
    updateConfig({
      markdown: { syntaxHighlight: { type: current, excludeLangs: [MERMAID_LANG] } }
    })
    return
  }

  const existing =
    typeof current === "object" && current !== null && Array.isArray((current as { excludeLangs?: unknown }).excludeLangs)
      ? ((current as { excludeLangs: string[] }).excludeLangs)
      : []
  if (existing.includes(MERMAID_LANG)) return
  updateConfig({ markdown: { syntaxHighlight: { excludeLangs: [MERMAID_LANG] } } })
}

/** A fence opener whose language is `mermaid`, at the start of a line. */
const MERMAID_FENCE = /^ {0,3}(?:`{3,}|~{3,})[ \t]*mermaid(?=[ \t]|$)/gm

/** Every emitted raw Markdown twin, as absolute paths. */
const emittedTwins = async (dir: string): Promise<ReadonlyArray<string>> => {
  const found: Array<string> = []
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    if (entry.name.startsWith("_") || entry.name === "pagefind") continue
    const path = join(dir, entry.name)
    if (entry.isDirectory()) found.push(...(await emittedTwins(path)))
    else if (entry.name.endsWith(".md")) found.push(path)
  }
  return found
}

/**
 * The built page for a twin: `x/y.md` is the twin of `x/y/index.html`, and the root twin `.md` is the
 * twin of `index.html`.
 */
const pageForTwin = (dir: string, twin: string): string => {
  const relative = twin.slice(dir.length).replace(/^\/+/, "")
  const slug = relative.slice(0, -".md".length)
  return join(dir, slug, "index.html")
}

/**
 * Refuses a build that emitted a page carrying a mermaid fence and no rendered figure.
 *
 * This closes a hole the plugin's own `throw` cannot. Astro's glob loader CATCHES every render error,
 * logs `error.message`, and stores the entry with `rendered: undefined`
 * (`astro/dist/content/loaders/glob.js`, verified 7.2.9) — so a diagram that fails to render costs one
 * red log line inside an otherwise green build, and the page ships with an EMPTY body. Throwing at
 * mdast is necessary and it is not sufficient.
 *
 * Both sides are counted off the build. The twin is the source of truth for how many diagrams a page
 * was supposed to have — `starlight-md-txt` builds it from the entry's own body, so it carries the
 * fences whether or not anything rendered — and the figure class is what this plugin emits. A page with
 * no fence is not inspected, so the check costs nothing on a corpus with no diagrams.
 */
const assertDiagramsRendered = async (
  dir: string,
  className: string
): Promise<{ pages: number; diagrams: number }> => {
  const missing: Array<string> = []
  let pages = 0
  let diagrams = 0
  for (const twin of await emittedTwins(dir)) {
    const fences = [...(await readFile(twin, "utf8")).matchAll(MERMAID_FENCE)].length
    if (fences === 0) continue
    pages += 1
    diagrams += fences
    const page = pageForTwin(dir, twin)
    const html = await readFile(page, "utf8").catch(() => undefined)
    if (html === undefined) {
      missing.push(`${twin.slice(dir.length)}: ${fences} fence(s), and no built page at ${page}`)
      continue
    }
    const figures = html.split(`class="${className}"`).length - 1
    if (figures !== fences) {
      missing.push(`${twin.slice(dir.length)}: ${fences} fence(s), ${figures} rendered figure(s)`)
    }
  }
  if (missing.length > 0) {
    throw new Error(
      `mermaid: a page carries a fence the build did not render.\n  ${missing.join("\n  ")}\n` +
        "Astro's content loader catches a render error and continues, so the earlier " +
        "`[ERROR] ... Error rendering` line in this log names the diagram and the reason."
    )
  }
  return { pages, diagrams }
}

/**
 * The integration.
 *
 * The plugin is attached at `astro:config:setup`, the last hook before the Markdown processor is built
 * and therefore the only one where a plugin can still be added to it. The rendered output is then
 * verified at `astro:build:done`, because the throw at mdast is caught one layer up.
 */
export default function mermaid(options: MermaidOptions = {}): AstroIntegration {
  const className = options.className ?? DEFAULT_CLASS_NAME
  return {
    name: "mermaid",
    hooks: {
      "astro:config:setup": ({ command, config, updateConfig }) => {
        /*
         * `sync` and `preview` build no Markdown. Attaching in `sync` would run every renderer during
         * type generation, for no output.
         */
        if (command !== "build" && command !== "dev") return

        excludeMermaidFromHighlighting(
          config.markdown.syntaxHighlight,
          updateConfig as Parameters<typeof excludeMermaidFromHighlighting>[1]
        )
        attachMermaidPlugin(config.markdown.processor, options)
      },
      "astro:build:done": async ({ dir, logger }) => {
        const { pages, diagrams } = await assertDiagramsRendered(dir.pathname, className)
        logger.info(`rendered ${diagrams} diagram(s) across ${pages} page(s) at build time`)
      }
    }
  }
}
