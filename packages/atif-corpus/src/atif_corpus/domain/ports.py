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
    """

    trajectory_dict: dict[str, Any]
    loss_report_dict: dict[str, Any]
    edges_lines: list[str]


class ConverterPort(Protocol):
    """Anything that can turn one session JSONL into corpus artifacts.

    Implementations may raise any exception: the materialize use case
    records the failure against the session and continues — one broken
    transcript must never abort a corpus sync.
    """

    def convert(self, session_jsonl: Path) -> ConversionOutput:
        """Convert one session (main JSONL + its side-files) to artifacts."""
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
