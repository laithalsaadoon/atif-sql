# SPDX-License-Identifier: Apache-2.0

"""Post-conversion enrichment for Codex: restore what harbor 0.22.0 drops.

Pure function over a trajectory dict plus the rollout's raw records — no
harbor import, no I/O. The Claude Code counterpart is
:mod:`atif_converter.domain.enrichment`; this module repairs the Codex gaps
that are repairable wrap-locally and, exactly like that one, REFUSES rather
than guesses when its re-derivation disagrees with harbor.

What it repairs, by the gap it answers
--------------------------------------
``ITEM_IDS_NOT_PRESERVED``
    ``step.extra["source_uuids"]`` lists the rollout record ids that
    contributed to the step, using the SAME key the Claude Code pass writes so
    atif-duck's ``steps.source_uuids`` column means one thing for every agent.

Cache split
    ``trajectory.extra["cache_creation_total"]`` is surfaced from
    ``final_metrics.extra.total_cache_write_input_tokens``, again matching the
    Claude Code key so one query answers cache-creation for both agents.

``COMPACTION_UNHANDLED``
    ``trajectory.extra["codex_compaction_count"]`` records how many
    ``compacted`` records the rollout carried. The summaries themselves are
    not reconstructable into steps — harbor emitted none — so the count is
    the honest repair: it tells a reader the step list has a seam without
    inventing a step to mark it.

ATTRIBUTION STRATEGY: identity first, and position only where the format
leaves no id
-----------------------------------------------------------------------------
Three passes, ordered by how much they can be trusted.

1. **Tool records, by call id.** harbor keeps each Codex ``call_id`` verbatim
   in ``tool_calls[].tool_call_id``, so a step's tool records are an exact
   lookup: no ordering assumption, no off-by-one to make, and a step whose
   call ids match nothing simply gets no tool attribution.

2. **Agent message records, by API call id.** harbor bundles the assistant
   messages of one model request into one step and stamps that request's id
   into ``step.extra.api_call_id``. This pass re-derives the same ids from the
   raw records — the counter advances on a ``token_count`` event that closed a
   request which produced output, which is harbor's own rule — and attributes
   each assistant ``message`` record to the step bearing its request id. That
   is what makes an assistant record with EMPTY text attributable at all: it
   contributes no text to its step (harbor filters empty parts), so no
   text-matching walk could ever place it, while its request id places it
   exactly.

3. **User and system message steps, by position.** These carry no id anywhere:
   harbor emits one step per non-assistant message record, in file order, with
   no bundling and nothing filtered. The walk therefore pairs them 1:1 and
   cross-checks each pairing against harbor's own text extraction. The FIRST
   pair whose text or source disagrees stops this pass and records
   ``trajectory.extra["enrichment_truncated_at_step"]``: once positions
   disagree, every later pairing is suspect.

Reasoning records are attributed only to a step that actually carries
``reasoning_content``. A reasoning record belongs to the API call that
consumed it, but recent Codex builds ship the content encrypted and harbor
reads only the plaintext summary
(:attr:`~atif_converter.domain.codex_fidelity.CodexFidelityGap.REASONING_ENCRYPTED_DROPPED`),
so claiming a dropped record "contributed to" a step would overstate what the
step holds.

After all three passes, any step left with no ``source_uuids`` and any
non-assistant message record never consumed are counted into
``trajectory.extra["enrichment_unattributed_steps"]`` and
``trajectory.extra["enrichment_leftover_messages"]`` (present only when
nonzero). ``extra`` is free-form per the contract, so all of this is
contract-compatible; a truncation marker or a nonzero count means the
re-derivation disagreed with harbor and the affected attributions were
REFUSED, not guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from atif_converter.domain.codex_edges import build_codex_edges

_SOURCE_UUIDS = "source_uuids"
_TRUNCATED_AT = "enrichment_truncated_at_step"
#: Message records that reached no step: the silent end of the positional walk.
_LEFTOVER_MESSAGES = "enrichment_leftover_messages"

#: ATIF ``source`` values keyed by the raw Codex message role. ``developer``
#: and anything unknown land on ``system`` — the flattening named by
#: ``DEVELOPER_ROLE_FLATTENED_TO_SYSTEM``.
_ROLE_TO_SOURCE: dict[str, str] = {"user": "user", "assistant": "agent"}

#: Payload types that CONSUME a pending reasoning record, mirroring harbor's
#: normalization: each one clears ``pending_reasoning`` after reading it.
_REASONING_CONSUMERS: frozenset[str] = frozenset(
    {
        "message",
        "web_search_call",
        "function_call",
        "custom_tool_call",
        "function_call_output",
        "custom_tool_call_output",
    }
)


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def message_text(payload: dict[str, Any]) -> str:
    """Replicate harbor's ``Codex._extract_message_text``.

    Every content block's ``text`` field, concatenated with NO separator —
    which is what harbor does, and the reason a two-block developer message
    reproduces as one run-together string rather than two lines.
    """
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _source_for_role(role: Any) -> str:
    return _ROLE_TO_SOURCE.get(role, "system") if isinstance(role, str) else "system"


@dataclass(slots=True)
class _RolloutIndex:
    """Everything the three passes need, derived from the raw records once."""

    #: call_id -> record ids naming it (the call and its output).
    by_call_id: dict[str, list[str]] = field(default_factory=dict)
    #: api_call_id -> assistant message / web-search record ids in that request.
    by_api_call: dict[str, list[str]] = field(default_factory=dict)
    #: Non-assistant message records in file order: (record id, source, text).
    non_agent_messages: list[tuple[str, str, str]] = field(default_factory=list)
    #: record id or call_id -> the reasoning record it consumed.
    reasoning_for: dict[str, str] = field(default_factory=dict)
    #: ``compacted`` records seen.
    compaction_count: int = 0


def _index_rollout(records: list[tuple[dict[str, Any], str]]) -> _RolloutIndex:
    """Re-derive harbor's grouping over the raw records, ids attached.

    The ``api_call_id`` counter replicates harbor's exactly: it starts at
    ``api_call_1``, and a ``token_count`` event advances it only when the
    request it closes produced model output. Anything that diverges here
    diverges from the step ids in ``step.extra.api_call_id``, which is what
    pass 2 joins on — so a mismatch shows up as an unattributed step rather
    than as a wrong attribution.
    """
    index = _RolloutIndex()
    edges = build_codex_edges(records)
    api_call_number = 1
    current_api_call = "api_call_1"
    saw_model_output = False
    pending_reasoning: str | None = None
    #: call_id -> the api_call id current when the CALL was seen, because
    #: harbor's normalized event for a tool call is emitted at its OUTPUT and
    #: carries the call's request id, not the one live at output time.
    pending_call_api: dict[str, str] = {}

    for (record, _source_file), edge in zip(records, edges, strict=True):
        record_id = str(edge["uuid"])
        record_type = record.get("type")
        payload = _payload(record)

        if record_type == "compacted":
            index.compaction_count += 1
            continue
        if record_type == "event_msg":
            if payload.get("type") == "token_count" and saw_model_output:
                api_call_number += 1
                current_api_call = f"api_call_{api_call_number}"
                saw_model_output = False
            continue
        if record_type != "response_item":
            continue

        item_type = payload.get("type")
        if item_type == "reasoning":
            pending_reasoning = record_id
            continue

        if item_type in _REASONING_CONSUMERS and pending_reasoning is not None:
            key = payload.get("call_id") if "call" in str(item_type) else record_id
            index.reasoning_for[str(key) if isinstance(key, str) and key else record_id] = (
                pending_reasoning
            )
            pending_reasoning = None

        if item_type == "message":
            role = payload.get("role")
            source = _source_for_role(role)
            if role == "assistant":
                index.by_api_call.setdefault(current_api_call, []).append(record_id)
                saw_model_output = True
            else:
                index.non_agent_messages.append((record_id, source, message_text(payload)))
            continue

        if item_type == "web_search_call":
            index.by_api_call.setdefault(current_api_call, []).append(record_id)
            saw_model_output = True
            continue

        call_id = payload.get("call_id")
        if isinstance(call_id, str) and call_id:
            index.by_call_id.setdefault(call_id, []).append(record_id)
            if item_type in {"function_call", "custom_tool_call"}:
                pending_call_api[call_id] = current_api_call
                saw_model_output = True

    return index


def _step_call_ids(step: dict[str, Any]) -> set[str]:
    """Every ``tool_call_id`` and ``source_call_id`` this step names."""
    ids: set[str] = set()
    for call in step.get("tool_calls") or []:
        if isinstance(call, dict):
            call_id = call.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                ids.add(call_id)
    observation = step.get("observation")
    if isinstance(observation, dict):
        for result in observation.get("results") or []:
            if isinstance(result, dict):
                call_id = result.get("source_call_id")
                if isinstance(call_id, str) and call_id:
                    ids.add(call_id)
    return ids


def _step_extra(step: dict[str, Any]) -> dict[str, Any]:
    extra = step.get("extra")
    return extra if isinstance(extra, dict) else {}


def _step_id(step: dict[str, Any]) -> int | None:
    step_id = step.get("step_id")
    return step_id if isinstance(step_id, int) else None


def _attach(step: dict[str, Any], ids: list[str]) -> None:
    """Merge ``ids`` into the step's ``extra.source_uuids``, order-preserving."""
    if not ids:
        return
    extra = step.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        step["extra"] = extra
    existing = extra.get(_SOURCE_UUIDS)
    merged: list[str] = [str(value) for value in existing] if isinstance(existing, list) else []
    for value in ids:
        if value not in merged:
            merged.append(value)
    extra[_SOURCE_UUIDS] = merged


def enrich_codex_trajectory(
    trajectory: dict[str, Any],
    records: list[tuple[dict[str, Any], str]],
    *,
    copy_input: bool = True,
) -> dict[str, Any]:
    """Return ``trajectory`` with Codex source attribution and cache totals.

    Parameters
    ----------
    trajectory
        The harbor Codex trajectory as a dict (``model_dump(mode="json")``).
    records
        The rollout's ``(record, source_file)`` pairs in FILE order — the same
        pairs :func:`~atif_converter.domain.codex_edges.build_codex_edges`
        reads, so the ids attributed here are the ids in ``edges.jsonl``.
    copy_input
        Deep-copy before mutating. The production caller passes ``False``
        because the harbor trajectory has no other consumer and a rollout's
        step list is large.
    """
    if copy_input:
        import copy

        trajectory = copy.deepcopy(trajectory)

    steps = trajectory.get("steps")
    if not isinstance(steps, list):
        return trajectory

    index = _index_rollout(records)

    # Passes 1 and 2 — identity. Neither can misattribute: both are lookups.
    for step in steps:
        if not isinstance(step, dict):
            continue
        attributed: list[str] = []
        call_ids = sorted(_step_call_ids(step))
        for call_id in call_ids:
            attributed.extend(index.by_call_id.get(call_id, ()))
        api_call_id = _step_extra(step).get("api_call_id")
        if isinstance(api_call_id, str):
            attributed.extend(index.by_api_call.get(api_call_id, ()))
        if step.get("reasoning_content"):
            for key in (*call_ids, *attributed):
                reasoning_id = index.reasoning_for.get(key)
                if reasoning_id is not None and reasoning_id not in attributed:
                    attributed.append(reasoning_id)
        _attach(step, attributed)

    # Pass 3 — position, over the non-assistant message steps only.
    truncated_at, leftover_messages = _attribute_non_agent_messages(steps, index.non_agent_messages)

    extra = trajectory.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    _record_cache_total(trajectory, extra)
    if index.compaction_count:
        extra["codex_compaction_count"] = index.compaction_count
    if truncated_at is not None:
        extra[_TRUNCATED_AT] = truncated_at
        logger.warning(
            "codex enrichment: message attribution refused at step {} — the "
            "positional walk disagreed with harbor's step list; later steps "
            "carry no source_uuids rather than a guessed one",
            truncated_at,
        )
    unattributed = sum(
        1 for step in steps if isinstance(step, dict) and not _step_extra(step).get(_SOURCE_UUIDS)
    )
    if unattributed:
        extra["enrichment_unattributed_steps"] = unattributed
    if leftover_messages:
        extra[_LEFTOVER_MESSAGES] = leftover_messages
        logger.warning(
            "codex enrichment: {} message record(s) attributed to no step — the "
            "loss report counts them convertible, so this is real unreported loss",
            leftover_messages,
        )
    if extra:
        trajectory["extra"] = extra
    return trajectory


def _attribute_non_agent_messages(
    steps: list[Any],
    messages: list[tuple[str, str, str]],
) -> tuple[int | None, int]:
    """Pair user/system steps with user/developer message records, 1:1.

    Returns the step id the walk refused at (``None`` when every such step
    paired cleanly) and the count of message records left unconsumed. A step is
    refused when the next unconsumed record's ATIF source or harbor-extracted
    text disagrees with it.

    THE TWO ENDS OF THE WALK FAIL DIFFERENTLY, and both have to be reported.
    Running out of RECORDS refuses the step, which is loud. Running out of
    STEPS is silent: the remaining records attribute to nothing, and the loss
    report still counts them convertible, so it would understate the loss with
    no marker anywhere. The leftover count is that marker.
    """
    cursor = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        source = step.get("source")
        if source not in {"user", "system"}:
            continue
        if cursor >= len(messages):
            return _step_id(step), 0
        record_id, record_source, record_text = messages[cursor]
        if record_source != source or record_text != (step.get("message") or ""):
            return _step_id(step), len(messages) - cursor
        _attach(step, [record_id])
        cursor += 1
    return None, len(messages) - cursor


def _record_cache_total(trajectory: dict[str, Any], extra: dict[str, Any]) -> None:
    """Surface the cache-creation total under the Claude Code key.

    harbor puts the Codex figure in
    ``final_metrics.extra.total_cache_write_input_tokens`` and the Claude Code
    figure in ``final_metrics.extra.cache_creation_input_tokens``. One name
    reaches SQL, so the Codex value is republished under
    ``trajectory.extra["cache_creation_total"]`` — the key the enrichment pass
    for Claude Code already writes and the corpus already carries.
    """
    final_metrics = trajectory.get("final_metrics")
    if not isinstance(final_metrics, dict):
        return
    metrics_extra = final_metrics.get("extra")
    if not isinstance(metrics_extra, dict):
        return
    total = metrics_extra.get("total_cache_write_input_tokens")
    if isinstance(total, int):
        extra["cache_creation_total"] = total
