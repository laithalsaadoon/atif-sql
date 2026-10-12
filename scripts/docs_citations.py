#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fail when a docs citation names a line that isn't there, or code moved out from under it.

    scripts/docs_citations.py [--base REV] [--fix] [--label LABEL] [--root DIR]

The docs cite code as `` `path:LINE` `` or `` `path:START-END` ``, and `:LINE` shorthands resolve
against the last full path in the same section (`site/src/lib/citation-links.ts` turns each one
into a permalink, with the same rules: the antecedent resets at every heading and every table
row). That build pass checks only that the path exists, so a line that moved, or a range past the
end of the file, ships as a permalink to the wrong code. This gate runs two checks over
`docs/**/*.md` and `site/authored/**/*.md` in the working tree:

- static     every citation names a tracked file, its lines exist in it (1 <= START <= END <=
             the file's line count), and every shorthand has an antecedent
- drift      against a base commit, every citation whose doc line this change leaves untouched
             still names the lines it named at the base. The cited file's own diff (`git diff
             -U0 BASE`) maps each cited line forward:

             ok       every line unchanged at the same number
             edited   changed in place at the same number; reported, not failed, since the
                      line most likely still holds the construct the sentence names
             moved    the same text now sits at other numbers: fails, and `--fix` rewrites the
                      citation to them
             changed  the cited lines were removed, or rewritten and shifted: fails, because only
                      a reader can say where the named code went

A citation on a doc line the change edits is out of the drift check: whoever edited the line
re-read it. That is also how a `changed` failure clears: re-point the citation, which edits its
line. The base is `--base`, else `ATIF_SQL_CITATIONS_BASE` (CI sets the pull request's base
commit), else `git merge-base HEAD origin/main`; a base that does not resolve is a usage error,
never a silent pass.

Why this exists: on 2026-10-12 main held 2,177 citations, and an audit against that tree found
1,318 of them needing a re-point, a rewrite, or removal. Replayed over the 29 commits before the
audit, the drift check fails 19 of them, on 991 citations, and names the new lines for 934.

stdlib only, 3.9-compatible, run with `python3` like the other gate helpers here.

Exit 0 when every citation resolves and nothing drifted, 1 when one did not or the scope held no
citation, 2 on a usage error or a base that does not resolve.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

ROOT = Path(__file__).resolve().parent.parent

#: The trees whose Markdown the site publishes and the citation pass links.
SCOPE = ("docs", "site/authored")

#: The extensions a citation path may carry: `EXT` in `site/src/lib/citation-links.ts`, verbatim.
EXT = (
    "rs|py|pyi|toml|json|jsonc|json5|md|mdx|yml|yaml|mjs|cjs|js|jsx|ts|tsx|tf|tfvars|sh|bash|"
    "zsh|lock|txt|typed|go|java|kt|kts|rb|php|cs|c|h|cc|cpp|hpp|swift|scala|sql|proto|graphql|"
    "gradle|cfg|ini|mk|css|scss|html|vue|svelte|ipynb|xml|csv|snap"
)

#: `CITATION` in `citation-links.ts`: an optional path, an optional `:START[-END]`, a label.
_CITATION = re.compile(
    r"^(?:(?P<repo>[A-Za-z0-9_.\-]+):)??"
    rf"(?P<path>[A-Za-z0-9_./\-]+\.(?:{EXT}))?"
    r"(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?"
    r"(?P<label> .*)?$",
    re.DOTALL,
)
_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_FENCE = re.compile(r"(`{3,}|~{3,})")
_HEADING = re.compile(r"#{1,6}\s")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)

_USAGE = "usage: scripts/docs_citations.py [--base REV] [--fix] [--label LABEL] [--root DIR]"


def _die(label: str, reason: str, code: int = 2) -> NoReturn:
    sys.stderr.write(f"{label}: {reason}\n")
    sys.exit(code)


@dataclass(frozen=True)
class Citation:
    """One `path:LINE` span in a doc, with its shorthand resolved."""

    doc: str
    line: int
    index: int
    span: str
    column: int
    path: str | None
    start: int
    end: int
    shorthand: bool

    @property
    def where(self) -> str:
        """`doc:line`, the location a failure names."""
        return f"{self.doc}:{self.line}"


@dataclass
class Report:
    """What one run found."""

    citations: int = 0
    pages: int = 0
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    moves: dict[tuple[str, int, int], str] = field(default_factory=dict)
    base: str | None = None
    changed_files: int = 0
    checked: int = 0

    @property
    def ok(self) -> bool:
        """True when the scope held a citation and no check failed."""
        return self.citations > 0 and not self.problems


def _group(match: re.Match[str], name: str) -> str | None:
    """A named group's text, or None when it did not take part in the match."""
    value = match.group(name)
    return None if value is None else str(value)


def citations(doc: str, text: str) -> Iterator[Citation]:
    """Every citation in one Markdown document, in order, under citation-links.ts's rules.

    Inline code spans only, outside fences and outside link text. A bare path sets the
    antecedent; a heading and the start of a table row reset it. A shorthand with no antecedent
    is yielded with `path=None`, which the static check reports.
    """
    antecedent: str | None = None
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        opener = _FENCE.match(stripped)
        if fence is not None:
            if opener is not None and stripped.startswith(fence):
                fence = None
            continue
        if opener is not None:
            fence = str(opener.group(1))
            continue
        if _HEADING.match(stripped) or stripped.startswith("|"):
            antecedent = None
        index = 0
        for span in _SPAN.finditer(line):
            value = span.group(2).strip() if len(span.group(1)) > 1 else span.group(2)
            if line[: span.start()].endswith("[") and line[span.end() :].startswith("]("):
                continue
            groups = _CITATION.match(value)
            if groups is None or groups["repo"] is not None:
                continue
            path = _group(groups, "path")
            start = _group(groups, "start")
            if path is None and start is None:
                continue
            normalized = path[2:] if path is not None and path.startswith("./") else path
            if start is None:
                antecedent = normalized
                continue
            if normalized is not None:
                antecedent = normalized
            end = _group(groups, "end")
            yield Citation(
                doc=doc,
                line=number,
                index=index,
                span=value,
                column=span.start(2) + len(span.group(2)) - len(span.group(2).lstrip()),
                path=normalized if normalized is not None else antecedent,
                start=int(start),
                end=int(end) if end is not None else int(start),
                shorthand=normalized is None,
            )
            index += 1


class Git:
    """The git reads the gate makes, memoized, all from one repository root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._shown: dict[tuple[str, str], list[str] | None] = {}
        self._hunks: dict[tuple[str, str], list[tuple[int, int, int, int]]] = {}

    def run(self, *args: str, check: bool = True) -> str:
        """Run git at the root; stdout, or "" on failure when `check` is false."""
        done = subprocess.run(  # noqa: S603 - git with fixed flags and repository-relative paths
            ["git", "-C", str(self.root), *args],  # noqa: S607 - git from PATH, as every gate here
            capture_output=True,
            text=True,
            check=False,
        )
        if check and done.returncode != 0:
            raise RuntimeError(done.stderr.strip() or f"git {' '.join(args)} failed")
        return done.stdout if done.returncode == 0 else ""

    def tracked(self) -> set[str]:
        """Every path git tracks, repository-relative."""
        return set(self.run("ls-files", "-z").split("\0")) - {""}

    def show(self, rev: str, path: str) -> list[str] | None:
        """The lines of `path` at `rev`, or None when it is absent there."""
        key = (rev, path)
        if key not in self._shown:
            done = subprocess.run(  # noqa: S603 - git with fixed flags and repository-relative paths
                ["git", "-C", str(self.root), "show", f"{rev}:{path}"],  # noqa: S607 - as above
                capture_output=True,
                text=True,
                check=False,
            )
            self._shown[key] = done.stdout.splitlines() if done.returncode == 0 else None
        return self._shown[key]

    def hunks(self, base: str, path: str) -> list[tuple[int, int, int, int]]:
        """`(old_start, old_count, new_start, new_count)` per hunk, base to working tree."""
        key = (base, path)
        if key not in self._hunks:
            diff = self.run("diff", "-U0", "--no-color", "--no-ext-diff", base, "--", path)
            self._hunks[key] = [
                (
                    int(m.group(1)),
                    int(m.group(2)) if m.group(2) is not None else 1,
                    int(m.group(3)),
                    int(m.group(4)) if m.group(4) is not None else 1,
                )
                for m in _HUNK.finditer(diff)
            ]
        return self._hunks[key]


def line_map(hunks: list[tuple[int, int, int, int]]) -> Callable[[int], tuple[str, int]]:
    """Map a base line through the hunks: `("same", new)`, `("edited", same)` or `("gone", near)`."""

    def forward(old: int) -> tuple[str, int]:
        delta = 0
        for old_start, old_count, new_start, new_count in hunks:
            if old_count == 0:  # a pure insertion lands after line `old_start`
                if old_start < old:
                    delta += new_count
                continue
            if old < old_start:
                break
            if old < old_start + old_count:
                offset = old - old_start
                if new_start == old_start + delta and offset < new_count:
                    return ("edited", new_start + offset)
                return ("gone", new_start + min(offset, max(new_count - 1, 0)))
            delta += new_count - old_count
        return ("same", old + delta)

    return forward


def _relocate(block: list[str], lines: list[str]) -> int | None:
    """The one line where `block` starts in `lines`, or None when it occurs zero or several times."""
    if not block or not any(text.strip() for text in block):
        return None
    hits = [i + 1 for i in range(len(lines) - len(block) + 1) if lines[i : i + len(block)] == block]
    return hits[0] if len(hits) == 1 else None


def _span_text(citation: Citation, start: int, end: int) -> str:
    numbers = f"{start}" if start == end else f"{start}-{end}"
    if citation.shorthand:
        return f":{numbers}"
    label = _CITATION.match(citation.span)
    tail = label["label"] if label is not None and label["label"] is not None else ""
    return f"{citation.path}:{numbers}{tail}"


def _static(report: Report, found: list[Citation], root: Path, tracked: set[str]) -> None:
    lengths: dict[str, int] = {}
    for c in found:
        if c.path is None:
            report.problems.append(
                f"orphan   {c.where}: `{c.span}` is a shorthand with no antecedent; the last full "
                "path resets at every heading and table row, so write the full path"
            )
            continue
        if c.path not in tracked or not (root / c.path).is_file():
            report.problems.append(
                f"missing  {c.where}: `{c.span}` names {c.path}, which git does not track"
            )
            continue
        if c.path not in lengths:
            lengths[c.path] = len(
                (root / c.path).read_text(encoding="utf-8", errors="replace").splitlines()
            )
        if not 1 <= c.start <= c.end <= lengths[c.path]:
            report.problems.append(
                f"range    {c.where}: `{c.span}` names lines {c.start}-{c.end} of {c.path}, "
                f"which has {lengths[c.path]}"
            )


def _drift(report: Report, git: Git, base: str, docs: dict[str, str], root: Path) -> None:
    changed = set(git.run("diff", "--name-only", "--no-ext-diff", base, "--").split())
    report.changed_files = len(changed)
    for doc, text in docs.items():
        before = git.show(base, doc)
        if before is None:
            continue
        old_by_key = {
            (c.line, c.index): c for c in citations(doc, "\n".join(before)) if c.path is not None
        }
        doc_forward = line_map(git.hunks(base, doc)) if doc in changed else None
        unchanged: dict[int, int] = {}
        for old_line in range(1, len(before) + 1):
            kind, new_line = doc_forward(old_line) if doc_forward else ("same", old_line)
            if kind == "same":
                unchanged[new_line] = old_line
        for c in citations(doc, text):
            old_line = unchanged.get(c.line)
            if old_line is None or c.path is None or c.path not in changed:
                continue
            old = old_by_key.get((old_line, c.index))
            if old is None or (old.span, old.path) != (c.span, c.path):
                continue
            current = (root / c.path).read_text(encoding="utf-8", errors="replace").splitlines()
            report.checked += 1
            forward = line_map(git.hunks(base, c.path))
            mapped = [(n, *forward(n)) for n in range(c.start, c.end + 1)]
            verdict, new_start, new_end = classify(mapped, git.show(base, c.path) or [], current)
            if verdict == "edited":
                report.notes.append(
                    f"edited   {c.where}: `{c.span}`: the cited lines changed in place; "
                    "re-read the sentence"
                )
            elif verdict == "moved":
                target = _span_text(c, new_start, new_end)
                report.moves[(c.doc, c.line, c.column)] = target
                report.problems.append(
                    f"moved    {c.where}: `{c.span}` now sits at `{target}` (--fix rewrites it)"
                )
            elif verdict == "changed":
                report.problems.append(
                    f"changed  {c.where}: `{c.span}`: the cited lines in {c.path} were removed or "
                    f"rewritten (nearest line now {new_start}); re-point the citation"
                )


def classify(
    mapped: list[tuple[int, str, int]], before: list[str], after: list[str]
) -> tuple[str, int, int]:
    """One citation's verdict from its lines mapped forward: `(verdict, new_start, new_end)`.

    `mapped` holds `(old_line, kind, new_line)` per cited line, from `line_map`. Every line
    unchanged at its own number is `ok`. Unchanged lines at other numbers are `moved` there. A
    block whose text still occurs exactly once in the new file is `moved` to it, which catches
    code that moved past a rewrite of the lines around its old place. Lines changed at their own
    numbers whose old text is gone are `edited`. Anything else is `changed`, with the nearest
    surviving line as the hint.
    """
    first, last = mapped[0][0], mapped[-1][0]
    if all(kind == "same" and new == old for old, kind, new in mapped):
        return ("ok", first, last)
    if all(kind == "same" for _, kind, _ in mapped):
        return ("moved", mapped[0][2], mapped[-1][2])
    hit = _relocate(before[first - 1 : last], after)
    if hit is not None and hit != first:
        return ("moved", hit, hit + (last - first))
    if all(kind in {"same", "edited"} and new == old for old, kind, new in mapped):
        return ("edited", first, last)
    near = next((new for _, kind, new in mapped if kind == "same"), mapped[0][2])
    return ("changed", near, near)


def resolve_base(git: Git, explicit: str | None) -> str:
    """The base commit as a full SHA: `--base`, then the env var, then the merge base with origin/main."""
    candidate = explicit or os.environ.get("ATIF_SQL_CITATIONS_BASE") or None
    if candidate is None:
        candidate = git.run("merge-base", "HEAD", "origin/main", check=False).strip() or None
    if candidate is None:
        msg = "no base commit: pass --base REV or set ATIF_SQL_CITATIONS_BASE (origin/main has no merge base with HEAD)"
        raise ValueError(msg)
    sha = git.run(
        "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}", check=False
    ).strip()
    if not sha:
        msg = f"base {candidate!r} does not name a commit here; fetch it or pass --base REV"
        raise ValueError(msg)
    return sha


def check(root: Path, base: str | None) -> Report:
    """Run both checks over the working tree at `root`; `base=None` skips the drift check."""
    git = Git(root)
    tracked = git.tracked()
    docs = {
        p: (root / p).read_text(encoding="utf-8")
        for p in sorted(tracked)
        if p.endswith(".md")
        and any(p.startswith(f"{tree}/") for tree in SCOPE)
        and (root / p).is_file()
    }
    report = Report(pages=len(docs), base=base)
    found = [c for doc, text in docs.items() for c in citations(doc, text)]
    report.citations = len(found)
    _static(report, found, root, tracked)
    if base is not None:
        _drift(report, git, base, docs, root)
    return report


def apply_moves(root: Path, moves: dict[tuple[str, int, int], str]) -> int:
    """Rewrite each moved citation in place, right to left within a line; return how many."""
    by_doc: dict[str, list[tuple[int, int, str]]] = {}
    for (doc, line, column), target in moves.items():
        by_doc.setdefault(doc, []).append((line, column, target))
    for doc, edits in by_doc.items():
        path = root / doc
        lines = path.read_text(encoding="utf-8").split("\n")
        for line, column, target in sorted(edits, key=lambda e: (e[0], -e[1])):
            text = lines[line - 1]
            close = text.index("`", column)
            lines[line - 1] = text[:column] + target + text[close:]
        path.write_text("\n".join(lines), encoding="utf-8")
    return len(moves)


def _print(label: str, report: Report) -> None:
    out = sys.stdout
    for line in report.problems:
        out.write(f"{line}\n")
    for line in report.notes:
        out.write(f"{line}\n")
    if report.citations == 0:
        out.write("short    found 0 citations: the gate checked nothing\n")
    drift = (
        f"; {report.changed_files} files differ from {report.base[:12]}, "
        f"{report.checked} untouched citations into them mapped forward"
        if report.base is not None
        else ""
    )
    out.write(
        f"{label}: {report.citations} citations on {report.pages} pages, "
        f"{len(report.problems)} problems, {len(report.notes)} edited in place{drift}\n"
    )


def _parse(argv: Sequence[str]) -> tuple[dict[str, str], bool]:
    options: dict[str, str] = {}
    fix = False
    it = iter(argv)
    for arg in it:
        if arg == "--fix":
            fix = True
            continue
        if arg not in {"--base", "--label", "--root"}:
            _die("docs:citations", _USAGE)
        value = next(it, None)
        if value is None:
            _die("docs:citations", f"{arg} needs a value\n{_USAGE}")
        options[arg] = value
    return options, fix


def main(argv: Sequence[str]) -> int:
    """Run the gate; return the exit status the module docstring defines."""
    options, fix = _parse(argv)
    label = options.get("--label", "docs:citations")
    root = Path(options.get("--root", str(ROOT))).resolve()
    try:
        base = resolve_base(Git(root), options.get("--base"))
    except (RuntimeError, ValueError) as error:
        _die(label, str(error))
    report = check(root, base)
    if fix and report.moves:
        sys.stdout.write(f"{label}: rewrote {apply_moves(root, report.moves)} moved citations\n")
        report = check(root, base)
    _print(label, report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
