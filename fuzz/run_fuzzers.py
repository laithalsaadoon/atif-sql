# SPDX-License-Identifier: Apache-2.0
"""Run each Atheris harness for a bounded time; fail on a finding or on a run that fuzzed nothing.

    uv run --group fuzz python fuzz/run_fuzzers.py [TARGET ...]

``mise run fuzz`` runs every target, and ``mise run fuzz:claude-code`` or
``mise run fuzz:codex`` one of them. ``ATIF_SQL_FUZZ_SECONDS`` sets each
target's ``-max_total_time`` (default 60).

A target is a name in ``transcript_targets.TARGETS``. It runs as
``fuzz_<name>.py`` under libFuzzer with the seeds in ``corpus/<name>/`` and the
dictionary ``<name>.dict``, and everything it writes lands under
``out/<name>/`` (gitignored): ``corpus/``, the inputs libFuzzer found worth
keeping (listed first so libFuzzer writes there and never into the committed
seeds), ``artifacts/``, a crash, timeout or out-of-memory input when there is
one, and ``fuzz.log``, the harness's whole output.

The run fails, naming the target, when:

- atheris is not importable        the ``fuzz`` group installs it on linux x86_64 only
- a target lacks its harness, its  a deleted ``fuzz_<name>.py`` must not leave a run
  seeds or its dictionary          that fuzzes the other target and passes
- a harness names no target        a new harness must be registered, or it never runs
- a requested target is unknown
- no target was selected
- a harness exited non-zero or     an uncaught exception, a timeout, an out-of-memory
  left an artifact                 input, or libFuzzer refusing its flags
- libFuzzer executed no input      the final ``stat::number_of_executed_units`` is
                                   missing or 0, so nothing was fuzzed

Each target prints one line, ``fuzz <name>: <runs> runs in <s> s, exit <code>``,
and a failure adds the reason and the artifact paths.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from transcript_targets import TARGETS

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

FUZZ_DIR = Path(__file__).resolve().parent
SECONDS_ENV = "ATIF_SQL_FUZZ_SECONDS"
DEFAULT_SECONDS = 60

#: libFuzzer's per-input limit in seconds: an input slower than this is a finding.
INPUT_TIMEOUT_SECONDS = 30
#: The largest input libFuzzer generates; the biggest seed is about 5 KB.
MAX_INPUT_BYTES = 16384

_EXECUTED = re.compile(r"^stat::number_of_executed_units:\s*(\d+)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Target:
    """One fuzz target's committed inputs."""

    name: str
    harness: Path
    seeds: Path
    dictionary: Path


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one harness run did."""

    name: str
    returncode: int
    runs: int | None
    seconds: float
    artifacts: tuple[Path, ...]
    log: Path

    @property
    def failure(self) -> str | None:
        """Why this run fails the task, or ``None`` when it passed."""
        if self.returncode != 0 or self.artifacts:
            return f"harness exited {self.returncode} with {len(self.artifacts)} artifact(s)"
        if not self.runs:
            return "libFuzzer executed no input"
        return None


def plan(names: Iterable[str], requested: Sequence[str], fuzz_dir: Path = FUZZ_DIR) -> list[Target]:
    """The targets to run, after checking every registered target is complete.

    Raises
    ------
        SystemExit: a target or harness is missing, a request is unknown, or none was selected.
    """
    registered = sorted(names)
    errors: list[str] = []
    targets: dict[str, Target] = {}
    for name in registered:
        target = Target(
            name=name,
            harness=fuzz_dir / f"fuzz_{name}.py",
            seeds=fuzz_dir / "corpus" / name,
            dictionary=fuzz_dir / f"{name}.dict",
        )
        if not target.harness.is_file():
            errors.append(f"target {name}: no harness {target.harness.name}")
        if not target.seeds.is_dir() or not any(p.is_file() for p in target.seeds.iterdir()):
            errors.append(f"target {name}: no seed inputs in corpus/{name}/")
        if not target.dictionary.is_file():
            errors.append(f"target {name}: no dictionary {target.dictionary.name}")
        targets[name] = target
    errors.extend(
        f"harness {path.name} names no registered target"
        for path in sorted(fuzz_dir.glob("fuzz_*.py"))
        if path.stem.removeprefix("fuzz_") not in targets
    )
    errors.extend(f"unknown target {name}" for name in requested if name not in targets)
    selected = [targets[name] for name in (requested or registered) if name in targets]
    if not selected:
        errors.append("no fuzz target selected")
    if errors:
        raise SystemExit("\n".join(f"fuzz: {error}" for error in errors))
    return selected


def executed_units(log_text: str) -> int | None:
    """LibFuzzer's final count of executed inputs, or ``None`` when it never printed one."""
    counts = _EXECUTED.findall(log_text)
    return int(counts[-1]) if counts else None


def run_target(
    target: Target, seconds: int, out_dir: Path, python: str = sys.executable
) -> Outcome:
    """Run one harness under libFuzzer for ``seconds`` and read back what it did."""
    work = out_dir / target.name
    corpus = work / "corpus"
    artifacts = work / "artifacts"
    shutil.rmtree(artifacts, ignore_errors=True)
    corpus.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True)
    log = work / "fuzz.log"
    command = [
        python,
        str(target.harness),
        f"-max_total_time={seconds}",
        f"-timeout={INPUT_TIMEOUT_SECONDS}",
        f"-max_len={MAX_INPUT_BYTES}",
        f"-dict={target.dictionary}",
        f"-artifact_prefix={artifacts}{os.sep}",
        "-print_final_stats=1",
        str(corpus),
        str(target.seeds),
    ]
    started = time.monotonic()
    with log.open("wb") as sink:
        returncode = subprocess.run(  # noqa: S603 - our interpreter, our harness, fixed flags
            command, stdout=sink, stderr=subprocess.STDOUT, check=False
        ).returncode
    elapsed = time.monotonic() - started
    return Outcome(
        name=target.name,
        returncode=returncode,
        runs=executed_units(log.read_text(encoding="utf-8", errors="replace")),
        seconds=elapsed,
        artifacts=tuple(sorted(artifacts.iterdir())),
        log=log,
    )


def report(outcome: Outcome) -> str:
    """The lines one outcome prints."""
    runs = "no" if outcome.runs is None else str(outcome.runs)
    lines = [
        f"fuzz {outcome.name}: {runs} runs in {outcome.seconds:.0f} s, exit {outcome.returncode}"
    ]
    if outcome.failure is not None:
        lines.append(f"fuzz {outcome.name}: FAILED: {outcome.failure}; log {outcome.log}")
        lines.extend(f"fuzz {outcome.name}: artifact {path}" for path in outcome.artifacts)
    return "\n".join(lines)


def seconds_from_env() -> int:
    """``ATIF_SQL_FUZZ_SECONDS`` as a positive whole number of seconds.

    Raises
    ------
        SystemExit: the variable is set to anything but a positive integer.
    """
    raw = os.environ.get(SECONDS_ENV, "").strip()
    if not raw:
        return DEFAULT_SECONDS
    if not raw.isdigit() or int(raw) < 1:
        msg = f"fuzz: {SECONDS_ENV} must be a positive whole number of seconds, got {raw!r}"
        raise SystemExit(msg)
    return int(raw)


def main(argv: Sequence[str]) -> int:
    """Run the requested targets (all when none is named); 0 only when each one passed."""
    seconds = seconds_from_env()
    targets = plan(TARGETS, argv)
    if importlib.util.find_spec("atheris") is None:
        msg = (
            "fuzz: atheris is not installed; it installs from the `fuzz` dependency group on "
            "linux x86_64 only (uv run --group fuzz)"
        )
        raise SystemExit(msg)
    outcomes = [run_target(target, seconds, FUZZ_DIR / "out") for target in targets]
    sys.stdout.writelines(f"{report(outcome)}\n" for outcome in outcomes)
    failed = [outcome.name for outcome in outcomes if outcome.failure is not None]
    if failed:
        sys.stdout.write(f"fuzz: {len(failed)} of {len(outcomes)} target(s) failed: {failed}\n")
        return 1
    total = sum(outcome.runs or 0 for outcome in outcomes)
    sys.stdout.write(f"fuzz: {len(outcomes)} target(s) passed, {total} runs\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
