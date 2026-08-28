#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Prove osv-scanner's license lookup actually resolved, then print what it found.

    scripts/verify_licenses.py <path> [label]

`mise run security:licenses` swallows osv-scanner's exit code, because a license VIOLATION is
a finding to report and this repository's license gate is
`.github/workflows/dependency-review.yml`, not this task. The swallow collapses two results
which must never read alike, exactly as it does for every SARIF scanner: "resolved every
package's license, none violated the allowlist" and "the license lookup failed and returned
nothing".

Those two are not hypothetical. osv-scanner resolves a PyPI package to an SPDX id through
deps.dev, a network service; observed 2026-08-28, a run printed
`Scanned .../uv.lock file and found 144 packages` and then an EMPTY license table, exit 1,
while the immediately following run printed the full 17-row histogram. The package count comes
from the local lockfile and says nothing about whether the remote lookup worked, so it is not
the property to check.

`license_summary` is. osv-scanner populates it from resolved licenses, so a non-empty summary
means the lookup returned data for this run — and an allowlist that clears everything still
produces a summary, because the summary counts licenses rather than violations.

The violations are then PRINTED, not failed on: 17 of the 144 locked packages fall outside any
allowlist for data-quality reasons no allowlist can fix (16 resolve to the literal string
`non-standard`, which osv-scanner rejects as an allowlist entry, and llvmlite's real expression
carries `LLVM-exception`, which is not a standalone SPDX id). See the task in mise.toml.

stdlib only, matching scripts/verify_sarif.py: this runs in a CI step whose purpose is to
survive a scanner that did not.

Exit 0 when the report proves a lookup happened, 1 with the failing property on stderr when it
does not, 2 on a usage error.
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

_USAGE = "usage: scripts/verify_licenses.py <path> [label]"


def _fail(prefix: str, path: str, reason: str) -> NoReturn:
    """Reject the report. The reason names the property, which is the whole value here."""
    sys.stderr.write(
        f"{prefix}: {path} does not prove a license lookup ran ({reason}) "
        f"— treat this run as NOT scanned\n"
    )
    sys.exit(1)


def _mapping_get(value: JsonValue, key: str) -> JsonValue:
    """Read `key` out of `value` when it is a JSON object, else None.

    The input is whatever a failing scanner left behind, so a wrong type is a rejection to
    report rather than a traceback to read.
    """
    return value.get(key) if isinstance(value, dict) else None


def _histogram(summary: list[JsonValue]) -> list[tuple[str, int]]:
    """Read `[{"name": ..., "count": ...}]` into sortable pairs, skipping malformed entries."""
    rows: list[tuple[str, int]] = []
    for entry in summary:
        name = _mapping_get(entry, "name")
        count = _mapping_get(entry, "count")
        if isinstance(name, str) and isinstance(count, int):
            rows.append((name, count))
    return rows


def _violations(document: JsonValue) -> list[str]:
    """Collect `name==version (license, ...)` for every package outside the allowlist."""
    found: list[str] = []
    results = _mapping_get(document, "results")
    if not isinstance(results, list):
        return found
    for result in results:
        packages = _mapping_get(result, "packages")
        if not isinstance(packages, list):
            continue
        for entry in packages:
            breaches = _mapping_get(entry, "license_violations")
            if not isinstance(breaches, list) or not breaches:
                continue
            package = _mapping_get(entry, "package")
            name = _mapping_get(package, "name")
            version = _mapping_get(package, "version")
            spdx = ", ".join(str(item) for item in breaches)
            found.append(f"{name}=={version} ({spdx})")
    return found


def verify(path: str, label: str = "") -> None:
    """Fail the process unless `path` proves osv-scanner resolved licenses on this run."""
    prefix = label or "verify-licenses"

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        _fail(prefix, path, f"unreadable: {error.strerror or type(error).__name__}")

    try:
        document: JsonValue = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        _fail(prefix, path, f"invalid JSON after {len(raw)} bytes: {error}")

    summary = _mapping_get(document, "license_summary")
    if not isinstance(summary, list) or not summary:
        _fail(prefix, path, "license_summary is empty — the deps.dev lookup returned nothing")

    rows = _histogram(summary)
    if not rows:
        _fail(prefix, path, "license_summary carries no {name, count} entry")

    breaches = _violations(document)
    total = sum(count for _, count in rows)
    top = ", ".join(f"{name} {count}" for name, count in sorted(rows, key=lambda r: -r[1])[:5])
    sys.stdout.write(
        f"{prefix}: {path} proves a lookup — {len(rows)} distinct license(s) over "
        f"{total} package version(s); top: {top}\n"
    )
    if breaches:
        sys.stdout.write(
            f"{prefix}: {len(breaches)} package(s) outside the allowlist (reported, not gated): "
            f"{'; '.join(breaches)}\n"
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
