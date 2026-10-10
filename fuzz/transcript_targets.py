# SPDX-License-Identifier: Apache-2.0
"""Fuzz targets for the two transcript converters, free of any fuzzing engine.

A target takes one fuzzer input, writes it to disk the way the agent writes a
transcript, and runs the use case materialize runs on that file:
:func:`~atif_converter.application.convert_and_audit.convert_and_audit` for a
Claude Code session, and
:func:`~atif_converter.application.convert_codex.convert_codex_and_audit` for a
Codex rollout. The whole read path is under test: the UTF-8 decode, the JSONL
split, attachment extraction, the conversion, the census, the edges and events
emitters, enrichment and the snapshot re-check.

THE CONTRACT. The converter's application layer surfaces only
:class:`~atif_converter.domain.errors.DomainError` subtypes (an empty session,
a non-UTF-8 file, a converter failure classified at the seam), and that is the
documented rejection of bad input that atif-cli's ``RealConverter`` relies on to
record a per-session failure and move on. Any other exception escapes the
target and is a finding. A conversion that succeeds must also hand the corpus
writer what it can write: the trajectory and the loss report encode as JSON with
the writer's encoder, every edges and events line decodes to one JSON object,
and every lifted attachment's bytes hash to the sha256 it is filed under.

INPUT LAYOUT. A NUL byte can't occur in a JSONL line (JSON rejects a raw control
character inside a string and outside one), so it splits one input into files.
For Claude Code the bytes up to the first NUL are the main
``<session-id>.jsonl``, the bytes up to the second NUL are a subagent
transcript at ``<session-id>/subagents/agent-fuzz.jsonl``, and the rest is that
subagent's ``agent-fuzz.meta.json`` sidecar. A Codex rollout has no side files,
so the whole input is one ``rollout-<ts>-<uuid>.jsonl``.

The atheris entrypoints beside this module (``fuzz_claude_code.py``,
``fuzz_codex.py``) instrument and drive these functions; ``test_fuzz_seed_corpus.py``
replays the committed seeds under ``corpus/`` through them in ``mise run check``,
where atheris is not installed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from atif_converter.application.convert_and_audit import convert_and_audit
from atif_converter.application.convert_codex import convert_codex_and_audit
from atif_converter.domain.errors import DomainError

if TYPE_CHECKING:
    from atif_converter.domain.fidelity import LossReport
    from atif_converter.infrastructure.harbor_adapter import ConversionResult

#: The exceptions a target lets through as the converter's verdict on bad input.
EXPECTED_REJECTIONS: tuple[type[Exception], ...] = (DomainError,)

#: The session id the Claude Code input is filed under; any id passes discovery.
CLAUDE_SESSION_ID = "00000000-0000-4000-8000-000000000000"

#: A Codex rollout filename, which ``codex_session_id`` parses for its trailing uuid.
CODEX_ROLLOUT_NAME = f"rollout-2026-01-01T00-00-00-{CLAUDE_SESSION_ID}.jsonl"

_SEPARATOR = b"\x00"

#: The corpus writer's encoder (``atif_corpus.infrastructure.atomic``): compact,
#: and NaN allowed, as the writer allows it.
_ENCODER = json.JSONEncoder(separators=(",", ":"))


class FuzzTarget(Protocol):
    """One target: libFuzzer calls it with the input alone, the replay test adds a directory."""

    def __call__(self, data: bytes, workdir: Path | None = None, /) -> None:
        """Convert ``data``; raise on anything but an expected rejection."""
        ...


class InvariantViolation(AssertionError):  # noqa: N818 - names the finding, as the domain errors do
    """A conversion succeeded but produced an artifact the corpus could not write."""


@cache
def _scratch_root() -> Path:
    """One scratch directory per process, reused by every input."""
    return Path(tempfile.mkdtemp(prefix="atif-fuzz-"))


def _fresh(workdir: Path | None, name: str) -> Path:
    """An empty directory for one input: ``workdir`` when given, else the process scratch."""
    root = (workdir if workdir is not None else _scratch_root()) / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return root


def _check_artifacts(result: ConversionResult, report: LossReport) -> None:
    """Fail when a successful conversion hands the corpus something it can't store."""
    _ENCODER.encode(result.trajectory)
    _ENCODER.encode(report.to_json())
    for line in (*result.edges_lines, *result.events_lines):
        if not isinstance(json.loads(line), dict):
            msg = f"an edges or events line is not a JSON object: {line[:200]!r}"
            raise InvariantViolation(msg)
    for blob in result.blobs:
        if hashlib.sha256(blob.data).hexdigest() != blob.ref.sha256:
            msg = f"blob filed under {blob.ref.sha256} hashes to something else"
            raise InvariantViolation(msg)
        if len(blob.data) != blob.ref.byte_count:
            msg = f"blob {blob.ref.sha256} holds {len(blob.data)} bytes, not {blob.ref.byte_count}"
            raise InvariantViolation(msg)


def write_claude_code_session(data: bytes, root: Path) -> Path:
    """Lay one input out as a Claude Code session under ``root``; return the main JSONL."""
    main, _, rest = data.partition(_SEPARATOR)
    session_jsonl = root / f"{CLAUDE_SESSION_ID}.jsonl"
    session_jsonl.write_bytes(main)
    if rest:
        subagent, _, sidecar = rest.partition(_SEPARATOR)
        side_dir = root / CLAUDE_SESSION_ID / "subagents"
        side_dir.mkdir(parents=True)
        (side_dir / "agent-fuzz.jsonl").write_bytes(subagent)
        if sidecar:
            (side_dir / "agent-fuzz.meta.json").write_bytes(sidecar)
    return session_jsonl


def claude_code_one_input(data: bytes, workdir: Path | None = None) -> None:
    """Convert one input as a Claude Code session; raise on anything but a DomainError."""
    session_jsonl = write_claude_code_session(data, _fresh(workdir, "claude_code"))
    try:
        result, report = convert_and_audit(session_jsonl)
    except EXPECTED_REJECTIONS:
        return
    _check_artifacts(result, report)


def codex_one_input(data: bytes, workdir: Path | None = None) -> None:
    """Convert one input as a Codex rollout; raise on anything but a DomainError."""
    rollout = _fresh(workdir, "codex") / CODEX_ROLLOUT_NAME
    rollout.write_bytes(data)
    try:
        result, report = convert_codex_and_audit(rollout)
    except EXPECTED_REJECTIONS:
        return
    _check_artifacts(result, report)


#: Every target by name: the name is its seed directory under ``corpus/`` and its
#: harness ``fuzz_<name>.py``, and ``run_fuzzers.py`` fails when either is missing.
TARGETS: dict[str, FuzzTarget] = {
    "claude_code": claude_code_one_input,
    "codex": codex_one_input,
}
