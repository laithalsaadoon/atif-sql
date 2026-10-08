#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fail unless a pnpm lockfile is exactly one YAML document with packages in it.

    scripts/verify_single_yaml_document.py <path> [label]

pnpm 12 can write its own pinned version into `pnpm-lock.yaml` as a SEPARATE first YAML
document (`packageManagerDependencies`), ahead of the project's. GitHub's dependency graph
reads only the first document, so the docs site's real dependencies vanish from the SBOM and
Dependabot closes their alerts as fixed with nothing fixed. `pmOnFail: ignore` in
`site/pnpm-workspace.yaml` keeps the file one document; this is the check that notices the day
it stops doing so.

Three properties, each one a way the check could pass on nothing:

- the file is readable and holds content    a missing or empty lockfile has no documents
- exactly one document carries content      a second `---` section is the defect
- that document has `importers:` and at     a lockfile with no packages is a file the graph
  least one `packages:` entry               would read as empty, which is the failure itself

stdlib only, no YAML parser: a pnpm lockfile is block-style with every nested line indented,
so a column-0 `---` or `...` is always a document marker and never scalar content.

Exit 0 when the lockfile is usable, 1 with the failing property on stderr when it is not, 2 on
a usage error.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Sequence

_USAGE = "usage: scripts/verify_single_yaml_document.py <path> [label]"


def _fail(prefix: str, path: str, reason: str) -> NoReturn:
    """Reject the lockfile. The reason names the property, which is the whole value here."""
    sys.stderr.write(f"{prefix}: {path} is not a single usable YAML document ({reason})\n")
    sys.exit(1)


def _is_marker(line: str) -> bool:
    """True for a column-0 document start `---` or end `...` marker."""
    return line.rstrip() in {"---", "..."} or line.startswith(("--- ", "... "))


def _has_content(lines: list[str]) -> bool:
    """True when a segment holds a line that is neither blank nor a comment."""
    return any(line.strip() and not line.lstrip().startswith("#") for line in lines)


def verify(path: str, label: str = "") -> None:
    """Fail the process unless `path` is one YAML document that lists packages."""
    prefix = label or "verify-single-yaml-document"

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        _fail(prefix, path, f"unreadable: {getattr(error, 'strerror', None) or error}")

    segments: list[list[str]] = [[]]
    for line in raw.splitlines():
        if _is_marker(line):
            segments.append([])
        else:
            segments[-1].append(line)
    documents = [segment for segment in segments if _has_content(segment)]

    if not documents:
        _fail(prefix, path, "no content")
    if len(documents) > 1:
        _fail(
            prefix,
            path,
            f"{len(documents)} YAML documents; GitHub's dependency graph reads only the first. "
            "Set `pmOnFail: ignore` in site/pnpm-workspace.yaml and drop the extra document",
        )

    lines = documents[0]
    if "importers:" not in lines:
        _fail(prefix, path, "no top-level importers:")
    try:
        start = lines.index("packages:")
    except ValueError:
        _fail(prefix, path, "no top-level packages:")
    entries = 0
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        if line.startswith("  ") and not line.startswith("   ") and line.strip():
            entries += 1
    if entries == 0:
        _fail(prefix, path, "packages: lists no package")

    sys.stdout.write(f"{prefix}: {path} is one YAML document with {entries} package(s)\n")


def main(argv: Sequence[str]) -> int:
    """Entry point. Returns the process exit code; 2 means the ARGUMENTS were wrong."""
    if not argv or len(argv) > 2:  # noqa: PLR2004 — the two argv slots named in _USAGE
        sys.stderr.write(f"{_USAGE}\n")
        return 2
    verify(argv[0], argv[1] if len(argv) > 1 else "")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
