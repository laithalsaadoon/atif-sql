#!/usr/bin/env node
/**
 * Publish a `comprehensive-codebase-understanding` tree as Starlight content: copy every Markdown
 * file under the tree into a target content directory and stamp frontmatter onto the copy.
 *
 * ## Frontmatter is added by the site, and the input tree is NEVER mutated
 *
 * Two facts leave no other arrangement:
 *
 * - Starlight's `docsSchema()` declares `title: z.string()` — required, no default, no H1
 *   fallback (`@astrojs/starlight@0.41.7/schema.ts:15-16`). A page without `title` fails the build.
 * - ccu forbids frontmatter on its outputs, and its cross-link pass STRIPS any frontmatter it finds
 *   there. Frontmatter written into the tree self-reverts on the next ccu run.
 *
 * So the title is derived here, written onto the copy, and the tree is read-only. This script opens
 * no file under the source for writing, and refuses to run at all when the target is inside the
 * source.
 *
 * ## Gitignore the target
 *
 * The target directory is derived output. Commit it and a reader sees ordinary editable pages, edits
 * one, and the next sync overwrites the edit with a fresh copy of the source body — a silent
 * discard, whose git history reads as if the edit were reverted on purpose. Add the target to
 * `.gitignore` and run the sync from the build, so the only editable copy is the tree itself.
 *
 * ## Usage
 *
 *   ./sync-ccu.mjs --source docs --target apps/docs/src/content/docs/code
 *   ./sync-ccu.mjs --source docs --target apps/docs/src/content/docs/code --dry-run
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

/**
 * ccu's reading order for the six category directories, and where everything else lands.
 *
 * Starlight computes a sidebar group's sort weight as the MINIMUM `sidebar.order` of the routes it
 * contains (`@astrojs/starlight@0.41.7/utils/navigation.ts:293-301`), so one rank stamped on every
 * page in a category is what orders the six GROUPS. Pages inside a category share that rank, tie,
 * and fall back to Starlight's collator — alphabetical within a category, ccu's order across them.
 * Without this the groups sort alphabetically and `analysis` opens the book.
 *
 * A directory absent from this table gets no `sidebar.order` at all, which Starlight reads as
 * `Number.MAX_VALUE`: it sorts after every ranked group, alphabetically among its peers.
 */
const CATEGORY_ORDER = new Map([
  ["architecture", 1],
  ["reference", 2],
  ["behavior", 3],
  ["analysis", 4],
  ["diagrams", 5],
  ["insights", 6]
])

/** The tree's landing page, pinned ahead of the six categories. */
const LANDING_PAGES = new Set(["README.md", "index.md"])
const LANDING_ORDER = 0

/** A file at the tree root that is not the landing page: outside ccu's reading order, so after it. */
const ROOT_ORDER = 7

/**
 * The tree root files this site publishes, beyond the landing page.
 *
 * Named one path at a time, never globbed. A ccu tree holds hand-authored siblings beside the
 * generated documents — `CONTRACT.md` and `parity/` in this repo — which carry no H1 contract, hold
 * bare braces the raw-twin builder's MDX parser reads as JSX expressions, and are addressed to a
 * maintainer rather than to a reader of the site. Publishing one is a decision, so it is spelled.
 */
const PUBLISHED_ROOT_FILES = new Set()

/**
 * Whether the site publishes a tree-relative path.
 *
 * The allowlist is the shape of the contract: a ccu category directory, or a named root file. A file
 * the tree grows outside both is REPORTED as skipped rather than silently swept in, because the two
 * failures are opposite — a swept-in file breaks the build on content nobody chose to publish, and a
 * silently dropped one is a page a reader is told exists.
 */
const publishes = (treePath) => {
  const segments = treePath.split("/")
  if (segments.length === 1) {
    return LANDING_PAGES.has(segments[0]) || PUBLISHED_ROOT_FILES.has(segments[0])
  }
  return CATEGORY_ORDER.has(segments[0])
}

/**
 * ccu writes its H1 as `identifier · Title`. The separator is U+00B7 MIDDLE DOT surrounded by single
 * spaces, spelled as an escape so no homoglyph can pass for it in this file.
 */
const H1_SEPARATOR = " \u00b7 "

/**
 * Directories holding build scratch rather than pages: `.packets/` carries the per-agent task
 * packets, `.repomix/` the flattened pack. Both are gitignored in a ccu repo, so a page built from
 * one would cite files a reader cannot open. Every dot-directory is skipped, which also keeps a
 * `.git` out of the copy when the source is pointed at a repo root.
 */
const isSkippedDirectory = (name) => name.startsWith(".")

/** The manifest of files this tool owns in the target, so a prune can never reach a foreign page. */
const MANIFEST = ".ccu-sync.json"

const usage = `sync-ccu.mjs --source <ccu-tree> --target <content-dir> [--dry-run]

  --source <dir>   the ccu docs tree, read-only
  --target <dir>   the Starlight content directory to write; gitignore it
  --dry-run        report what would change and write nothing
  --help           this text
`

const fail = (message) => {
  process.stderr.write(`sync-ccu: ${message}\n`)
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
 * the rest. Comparing unresolved paths lets a symlinked target sit inside the source undetected,
 * which is the one arrangement that writes into the tree.
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

/** Every `.md` file under `root`, as tree-relative slash-separated paths, in path order. */
const markdownUnder = (root) => {
  const walk = (dir) =>
    readdirSync(join(root, dir), { withFileTypes: true }).flatMap((entry) => {
      const path = dir === "" ? entry.name : `${dir}/${entry.name}`
      if (entry.isDirectory()) return isSkippedDirectory(entry.name) ? [] : walk(path)
      return entry.isFile() && entry.name.endsWith(".md") ? [path] : []
    })
  return walk("").sort()
}

/**
 * The document's H1, or `undefined` when it has none.
 *
 * Fences are tracked because a `# ` on the first line of a shell block is not a heading, and a file
 * opening with such a fence would otherwise take its title from a comment.
 */
const headingOf = (body) => {
  let fence
  for (const line of body.split("\n")) {
    const marker = /^(`{3,}|~{3,})/.exec(line.trimStart())?.[1]
    if (fence === undefined) {
      if (marker !== undefined) {
        fence = marker
        continue
      }
      const heading = /^#[ \t]+(\S.*?)[ \t]*$/.exec(line)?.[1]
      if (heading !== undefined) return heading
      continue
    }
    // A closing fence uses the opener's character and is at least as long.
    if (marker !== undefined && marker[0] === fence[0] && marker.length >= fence.length) {
      fence = undefined
    }
  }
  return undefined
}

/**
 * The page title, and the reason it fell back when it did.
 *
 * Two sources and never a third: the Title segment of a ccu-shaped H1, or the filename stem. A
 * fallback is REPORTED rather than dressed up, because an invented title is a claim about the
 * document that nothing checks — and the fix (write the H1 ccu's way) belongs to whoever owns the
 * tree, who has to be told the site guessed before they can make that call.
 */
const titleOf = (body, treePath) => {
  const stem = basename(treePath, ".md")
  const heading = headingOf(body)
  if (heading === undefined) return { title: stem, fallback: "no H1" }
  const at = heading.lastIndexOf(H1_SEPARATOR)
  if (at === -1) return { title: stem, fallback: `H1 carries no separator: ${heading}` }
  const title = heading.slice(at + H1_SEPARATOR.length).trim()
  if (title === "") return { title: stem, fallback: `H1 has an empty title: ${heading}` }
  return { title, fallback: undefined }
}

/** The `sidebar.order` for a tree-relative path, or `undefined` to leave the page unranked. */
const orderOf = (treePath) => {
  const segments = treePath.split("/")
  if (segments.length === 1) return LANDING_PAGES.has(segments[0]) ? LANDING_ORDER : ROOT_ORDER
  return CATEGORY_ORDER.get(segments[0])
}

/**
 * A YAML double-quoted scalar.
 *
 * Quoted unconditionally, so a title carrying a colon stays one scalar instead of reparsing as a
 * nested mapping: `AWS Lambda MicroVMs: measured platform behavior` is one from a real tree.
 * Compared by code point rather than through a character class, because a control character written
 * into a regex is itself the kind of thing a linter rejects.
 */
const yamlString = (value) => {
  for (const character of value) {
    const code = character.codePointAt(0)
    if (code !== undefined && (code < 0x20 || code === 0x7f)) {
      throw new Error(`title carries a control character: ${JSON.stringify(value)}`)
    }
  }
  return `"${value.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`
}

/**
 * The frontmatter block for one page.
 *
 * `editUrl: false` because the file at this path is a copy. An edit link pointing at it invites the
 * edit the next sync discards; the tree is where a change belongs.
 */
const frontmatter = ({ title, order }) =>
  [
    "---",
    `title: ${yamlString(title)}`,
    "editUrl: false",
    ...(order === undefined ? [] : ["sidebar:", `  order: ${order}`]),
    "---",
    ""
  ].join("\n")

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

  // Refused in both directions. A target inside the source writes pages into the tree, which the
  // next ccu run then documents as if they were source. A source inside the target puts the tree in
  // the prune's reach.
  if (contains(source, target)) {
    fail(
      `refusing to write into the source tree: --target ${target} is inside --source ${source}.\n` +
        "  The tree is read-only. Point --target at a content directory outside it, and gitignore it."
    )
  }
  if (contains(target, source)) {
    fail(
      `refusing to run: --source ${source} is inside --target ${target}, where the prune reaches ` +
        "it.\n  Point --target at a content directory outside the tree."
    )
  }

  const found = markdownUnder(source)
  if (found.length === 0) fail(`no .md files under ${source}`)

  const pages = found.filter((treePath) => publishes(treePath))
  const skipped = found.filter((treePath) => !publishes(treePath))

  const owned = new Set(readManifest(target))
  const written = []
  const unchanged = []
  const fallbacks = []
  const generated = new Set()

  for (const treePath of pages) {
    const body = readFileSync(join(source, treePath), "utf8")
    // A source file carrying frontmatter is an upstream contract break, not something to paper over:
    // ccu forbids frontmatter and strips it, so prepending here stacks two blocks on this run and
    // produces a different file on the run after the strip. Name it and stop.
    if (/^---\r?\n/.test(body)) {
      fail(
        `${treePath} opens with YAML frontmatter, which a ccu output never carries.\n` +
          "  Stamping a second block would leave the page with two. Remove it from the tree " +
          "(ccu's cross-link pass does this) and re-run."
      )
    }

    const { title, fallback } = titleOf(body, treePath)
    if (fallback !== undefined) fallbacks.push({ treePath, title, fallback })
    const order = orderOf(treePath)

    // Composed whole from the source body on every run, so frontmatter cannot stack: the output is
    // always exactly one block followed by the tree's own bytes. That is also what makes a second
    // run report every page unchanged.
    const content = `${frontmatter({ title, order })}${body}`
    const destination = join(target, treePath)
    generated.add(treePath)

    const current = existsSync(destination) ? readFileSync(destination, "utf8") : undefined
    if (current === content) {
      unchanged.push(treePath)
      continue
    }
    written.push({ treePath, title, order })
    if (options.dryRun) continue
    mkdirSync(dirname(destination), { recursive: true })
    writeFileSync(destination, content)
  }

  // Only a path this tool wrote on a previous run is a prune candidate, so a hand-authored page
  // sharing the target survives and a first run against a populated directory removes nothing.
  const stale = [...owned].filter((treePath) => !generated.has(treePath)).sort()

  if (!options.dryRun) {
    for (const treePath of stale) rmSync(join(target, treePath), { force: true })
    mkdirSync(target, { recursive: true })
    const manifest = { source: relative(process.cwd(), source), files: [...generated].sort() }
    writeFileSync(join(target, MANIFEST), `${JSON.stringify(manifest, undefined, 2)}\n`)
  }

  const out = process.stdout
  const verb = options.dryRun ? "would write" : "wrote"
  for (const { treePath, title, order } of written) {
    out.write(
      `${verb}  ${join(target, treePath)}  order=${order ?? "-"}  title=${JSON.stringify(title)}\n`
    )
  }
  for (const treePath of stale) {
    out.write(`${options.dryRun ? "would remove" : "removed"}  ${join(target, treePath)}\n`)
  }
  for (const { treePath, title, fallback } of fallbacks) {
    out.write(`  fallback title  ${treePath} -> ${JSON.stringify(title)}  (${fallback})\n`)
  }
  for (const treePath of skipped) {
    out.write(`  not published  ${treePath}  (outside the ccu categories and not named)\n`)
  }
  out.write(
    `${options.dryRun ? "dry run: " : ""}${pages.length} pages, ${written.length} ${verb}, ` +
      `${unchanged.length} unchanged, ${stale.length} stale, ${fallbacks.length} fallback titles, ` +
      `${skipped.length} not published\n`
  )
  if (pages.length === 0) {
    out.write(
      `  ${found.length} .md files under ${source} and none inside a ccu category. The generated ` +
        "tree is not written yet; the site builds from its own authored pages until it is.\n"
    )
  }
  if (fallbacks.length > 0) {
    out.write(
      "  A fallback title is the filename stem. Give the page an H1 shaped " +
        `\`# identifier${H1_SEPARATOR}Title\` in the tree to replace it.\n`
    )
  }
}

main()
