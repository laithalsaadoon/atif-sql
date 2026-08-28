#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Render pip-audit's JSON report as SARIF.

    scripts/pip_audit_to_sarif.py <pip-audit.json> <out.sarif> [locked-file]

pip-audit 2.10.1 emits `columns`, `json`, `cyclonedx-json`, `cyclonedx-xml`, and `markdown`
and no SARIF (probed 2026-08-28 against `pip-audit --help`). Every other scanner in
`mise run security` reaches GitHub code scanning through one `upload-sarif` of the `.sarif`
directory, so a scanner without a SARIF emitter either gets a converter or reports only to
whoever reads the CI log. This is the converter, and its output goes through the same
`scripts/verify_sarif.py` every other report does.

The advisory has no source position — it is a property of a resolved dependency, not of a
line of first-party code. Each result is therefore anchored at line 1 of the lockfile that
resolved the package, because that IS the file a maintainer edits to fix it, and code
scanning requires a physical location to place an alert at all.

stdlib only, for the same reason `verify_sarif.py` is: it runs inside the security task,
whose job is to survive the environment being wrong.

Exit 0 on a written report, 2 on a usage error, 1 when the input is unreadable or is not a
pip-audit JSON document.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

#: One decoded JSON value. `json.loads` is typed `Any`, so without a declared shape every
#: read below is checked against nothing; naming the shape here makes each `isinstance`
#: narrowing land on a real type and keeps the reads type-checked. Recursive by design —
#: the documents these scripts read nest objects inside arrays inside objects.
type JsonValue = str | int | float | bool | list["JsonValue"] | dict[str, "JsonValue"] | None

_USAGE = "usage: scripts/pip_audit_to_sarif.py <pip-audit.json> <out.sarif> [locked-file]"

_SARIF_VERSION = "2.1.0"
_SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"


def _rule(vuln_id: str, description: str) -> dict[str, Any]:
    """One SARIF rule per advisory id, so code scanning groups repeat hits of the same CVE."""
    return {
        "id": vuln_id,
        "name": vuln_id,
        "shortDescription": {"text": vuln_id},
        "fullDescription": {"text": description or vuln_id},
        "help": {"text": description or vuln_id},
        "defaultConfiguration": {"level": "warning"},
        "properties": {"tags": ["security", "dependency"]},
    }


def convert(report: JsonValue, locked_file: str) -> dict[str, Any]:
    """Build a one-run SARIF document from pip-audit's parsed JSON report."""
    dependencies = report.get("dependencies") if isinstance(report, dict) else None
    if not isinstance(dependencies, list):
        message = "input is not a pip-audit JSON report (no dependencies[])"
        raise TypeError(message)

    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []

    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        name = str(dependency.get("name", "?"))
        version = str(dependency.get("version", "?"))
        vulns = dependency.get("vulns")
        if not isinstance(vulns, list):
            continue
        for vuln in vulns:
            if not isinstance(vuln, dict):
                continue
            vuln_id = str(vuln.get("id", "UNKNOWN"))
            description = str(vuln.get("description") or "")
            fixes = vuln.get("fix_versions")
            fix_text = (
                f" Fixed in: {', '.join(str(fix) for fix in fixes)}."
                if isinstance(fixes, list) and fixes
                else " No fixed version is published."
            )
            rules.setdefault(vuln_id, _rule(vuln_id, description))
            results.append(
                {
                    "ruleId": vuln_id,
                    "level": "warning",
                    "message": {"text": f"{name} {version} is affected by {vuln_id}.{fix_text}"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": locked_file},
                                "region": {"startLine": 1},
                            }
                        }
                    ],
                    "partialFingerprints": {"pipAuditPackageVuln": f"{name}@{version}:{vuln_id}"},
                }
            )

    return {
        "$schema": _SARIF_SCHEMA,
        "version": _SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "pip-audit",
                        "informationUri": "https://github.com/pypa/pip-audit",
                        "rules": list(rules.values()),
                    }
                },
                # Always present, even when empty: an empty array is "found nothing" and a
                # missing one is "never got that far", which is exactly the distinction
                # verify_sarif.py exists to keep.
                "results": results,
            }
        ],
    }


def main(argv: Sequence[str]) -> int:
    """Entry point. Returns the process exit code; 2 means the ARGUMENTS were wrong."""
    if len(argv) not in (2, 3):
        sys.stderr.write(f"{_USAGE}\n")
        return 2
    source, destination = Path(argv[0]), Path(argv[1])
    locked_file = argv[2] if len(argv) == 3 else "uv.lock"  # noqa: PLR2004 — 3rd _USAGE slot

    try:
        report: JsonValue = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        sys.stderr.write(f"pip_audit_to_sarif: cannot read {source}: {error}\n")
        return 1

    try:
        sarif = convert(report, locked_file)
    except TypeError as error:
        sys.stderr.write(f"pip_audit_to_sarif: {error}\n")
        return 1

    destination.write_text(json.dumps(sarif, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
