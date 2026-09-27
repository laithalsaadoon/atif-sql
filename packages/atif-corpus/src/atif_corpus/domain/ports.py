# SPDX-License-Identifier: Apache-2.0

"""The ports materialization needs filled: a converter and, optionally, an artifact producer.

atif-corpus may never import atif-converter or atif-duck (import-linter
independence contract), so both Protocols are typed to CONTRACT.md's artifact
shapes rather than to either package's internals. atif-cli adapts the real
converter and the real columnar producer to these ports; tests use
:class:`atif_corpus.infrastructure.fake_converter.FakeConverter` and a fake
producer of their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class EmptySourceError(Exception):
    """The session holds nothing to convert (no user or assistant record).

    Not a failure: the transcript is well-formed and simply empty, and it stays
    empty until something writes to it. An adapter translates its converter's
    own "empty" verdict into this so the use case can record the session as
    empty, keyed on its source mtimes, instead of retrying it every pass. Any
    OTHER exception is a real failure.
    """


@dataclass(frozen=True, slots=True)
class ArchivedSource:
    """One file of a session's raw source archive, as the converter wrote it.

    The compressed copy lives at ``<archive_dir>/<relative_path>.zst``;
    ``size`` and ``sha256`` describe the UNCOMPRESSED bytes, so a restore can
    prove it got the original back.
    """

    #: POSIX path relative to the main transcript's parent directory.
    relative_path: str
    #: Uncompressed size in bytes.
    size: int
    #: Hex sha256 of the uncompressed bytes.
    sha256: str


@dataclass(frozen=True, slots=True)
class ConversionOutput:
    """Everything one conversion yields that the corpus writes to disk.

    Shapes are the contract's, pre-serialized by the adapter:

    * ``trajectory_dict`` — the ATIF-v1.7 trajectory, ready for compact
      ``json.dumps(separators=(",", ":"))``.
    * ``loss_report_dict`` — ``LossReport.to_json()``-shaped dict.
    * ``edges_lines`` — one already-serialized JSON line per RAW record
      (uuid, parent_uuid, message_id, type, ts, is_sidechain,
      is_compact_summary, source_file, tool_use_ids), WITHOUT trailing
      newlines; the writer owns line termination.
    * ``source_archive`` — the files the converter archived into the
      ``archive_dir`` it was handed (empty when it was handed none, or when
      the adapter does not archive). Every entry must exist on disk as
      ``<archive_dir>/<relative_path>.zst``; the use case checks that.
    """

    trajectory_dict: dict[str, Any]
    loss_report_dict: dict[str, Any]
    edges_lines: list[str]
    source_archive: tuple[ArchivedSource, ...] = ()


class ConverterPort(Protocol):
    """Anything that can turn one session JSONL into corpus artifacts.

    Implementations may raise any exception: the materialize use case
    records the failure against the session and continues — one broken
    transcript must never abort a corpus sync. :class:`EmptySourceError` is
    the one exception with its own meaning: the session is empty, not broken.
    """

    def convert(self, session_jsonl: Path, *, archive_dir: Path | None = None) -> ConversionOutput:
        """Convert one session (main JSONL + its side-files) to artifacts.

        With ``archive_dir``, also write a zstd copy of every source file of the
        session under it, named by its path relative to ``session_jsonl``'s
        parent plus ``.zst``, and list them in
        :attr:`ConversionOutput.source_archive`. The copy must be of the bytes
        the artifacts were built from; an adapter that cannot promise that
        should archive nothing rather than something else.
        """
        ...


class ArtifactProducer(Protocol):
    """Anything that writes EXTRA per-session artifacts beside the four contract ones.

    The materialize use case calls :meth:`produce` once per session, inside
    the staged session directory, after ``trajectory.json`` /
    ``loss_report.json`` / ``edges.jsonl`` are written and before
    ``meta.json`` is. Whatever the producer writes therefore publishes
    atomically with the four contract artifacts (the whole directory is
    swapped into place) and is covered by the same completeness marker.

    The producer returns extra keys for ``meta.json``. They must not collide
    with the contract's own keys; the use case treats a collision as a
    programming error and fails the session.

    Implementations may raise any exception: the use case records the
    failure against the session, never publishes the staged directory, and
    retries the session next pass, exactly as it does for a converter
    failure.
    """

    def produce(
        self,
        session_dir: Path,
        *,
        session_id: str,
        trajectory: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Write extra artifacts for ``session_id`` into ``session_dir``; return meta extras."""
        ...
