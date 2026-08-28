#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Prove a scanner actually produced a usable SARIF report.

    scripts/verify_sarif.py <path> [label]

Every scanner in `mise run security` has its FINDINGS exit code swallowed (`|| true`,
`--exit-code 0`, `--exit-code=0`), because a non-zero exit kills the workflow step before
`upload-sarif` runs and throws away the finding detail that made the scan worth running.
That swallow also collapses two results which must never read alike: "scanned, found
nothing" and "crashed before writing anything". This is the discriminator, and it is ONE
definition called by every scan task — a check that can drift from the thing it checks is
the defect it exists to prevent.

`[ -s file ]` is not that check. A truncated SARIF is non-empty: a 43-byte
`{"runs":[{"tool":{"driver":{"name":"osv-sca` passes `-s` and parses as nothing.
`{"runs":[]}` passes `-s` too, and means the document carries no scan at all. semgrep has
its own version of the same shape: its formatter emits the literal string
`<ERROR: no SARIF output>` when the RPC that renders SARIF yields nothing, which is nine
words of non-empty file.

So four properties are checked, each of them one `upload-sarif` already requires — a report
this accepts is a report the upload accepts, so the check cannot refuse a scan the pipeline
would otherwise have shipped:

- the file parses as JSON        the observed truncation failure, and semgrep's sentinel
                                string
- `runs` is a non-empty array   a document with no scan in it
- every run carries `results[]` an empty array means "found nothing"; a missing one means
                                the run never got that far
- every run names              one upload carries every report and code scanning
  `tool.driver.name`           attributes each by that name, so an unnamed run lands
                                nowhere

stdlib only, and deliberately so: this runs before `uv sync` in a fresh clone and inside a
CI step whose whole purpose is to survive a scanner that did not.

Exit 0 when the report is usable, 1 with the failing property on stderr when it is not, 2
on a usage error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Sequence

#: One decoded JSON value. `json.loads` is typed `Any`, so without a declared shape every
#: read below is checked against nothing; naming the shape here makes each `isinstance`
#: narrowing land on a real type and keeps the reads type-checked. Recursive by design —
#: SARIF nests objects inside arrays inside objects.
type JsonValue = str | int | float | bool | list["JsonValue"] | dict[str, "JsonValue"] | None

_USAGE = "usage: scripts/verify_sarif.py <path> [label]"


def _fail(prefix: str, path: str, reason: str) -> NoReturn:
    """Reject the report. The reason names the property, which is the whole value here."""
    sys.stderr.write(
        f"{prefix}: {path} is not a usable SARIF report ({reason}) — the scan did NOT run\n"
    )
    sys.exit(1)


def _mapping_get(value: JsonValue, key: str) -> JsonValue:
    """Read `key` out of `value` when it is a JSON object, else None.

    Every level of a SARIF document is attacker-shaped as far as this script is concerned:
    the input is whatever a crashing scanner left behind, so a wrong type is a rejection to
    report rather than a traceback to read.
    """
    return value.get(key) if isinstance(value, dict) else None


def verify(path: str, label: str = "") -> None:
    """Fail the process unless `path` is a SARIF report code scanning would accept."""
    prefix = label or "verify-sarif"

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        _fail(prefix, path, f"unreadable: {error.strerror or type(error).__name__}")

    try:
        document: JsonValue = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        _fail(prefix, path, f"invalid JSON after {len(raw)} bytes: {error}")

    runs = _mapping_get(document, "runs")
    if not isinstance(runs, list) or not runs:
        _fail(prefix, path, "no runs[]")

    total_results = 0
    tools: list[str] = []
    # `offset` is 0-based within this document's runs — the position a reader needs to find
    # the run in the file.
    for offset, run in enumerate(runs):
        results = _mapping_get(run, "results")
        if not isinstance(results, list):
            _fail(prefix, path, f"runs[{offset}] carries no results[]")
        driver = _mapping_get(_mapping_get(_mapping_get(run, "tool"), "driver"), "name")
        if not isinstance(driver, str) or not driver:
            _fail(prefix, path, f"runs[{offset}] names no tool.driver.name")
        total_results += len(results)
        tools.append(driver)

    sys.stdout.write(
        f"{prefix}: {path} is usable — {len(runs)} run(s) from {', '.join(tools)}, "
        f"{total_results} result(s)\n"
    )


def main(argv: Sequence[str]) -> int:
    """Entry point. Returns the process exit code; 2 means the ARGUMENTS were wrong."""
    if not argv or len(argv) > 2:  # noqa: PLR2004 — the two argv slots named in _USAGE
        sys.stderr.write(f"{_USAGE}\n")
        return 2
    verify(argv[0], argv[1] if len(argv) > 1 else "")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
