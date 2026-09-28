# SPDX-License-Identifier: Apache-2.0

"""Test adapter for :class:`atif_corpus.domain.ports.ConverterPort`.

Lives in ``infrastructure`` (not ``tests/``) on purpose: atif-cli's tests
and future integration harnesses need the same fake, and a fake that ships
with the package is type-checked against the port on every ``ty`` run
instead of drifting in a conftest.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, Any

import zstandard

from atif_corpus.domain.ports import ArchivedSource, ConversionOutput, EmptySourceError

if TYPE_CHECKING:
    from pathlib import Path


def _archive_sources(session_jsonl: Path, archive_dir: Path) -> tuple[ArchivedSource, ...]:
    """Write a zstd copy of the main file and every file under its side dir.

    The fake reads the files itself; the real adapter takes the bytes from the
    converter's verifying read instead (see ``atif_converter.infrastructure
    .source_archive``). The on-disk shape is the same, which is all the use
    case sees.
    """
    base = session_jsonl.parent
    side_dir = base / session_jsonl.stem
    paths = [session_jsonl]
    if side_dir.is_dir():
        paths.extend(p for p in sorted(side_dir.rglob("*")) if p.is_file() and not p.is_symlink())
    archived: list[ArchivedSource] = []
    for path in paths:
        data = path.read_bytes()
        relative = path.relative_to(base).as_posix()
        target = archive_dir / f"{relative}.zst"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zstandard.ZstdCompressor(level=3).compress(data))
        archived.append(
            ArchivedSource(
                relative_path=relative, size=len(data), sha256=hashlib.sha256(data).hexdigest()
            )
        )
    return tuple(archived)


#: This process's simulated clock, in seconds. Only :class:`FakeConverter`
#: with ``simulated_seconds`` moves it, and only inside ``convert``.
_simulated_now: float = 0.0


def simulated_clock() -> float:
    """A clock that advances only while a :class:`FakeConverter` converts.

    Hand it to ``materialize(convert_clock=...)`` with
    ``FakeConverter(simulated_seconds=s)`` and every conversion measures
    exactly ``s``, in whichever process (inline or pool worker) runs it,
    however long the pass really takes. Module-level, so it pickles into a
    spawned worker by reference; each process keeps its own count.
    """
    return _simulated_now


class FakeConverter:
    """A deterministic in-memory converter with scriptable failures.

    ``convert`` fabricates a minimal-but-contract-shaped output keyed by the
    session stem, records every call in ``converted``, and raises for any
    session listed in ``fail_sessions`` — which is how the "a failing
    session is recorded and skipped" behavior gets exercised.

    Two knobs exist for the process-pool tests. ``record_pid`` stamps the
    converting process's pid into the trajectory as ``worker_pid``, so a test
    can read back from disk WHICH process converted each session — a pool
    that quietly ran inline shows the parent's pid on every one.
    ``delay_seconds`` holds each conversion open long enough for the pool's
    other workers to start and take a session of their own; without it one
    fast worker can drain a small fixture before the second exists. Both are
    off by default, and ``converted`` is only meaningful on the inline path:
    a pool worker's copy of this object never comes back.

    ``simulated_seconds`` advances :func:`simulated_clock` by that much on
    every conversion, so a pass timed with that clock reports a known
    duration per session.

    ``empty_sessions`` raises :class:`~atif_corpus.domain.ports.EmptySourceError`
    for the named sessions, the port's "nothing to convert" verdict.
    ``output_tag`` stamps ``converter_tag`` into every trajectory so a test can
    read back which converter build wrote a session. Handed an
    ``archive_dir``, the fake writes a real zstd archive of the session's files.
    """

    def __init__(
        self,
        *,
        fail_sessions: frozenset[str] = frozenset(),
        empty_sessions: frozenset[str] = frozenset(),
        record_pid: bool = False,
        delay_seconds: float = 0.0,
        simulated_seconds: float = 0.0,
        output_tag: str | None = None,
    ) -> None:
        #: Session ids whose conversion should raise.
        self.fail_sessions = fail_sessions
        #: Session ids whose conversion raises :class:`EmptySourceError`.
        self.empty_sessions = empty_sessions
        #: Stamped into the trajectory as ``converter_tag``, so a test can see
        #: which converter wrote the artifacts on disk (a version bump's re-run).
        self.output_tag = output_tag
        #: Stamp ``os.getpid()`` into the trajectory as ``worker_pid``.
        self.record_pid = record_pid
        #: Seconds each conversion sleeps before returning.
        self.delay_seconds = delay_seconds
        #: Seconds each conversion advances :func:`simulated_clock` by.
        self.simulated_seconds = simulated_seconds
        #: Every session path convert() was asked about, in call order.
        self.converted: list[Path] = []

    def convert(self, session_jsonl: Path, *, archive_dir: Path | None = None) -> ConversionOutput:
        """Fabricate contract-shaped artifacts for ``session_jsonl``."""
        session_id = session_jsonl.stem
        self.converted.append(session_jsonl)
        if session_id in self.fail_sessions:
            msg = f"scripted failure for {session_id}"
            raise RuntimeError(msg)
        if session_id in self.empty_sessions:
            msg = f"no convertible events in {session_jsonl}"
            raise EmptySourceError(msg)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.simulated_seconds:
            global _simulated_now  # noqa: PLW0603 — the clock is per process by design
            _simulated_now += self.simulated_seconds
        trajectory: dict[str, Any] = {
            "schema_version": "ATIF-v1.7",
            "session_id": session_id,
            "steps": [],
        }
        if self.record_pid:
            trajectory["worker_pid"] = os.getpid()
        if self.output_tag is not None:
            trajectory["converter_tag"] = self.output_tag
        loss_report: dict[str, Any] = {
            "records_converted": 0,
            "records_dropped": 0,
            "gaps_observed": [],
        }
        edge: dict[str, Any] = {
            "uuid": f"{session_id}-u1",
            "parent_uuid": None,
            "message_id": None,
            "type": "user",
            "ts": "2026-01-01T00:00:00Z",
            "is_sidechain": False,
            "is_compact_summary": False,
            "source_file": str(session_jsonl),
            "tool_use_ids": [],
        }
        return ConversionOutput(
            trajectory_dict=trajectory,
            loss_report_dict=loss_report,
            edges_lines=[json.dumps(edge, separators=(",", ":"))],
            source_archive=(
                () if archive_dir is None else _archive_sources(session_jsonl, archive_dir)
            ),
        )
