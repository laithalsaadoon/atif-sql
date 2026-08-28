# SPDX-License-Identifier: Apache-2.0

"""The converter port: what materialization NEEDS from a converter.

atif-corpus may never import atif-converter (import-linter independence
contract), and the converter's real signature is changing on a sibling
branch — so this Protocol is typed to CONTRACT.md's artifact shapes, not to
converter internals. atif-cli adapts the real converter to this port later;
tests use :class:`atif_corpus.infrastructure.fake_converter.FakeConverter`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
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
