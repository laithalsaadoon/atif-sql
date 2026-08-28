# SPDX-License-Identifier: Apache-2.0

"""Test adapter for :class:`atif_corpus.domain.ports.ConverterPort`.

Lives in ``infrastructure`` (not ``tests/``) on purpose: atif-cli's tests
and future integration harnesses need the same fake, and a fake that ships
with the package is type-checked against the port on every ``ty`` run
instead of drifting in a conftest.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from atif_corpus.domain.ports import ConversionOutput

if TYPE_CHECKING:
    from pathlib import Path


class FakeConverter:
    """A deterministic in-memory converter with scriptable failures.

    ``convert`` fabricates a minimal-but-contract-shaped output keyed by the
    session stem, records every call in ``converted``, and raises for any
    session listed in ``fail_sessions`` — which is how the "a failing
    session is recorded and skipped" behavior gets exercised.
    """

    def __init__(self, *, fail_sessions: frozenset[str] = frozenset()) -> None:
        #: Session ids whose conversion should raise.
        self.fail_sessions = fail_sessions
        #: Every session path convert() was asked about, in call order.
        self.converted: list[Path] = []

    def convert(self, session_jsonl: Path) -> ConversionOutput:
        """Fabricate contract-shaped artifacts for ``session_jsonl``."""
        session_id = session_jsonl.stem
        self.converted.append(session_jsonl)
        if session_id in self.fail_sessions:
            msg = f"scripted failure for {session_id}"
            raise RuntimeError(msg)
        trajectory: dict[str, Any] = {
            "schema_version": "ATIF-v1.7",
            "session_id": session_id,
            "steps": [],
        }
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
        )
