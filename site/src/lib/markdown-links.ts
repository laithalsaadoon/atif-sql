import { existsSync, readdirSync } from "node:fs"
import { readFile, rename, writeFile } from "node:fs/promises"
import { join, posix } from "node:path"
import { fileURLToPath } from "node:url"

import type { AstroIntegration } from "astro"
import type { MdastPluginDefinition } from "satteri"

import { slugFor, trackedAtCommit } from "./citation-links.js"

/**
 * Relative Markdown links as routes: `[module map](../architecture/module-map.md)` in a docs page
 * becomes a link to `/atif-sql/architecture/module-map/` on the rendered page, and a link to a
 * Markdown file the site does not publish becomes its GitHub permalink.
 *
 * ## The defect this closes
 *
 * The docs tree links page to page the way GitHub renders it: a path relative to the source file,
 * ending in `.md`. Emitted verbatim, that href resolves against the page's URL, and every page here
 * is a directory route. `../architecture/module-map.md` on `/atif-sql/insights/business-logic/`
 * resolves to `/atif-sql/insights/architecture/module-map.md`, one directory too deep and a 404;
 * a sibling `contract-map.md` resolves to `/atif-sql/insights/business-logic/contract-map.md`.
 * Measured 2026-10-09 on the deployed site at 36dc203: 75 such 404s across 18 of the 20 pages.
 * lychee over the source was green throughout, because the links resolve on disk, and
 * `starlight-links-validator` skipped all of them while `errorOnRelativeLinks` was off.
 *
 * ## Two surfaces, two answers
 *
 * - **The rendered page**, through the mdast pass `markdownLinks`. A link to a published page gets
 *   that page's route under the base; one to an unpublished Markdown file in the repository
 *   (`docs/CONTRACT.md`, which `sync-ccu.mjs` leaves out on purpose) gets the GitHub blob at the
 *   permalink commit. The pass reaches the llms bundles too, which `starlight-llms-txt` flattens from
 *   the rendered page.
 * - **The raw `.md` twin**, at `astro:build:done` through `rawTwinLinks`. `starlight-md-txt` builds a
 *   twin from the source body, so the pass never touches it. There a relative `.md` link mostly
 *   resolves already: the twin is a file, not a directory, so `../architecture/module-map.md` from
 *   `/atif-sql/insights/business-logic.md` reaches `/atif-sql/architecture/module-map.md`. Such a
 *   link stays exactly as authored. Only a link that would NOT resolve from the twin's own URL is
 *   rewritten: to the base-prefixed twin of the page it names (a case or index mismatch), or to the
 *   GitHub blob for an unpublished file. That keeps the twin as close to the source bytes as the
 *   surface allows, the same rule `baseRawLinks` follows for root-relative links.
 *
 * ## It never throws
 *
 * Astro's glob loader catches a render error, logs one line and stores the entry with no rendered
 * body (see `mermaid-integration.ts`), so a throw here would ship an empty page from a green build.
 * A link this cannot resolve is left as written instead, and two gates fail on it: the links
 * validator, which reports any relative link left in a page (`errorOnRelativeLinks: true`), and
 * `scripts/docs_links.py`, which resolves every built href against `dist/`.
 *
 * Registered as a factory like the other mdast plugins: it keeps no per-document state, but the
 * shared array's contract is one instance per compile.
 */

export interface MarkdownLinksOptions {
  /** Absolute path of the content collection, which holds exactly the published pages. */
  readonly collectionRoot: string
  /** The repository root; blob paths are relative to it. */
  readonly repoRoot: string
  /** The docs tree, repository-relative (`docs`): where a synced page's source sits. */
  readonly treeDir: string
  /** The authored pages, repository-relative (`site/authored`): where every other page's sits. */
  readonly authoredDir: string
  /** The tree-relative paths the sync wrote, from its manifest. */
  readonly syncedPaths: ReadonlySet<string>
  /** Repository web root, no trailing slash. */
  readonly repoUrl: string
  /** The full SHA every blob link pins to. */
  readonly commit: string
  /** The site base with no trailing slash: `/atif-sql`. */
  readonly siteBase: string
  /** Whether a repository path exists at `commit`. Defaults to one `git ls-tree` at that commit. */
  readonly existsAtCommit?: (repoRelativePath: string) => boolean
}

/** Where one relative Markdown link goes. */
export type MarkdownLinkTarget =
  | { readonly kind: "page"; readonly slug: string; readonly hash: string }
  | { readonly kind: "blob"; readonly repoPath: string; readonly hash: string }
  | { readonly kind: "missing"; readonly path: string }

/**
 * The path and fragment of a link to a Markdown file by relative path, or `undefined` for anything
 * else: a URL with a scheme, a protocol-relative or root-relative path, a bare fragment, or a path
 * that does not end in `.md`.
 */
export const relativeMarkdownLink = (
  url: string
): { readonly path: string; readonly hash: string } | undefined => {
  if (url === "" || url.startsWith("#") || url.startsWith("/")) return undefined
  if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(url)) return undefined
  const pathEnd = url.search(/[?#]/)
  const path = pathEnd === -1 ? url : url.slice(0, pathEnd)
  const hashAt = url.indexOf("#")
  const hash = hashAt === -1 ? "" : url.slice(hashAt)
  if (!path.toLowerCase().endsWith(".md")) return undefined
  return { path, hash }
}

/** A page's route slug: `README.md` is `readme`, the collection's `index.md` is the empty slug. */
export const pageSlug = (collectionPath: string): string | undefined => {
  const slug = slugFor(collectionPath)
  return slug === "index" ? "" : slug
}

/** The rendered route of a slug under a base with no trailing slash. */
export const routeOf = (siteBase: string, slug: string): string =>
  slug === "" ? `${siteBase}/` : `${siteBase}/${slug}/`

/**
 * The raw twin of a slug under a base with no trailing slash. The root page's twin is `index.md`:
 * `starlight-md-txt` writes it as the dotfile `.md`, which `actions/upload-pages-artifact` leaves
 * out of the deployed site (it excludes every hidden file), so `rawTwinLinks` renames it.
 */
export const twinOf = (siteBase: string, slug: string): string =>
  `${siteBase}/${slug === "" ? "index" : slug}.md`

const isPublishedFile = (collectionRoot: string, collectionPath: string): boolean =>
  !collectionPath.split("/").some((segment) => segment.startsWith(".")) &&
  existsSync(join(collectionRoot, collectionPath))

/**
 * Resolves one relative Markdown link written in a published page.
 *
 * Resolution follows the source file, never the URL: the link is what the author wrote against the
 * tree on disk, and that is what lychee checks it against too.
 */
export const resolveMarkdownLink = (
  fromCollectionPath: string,
  url: string,
  options: MarkdownLinksOptions
): MarkdownLinkTarget | undefined => {
  const link = relativeMarkdownLink(url)
  if (link === undefined) return undefined
  let decoded: string
  try {
    decoded = decodeURI(link.path)
  } catch {
    return { kind: "missing", path: link.path }
  }
  const fromDir = posix.dirname(fromCollectionPath)
  const inCollection = posix.normalize(posix.join(fromDir, decoded))
  if (!inCollection.startsWith("../") && isPublishedFile(options.collectionRoot, inCollection)) {
    const slug = pageSlug(inCollection)
    if (slug !== undefined) return { kind: "page", slug, hash: link.hash }
  }
  const sourceDir = options.syncedPaths.has(fromCollectionPath)
    ? options.treeDir
    : options.authoredDir
  const repoPath = posix.normalize(posix.join(sourceDir, fromDir, decoded))
  const exists =
    options.existsAtCommit ??
    ((path: string): boolean => trackedAtCommit(options.repoRoot, options.commit).has(path))
  if (!repoPath.startsWith("../") && exists(repoPath)) {
    return { kind: "blob", repoPath, hash: link.hash }
  }
  return { kind: "missing", path: inCollection }
}

const blobUrl = (options: MarkdownLinksOptions, repoPath: string, hash: string): string =>
  `${options.repoUrl.replace(/\/$/, "")}/blob/${options.commit}/${repoPath}${hash}`

/** The collection-relative path of the document a visitor runs on, or `undefined` outside it. */
const collectionPathOf = (fileURL: URL | undefined, collectionRoot: string): string | undefined => {
  if (fileURL === undefined) return undefined
  const prefix = `${collectionRoot.replace(/\/$/, "")}/`
  const path = fileURLToPath(fileURL)
  return path.startsWith(prefix) ? path.slice(prefix.length) : undefined
}

type LinkVisitor = NonNullable<MdastPluginDefinition["link"]>
type DefinitionVisitor = NonNullable<MdastPluginDefinition["definition"]>

/**
 * The mdast pass for the rendered page.
 *
 * A `link` gets its route WITHOUT the base: `starlight-base-path` appends its own `link` visitor
 * after this pass and prefixes the base onto every root-relative url it sees, with no check for one
 * already there, and it sees the url this pass set (and visits a node this pass creates). Emitting
 * the base here shipped `/atif-sql/atif-sql/architecture/module-map/` in the first build: 108
 * invalid links. A `definition` gets the base, because that plugin never visits definitions. Were
 * the order ever to change, a base-free route is a link to no page, which the links validator and
 * `docs:links` both fail on.
 */
export const markdownLinks = (options: MarkdownLinksOptions): MdastPluginDefinition => {
  const siteBase = options.siteBase.replace(/\/$/, "")
  const href = (fileURL: URL | undefined, url: string, base: string): string | undefined => {
    const from = collectionPathOf(fileURL, options.collectionRoot)
    if (from === undefined) return undefined
    const target = resolveMarkdownLink(from, url, options)
    if (target === undefined || target.kind === "missing") return undefined
    if (target.kind === "page") return `${routeOf(base, target.slug)}${target.hash}`
    return blobUrl(options, target.repoPath, target.hash)
  }
  const link: LinkVisitor = (node, ctx) => {
    const next = href(ctx.fileURL, node.url, "")
    if (next !== undefined) ctx.setProperty(node, "url", next)
  }
  const definition: DefinitionVisitor = (node, ctx) => {
    const next = href(ctx.fileURL, node.url, siteBase)
    if (next !== undefined) ctx.setProperty(node, "url", next)
  }
  return { name: "docs-markdown-links", link, definition }
}

/** Every published page in the collection, as collection-relative paths. */
const collectionPages = (root: string): ReadonlyArray<string> => {
  const walk = (dir: string): ReadonlyArray<string> =>
    readdirSync(join(root, dir), { withFileTypes: true }).flatMap((entry) => {
      if (entry.name.startsWith(".")) return []
      const path = dir === "" ? entry.name : `${dir}/${entry.name}`
      if (entry.isDirectory()) return walk(path)
      return entry.isFile() && entry.name.endsWith(".md") ? [path] : []
    })
  return walk("")
}

/** `](target)` and `]: target`, the two forms a Markdown link target is written in. */
const INLINE_TARGET = /\]\(([^)\s]+)\)/g
const DEFINITION_TARGET = /^(\[[^\]]+\]:[ \t]+)(\S+)/

/**
 * Rewrites one twin's relative Markdown links that would not resolve from the twin's own URL.
 * Exported for the unit test; `rawTwinLinks` is the caller.
 */
export const rewriteTwin = (
  body: string,
  fromCollectionPath: string,
  twinPath: string,
  options: MarkdownLinksOptions
): { readonly body: string; readonly rewritten: number } => {
  const siteBase = options.siteBase.replace(/\/$/, "")
  let rewritten = 0
  const fix = (url: string): string => {
    const target = resolveMarkdownLink(fromCollectionPath, url, options)
    if (target === undefined || target.kind === "missing") return url
    if (target.kind === "blob") {
      rewritten += 1
      return blobUrl(options, target.repoPath, target.hash)
    }
    const twin = twinOf(siteBase, target.slug)
    const link = relativeMarkdownLink(url)
    const resolved = new URL(link?.path ?? url, `https://twin.invalid${twinPath}`).pathname
    if (resolved === twin) return url
    rewritten += 1
    return `${twin}${target.hash}`
  }
  // Fenced code is left alone: a link written inside a fence is an example, not a link.
  let fence: string | undefined
  const next = body
    .split("\n")
    .map((line) => {
      const marker = /^ {0,3}(`{3,}|~{3,})/.exec(line)?.[1]
      if (fence !== undefined) {
        if (marker !== undefined && marker[0] === fence[0] && marker.length >= fence.length) {
          fence = undefined
        }
        return line
      }
      if (marker !== undefined) {
        fence = marker
        return line
      }
      return line
        .replace(INLINE_TARGET, (_match, url: string) => `](${fix(url)})`)
        .replace(DEFINITION_TARGET, (_match, label: string, url: string) => `${label}${fix(url)}`)
    })
    .join("\n")
  return { body: next, rewritten }
}

/**
 * The twin half, at `astro:build:done`: renames the root twin `.md` to `index.md`, then rewrites the
 * relative Markdown links of every page's twin that would not resolve from where the twin is served.
 *
 * Listed after `baseRawLinks`, whose root-relative rewrite it leaves alone: a root-relative link is
 * not a relative one, so the two never touch the same target.
 */
export const rawTwinLinks = (options: MarkdownLinksOptions): AstroIntegration => ({
  name: "docs:raw-twin-links",
  hooks: {
    "astro:build:done": async ({ dir, logger }) => {
      const out = fileURLToPath(dir)
      if (existsSync(join(out, ".md"))) await rename(join(out, ".md"), join(out, "index.md"))
      const siteBase = options.siteBase.replace(/\/$/, "")
      let files = 0
      let links = 0
      for (const page of collectionPages(options.collectionRoot)) {
        const slug = pageSlug(page)
        if (slug === undefined) continue
        const twinPath = twinOf(siteBase, slug)
        const file = join(out, twinPath.slice(siteBase.length + 1))
        if (!existsSync(file)) continue
        const body = await readFile(file, "utf8")
        const next = rewriteTwin(body, page, twinPath, options)
        if (next.rewritten === 0) continue
        await writeFile(file, next.body)
        files += 1
        links += next.rewritten
      }
      logger.info(
        `moved the root twin to index.md; rewrote ${links} relative .md links across ${files} raw routes`
      )
    }
  }
})
