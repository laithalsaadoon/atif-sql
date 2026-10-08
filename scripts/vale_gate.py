#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run Vale over the published prose and fail on any error-level alert, or on nothing checked.

    scripts/vale_gate.py [--label LABEL] [PATH ...]

With no PATH the scope is the text the docs site, llms.txt and the raw Markdown twins publish,
plus the two root manuals: `docs/**/*.md`, `site/authored/**/*.md`, `README.md` and `AGENTS.md`.
With PATHs (the selftest's fixtures) exactly those files are checked. Either way Vale reads
`.vale.ini` at the repository root and the styles committed under `.vale/styles`, so the gate
needs no network.

Three properties, each one a way the gate could pass on nothing:

- the scope matched files          a moved docs tree or a broken glob is a scope of zero,
                                   and Vale given zero files reports zero errors
- Vale checked every one of them   the count comes from this script's own walk and must
                                   equal the `in N files` Vale's summary line reports
- Vale produced a usable report    its JSON parses, is an object, and names only files in
                                   the list; an `E100` runtime error is a crash, not a pass

Errors fail the gate and print as `path:line:col: Rule: message`. Warnings are counted and
reported as the ratchet a later change lowers, never gated: `.vale.ini` says why each rule
below error is there.

stdlib only, 3.9-compatible, run with `python3` like the other gate helpers here, so it works
before `uv sync`.

Exit 0 when Vale checked every file and found no error, 1 when it found errors or checked
nothing, 2 on a usage error or a Vale that did not run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".vale.ini"

#: The published prose, as globs under ROOT. A new page under docs/ joins the scope by itself.
SCOPE = ("docs/**/*.md", "site/authored/**/*.md", "README.md", "AGENTS.md")

#: Vale's default output ends with `✔ 0 errors, 0 warnings and 0 suggestions in 24 files.`
_SUMMARY = re.compile(r"\bin (\d+) files?\.\s*$")

_USAGE = "usage: scripts/vale_gate.py [--label LABEL] [PATH ...]"


def _die(label: str, reason: str, code: int = 2) -> NoReturn:
    sys.stderr.write(f"{label}: {reason}\n")
    sys.exit(code)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def scope_files(root: Path = ROOT) -> list[Path]:
    """Every file the SCOPE globs match that git does not ignore, deduplicated and sorted.

    Ignored files (`docs/.packets/`, `docs/parity/`) exist only in a working tree, so linting
    them would make a laptop's run disagree with CI's, as lychee.toml says of the same paths.
    """
    found: set[Path] = set()
    for pattern in SCOPE:
        found.update(p for p in root.glob(pattern) if p.is_file())
    if not found:
        return []
    git = shutil.which("git")
    if git is None:
        return sorted(found)
    ignored = subprocess.run(  # noqa: S603 - git with fixed flags, our own paths on stdin
        [git, "check-ignore", "--stdin"],
        cwd=root,
        input="\n".join(os.path.relpath(p, root) for p in sorted(found)),
        capture_output=True,
        text=True,
        check=False,
    )
    # 0: some ignored, 1: none ignored; anything else (not a repository) keeps them all.
    if ignored.returncode in (0, 1):
        found -= {(root / line).resolve() for line in ignored.stdout.splitlines() if line}
    return sorted(found)


def _vale(label: str, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("vale")
    if exe is None:
        _die(label, "vale is not on PATH: run it through mise (`mise run docs:prose`)")
    return subprocess.run(  # noqa: S603 - the mise-pinned vale with fixed flags and our paths
        [exe, f"--config={CONFIG}", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _parse_args(argv: Sequence[str]) -> tuple[str, list[str]]:
    label = "docs:prose"
    paths: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--label":
            label = next(it, "") or _die(label, _USAGE)
        elif arg.startswith("-"):
            _die(label, _USAGE)
        else:
            paths.append(arg)
    return label, paths


def main(argv: Sequence[str]) -> int:
    """Run the gate; return the exit status the module docstring defines."""
    label, raw = _parse_args(argv)
    if not CONFIG.is_file():
        _die(label, f"{CONFIG} is missing")

    if raw:
        files = [Path(p).resolve() for p in raw]
        missing = [str(p) for p in files if not p.is_file()]
        if missing:
            _die(label, "no such file: " + ", ".join(missing))
    else:
        files = scope_files()
    if not files:
        sys.stderr.write(f"{label}: checked 0 files: the scope matched nothing\n")
        return 1
    # Relative to ROOT, where Vale runs, so a report line reads `docs/x.md:3:1: ...`.
    names = [os.path.relpath(p, ROOT) for p in files]

    # One run for the alerts, structured.
    report = _vale(label, ["--output=JSON", "--minAlertLevel=warning", *names])
    if report.returncode not in (0, 1):
        _die(label, f"vale exited {report.returncode}: {(report.stdout + report.stderr).strip()}")
    try:
        alerts = json.loads(report.stdout)
    except json.JSONDecodeError:
        _die(label, f"vale's JSON report does not parse: {report.stdout[:300]!r}")
    if not isinstance(alerts, dict) or "Code" in alerts:
        _die(label, f"vale reported a runtime error, not alerts: {report.stdout[:300]!r}")
    listed = set(files)
    for key in alerts:
        if (ROOT / key).resolve() not in listed:
            _die(label, f"vale reported alerts for {key}, which is not in the list it was given")

    # One run for the count: Vale's JSON names only files that have alerts, so a clean file is
    # invisible there. Its summary line counts every file it read.
    summary = _vale(label, ["--minAlertLevel=error", "--no-wrap", *names])
    if summary.returncode not in (0, 1):
        _die(
            label, f"vale exited {summary.returncode}: {(summary.stdout + summary.stderr).strip()}"
        )
    lines = summary.stdout.strip().splitlines()
    match = _SUMMARY.search(lines[-1]) if lines else None
    if match is None:
        _die(label, f"vale printed no summary line: {summary.stdout[-300:]!r}")
    checked = int(match.group(1))
    if checked != len(files):
        sys.stderr.write(
            f"{label}: vale checked {checked} of {len(files)} files in scope; "
            "a file it skipped is a file nothing checked\n"
        )
        return 1

    errors: list[str] = []
    warnings = 0
    for key in sorted(alerts):
        for alert in alerts[key]:
            if alert.get("Severity") == "error":
                line, col = alert.get("Line"), (alert.get("Span") or [0])[0]
                errors.append(f"{key}:{line}:{col}: {alert.get('Check')}: {alert.get('Message')}")
            elif alert.get("Severity") == "warning":
                warnings += 1
    sys.stdout.writelines(f"{err}\n" for err in errors)
    sys.stdout.write(
        f"{label}: vale checked {checked} of {len(files)} files: {_count(len(errors), 'error')}, "
        f"{_count(warnings, 'warning')} (reported, not gated)\n"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
