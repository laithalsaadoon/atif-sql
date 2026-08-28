# SPDX-License-Identifier: Apache-2.0

"""Raw-side census of a Claude Code session: the input half of a LossReport.

Counts come from records already parsed out of a
:class:`~atif_converter.infrastructure.raw_records.SessionSnapshot`, and the
side-file classification is derived from that same snapshot's file list — no
second parse, no disk access, no harbor, no env, no network. Both halves read
from the one snapshot, so the census, the trajectory, and edges.jsonl
describe the same bytes of a session that may still be growing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atif_converter.domain.fidelity import RecordType
from atif_converter.infrastructure.raw_records import SessionSnapshot

_KNOWN_TYPES: dict[str, RecordType] = {
    rt.value: rt for rt in RecordType if rt is not RecordType.OTHER
}

#: harbor's ``rglob("subagents/*.jsonl")`` discovers a side-file only when this
#: is the name of its immediate parent directory.
_HARBOR_SUBAGENT_DIR = "subagents"


@dataclass(frozen=True, slots=True)
class SessionCensus:
    """Raw record counts for one session (main file + every side-file).

    ``record_counts`` covers EVERY record of EVERY discovered ``*.jsonl``, so
    it agrees line-for-line with edges.jsonl. ``subagent_files`` are the
    side-files harbor 0.22.0's ``rglob("subagents/*.jsonl")`` can discover;
    ``workflow_subagent_files`` are the ones it cannot — those nested deeper
    (``subagents/workflows/wf_*/agent-*.jsonl``) plus any other ``*.jsonl``
    elsewhere in the side directory — fidelity gap 1.
    """

    session_jsonl: Path
    record_counts: dict[RecordType, int] = field(default_factory=dict)
    subagent_files: tuple[Path, ...] = ()
    workflow_subagent_files: tuple[Path, ...] = ()


def _record_type(record: dict[str, Any]) -> RecordType:
    raw_type = record.get("type")
    if isinstance(raw_type, str):
        return _KNOWN_TYPES.get(raw_type, RecordType.OTHER)
    return RecordType.OTHER


def census_from_snapshot(
    snapshot: SessionSnapshot,
    records: Iterable[tuple[dict[str, Any], str]],
) -> SessionCensus:
    """Census one snapshot: record-type counts plus side-file classification.

    ``records`` are that snapshot's parsed pairs from
    :func:`~atif_converter.infrastructure.raw_records.read_snapshot_records`.
    Only each record's ``type`` is read, so a generator whose records are
    discarded as they are consumed counts the same as a list.
    """
    counts: dict[RecordType, int] = {}
    for record, _source_file in records:
        record_type = _record_type(record)
        counts[record_type] = counts.get(record_type, 0) + 1

    session_jsonl = snapshot.session_jsonl
    subagent_files: list[Path] = []
    workflow_files: list[Path] = []
    for candidate in snapshot.files:
        if candidate == session_jsonl:
            continue
        if candidate.parent.name == _HARBOR_SUBAGENT_DIR:
            subagent_files.append(candidate)
        else:
            workflow_files.append(candidate)

    return SessionCensus(
        session_jsonl=session_jsonl,
        record_counts=counts,
        subagent_files=tuple(subagent_files),
        workflow_subagent_files=tuple(workflow_files),
    )
