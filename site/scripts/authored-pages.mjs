#!/usr/bin/env node
/**
 * Publish the site's OWN authored pages into the generated content directory, and expand the one
 * table on them that is derived rather than written.
 *
 * ## Why the authored pages are copied rather than committed in place
 *
 * `scripts/sync-ccu.mjs` writes the documentation tree into `src/content/docs`, which is therefore
 * generated output and gitignored. The site's two authored pages have to sit in the same collection —
 * the agent page is the sidebar's first entry and the landing page is the root route — so they are
 * committed HERE, under `authored/`, and copied in beside the synced tree. Committing them inside the
 * generated directory would mean gitignoring a directory that also holds tracked files, and a reader
 * would have no way to tell which of the two a given page is.
 *
 * Unlike a ccu output, an authored page owns its own frontmatter: it is a page a maintainer writes, so
 * `title` and `description` are authored rather than derived, and this script refuses a page without
 * them instead of inventing either.
 *
 * ## The one generated region
 *
 * The agent page's "Read next" table is pure derivation: one row per published page, that page's route
 * beside its raw Markdown twin. It is built from the manifests both publishers write, so a page added
 * to the tree appears without an edit and a removed page cannot leave a row behind. Everything else on
 * the page is authored judgment a generator has no basis for.
 *
 * The placeholder is a bare token rather than an HTML comment on purpose: the raw-twin builder parses
 * every page through `remark-mdx`, where `<!--` is a parse error. An unexpanded token therefore ships
 * as visible nonsense in the middle of the page, which is the loud failure — a comment marker would
 * have failed the build with a parser position instead.
 *
 * ## Usage
 *
 *   ./authored-pages.mjs --source authored --target src/content/docs
 *   ./authored-pages.mjs --source authored --target src/content/docs --dry-run
 *
 * Node >= 20, ESM, no dependencies.
 */

import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync
} from "node:fs"
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path"

/** The token the derived table replaces, alone on its own line. */
const READ_NEXT_TOKEN = "GENERATED-READ-NEXT-TABLE"

/** The manifest this tool owns in the target, so a prune can never reach a page it did not write. */
const MANIFEST = ".authored-sync.json"

/** The manifest `sync-ccu.mjs` owns, which names every documentation-tree page in the collection. */
const CCU_MANIFEST = ".ccu-sync.json"

/** The collection-relative path of the landing page, which heads the derived table. */
const LANDING = "index.md"

const usage = `authored-pages.mjs --source <dir> --target <content-dir> [--dry-run]

  --source <dir>   the committed authored pages, read-only
  --target <dir>   the generated Starlight content directory; gitignore it
  --dry-run        report what would change and write nothing
  --help           this text
`

const fail = (message) => {
  process.stderr.write(`authored-pages: ${message}\n`)
  process.exit(2)
}

const parseArgs = (argv) => {
  const options = { source: undefined, target: undefined, dryRun: false, help: false }
  const positional = []
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (arg === "--help" || arg === "-h") return { ...options, help: true }
    if (arg === "--dry-run") options.dryRun = true
    else if (arg === "--source" || arg === "--target") {
      const value = argv[i + 1]
      if (value === undefined || value.startsWith("--")) fail(`${arg} needs a directory`)
      if (arg === "--source") options.source = value
      else options.target = value
      i += 1
    } else if (arg.startsWith("-")) fail(`unknown flag ${arg}\n\n${usage}`)
    else positional.push(arg)
  }
  options.source ??= positional[0]
  options.target ??= positional[1]
  if (options.source === undefined || options.target === undefined) fail(`\n${usage}`)
  return options
}

/**
 * A resolved path with every symlink on its existing prefix collapsed.
 *
 * The target need not exist yet, so the walk stops at the deepest ancestor that does and re-appends
 * the rest. Comparing unresolved paths lets a symlinked target sit inside the source undetected.
 */
const realish = (path) => {
  let head = resolve(path)
  const tail = []
  while (!existsSync(head)) {
    const parent = dirname(head)
    if (parent === head) return resolve(path)
    tail.unshift(basename(head))
    head = parent
  }
  return join(realpathSync(head), ...tail)
}

/** Whether `child` is `parent` or sits beneath it. */
const contains = (parent, child) => {
  const rel = relative(parent, child)
  return rel === "" || (rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel))
}

/** Every `.md` file under `root`, as root-relative slash-separated paths, in path order. */
const markdownUnder = (root) => {
  const walk = (dir) =>
    readdirSync(join(root, dir), { withFileTypes: true }).flatMap((entry) => {
      const path = dir === "" ? entry.name : `${dir}/${entry.name}`
      if (entry.isDirectory()) return entry.name.startsWith(".") ? [] : walk(path)
      return entry.isFile() && entry.name.endsWith(".md") ? [path] : []
    })
  return walk("").sort()
}

/** The leading YAML frontmatter block of a page, or `undefined` when it carries none. */
const frontmatterOf = (body) => /^---\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/.exec(body)?.[1]

/**
 * One scalar out of a frontmatter block.
 *
 * Deliberately not a YAML parser: the fields read here are the ones this script writes or requires,
 * and every one of them is a single-line scalar. A value that is quoted keeps its quoting stripped,
 * and a value this shape cannot read is reported as absent rather than guessed at.
 */
const scalar = (frontmatter, key) => {
  const raw = new RegExp(`^${key}:[ \\t]*(.+?)[ \\t]*$`, "m").exec(frontmatter ?? "")?.[1]
  if (raw === undefined) return undefined
  const quoted = /^"((?:[^"\\]|\\.)*)"$|^'((?:[^']|'')*)'$/.exec(raw)
  if (quoted?.[1] !== undefined) return quoted[1].replaceAll('\\"', '"').replaceAll("\\\\", "\\")
  if (quoted?.[2] !== undefined) return quoted[2].replaceAll("''", "'")
  return raw
}

/** The `sidebar.order` a page declares, or `undefined`. Nested one level under `sidebar:`. */
const sidebarOrder = (frontmatter) => {
  const raw = /^sidebar:[ \t]*\r?\n(?:[ \t]+.*\r?\n?)*/m.exec(frontmatter ?? "")?.[0]
  const order = /^[ \t]+order:[ \t]*(-?\d+)[ \t]*$/m.exec(raw ?? "")?.[1]
  return order === undefined ? undefined : Number(order)
}

/**
 * The route slug for a collection-relative path, the way Astro's glob loader derives one.
 *
 * Lowercased per segment, `.md` dropped, a trailing `index` segment removed. A segment outside
 * `[A-Za-z0-9_-]` is not slugged the same way by `github-slugger`, so it yields `undefined` and the
 * page is reported rather than given a row pointing at a route the build never wrote.
 */
const slugFor = (path) => {
  if (!path.endsWith(".md")) return undefined
  const segments = path.slice(0, -".md".length).split("/")
  if (!segments.every((segment) => /^[A-Za-z0-9_-]+$/.test(segment))) return undefined
  return segments
    .map((segment) => segment.toLowerCase())
    .join("/")
    .replace(/(^|\/)index$/, "")
}

/** The page route for a slug, root-relative and base-free: the base is prefixed at build time. */
const routeOf = (slug) => (slug === "" ? "/" : `/${slug}/`)

/** The raw-Markdown twin for a slug. The root entry's twin is the dotfile `<base>/.md`. */
const twinOf = (slug) => (slug === "" ? "/.md" : `/${slug}.md`)

/** A cell value with the characters that would break out of a Markdown table escaped. */
const cell = (value) => value.replaceAll("\\", "\\\\").replaceAll("|", "\\|").trim()

/**
 * The derived table: one row per published page, the page's route beside its twin's.
 *
 * The landing page heads the list because it is the site root; everything after it is ordered by the
 * `sidebar.order` its publisher stamped, which is the documentation tree's own reading order, and
 * ties fall back to path order so the table is stable across runs.
 */
const readNextTable = (pages) => {
  const ordered = [...pages].sort((left, right) => {
    if (left.path === LANDING) return -1
    if (right.path === LANDING) return 1
    const leftOrder = left.order ?? Number.MAX_SAFE_INTEGER
    const rightOrder = right.order ?? Number.MAX_SAFE_INTEGER
    return leftOrder === rightOrder ? left.path.localeCompare(right.path) : leftOrder - rightOrder
  })
  return [
    "| Read this | Raw Markdown |",
    "| --- | --- |",
    ...ordered.map((page) => {
      const summary = page.description === undefined ? "" : `: ${cell(page.description)}`
      return `| [${cell(page.title)}](${page.route})${summary} | [\`${page.twin}\`](${page.twin}) |`
    })
  ].join("\n")
}

/** Every page in the collection, from the two manifests plus the authored set being written now. */
const publishedPages = (target, authored) => {
  const pages = [...authored]
  let ccuFiles = []
  try {
    const parsed = JSON.parse(readFileSync(join(target, CCU_MANIFEST), "utf8"))
    ccuFiles = Array.isArray(parsed?.files) ? parsed.files.filter((f) => typeof f === "string") : []
  } catch {
    // No manifest means the documentation tree has not been synced yet, which is a state the site
    // builds in: the table then carries the authored pages only.
    ccuFiles = []
  }
  for (const path of ccuFiles) {
    const file = join(target, path)
    if (!existsSync(file)) continue
    const frontmatter = frontmatterOf(readFileSync(file, "utf8"))
    const title = scalar(frontmatter, "title")
    const slug = slugFor(path)
    if (title === undefined || slug === undefined) {
      fail(
        `${path} is in ${CCU_MANIFEST} but has no readable title or no sluggable path, so it cannot ` +
          "be given a row that resolves. Re-run the tree sync."
      )
    }
    pages.push({
      path,
      title,
      description: scalar(frontmatter, "description"),
      order: sidebarOrder(frontmatter),
      route: routeOf(slug),
      twin: twinOf(slug)
    })
  }
  return pages
}

/** The paths a previous run wrote, or none when the target holds no manifest. */
const readManifest = (target) => {
  try {
    const parsed = JSON.parse(readFileSync(join(target, MANIFEST), "utf8"))
    return Array.isArray(parsed?.files) ? parsed.files.filter((f) => typeof f === "string") : []
  } catch {
    return []
  }
}

const main = () => {
  const options = parseArgs(process.argv.slice(2))
  if (options.help) {
    process.stdout.write(usage)
    return
  }

  const source = realish(options.source)
  const target = realish(options.target)
  if (!existsSync(source)) fail(`no such source directory: ${source}`)
  if (contains(source, target)) {
    fail(`refusing to write into the authored pages: --target ${target} is inside --source ${source}`)
  }
  if (contains(target, source)) {
    fail(`refusing to run: --source ${source} is inside --target ${target}, where the prune reaches it`)
  }

  const found = markdownUnder(source)
  if (found.length === 0) fail(`no .md files under ${source}`)

  // Read every authored page first: the derived table names all of them, including the page that
  // carries it, so the whole set has to be known before any page is written.
  const authored = found.map((path) => {
    const body = readFileSync(join(source, path), "utf8")
    const frontmatter = frontmatterOf(body)
    if (frontmatter === undefined) {
      fail(`${path} carries no frontmatter. An authored page owns its own \`title\`.`)
    }
    const title = scalar(frontmatter, "title")
    if (title === undefined) {
      fail(`${path} has no \`title\`, which Starlight requires and offers no H1 fallback for.`)
    }
    const slug = slugFor(path)
    if (slug === undefined) fail(`${path} has a path Astro does not slug predictably. Rename it.`)
    return {
      path,
      body,
      title,
      description: scalar(frontmatter, "description"),
      order: sidebarOrder(frontmatter),
      route: routeOf(slug),
      twin: twinOf(slug)
    }
  })

  const pages = publishedPages(target, authored)

  const owned = new Set(readManifest(target))
  const written = []
  const unchanged = []
  const generated = new Set()
  let expansions = 0

  for (const page of authored) {
    let content = page.body
    if (content.includes(READ_NEXT_TOKEN)) {
      // Its own row is left out: a page linking to itself is noise, and the table's job is to say
      // where to go next.
      const others = pages.filter((candidate) => candidate.path !== page.path)
      if (others.length === 0) {
        fail(`${page.path} carries the ${READ_NEXT_TOKEN} token and no other page exists to list.`)
      }
      content = content.replaceAll(READ_NEXT_TOKEN, readNextTable(others))
      expansions += 1
    }
    if (content.includes(READ_NEXT_TOKEN)) {
      fail(`${page.path} still carries the ${READ_NEXT_TOKEN} token after expansion.`)
    }

    generated.add(page.path)
    const destination = join(target, page.path)
    const current = existsSync(destination) ? readFileSync(destination, "utf8") : undefined
    if (current === content) {
      unchanged.push(page.path)
      continue
    }
    written.push(page.path)
    if (options.dryRun) continue
    mkdirSync(dirname(destination), { recursive: true })
    writeFileSync(destination, content)
  }

  // Only a path this tool wrote on a previous run is a prune candidate, so the synced tree and any
  // hand-placed file sharing the target survive.
  const stale = [...owned].filter((path) => !generated.has(path)).sort()

  if (!options.dryRun) {
    for (const path of stale) rmSync(join(target, path), { force: true })
    mkdirSync(target, { recursive: true })
    const manifest = { source: relative(process.cwd(), source), files: [...generated].sort() }
    writeFileSync(join(target, MANIFEST), `${JSON.stringify(manifest, undefined, 2)}\n`)
  }

  const out = process.stdout
  const verb = options.dryRun ? "would write" : "wrote"
  for (const path of written) out.write(`${verb}  ${join(target, path)}\n`)
  for (const path of stale) {
    out.write(`${options.dryRun ? "would remove" : "removed"}  ${join(target, path)}\n`)
  }
  out.write(
    `${options.dryRun ? "dry run: " : ""}${authored.length} authored pages, ${written.length} ${verb}, ` +
      `${unchanged.length} unchanged, ${stale.length} stale; ` +
      `read-next table expanded ${expansions === 1 ? "once" : `${expansions} times`} over ` +
      `${pages.length} published pages\n`
  )
  if (expansions === 0) {
    fail(
      `no authored page carries the ${READ_NEXT_TOKEN} token, so the derived read-next table was ` +
        "never emitted. The agent page is where it belongs."
    )
  }
}

main()
