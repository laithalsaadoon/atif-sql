# SPDX-License-Identifier: Apache-2.0

"""Post-conversion enrichment: restore what harbor 0.22.0 drops.

Pure function over a trajectory dict + raw records — no harbor import, no
I/O. Repairs three fidelity gaps wrap-locally (fidelity.py names them):

- gap 7 (``UUID_NOT_PRESERVED``): ``step.extra["source_uuids"]`` lists the
  raw record uuids that contributed to each step;
- gap 5 (``COMPACT_SUMMARY_UNHANDLED``): ``step.extra["is_compact_summary"]``
  when any contributing record carried ``isCompactSummary``;
- gap 6 (``CACHE_SPLIT_PARTIAL``): ``trajectory.extra["cache_creation_total"]``
  surfaced from ``final_metrics.extra``.

MATCHING STRATEGY: harbor 0.22.0 puts no record identity in ``step.extra`` —
it reads ``requestId`` off the MESSAGE dict and ``id`` off the EVENT dict,
whereas real transcripts carry ``requestId`` on the event and ``uuid`` on the
event, so neither lands in extra and there is nothing to join on. The pass
therefore re-runs harbor's deterministic normalization order:

- agent steps <- assistant records grouped by ``message.id``, groups in
  first-appearance order over the uuid-deduped, timestamp-sorted record walk
  (exactly harbor's ``turn_by_msgid`` bundling); cross-checked via
  ``tool_calls[].call_id`` vs the group's tool_use block ids when present;
- tool_result user records <- joined into the agent step whose
  ``tool_calls`` / ``observation.results[].source_call_id`` contain their
  ``tool_use_id``;
- plain user text steps <- 1:1 walk of raw user records that produce a
  visible text message (harbor's rules replicated), in timestamp order.

Known edge cases:
- a user record mixing text blocks AND tool_result blocks contributes to
  BOTH a user step (text) and an agent step (result attachment) — its uuid
  appears in both steps' ``source_uuids``;
- orphan tool_results (no pending call, e.g. replayed after compaction)
  become standalone harbor AGENT steps interleaved in event order. They have
  no assistant record, so they must NOT consume a ``message.id`` group in
  the positional zip — the pass detects them (every one of the step's tool
  call ids is absent from every assistant group's tool_use ids), keeps them
  out of the zip, and attributes each one from the tool_result-carrying
  user record via ``tool_use_id`` when possible (else it stays
  unattributed);
- assistant records without ``message.id`` each form their own group,
  mirroring harbor's one-turn-per-event fallback.

DESYNC ACCOUNTING (refuse-and-log, never guess): the agent walk advances a
(step, group) pair only when the tool-id cross-check passes — the ids
intersect, or BOTH sides are text-only (no tool ids on either side). The
first mismatching pair STOPS agent attribution outright: once positions
disagree, every later pairing is suspect, so continuing would misattribute.
The user walk likewise advances only while the record's harbor-derived text
equals the step's message. Either walk halting (mismatch or leftover steps)
records the first refused step in
``trajectory.extra["enrichment_truncated_at_step"]`` and logs a warning.
After both walks, any step left with no ``source_uuids`` and any group /
user text record never attached to a step are counted and surfaced as
``trajectory.extra["enrichment_unattributed_steps"]`` /
``trajectory.extra["enrichment_leftover_groups"]`` (only present when
nonzero). ``extra`` is free-form per the contract, so this is
contract-compatible; a truncation marker or nonzero count means the
positional re-derivation disagreed with harbor and the affected
attributions were REFUSED, not guessed.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

_SOURCE_UUIDS = "source_uuids"
_TRUNCATED_AT = "enrichment_truncated_at_step"


@dataclass(slots=True)
class _AssistantGroup:
    """One harbor turn: assistant records sharing a ``message.id``."""

    uuids: list[str] = field(default_factory=list)
    compact: bool = False
    tool_use_ids: set[str] = field(default_factory=set)


def _dedup_and_sort(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replicate harbor's pre-pass: dedup by uuid, then stable-sort by ts."""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for record in records:
        uuid = record.get("uuid")
        if isinstance(uuid, str) and uuid:
            if uuid in seen:
                continue
            seen.add(uuid)
        deduped.append(record)
    deduped.sort(key=lambda r: r.get("timestamp") or "")
    return deduped


def _stringify(value: Any) -> str:
    """Harbor's ``_stringify``, replicated: str verbatim, else JSON, else str."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _visible_user_text(message: dict[str, Any]) -> str | None:
    """The user-step message text harbor would emit for this record, or None.

    Replicates harbor 0.22.0's user-content rules exactly (including the
    finding-5 divergence: a ``{"type": "text", "text": <non-str>}`` block is
    NOT skipped — harbor json-encodes the whole block into a text part, so
    the record still produces a visible step). Returning the text (not just
    a bool) lets the user-step walk cross-check each pairing instead of
    trusting position alone.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                continue
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                parts.append(block["text"])
                continue
            # any other block (incl. text blocks with non-str text) is
            # stringified by harbor into a text part
            parts.append(_stringify(block))
        text = "\n\n".join(part for part in parts if part.strip())
        return text or None
    if content not in (None, ""):
        text = _stringify(content)
        return text if text.strip() else None
    return None


def _tool_result_ids(message: dict[str, Any]) -> list[str]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    ids: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            tool_use_id = block.get("tool_use_id")
            if isinstance(tool_use_id, str) and tool_use_id:
                ids.append(tool_use_id)
    return ids


def _step_call_ids(step: dict[str, Any]) -> set[str]:
    """All tool call ids a step owns: tool_calls plus observation results."""
    ids: set[str] = set()
    for call in step.get("tool_calls") or []:
        call_id = call.get("tool_call_id") or call.get("call_id")
        if isinstance(call_id, str) and call_id:
            ids.add(call_id)
    observation = step.get("observation") or {}
    for result in observation.get("results") or []:
        source_call_id = result.get("source_call_id")
        if isinstance(source_call_id, str) and source_call_id:
            ids.add(source_call_id)
    return ids


def _step_id(step: dict[str, Any]) -> int | None:
    """The step's ``step_id`` when it is an int (ATIF requires it; be safe)."""
    value = step.get("step_id")
    return value if isinstance(value, int) else None


def _attach(step: dict[str, Any], uuids: list[str], *, compact: bool) -> None:
    extra = step.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        step["extra"] = extra
    existing = extra.get(_SOURCE_UUIDS)
    merged: list[str] = list(existing) if isinstance(existing, list) else []
    for uuid in uuids:
        if uuid not in merged:
            merged.append(uuid)
    extra[_SOURCE_UUIDS] = merged
    if compact:
        extra["is_compact_summary"] = True


def enrich_trajectory(
    trajectory: dict[str, Any],
    raw_records: Sequence[dict[str, Any]],
    *,
    copy_input: bool = True,
) -> dict[str, Any]:
    """Return the enriched trajectory.

    ``raw_records`` are the parsed records of EVERY discovered session file
    (main + all side-files); order does not matter — the pass re-applies
    harbor's dedup + timestamp sort itself.

    ``copy_input=True`` (default) leaves ``trajectory`` untouched. Pass
    ``False`` when the caller owns the only reference and wants the mutation
    in place: the deep copy duplicates the whole step list, which buys
    nothing when the original is discarded straight afterwards.
    """
    enriched = copy.deepcopy(trajectory) if copy_input else trajectory
    records = _dedup_and_sort(raw_records)

    # -- assistant records: group by message.id in first-appearance order.
    group_order: list[str] = []
    groups: dict[str, _AssistantGroup] = {}
    # tool_use_id -> contributing user (tool_result) records
    results_by_call_id: dict[str, list[dict[str, Any]]] = {}
    user_text_records: list[dict[str, Any]] = []

    for record in records:
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        record_type = record.get("type")
        uuid = record.get("uuid")
        uuid_str = uuid if isinstance(uuid, str) and uuid else None
        compact = bool(record.get("isCompactSummary", False))

        if record_type == "assistant":
            msg_id = message.get("id")
            key = msg_id if isinstance(msg_id, str) and msg_id else f"__event__{id(record)}"
            group = groups.get(key)
            if group is None:
                group = _AssistantGroup()
                groups[key] = group
                group_order.append(key)
            if uuid_str:
                group.uuids.append(uuid_str)
            group.compact = group.compact or compact
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        block_id = block.get("id")
                        if isinstance(block_id, str) and block_id:
                            group.tool_use_ids.add(block_id)
        elif record_type == "user":
            for call_id in _tool_result_ids(message):
                results_by_call_id.setdefault(call_id, []).append(record)
            if _visible_user_text(message) is not None:
                user_text_records.append(record)

    steps = enriched.get("steps") or []
    agent_steps = [s for s in steps if s.get("source") == "agent"]
    user_steps = [s for s in steps if s.get("source") == "user"]

    # -- partition agent steps: ORPHAN tool_result steps (harbor emits a
    # standalone agent step for a tool_result with no pending call, e.g.
    # replayed after compaction) have NO assistant record behind them, so
    # they must not consume a message.id group in the positional zip below.
    # Detection: the step owns tool call ids, and every one of them is
    # absent from every assistant group's tool_use ids.
    all_group_tool_use_ids: set[str] = set()
    for group in groups.values():
        all_group_tool_use_ids |= group.tool_use_ids
    orphan_steps: list[dict[str, Any]] = []
    genuine_agent_steps: list[dict[str, Any]] = []
    for step in agent_steps:
        step_ids = _step_call_ids(step)
        if step_ids and step_ids.isdisjoint(all_group_tool_use_ids):
            orphan_steps.append(step)
        else:
            genuine_agent_steps.append(step)

    # -- orphan steps <- the tool_result-carrying user record, joined on
    # tool_use_id when possible; otherwise the step stays unattributed
    # (and is counted below).
    for step in orphan_steps:
        for call_id in sorted(_step_call_ids(step)):
            for record in results_by_call_id.get(call_id, ()):
                uuid = record.get("uuid")
                if isinstance(uuid, str) and uuid:
                    _attach(
                        step,
                        [uuid],
                        compact=bool(record.get("isCompactSummary", False)),
                    )

    # -- genuine agent steps <- assistant groups, walked in lockstep
    # (harbor's turn order). The cross-check is REQUIRED to advance: a pair
    # attributes only when the two sides' tool ids intersect, or when BOTH
    # sides are text-only. The FIRST mismatching pair stops the walk — once
    # positions disagree, every later pairing is suspect, so refusing from
    # that point beats silently misattributing (module docstring). A side
    # running out while the other has leftovers is the same desync; the
    # accounting below surfaces it.
    truncated_at: int | None = None
    consumed_groups = 0
    for step, key in zip(genuine_agent_steps, group_order, strict=False):
        group = groups[key]
        step_ids = _step_call_ids(step)
        aligned = bool(step_ids & group.tool_use_ids) if (step_ids or group.tool_use_ids) else True
        if not aligned:
            truncated_at = _step_id(step)
            logger.warning(
                "enrichment: agent-step alignment mismatch at step {} "
                "(step tool ids {} vs group tool_use ids {}); "
                "attribution stopped from this step onward",
                truncated_at,
                sorted(step_ids),
                sorted(group.tool_use_ids),
            )
            break
        consumed_groups += 1
        _attach(step, group.uuids, compact=group.compact)
        # tool_result user records join the agent step that owns their call
        for call_id in sorted(step_ids):
            for record in results_by_call_id.get(call_id, ()):
                uuid = record.get("uuid")
                if isinstance(uuid, str) and uuid:
                    _attach(
                        step,
                        [uuid],
                        compact=bool(record.get("isCompactSummary", False)),
                    )
    if truncated_at is None and consumed_groups < len(genuine_agent_steps):
        # length mismatch: agent steps left over with no group to claim them.
        truncated_at = _step_id(genuine_agent_steps[consumed_groups])
        logger.warning(
            "enrichment: {} agent step(s) beyond the {} assistant group(s); "
            "attribution stopped at step {}",
            len(genuine_agent_steps) - consumed_groups,
            len(group_order),
            truncated_at,
        )

    # -- plain user text steps <- ts-order walk of user text records, with
    # the SAME refuse-and-log protection: a pair advances only while the
    # record's harbor-derived text equals the step's message, and leftover
    # steps stop the walk instead of being silently dropped.
    consumed_user_records = 0
    for step, record in zip(user_steps, user_text_records, strict=False):
        message = record.get("message")
        expected = _visible_user_text(message) if isinstance(message, dict) else None
        if step.get("message") != expected:
            step_id = _step_id(step)
            logger.warning(
                "enrichment: user-step alignment mismatch at step {} "
                "(step message differs from the walked record's text); "
                "user attribution stopped from this step onward",
                step_id,
            )
            if truncated_at is None or (step_id is not None and step_id < truncated_at):
                truncated_at = step_id
            break
        consumed_user_records += 1
        uuid = record.get("uuid")
        uuids = [uuid] if isinstance(uuid, str) and uuid else []
        _attach(step, uuids, compact=bool(record.get("isCompactSummary", False)))
    else:
        if consumed_user_records < len(user_steps):
            # length mismatch: user steps left over with no record.
            step_id = _step_id(user_steps[consumed_user_records])
            logger.warning(
                "enrichment: {} user step(s) beyond the {} user text record(s); "
                "attribution stopped at step {}",
                len(user_steps) - consumed_user_records,
                len(user_text_records),
                step_id,
            )
            if truncated_at is None or (step_id is not None and step_id < truncated_at):
                truncated_at = step_id

    # -- desync accounting (see module docstring): count anything the walks
    # left behind on EITHER side and surface it on trajectory.extra. A
    # truncation marker / nonzero count means attribution was refused
    # somewhere, never guessed.
    unattributed_steps = sum(
        1 for step in steps if not (step.get("extra") or {}).get(_SOURCE_UUIDS)
    )
    leftover_groups = (len(group_order) - consumed_groups) + (
        len(user_text_records) - consumed_user_records
    )
    if unattributed_steps or leftover_groups or truncated_at is not None:
        trajectory_extra = enriched.get("extra")
        if not isinstance(trajectory_extra, dict):
            trajectory_extra = {}
            enriched["extra"] = trajectory_extra
        if unattributed_steps:
            trajectory_extra["enrichment_unattributed_steps"] = unattributed_steps
        if leftover_groups:
            trajectory_extra["enrichment_leftover_groups"] = leftover_groups
        if truncated_at is not None:
            trajectory_extra[_TRUNCATED_AT] = truncated_at

    # -- gap 6: surface cache_creation_total on trajectory.extra.
    final_metrics = enriched.get("final_metrics") or {}
    metrics_extra = final_metrics.get("extra") or {}
    cache_creation = metrics_extra.get("total_cache_creation_input_tokens")
    if isinstance(cache_creation, int):
        trajectory_extra = enriched.get("extra")
        if not isinstance(trajectory_extra, dict):
            trajectory_extra = {}
            enriched["extra"] = trajectory_extra
        trajectory_extra["cache_creation_total"] = cache_creation

    return enriched
