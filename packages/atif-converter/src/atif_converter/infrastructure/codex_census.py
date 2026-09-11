# SPDX-License-Identifier: Apache-2.0

"""Raw-side census of a Codex rollout: the input half of its LossReport.

The Codex counterpart of :mod:`atif_converter.infrastructure.census`, and it
counts the same way: over records already parsed out of a
:class:`~atif_converter.infrastructure.raw_records.SessionSnapshot`, with no
second parse, no disk access, no harbor, no env and no network.

Two things differ from the Claude Code census, both because a rollout is one
file rather than a main transcript plus a side-file tree:

* there are no subagent side-files to classify — a Codex sub-agent writes its
  own rollout under its own session id, so it is a separate session, not a
  side-file of this one;
* the convertible subset is finer than a record type. Only ``response_item``
  records become steps, and only SOME payload types within them do
  (:data:`~atif_converter.domain.codex_fidelity.CODEX_CONVERTIBLE_ITEM_TYPES`),
  so the census counts the convertible items separately instead of declaring
  every response item converted.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atif_converter.domain.codex_fidelity import (
    CODEX_CONVERTIBLE_ITEM_TYPES,
    CodexRecordType,
)

_KNOWN_TYPES: dict[str, CodexRecordType] = {
    rt.value: rt for rt in CodexRecordType if rt is not CodexRecordType.OTHER
}


@dataclass(frozen=True, slots=True)
class CodexSessionCensus:
    """Raw record counts for one rollout, plus the convertible-item detail.

    ``record_counts`` covers EVERY record of the rollout, so it agrees
    line-for-line with edges.jsonl. ``convertible_items`` is the subset of
    ``response_item`` records whose payload type reaches a step, and
    ``item_type_counts`` is the full response-item breakdown — which is what
    makes a dropped ``tool_search_call`` visible as a number rather than as an
    absence.
    """

    session_jsonl: Path
    record_counts: dict[CodexRecordType, int] = field(default_factory=dict)
    item_type_counts: dict[str, int] = field(default_factory=dict)
    convertible_items: int = 0

    @property
    def response_items(self) -> int:
        """Total ``response_item`` records, convertible or not."""
        return self.record_counts.get(CodexRecordType.RESPONSE_ITEM, 0)


def _record_type(record: dict[str, Any]) -> CodexRecordType:
    raw_type = record.get("type")
    if isinstance(raw_type, str):
        return _KNOWN_TYPES.get(raw_type, CodexRecordType.OTHER)
    return CodexRecordType.OTHER


def codex_census_from_records(
    session_jsonl: Path,
    records: Iterable[tuple[dict[str, Any], str]],
) -> CodexSessionCensus:
    """Census one rollout's parsed records.

    Only each record's ``type`` and, for a response item, its payload
    ``type`` are read, so a generator whose records are discarded as they are
    consumed counts the same as a list.
    """
    counts: dict[CodexRecordType, int] = {}
    item_counts: dict[str, int] = {}
    convertible = 0
    for record, _source_file in records:
        record_type = _record_type(record)
        counts[record_type] = counts.get(record_type, 0) + 1
        if record_type is not CodexRecordType.RESPONSE_ITEM:
            continue
        payload = record.get("payload")
        item_type = payload.get("type") if isinstance(payload, dict) else None
        key = item_type if isinstance(item_type, str) and item_type else "unknown"
        item_counts[key] = item_counts.get(key, 0) + 1
        if key in CODEX_CONVERTIBLE_ITEM_TYPES:
            convertible += 1

    return CodexSessionCensus(
        session_jsonl=session_jsonl,
        record_counts=counts,
        item_type_counts=item_counts,
        convertible_items=convertible,
    )
