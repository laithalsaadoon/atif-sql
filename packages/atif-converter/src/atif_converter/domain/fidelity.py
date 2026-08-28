# SPDX-License-Identifier: Apache-2.0

"""The fidelity policy: what harbor's Claude Code -> ATIF conversion loses.

Pure value objects — no harbor import, no I/O. This module NAMES the known
upstream gaps (verified empirically against harbor==0.22.0 on 2026-08-22)
rather than fixing them; the converter's job today is honest loss accounting,
not repair. The unit tests pin each gap so a harbor version bump that changes
behavior trips the suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RecordType(Enum):
    """Taxonomy of raw Claude Code session JSONL record types.

    ``type`` field values seen in the wild, plus ``OTHER`` for anything new.
    Only USER and ASSISTANT records are converted by harbor 0.22.0; everything
    else is silently dropped (:attr:`FidelityGap.NON_MESSAGE_RECORDS_DROPPED`).
    """

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    ATTACHMENT = "attachment"
    QUEUE_OPERATION = "queue-operation"
    MODE = "mode"
    LAST_PROMPT = "last-prompt"
    SUMMARY = "summary"
    PR_LINK = "pr-link"
    STARTED = "started"
    RESULT = "result"
    OTHER = "other"


#: Record types harbor 0.22.0 actually converts into ATIF steps.
CONVERTIBLE_RECORD_TYPES: frozenset[RecordType] = frozenset({RecordType.USER, RecordType.ASSISTANT})


class FidelityGap(Enum):
    """The seven known upstream gaps in harbor 0.22.0's converter.

    Each member documents one loss verified against
    ``ClaudeCode._convert_events_to_trajectory``.
    """

    #: (1) Discovery uses ``rglob("subagents/*.jsonl")``, which misses
    #: workflow-nested side-files at ``subagents/workflows/wf_*/agent-*.jsonl``.
    WORKFLOW_SUBAGENTS_MISSED = "workflow_subagents_missed"

    #: (2) Only user/assistant events are converted; system / attachment /
    #: queue-operation / mode / last-prompt / summary records are silently dropped.
    NON_MESSAGE_RECORDS_DROPPED = "non_message_records_dropped"

    #: (3) The parentUuid tree is flattened by timestamp sort, not chain-walked,
    #: so branch/rewind structure is lost.
    PARENT_CHAIN_FLATTENED = "parent_chain_flattened"

    #: (4) Subagent events are INLINED into the flat step list (marked only via
    #: ``extra.is_sidechain``) instead of ``subagent_trajectories`` embedding.
    SUBAGENTS_INLINED = "subagents_inlined"

    #: (5) No ``isCompactSummary`` handling — compaction summaries are not
    #: distinguished from ordinary user messages.
    COMPACT_SUMMARY_UNHANDLED = "compact_summary_unhandled"

    #: (6) Cache split partially preserved: ``cached_tokens`` = cache_read only;
    #: cache_creation survives only inside ``metrics.extra``.
    CACHE_SPLIT_PARTIAL = "cache_split_partial"

    #: (7) The event-level ``uuid`` is read only for dedup and never lands in
    #: ``Step.extra`` — step-to-raw-record identity is unrecoverable.
    #: (Verified 2026-08-22: step.extra carries requestId / message id /
    #: is_sidechain, never the event uuid.)
    UUID_NOT_PRESERVED = "uuid_not_preserved"


@dataclass(frozen=True, slots=True)
class LossReport:
    """Per-session loss accounting: raw-side census vs converted output.

    ``record_counts`` is the raw census by :class:`RecordType`;
    ``records_converted`` / ``records_dropped`` partition the total;
    ``gaps_observed`` names which :class:`FidelityGap` members this session
    actually exhibits (a session with no subagents cannot observe gap 1 or 4).

    CENSUS SCOPE: ``record_counts`` covers every record of every ``*.jsonl``
    discovered under the session's side directory, at ANY depth and whether
    or not a ``subagents/`` part appears in its path. That is the same file
    set the adapter stages into harbor and the same set edges.jsonl is built
    from, so ``records_total`` equals the edges line count for any session.
    A side-file outside ``subagents/`` therefore raises ``records_total``
    (and, for a non-message type, ``records_dropped``) relative to a census
    scoped to ``subagents/`` alone.
    """

    record_counts: dict[RecordType, int] = field(default_factory=dict)
    records_converted: int = 0
    records_dropped: int = 0
    gaps_observed: frozenset[FidelityGap] = frozenset()
    subagent_files_found: int = 0
    subagent_files_convertible: int = 0
    workflow_subagent_files_found: int = 0

    @property
    def records_total(self) -> int:
        """Total raw records counted in the census."""
        return sum(self.record_counts.values())

    def to_json(self) -> dict[str, int | dict[str, int] | list[str]]:
        """The CONTRACT ``loss_report.json`` document, JSON-serializable.

        Named by docs/CONTRACT.md ("loss_report.json — atif_converter
        LossReport.to_json()") and read back by atif-duck's
        ``_LOSS_REPORT_COLUMNS`` projection, so the keys here are wire
        contract. Enum members flatten to their string values;
        ``gaps_observed`` is sorted for deterministic output.
        """
        return {
            "record_counts": {
                record_type.value: count
                for record_type, count in sorted(
                    self.record_counts.items(), key=lambda kv: kv[0].value
                )
            },
            "records_total": self.records_total,
            "records_converted": self.records_converted,
            "records_dropped": self.records_dropped,
            "gaps_observed": sorted(gap.value for gap in self.gaps_observed),
            "subagent_files_found": self.subagent_files_found,
            "subagent_files_convertible": self.subagent_files_convertible,
            "workflow_subagent_files_found": self.workflow_subagent_files_found,
        }
