#!/usr/bin/env node
/**
 * The brace gate: refuses a Markdown corpus that carries a bare `{` where an MDX parser reads a JSX
 * expression.
 *
 * The raw-twin builder parses every page's body through `remark-mdx` unconditionally — authored and
 * generated alike, `.md` as well as `.mdx` — so a brace in flow or text position is handed to acorn as
 * the start of an expression. `## GET /v1/exec/{id}` is not one, and the build fails with
 * `Could not parse expression with acorn`: a message that names neither the page nor the field it came
 * from, on a build whose Markdown looked correct in every editor and every renderer.
 *
 * The fix is always the same and always local: wrap the braced span in backticks. Inside a code span or
 * a fence the brace is leaf content the MDX parser never enters, so the page keeps reading the way its
 * author wrote it.
 *
 * Run it BEFORE the site build. A gate that reports `file:line` in 30 ms is worth more than a build
 * that reports a parser position 90 seconds in.
 *
 *   node gates/brace-gate.mjs src/content/docs
 *   node gates/brace-gate.mjs ../docs/architecture ../docs/reference     # several roots at once
 *   node gates/brace-gate.mjs src/content/docs --json
 *   node gates/brace-gate.mjs ../docs/insights --allow-empty             # a root not written yet
 *
 * SCOPE IT TO WHAT THE SITE PUBLISHES. Only a page the site serves reaches the twin builder, so a scan
 * over a whole source tree reports braces in files nobody publishes — and a gate that fires on findings
 * the build does not care about is a gate that gets ignored. Several roots are accepted for exactly that
 * reason: an absent root is reported and skipped, so a pre-flight over the generated categories stays
 * clean while the generator has written only some of them.
 *
 * `--allow-empty` makes "no named root exists" a clean scan instead of a bad invocation. Pass it ONLY
 * where the roots are legitimately unwritten — a pre-flight over a documentation tree a generator has
 * not produced yet. Without it a mistyped path exits 2 rather than passing as an empty corpus.
 *
 * Exit 0: clean. Exit 1: offenders, one `file:line` per line on stdout. Exit 2: bad invocation, which
 * includes every named root being absent unless `--allow-empty` says that is expected.
 */

import { readdirSync, readFileSync, statSync } from "node:fs"
import { join, relative, resolve } from "node:path"

/** Extensions handed to the MDX parser. A `.txt` beside them is not. */
const EXTENSIONS = [".md", ".mdx"]

/**
 * A fenced block, opener through the matching closer of the same run.
 *
 * An unterminated fence never matches, so its body is scanned as prose. That is the useful direction of
 * the error: an unclosed fence is itself a defect, and a brace inside one really does reach the parser.
 */
const FENCED = /^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?^ {0,3}\1[^\n]*$/gm

/** An inline code span: a backtick run closed by a run of the same length, which may cross lines. */
const CODE_SPAN = /(`+)(?:(?!\1)[\s\S])*?\1/g

/** Leading YAML frontmatter, which reaches the frontmatter parser and never the expression parser. */
const FRONTMATTER = /^---\r?\n[\s\S]*?\r?\n---[^\n]*(?:\r?\n|$)/

/**
 * Blank a region while keeping every newline, so a later line number is the line number in the file.
 *
 * Deleting the region instead — which is enough when the only question is whether a brace exists — moves
 * every subsequent line and makes the report point at the wrong one.
 */
const blank = (text) => text.replace(/[^\n]/g, " ")

/** The body as the MDX expression parser sees it: code and frontmatter blanked, lines preserved. */
export const maskedBody = (markdown) =>
  markdown
    .replace(FRONTMATTER, blank)
    .replace(FENCED, blank)
    .replace(CODE_SPAN, blank)

/**
 * Every brace the MDX parser reads as opening an expression, with the line it sits on.
 *
 * A backslash-escaped `\{` is literal text in MDX and is skipped: flagging it would send an author to
 * re-fix a page that already builds.
 */
export const braceOffenders = (markdown) => {
  const masked = maskedBody(markdown)
  const source = markdown.split("\n")
  return masked.split("\n").flatMap((line, index) => {
    const columns = [...line.matchAll(/\{/g)]
      .map((match) => match.index ?? 0)
      .filter((column) => line[column - 1] !== "\\")
    if (columns.length === 0) return []
    return [{ line: index + 1, column: (columns[0] ?? 0) + 1, text: (source[index] ?? "").trim() }]
  })
}

const walk = (directory) =>
  readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return entry.name.startsWith(".") ? [] : walk(path)
    return EXTENSIONS.some((extension) => entry.name.endsWith(extension)) ? [path] : []
  })

/**
 * Report a path so it is clickable from where the gate was run: relative to the working directory,
 * which is what an editor and a CI annotation resolve. A run from outside the tree would print a ladder
 * of `../` segments that buries the filename, so that case falls back to the path relative to the root
 * it was found under. A finding nobody can open is a finding nobody acts on.
 */
const label = (root, file) => {
  const fromCwd = relative(process.cwd(), file)
  return fromCwd.startsWith("..") ? relative(root, file) : fromCwd
}

/** Every offender under one root, already labelled. */
const scan = (root) =>
  walk(root).flatMap((file) =>
    braceOffenders(readFileSync(file, "utf8")).map((offender) => ({
      file: label(root, file),
      ...offender
    }))
  )

const main = (argv) => {
  const asJson = argv.includes("--json")
  const allowEmpty = argv.includes("--allow-empty")
  const requested = argv.filter((argument) => !argument.startsWith("-"))
  if (requested.length === 0) {
    process.stderr.write("usage: brace-gate.mjs <directory>... [--json] [--allow-empty]\n")
    return 2
  }

  const roots = []
  const absent = []
  for (const request of requested) {
    const root = resolve(request)
    if (statSync(root, { throwIfNoEntry: false })?.isDirectory()) roots.push(root)
    else absent.push(request)
  }
  for (const request of absent) {
    process.stderr.write(`brace-gate: skipped ${request} (absent)\n`)
  }
  // Every named root absent is a bad invocation unless the caller said the roots may not exist yet: a
  // scan of nothing is not otherwise a clean scan.
  if (roots.length === 0) {
    if (!allowEmpty) {
      process.stderr.write(`brace-gate: no directory among ${requested.join(", ")}\n`)
      return 2
    }
    process.stdout.write("brace-gate: no root present yet, nothing to scan\n")
    return 0
  }

  const scanned = roots.reduce((total, root) => total + walk(root).length, 0)
  const findings = roots.flatMap(scan)

  if (asJson) {
    process.stdout.write(
      `${JSON.stringify({ roots: requested, scanned, findings: findings.length, offenders: findings }, null, 2)}\n`
    )
    return findings.length === 0 ? 0 : 1
  }
  if (findings.length === 0) {
    process.stdout.write(`brace-gate: ${scanned} files, no bare brace\n`)
    return 0
  }
  for (const { file, line, column, text } of findings) {
    process.stdout.write(`${file}:${line}:${column}  ${text}\n`)
  }
  const pages = new Set(findings.map((finding) => finding.file)).size
  process.stdout.write(
    `\nbrace-gate: ${findings.length} bare braces across ${pages} of ${scanned} files.\n` +
      "Wrap each braced span in backticks; inside a code span the MDX parser never enters it.\n"
  )
  return 1
}

/*
 * The CLI runs only when this file IS the entry point, so `braceOffenders` and `maskedBody` are
 * importable by the negative-control test. Without the guard, importing either one runs the scan and
 * exits the test process — which reads as a passing suite that ran nothing.
 */
if (import.meta.main) process.exit(main(process.argv.slice(2)))
