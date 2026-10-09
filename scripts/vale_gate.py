#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run Vale over the published prose: fail on an error-level alert, a shrunken scope or a rise in warnings.

    scripts/vale_gate.py [--label LABEL] [PATH ...]
    scripts/vale_gate.py --write-baseline

With no PATH the scope is the text the docs site, llms.txt and the raw Markdown twins publish,
plus the three root manuals: `docs/**/*.md`, `site/authored/**/*.md`, `README.md`, `AGENTS.md`
and `CONTRIBUTING.md`. With PATHs (the selftest's fixtures) exactly those files are checked, and
neither the floors below nor the warning baseline apply. Either way Vale reads
`.vale.ini` at the repository root and the styles committed under `.vale/styles`, so the gate
needs no network.

Four properties, each one a way the gate could pass on nothing or on less than it should:

- the scope matched files          a moved docs tree or a broken glob is a scope of zero,
                                   and Vale given zero files reports zero errors
- each glob met its floor          a moved docs/ leaves the root manuals, and "0 errors in 5
                                   files" reads like a pass; every glob in SCOPE names the
                                   fewest files it may match, and the failure names the glob
- Vale checked every one of them   the count comes from this script's own walk and must
                                   equal the `in N files` Vale's summary line reports
- Vale produced a usable report    its JSON parses, is an object, and names only files in
                                   the list; an `E100` runtime error is a crash, not a pass

Errors fail the gate and print as `path:line:col: Rule: message`. Warnings are a ratchet over
the default scope: `.vale-baseline.json` at the root holds `{rule: count}` for every
warning-level rule, and the gate fails when any rule's count rises above its baseline or a rule
the baseline does not name raises a warning, one stderr line per rule
(`Google.EmDash: 450 warnings, baseline 449`). A count below its baseline passes and prints
`lower the baseline: mise run docs:prose:baseline`. The baseline itself must be usable: a
missing, unparsable, empty or non-count file fails the gate before Vale runs, so a lost
baseline is never a pass. `.vale.ini` says why each rule below error is there.

One limit is Vale's, not this script's: Vale 3.24.0 places an alert by searching the file for
its matched text, and an alert from a Markdown list item that wraps onto a second line can land
on a later occurrence of the same text. When that occurrence raises the same rule, the two
report as one, so a page that adds a spaced em dash after such an item can count fewer
Google.EmDash warnings, not more (`.vale/fixtures/merged-emdash.md` reproduces it, and a strict
xfail in the selftest turns red when Vale fixes it). The ratchet holds Vale's count as Vale
reports it; a count that falls on a change that added a warning is that merge, so leave the
baseline where it is.

`--write-baseline` (`mise run docs:prose:baseline`) rewrites `.vale-baseline.json` from the
current tree, after the same scope checks. A rule that drops to zero keeps its entry at 0, so
its return fails the gate. A baseline only goes down in review: the writer names every rule it
raised.

stdlib only, 3.9-compatible, run with `python3` like the other gate helpers here, so it works
before `uv sync`.

Exit 0 when every glob met its floor, Vale checked every file and found no error, and no rule's
warnings rose above the baseline; 1 when it found errors, checked nothing, a glob came up short,
a rule rose or the baseline is unusable; 2 on a usage error or a Vale that did not run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".vale.ini"
BASELINE = ROOT / ".vale-baseline.json"

#: What the gate prints when a rule's warnings fell below its baseline.
LOWER_HINT = "lower the baseline: mise run docs:prose:baseline"

#: The published prose, as globs under ROOT, each with its floor: the fewest files it may match
#: (after git's ignore rules) before the gate fails naming it. A new page under docs/ joins the
#: scope by itself. Every floor is at least 1, so no glob can match nothing.
#:
#: The docs/ floor sits at three quarters of the tracked pages (20 on 2026-10-09), low enough that
#: a page merged or retired does not trip it and high enough that losing the tree or a section of
#: it does. Raise it when docs/ grows past about 20 / 0.75 = 27 pages, back to three quarters of
#: the new count; lower it only in the commit that retires pages, saying why. site/authored/ holds
#: exactly its two pages (index.md and agents.md), so its floor is both of them.
SCOPE: dict[str, int] = {
    "docs/**/*.md": 15,
    "site/authored/**/*.md": 2,
    "README.md": 1,
    "AGENTS.md": 1,
    "CONTRIBUTING.md": 1,
}

#: Vale's default output ends with `✔ 0 errors, 0 warnings and 0 suggestions in 24 files.`
_SUMMARY = re.compile(r"\bin (\d+) files?\.\s*$")

_USAGE = "usage: scripts/vale_gate.py [--label LABEL] [PATH ...] | --write-baseline"


class BaselineError(ValueError):
    """The warning baseline is missing, unparsable or not a non-empty `{rule: count}` object."""


def _die(label: str, reason: str, code: int = 2) -> NoReturn:
    sys.stderr.write(f"{label}: {reason}\n")
    sys.exit(code)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def scope_by_glob(root: Path = ROOT) -> dict[str, list[Path]]:
    """The files each SCOPE glob matches that git does not ignore, sorted, keyed by glob.

    Ignored files (`docs/.packets/`, `docs/parity/`) exist only in a working tree, so linting
    them would make a laptop's run disagree with CI's, as lychee.toml says of the same paths.
    """
    matched = {pattern: {p for p in root.glob(pattern) if p.is_file()} for pattern in SCOPE}
    ignored = _ignored(root, set().union(*matched.values()))
    return {pattern: sorted(paths - ignored) for pattern, paths in matched.items()}


def scope_files(root: Path = ROOT) -> list[Path]:
    """Every file in scope, deduplicated and sorted."""
    return sorted(set().union(*scope_by_glob(root).values()))


def shortfalls(by_glob: dict[str, list[Path]], floors: dict[str, int] | None = None) -> list[str]:
    """One line per glob that matched fewer files than its floor, naming the glob; [] when none."""
    floors = SCOPE if floors is None else floors
    short: list[str] = []
    for pattern, floor in floors.items():
        n = len(by_glob.get(pattern, []))
        if n < floor:
            short.append(
                f"{pattern} matched {_count(n, 'file')}, below its floor of {floor}: "
                "a moved, emptied or misspelled path is prose nothing checked"
            )
    return short


def _ignored(root: Path, found: set[Path]) -> set[Path]:
    """The members of found that git ignores; none when git is absent or root is no repository."""
    git = shutil.which("git")
    if git is None or not found:
        return set()
    ignored = subprocess.run(  # noqa: S603 - git with fixed flags, our own paths on stdin
        [git, "check-ignore", "--stdin"],
        cwd=root,
        input="\n".join(os.path.relpath(p, root) for p in sorted(found)),
        capture_output=True,
        text=True,
        check=False,
    )
    # 0: some ignored, 1: none ignored; anything else (not a repository) keeps them all.
    if ignored.returncode not in (0, 1):
        return set()
    return {(root / line).resolve() for line in ignored.stdout.splitlines() if line}


def _baseline_problem(name: str, doc: object) -> str:
    """Why a parsed baseline cannot gate anything, or "" when it is a usable `{rule: count}`."""
    if not isinstance(doc, dict):
        return f"{name} must be a JSON object of rule: count, not {type(doc).__name__}"
    if not doc:
        return f"{name} is empty: a baseline naming no rule would gate nothing"
    for rule, count in cast("dict[object, object]", doc).items():
        if not isinstance(rule, str) or "." not in rule:
            return f"{name}: {rule!r} is not a rule name such as Google.EmDash"
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return f"{name}: {rule} has count {count!r}, not an integer of 0 or more"
    return ""


def load_baseline(path: Path) -> dict[str, int]:
    """The `{rule: count}` baseline at path; raises BaselineError naming what is wrong with it.

    Every way the file could stand for "no limit" is refused: absent, unparsable, not an object,
    empty, a key that is not a rule name, or a count that is not a non-negative integer.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        msg = f"{path.name} is missing: run `mise run docs:prose:baseline`"
        raise BaselineError(msg) from None
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"{path.name} cannot be read: {exc}"
        raise BaselineError(msg) from None
    except json.JSONDecodeError as exc:
        msg = f"{path.name} does not parse as JSON: {exc}"
        raise BaselineError(msg) from None
    problem = _baseline_problem(path.name, doc)
    if problem:
        raise BaselineError(problem)
    return cast("dict[str, int]", doc)


def warning_counts(alerts: dict[str, list[dict[str, object]]]) -> dict[str, int]:
    """Warning-level alerts per rule, from Vale's JSON report."""
    counts: dict[str, int] = {}
    for file_alerts in alerts.values():
        for alert in file_alerts:
            if alert.get("Severity") == "warning":
                rule = str(alert.get("Check"))
                counts[rule] = counts.get(rule, 0) + 1
    return counts


def ratchet(counts: dict[str, int], baseline: dict[str, int]) -> tuple[list[str], list[str]]:
    """(rules above or missing from the baseline, rules below it), one line each, sorted by rule."""
    over: list[str] = []
    under: list[str] = []
    for rule in sorted(set(counts) | set(baseline)):
        n = counts.get(rule, 0)
        if rule not in baseline:
            over.append(f"{rule}: {_count(n, 'warning')}, not in the baseline")
        elif n > baseline[rule]:
            over.append(f"{rule}: {_count(n, 'warning')}, baseline {baseline[rule]}")
        elif n < baseline[rule]:
            under.append(f"{rule}: {_count(n, 'warning')}, baseline {baseline[rule]}")
    return over, under


def write_baseline(path: Path, counts: dict[str, int]) -> list[str]:
    """Rewrite the baseline from counts; return one line per rule whose count moved.

    A rule of the old baseline that counts zero now keeps its entry at 0, so its return fails.
    """
    try:
        previous = load_baseline(path)
    except BaselineError:
        previous = {}
    rules = sorted(set(counts) | set(previous))
    if not rules:
        msg = f"no warnings and no earlier {path.name}: nothing to write"
        raise BaselineError(msg)
    new = {rule: counts.get(rule, 0) for rule in rules}
    path.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    moved: list[str] = []
    for rule in rules:
        old = previous.get(rule)
        if old == new[rule]:
            continue
        if old is None:
            moved.append(f"{rule}: added at {new[rule]}")
        elif new[rule] > old:
            moved.append(
                f"{rule}: raised {old} -> {new[rule]}: a baseline only goes down in review"
            )
        else:
            moved.append(f"{rule}: lowered {old} -> {new[rule]}")
    return moved


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


def _parse_args(argv: Sequence[str]) -> tuple[str, list[str], bool]:
    label = "docs:prose"
    paths: list[str] = []
    write = False
    it = iter(argv)
    for arg in it:
        if arg == "--label":
            label = next(it, "") or _die(label, _USAGE)
        elif arg == "--write-baseline":
            write = True
        elif arg.startswith("-"):
            _die(label, _USAGE)
        else:
            paths.append(arg)
    if write and paths:
        _die(label, "--write-baseline measures the default scope and takes no PATH")
    return label, paths, write


def main(argv: Sequence[str]) -> int:  # noqa: PLR0911 - one exit per property the docstring lists
    """Run the gate; return the exit status the module docstring defines."""
    label, raw, write = _parse_args(argv)
    if not CONFIG.is_file():
        _die(label, f"{CONFIG} is missing")

    # The ratchet binds the default scope only; the selftest's PATH mode stays error-only.
    baseline: dict[str, int] | None = None
    if not raw and not write:
        try:
            baseline = load_baseline(BASELINE)
        except BaselineError as exc:
            sys.stderr.write(f"{label}: {exc}\n")
            return 1

    if raw:
        files = [Path(p).resolve() for p in raw]
        missing = [str(p) for p in files if not p.is_file()]
        if missing:
            _die(label, "no such file: " + ", ".join(missing))
    else:
        by_glob = scope_by_glob()
        short = shortfalls(by_glob)
        if short:
            sys.stderr.writelines(f"{label}: {line}\n" for line in short)
            return 1
        files = sorted(set().union(*by_glob.values()))
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
    for key in sorted(alerts):
        for alert in alerts[key]:
            if alert.get("Severity") == "error":
                line, col = alert.get("Line"), (alert.get("Span") or [0])[0]
                errors.append(f"{key}:{line}:{col}: {alert.get('Check')}: {alert.get('Message')}")
    counts = warning_counts(alerts)
    warnings = sum(counts.values())
    sys.stdout.writelines(f"{err}\n" for err in errors)
    head = (
        f"{label}: vale checked {checked} of {len(files)} files: {_count(len(errors), 'error')}, "
        f"{_count(warnings, 'warning')}"
    )

    if write:
        try:
            moved = write_baseline(BASELINE, counts)
        except BaselineError as exc:
            sys.stderr.write(f"{label}: {exc}\n")
            return 1
        sys.stdout.writelines(f"{label}: {line}\n" for line in moved)
        sys.stdout.write(f"{head}: wrote {BASELINE.name}\n")
        return 1 if errors else 0

    if baseline is None:
        sys.stdout.write(f"{head} (reported, not gated)\n")
        return 1 if errors else 0

    over, under = ratchet(counts, baseline)
    sys.stderr.writelines(f"{label}: {line}\n" for line in over)
    sys.stdout.writelines(f"{label}: {line}\n" for line in under)
    if under:
        sys.stdout.write(f"{label}: {LOWER_HINT}\n")
    sys.stdout.write(
        f"{head}, {_count(len(over), 'rule')} above the baseline of "
        f"{sum(baseline.values())} in {BASELINE.name}\n"
    )
    return 1 if errors or over else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
