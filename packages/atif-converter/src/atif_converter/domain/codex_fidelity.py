# SPDX-License-Identifier: Apache-2.0

"""The fidelity policy for Codex CLI: what harbor's Codex converter loses.

Pure value objects — no harbor import, no I/O. The Claude Code half of this
policy lives in :mod:`atif_converter.domain.fidelity`; this module is its
Codex counterpart and follows the same discipline: NAME each upstream gap
rather than fix it, and let the unit tests pin it so a harbor bump that
changes behavior trips the suite.

Every member below was verified empirically on 2026-09-11 against
harbor==0.22.0's Codex conversion over real rollouts from ``~/.codex/sessions``
written by codex-cli 0.153.4 and 0.154.0. Our converter
(:mod:`atif_converter.domain.codex_conversion`) is a parity port of that
conversion, so these losses are ours now, by choice.
"""

from __future__ import annotations

from enum import Enum


class CodexRecordType(Enum):
    """Taxonomy of raw Codex rollout JSONL record types.

    The top-level ``type`` field values seen in the wild, plus ``OTHER`` for
    anything new. Only ``RESPONSE_ITEM`` records become ATIF steps;
    ``EVENT_MSG`` and ``TURN_CONTEXT`` are read for turn ids and token
    counts but produce no step of their own, and the rest are dropped
    (:attr:`CodexFidelityGap.NON_ITEM_RECORDS_DROPPED`).
    """

    SESSION_META = "session_meta"
    RESPONSE_ITEM = "response_item"
    EVENT_MSG = "event_msg"
    TURN_CONTEXT = "turn_context"
    TOKEN_USAGE_RECORD = "token_usage_record"  # noqa: S105 — a record type, not a secret
    WORLD_STATE = "world_state"
    COMPACTED = "compacted"
    OTHER = "other"


#: Record types harbor 0.22.0 turns into ATIF steps. ``EVENT_MSG`` and
#: ``TURN_CONTEXT`` are deliberately absent: the converter reads them for
#: ``token_count`` metrics and turn ids, and emits no step for either, so
#: counting them as converted would overstate what the trajectory carries.
CODEX_CONVERTIBLE_RECORD_TYPES: frozenset[CodexRecordType] = frozenset(
    {CodexRecordType.RESPONSE_ITEM}
)

#: ``response_item`` payload types that DO reach a step, so a rollout whose
#: response items are all of some other type is honestly reported as
#: converting none of them.
CODEX_CONVERTIBLE_ITEM_TYPES: frozenset[str] = frozenset(
    {
        "message",
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "web_search_call",
    }
)


class CodexFidelityGap(Enum):
    """The known Codex conversion gaps, inherited from harbor 0.22.0.

    Values are namespaced ``codex_*`` because they share the
    ``gaps_observed`` array in ``loss_report.json`` with the Claude Code
    gaps: a reader of one corpus must be able to tell which policy a gap
    came from without consulting a second column.
    """

    #: (1) RETIRED. ``codex_single_rollout_per_directory`` named harbor's
    #: directory globbing, which converted only ``max(*.jsonl)`` of a session
    #: dir. Our converter reads the one rollout it is given, so there is no
    #: directory and no gap. The value is not reused; a corpus materialized
    #: before the port still carries it.

    #: (2) Only ``response_item`` records become steps. ``world_state``,
    #: ``token_usage_record`` and ``compacted`` records are dropped outright,
    #: and ``event_msg`` / ``turn_context`` contribute only turn ids and token
    #: counts. Verified: a 17-record rollout yields 7 steps.
    NON_ITEM_RECORDS_DROPPED = "codex_non_item_records_dropped"

    #: (3) ``compacted`` records carry the compaction summary plus the
    #: ``replacement_history`` the model actually saw afterwards. Neither
    #: reaches the trajectory, so a compaction boundary is invisible in the
    #: step list and the pre-compaction turns simply stop.
    COMPACTION_UNHANDLED = "codex_compaction_unhandled"

    #: (4) Reasoning is read from ``reasoning.summary`` only. Recent Codex
    #: builds ship an EMPTY summary and put the content in
    #: ``encrypted_content``, so reasoning is lost entirely: verified on a
    #: rollout with 4 reasoning records and 0 steps carrying
    #: ``reasoning_content``.
    REASONING_ENCRYPTED_DROPPED = "codex_reasoning_encrypted_dropped"

    #: (5) ``tool_search_call`` / ``tool_search_output`` items (Codex's
    #: deferred-tool-loading search) match no branch of the payload-type
    #: dispatch, so a tool search and its result vanish without a warning.
    TOOL_SEARCH_CALLS_DROPPED = "codex_tool_search_calls_dropped"

    #: (6) ``response_item.payload.id`` (``msg_*`` / ``call_*`` / ``rs_*``) is
    #: never carried into ``Step.extra``, so step-to-raw-record identity is
    #: unrecoverable from the trajectory alone — the same shape of loss as
    #: Claude Code's gap 7, and repaired the same way by the enrichment pass.
    ITEM_IDS_NOT_PRESERVED = "codex_item_ids_not_preserved"

    #: (7) A ``developer`` role message becomes ATIF ``source="system"``,
    #: which is also where a genuine system message lands. The two are
    #: indistinguishable in the trajectory; ``edges.jsonl`` keeps the raw
    #: role, so the distinction survives in the corpus but not in the steps.
    DEVELOPER_ROLE_FLATTENED_TO_SYSTEM = "codex_developer_role_flattened_to_system"


#: Gaps every harbor 0.22.0 Codex conversion exhibits regardless of rollout
#: content — the structural set, mirroring the Claude Code policy's.
CODEX_STRUCTURAL_GAPS: frozenset[CodexFidelityGap] = frozenset(
    {
        CodexFidelityGap.ITEM_IDS_NOT_PRESERVED,
    }
)
