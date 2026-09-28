# SPDX-License-Identifier: Apache-2.0

"""Pure transcript rendering over materialized ATIF steps.

This is the domain half of the corpus-reader seam. A session source
(:mod:`atif_analytics.domain.ports`: the per-session files, or the lake)
turns a session's steps into :class:`StepEvent` rows through
:func:`step_event`; this module turns them into the byte-shape the prompts
are tuned on
(CONTRACT-V2 §Ports & state: transcript text for prompts is steps-based
rendering under the caps below):

* ``[role ts] text`` lines (``[uuid=<edges-uuid> role ts] text`` for the
  conflicts variant — the uuid is the step's FIRST ``source_uuids`` entry,
  the documented primary raw-record key);
* ``[tool_use:<name> ts] <args preview>`` — 400-char preview;
* ``[tool_result <call_id> ts] <content preview>`` — 50K-char cap with the
  chars-dropped footer;
* whole-session cap 800K chars with the truncation notice.

Rendering rules the prompts depend on (documented per CONTRACT-V2):

* role vocabulary — ATIF ``Step.source`` is ``user``/``agent``; ``agent``
  renders as ``assistant`` so the prompts' "user turns, assistant turns"
  framing keeps matching the transcript. A user-role step a HUMAN did not
  write (:mod:`atif_analytics.domain.authorship`) renders under its author
  label instead — ``stop_hook``, ``task_notification``, ``harness``,
  ``audit_prompt`` — with its body clipped to
  :data:`NON_HUMAN_PREVIEW_CHARS`, so the model never reads hook feedback or
  a skill body as the user speaking, and a 20K-char skill body costs 300.
* sidechain steps are EXCLUDED: the prompts are calibrated on transcripts
  containing no subagent content, and harbor inlines sidechains into the
  flat step list, so they must be filtered back out here.
* timestamps are spelled by :func:`format_step_ts`: ISO-8601 UTC with
  milliseconds and a ``Z`` (``2026-08-20T10:00:00.000Z``), which is how both
  agents write them, so on a real transcript it is the ATIF string verbatim.
  Both session sources spell it through the one function, because the lake
  keeps a typed timestamp rather than the source text. Not a Python
  ``isoformat()`` round-trip (that writes ``+00:00``).
* empty-text steps contribute no text line — an ATIF step carrying only
  tool calls has ``message == ""`` and has nothing to render.

Header forgery: a rendered line's ``[uuid=... role ts]`` prefix is the ONLY
thing telling the model which turn a body belongs to, and step bodies are
untrusted — a transcript can contain a pasted transcript. Bodies therefore
have their own ``[uuid=`` sequences neutralized (:func:`escape_uuid_headers`)
so a body can never present itself as a turn header the model would attribute
a real uuid to.

Text rendered here is sent to a third-party model provider: session bodies,
tool arguments, and tool results leave this machine on that path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from atif_analytics.domain.authorship import SessionKind, kind_of, step_author

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Default per-``tool_use`` args preview length.
TOOL_INPUT_PREVIEW_CHARS: int = 400

#: The literal a forged turn header would have to open with. Matched
#: case-insensitively because a model reading the transcript does not
#: tokenize case-sensitively either.
_UUID_HEADER_RE = re.compile(r"\[uuid=", re.IGNORECASE)

#: What a body's ``[uuid=`` becomes. The opening bracket is what makes a
#: header, so swapping it for a paren removes the forgery while leaving the
#: content readable and the length unchanged (the caps arithmetic still
#: measures the same string).
UUID_HEADER_ESCAPE: str = "(uuid="


def escape_uuid_headers(text: str) -> str:
    """Neutralize ``[uuid=`` sequences in an untrusted body.

    The renderer's own headers are built AFTER this runs, so only body
    content is rewritten. Without it a body containing
    ``[uuid=<other-id> user <ts>] ...`` is byte-identical to a real header,
    and the model can be steered to return a uuid it was never shown —
    which then reaches the parquet as a legitimate-looking turn key.
    """
    return _UUID_HEADER_RE.sub(UUID_HEADER_ESCAPE, text)


#: Body budget for a user-role step a human did not write. Enough to show
#: what the machine said (the first lines of a Stop hook's verdict, the task
#: a notification reports on) without paying for a whole skill body.
NON_HUMAN_PREVIEW_CHARS: int = 300


def tool_input_preview(
    tool_input_json: str | None, max_chars: int = TOOL_INPUT_PREVIEW_CHARS
) -> str:
    """Truncate a ``tool_input`` JSON blob to the first ``max_chars``."""
    if not tool_input_json:
        return ""
    s = str(tool_input_json)
    return s if len(s) <= max_chars else s[:max_chars] + "…(truncated)"


def tool_result_preview(content: str | None, max_chars: int) -> str:
    """Truncate a ``tool_result`` content blob with a "chars dropped" footer."""
    if not content:
        return ""
    s = str(content)
    if len(s) <= max_chars:
        return s
    dropped = len(s) - max_chars
    return s[:max_chars] + "\n…(truncated, " + str(dropped) + " chars dropped)"


@dataclass(slots=True)
class StepEvent:
    """One materialized ATIF step, projected to what the renderer needs.

    ``uuid`` is the step's FIRST ``extra.source_uuids`` entry (the primary
    raw-record uuid per CONTRACT-V2 — the same choice the VSS branch keys
    embeddings on), or ``None`` when the enrichment pass recorded none.
    ``tool_calls`` is ``[(function_name, args_json)]``; ``tool_results`` is
    ``[(source_call_id, content_str)]``. ``author`` is the
    :func:`~atif_analytics.domain.authorship.step_author` label for a user
    step (``harness`` for a compaction summary) and ``None`` for any other
    role; left unset, it is derived from ``role`` and ``text``.
    """

    ts: str
    role: str  # "user" | "assistant" (ATIF source mapped)
    text: str
    uuid: str | None = None
    is_sidechain: bool = False
    is_compact_summary: bool = False
    has_error_result: bool = False
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    tool_results: list[tuple[str, str]] = field(default_factory=list)
    author: str | None = None

    def __post_init__(self) -> None:
        """Derive ``author`` for a user step the caller did not label."""
        if self.author is None and self.role == "user":
            self.author = "harness" if self.is_compact_summary else step_author("user", self.text)


def format_step_ts(ts: datetime | None) -> str:
    """Spell a step timestamp the way the transcript renders it (``...T10:00:00.000Z``).

    UTC, milliseconds unless the value carries sub-millisecond precision (then
    microseconds, so nothing is dropped), and a ``Z``. An aware value is
    converted to UTC first; a naive one is taken as UTC, which is how the lake
    stores it. ``None`` spells as the empty string. The two sources agree only
    on ``Z`` stamps, which is what both converters write: the lake's timestamp
    cast drops any other offset, while the file path converts it.
    """
    if ts is None:
        return ""
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC).replace(tzinfo=None)
    micros = ts.microsecond
    fraction = f"{micros // 1000:03d}" if micros % 1000 == 0 else f"{micros:06d}"
    return f"{ts:%Y-%m-%dT%H:%M:%S}.{fraction}Z"


def step_event(
    *,
    ts: datetime | None,
    source: str | None,
    text: str | None,
    source_uuids: object,
    is_sidechain: bool,
    is_compact_summary: bool,
    tool_calls: Iterable[tuple[str | None, object]] = (),
    tool_results: Iterable[tuple[str | None, object, bool]] = (),
    has_error_result: bool = False,
) -> StepEvent:
    """Project one ATIF step's values into a :class:`StepEvent`: the rules both sources share.

    ``tool_calls`` holds ``(function_name, arguments)`` and ``tool_results``
    ``(source_call_id, content, is_error)``, with ``arguments`` and
    ``content`` as parsed JSON values. The rules:

    * ATIF ``source`` ``agent`` renders as ``assistant``; an absent source as
      ``unknown``.
    * the uuid is the FIRST ``source_uuids`` entry when it is a string.
    * arguments render as JSON (non-ASCII kept); absent arguments as ``""``.
    * a string result renders as itself, anything else as JSON, and an absent
      one as ``null`` (what ``json.dumps(None)`` writes).
    * the step carries an error when any of its results says so, or when the
      caller says so without handing the results over (``has_error_result``).
    * the author label is derived in :class:`StepEvent` from the role and text.
    """
    uuids = source_uuids if isinstance(source_uuids, list) else []
    uuid = uuids[0] if uuids and isinstance(uuids[0], str) else None
    raw_source = source or ""
    calls = [
        (
            str(name or ""),
            json.dumps(args, ensure_ascii=False, default=str) if args is not None else "",
        )
        for name, args in tool_calls
    ]
    results: list[tuple[str, str]] = []
    has_error = has_error_result
    for call_id, content, is_error in tool_results:
        content_str = content if isinstance(content, str) else json.dumps(content, default=str)
        results.append((str(call_id or ""), content_str or ""))
        has_error = has_error or is_error
    return StepEvent(
        ts=format_step_ts(ts),
        role="assistant" if raw_source == "agent" else (raw_source or "unknown"),
        text=text or "",
        uuid=uuid,
        is_sidechain=is_sidechain,
        is_compact_summary=is_compact_summary,
        has_error_result=has_error,
        tool_calls=calls,
        tool_results=results,
    )


def is_human_turn(step: StepEvent) -> bool:
    """A main-chain user step a human wrote: what every reader counts as the user."""
    return step.role == "user" and not step.is_sidechain and step.author == "human"


def session_kind(steps: list[StepEvent]) -> SessionKind:
    """``interactive`` | ``one_shot_job`` | ``turn_audit`` for one session.

    Same rule as atif-duck's ``session_outcomes.kind``: the first main-chain
    user step's author, then the count of human turns.
    """
    return kind_of(s.author or "human" for s in steps if s.role == "user" and not s.is_sidechain)


def main_chain(steps: list[StepEvent]) -> list[StepEvent]:
    """The non-sidechain steps, in materialized order."""
    return [s for s in steps if not s.is_sidechain]


def render_session_text(
    steps: list[StepEvent],
    *,
    total_max_chars: int = 800_000,
    tool_result_max_chars: int = 50_000,
    include_uuids: bool = False,
) -> str:
    """Render one session's steps as a single newline-separated transcript.

    Per step (main chain only, order preserved) the text line renders
    first, then that step's ``tool_use`` lines, then its ``tool_result``
    lines. That text → tool_use → tool_result precedence is the order the
    prompts are calibrated on.

    When ``include_uuids`` is True, each text line's header carries the
    step's uuid as ``[uuid=<id> role ts]`` so a classifier can copy the
    turn's natural key verbatim (the conflicts pipeline needs this). Steps
    without a uuid keep the plain header even under ``include_uuids`` —
    an absent key must not become an empty ``uuid=`` attribute the model
    could echo.

    Every body (text, tool arguments, tool results) is run through
    :func:`escape_uuid_headers` first, so only THIS function writes a real
    ``[uuid=`` header and a body cannot forge one.

    A user step a human did not write renders under its ``author`` label
    with its body clipped to :data:`NON_HUMAN_PREVIEW_CHARS`.
    """
    chain = main_chain(steps)
    events_total = sum(
        (1 if s.text else 0) + len(s.tool_calls) + len(s.tool_results) for s in chain
    )
    lines: list[str] = []
    running = 0
    truncated = False

    for step in chain:
        candidates: list[str] = []
        if step.text:
            role = step.role
            text = step.text
            if role == "user" and step.author not in (None, "human"):
                role = step.author or role
                text = tool_result_preview(text, NON_HUMAN_PREVIEW_CHARS)
            body = escape_uuid_headers(text)
            if include_uuids and step.uuid:
                candidates.append(f"[uuid={step.uuid} {role} {step.ts}] {body}")
            else:
                candidates.append(f"[{role} {step.ts}] {body}")
        for name, args_json in step.tool_calls:
            preview = escape_uuid_headers(tool_input_preview(args_json))
            candidates.append(f"[tool_use:{name or 'tool'} {step.ts}] {preview}")
        for call_id, content in step.tool_results:
            preview = escape_uuid_headers(tool_result_preview(content, tool_result_max_chars))
            candidates.append(f"[tool_result {call_id or '?'} {step.ts}] {preview}")
        for line in candidates:
            if running + len(line) + 1 > total_max_chars:
                lines.append(
                    f"…(session truncated at {total_max_chars} chars, {events_total} events total)"
                )
                truncated = True
                break
            lines.append(line)
            running += len(line) + 1
        if truncated:
            break

    return "\n".join(lines)


def human_ai_pair_count(steps: list[StepEvent]) -> int:
    """Count completed human→AI text exchanges (the perceived-error gate).

    Mirrors LangSmith's Perceived Error eligibility rule ("at least two
    human-AI message pairs"): one pair = a HUMAN turn (:func:`is_human_turn`)
    followed later by a non-empty main-chain assistant text turn. A user-role
    step a human did not write (hook feedback, notifications, harness text,
    compaction summaries) is not the human responding, so it neither opens
    nor closes a pair, and consecutive human turns collapse into the same
    pending pair — only a human turn that actually received an assistant
    reply completes one.
    """
    pairs = 0
    awaiting_reply = False
    for s in steps:
        if s.is_sidechain or s.is_compact_summary or not s.text:
            continue
        if s.role == "user":
            if not is_human_turn(s):
                continue
            awaiting_reply = True
        elif awaiting_reply:
            pairs += 1
            awaiting_reply = False
    return pairs


__all__ = [
    "NON_HUMAN_PREVIEW_CHARS",
    "TOOL_INPUT_PREVIEW_CHARS",
    "UUID_HEADER_ESCAPE",
    "StepEvent",
    "escape_uuid_headers",
    "format_step_ts",
    "human_ai_pair_count",
    "is_human_turn",
    "main_chain",
    "render_session_text",
    "session_kind",
    "step_event",
    "tool_input_preview",
    "tool_result_preview",
]
