# SPDX-License-Identifier: Apache-2.0

"""The review pipeline's pure half: the checklist, step labels, the citation check, and finding identity.

A review finding is a pair, what the agent should have done and what it did
instead, plus quotes of the steps that show it. This module holds the parts
of that which need no model and no file:

* :data:`CHECKLIST` is the nine places a reviewer looks.
* :func:`label_steps` gives every step of a call a number (``[step 12]``),
  so a finding names a step by a short number and code maps it back to the
  step's uuid. A model never copies a uuid.
* :func:`check_citations` is the pass bar. A finding is kept only when every
  quote it carries is found, verbatim, in the step it names. A narration is
  an index a reviewer reads to decide which steps to open; it's never
  evidence, so nothing a narration says can reach a stored finding unless the
  recorded steps say it too.
* :func:`finding_key` says when two rows are the same finding.

What the check proves and what it doesn't: it proves the quoted text is in
the cited step. It doesn't prove the quote supports the finding's reading of
it; that's a judgment, and a later verify stage's job.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast, get_args

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from atif_analytics.domain.transcript import StepEvent

#: The checklist headings, in the order the prompt lists them. The first
#: nine follow the shape of Google's Ambient Quality Agent review checklist
#: (google/adk-recipes, core/python/ambient-quality-agent), reworded for a
#: coding agent that edits files and runs commands; the notes in
#: :data:`CHECKLIST` say where each of a Stop-time turn audit's usual classes
#: (unsupported claim, suppressed failure, unreported work, unanswered
#: request, turn ends on intent, unchecked dismissal) lands.
Heading = Literal[
    "procedure",
    "output_requirements",
    "tool_selection",
    "tool_arguments",
    "tool_results",
    "progress_completion",
    "grounding",
    "answer_quality",
    "configuration",
]

HEADINGS: tuple[str, ...] = cast("tuple[str, ...]", get_args(Heading))

#: Heading to what it covers. The text is prompt text: it reaches the model
#: as written, and the rubric digest covers it.
CHECKLIST: dict[str, str] = {
    "procedure": (
        "a step the request or the agent's instructions required was skipped, taken out "
        "of order, or taken before its precondition held; a forbidden action was "
        "performed; a limit on scope or authority was crossed (a file, branch, account "
        "or service the agent was told to leave alone); the agent asked where it was "
        "told to proceed, or acted where it was told to ask."
    ),
    "output_requirements": (
        "the request or the instructions demanded that the reply or the artifact "
        "contain, name, cite or lay out something, and it doesn't: a required section, "
        "file, format or statement is missing or in the wrong place."
    ),
    "tool_selection": (
        "a check the situation required was never run (the tests after a change, a "
        "read of the file before editing it, a lookup before answering); another tool "
        "was used in its place; a question was dismissed, or something was called "
        "absent, fine or unrelated, with no lookup behind it."
    ),
    "tool_arguments": (
        "a command or call was given a value that contradicts the conversation: the "
        "wrong path, branch, id, flag or environment; an identifier that appears in no "
        "earlier message or result."
    ),
    "tool_results": (
        "a failed or empty result was treated as success; a result was read wrongly "
        "(a number, a status or a name taken from the wrong line); a failed call was "
        "repeated unchanged; a later step rests on something the result contradicts; "
        "a failure the agent saw is missing from what it told the reader."
    ),
    "progress_completion": (
        "the agent stopped before finishing what was asked; answered one part of a "
        "multi-part request; ended its turn on a plan or an offer to do work that was "
        "in scope and that it could have done; circled without progress; did "
        "substantial work nobody asked for."
    ),
    "grounding": (
        "a statement no step supports; an invented fact, file, identifier, link, "
        "quotation or figure; a claim to have done, run or verified something the "
        "steps show it never did; a guess presented as settled."
    ),
    "answer_quality": (
        "the final answer is wrong, or answers a question other than the one asked; it "
        "contradicts the results it rests on; it leaves out work the agent did that "
        "the reader needs to know about (a file changed, a message sent, a thing "
        "deleted); it is empty or cut off."
    ),
    "configuration": (
        "the agent's instructions and its tools contradict each other, and this "
        "session ran into it: an instruction sends the agent to a tool or a path that "
        "doesn't exist, or two rules it was given can't both be kept. Report only a "
        "contradiction you can quote on both sides."
    ),
}


def checklist_text() -> str:
    """The checklist as the prompt carries it: one numbered line per heading."""
    return "\n".join(
        f"{position}. {heading}: {CHECKLIST[heading]}"
        for position, heading in enumerate(HEADINGS, start=1)
    )


# ---------------------------------------------------------------------------
# Step labels
# ---------------------------------------------------------------------------

#: What opens a labeled step in a call's transcript.
_STEP_HEADER = "[step "
#: What a body's ``[step `` becomes, so a pasted transcript can't present a
#: line of its own as a step the reviewer may cite.
_STEP_HEADER_ESCAPE = "(step "
_STEP_HEADER_RE = re.compile(r"\[step ", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LabeledStep:
    """One step as a review call shows it.

    ``number`` is the label the call's transcript gives the step (1-based,
    unique within the call). ``raw`` is the step as recorded and ``shown``
    the step as rendered for the call (long text cut, tool results
    excerpted); the citation check searches both, since a result the
    harness saved to a file is shown from the corpus's archived copy, which
    the recorded step doesn't hold. ``position`` is the step's index in its
    stream, so a finding's cited steps can be put in order.
    """

    number: int
    raw: StepEvent
    shown: StepEvent
    agent_id: str | None
    position: int


def escape_step_headers(text: str) -> str:
    """Neutralize ``[step `` in rendered step text; the length is unchanged."""
    return _STEP_HEADER_RE.sub(_STEP_HEADER_ESCAPE, text)


def render_labeled(
    steps: Sequence[LabeledStep], render: Callable[[Sequence[StepEvent]], str]
) -> str:
    """The transcript of a review call: each step under its ``[step N]`` line.

    ``render`` is the window renderer in use. A step that renders nothing
    (no text, calls or results) is left out, so no label names an empty
    step.
    """
    blocks: list[str] = []
    for step in steps:
        body = render([step.shown])
        if body:
            blocks.append(f"{_STEP_HEADER}{step.number}]\n{escape_step_headers(body)}")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# The citation check
# ---------------------------------------------------------------------------

#: The fewest characters (after whitespace is collapsed) a quote may have.
#: Shorter than this, a quote such as ``ok`` or ``1 failed`` is found in
#: nearly any step and proves nothing about the one it names.
MIN_QUOTE_CHARS: int = 12

#: Why a finding was dropped, in the order the check tries them.
DropReason = Literal[
    "no_quote", "unknown_step", "step_without_uuid", "quote_too_short", "quote_not_found"
]
DROP_REASONS: tuple[str, ...] = cast("tuple[str, ...]", get_args(DropReason))

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_quote(text: str) -> str:
    """``text`` as the citation check compares it: NFC, whitespace runs collapsed, ends trimmed.

    Line breaks and indentation are the one thing a model re-flows when it
    copies a line, so they're the one thing the comparison forgives. Case,
    punctuation and digits must match as written.
    """
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def _string_leaves(value: object) -> list[str]:
    """Every string inside a parsed JSON value, in document order."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _string_leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _string_leaves(item)]
    return []


def step_haystacks(step: StepEvent) -> list[str]:
    """Every text of one step a quote may come from, normalized.

    The step's message, each tool call's arguments both as the JSON the
    transcript shows and as the strings inside it (a command quoted without
    its JSON escapes is still the command), and each tool result.
    """
    texts = [step.text]
    for name, args_json in step.tool_calls:
        texts.append(f"{name} {args_json}")
        try:
            texts.extend(_string_leaves(json.loads(args_json)))
        except ValueError:
            continue
    texts.extend(content for _, content in step.tool_results)
    return [normalized for text in texts if (normalized := normalize_quote(text))]


def quote_in_step(quote: str, step: LabeledStep) -> bool:
    """True when ``quote`` is in the step, as recorded or as shown, verbatim after normalizing."""
    needle = normalize_quote(quote)
    if not needle:
        return False
    return any(
        needle in haystack
        for version in (step.raw, step.shown)
        for haystack in step_haystacks(version)
    )


@dataclass(frozen=True, slots=True)
class Citation:
    """One verified quote: the step it's in, and the text."""

    step_uuid: str
    quote: str
    agent_id: str | None
    position: int


@dataclass(frozen=True, slots=True)
class CitationResult:
    """What the check decided for one finding.

    ``reason`` is ``None`` when every quote verified, and then ``citations``
    holds them in step order. Otherwise ``reason`` says why the finding is
    dropped and ``citations`` is empty.
    """

    reason: DropReason | None
    citations: tuple[Citation, ...] = ()

    @property
    def ok(self) -> bool:
        """True when the finding may be stored."""
        return self.reason is None


def _quote_fault(
    number: int, text: str, steps: dict[int, LabeledStep], min_chars: int
) -> DropReason | None:
    """Why one quote fails the check, or ``None`` when it's in the step it names."""
    step = steps.get(number)
    if step is None:
        return "unknown_step"
    if not step.raw.uuid:
        return "step_without_uuid"
    if len(normalize_quote(text)) < min_chars:
        return "quote_too_short"
    return None if quote_in_step(text, step) else "quote_not_found"


def check_citations(
    quotes: Sequence[tuple[int, str]],
    steps: dict[int, LabeledStep],
    *,
    min_chars: int = MIN_QUOTE_CHARS,
) -> CitationResult:
    """Verify a finding's quotes against the steps they name.

    ``quotes`` is ``(step number, quoted text)`` pairs as the model returned
    them, and ``steps`` the labeled steps the call was shown, by number. The
    finding passes only when it has at least one quote and every quote names
    a step that was shown, that carries a uuid, is at least ``min_chars``
    long, and is in that step. One bad quote drops the finding: the
    ``actual`` side was written from all of them, so a quote that isn't
    there means that side rests on something the record doesn't show.
    """
    if not quotes:
        return CitationResult("no_quote")
    citations: list[Citation] = []
    for number, text in quotes:
        fault = _quote_fault(number, text, steps, min_chars)
        if fault is not None:
            return CitationResult(fault)
        step = steps[number]
        citations.append(
            Citation(
                step_uuid=step.raw.uuid or "",
                quote=normalize_quote(text),
                agent_id=step.agent_id,
                position=step.position,
            )
        )
    # Main chain first, then by position: the first citation is the anchor.
    citations.sort(key=lambda c: (c.agent_id is not None, c.agent_id or "", c.position))
    return CitationResult(None, tuple(citations))


# ---------------------------------------------------------------------------
# Finding identity
# ---------------------------------------------------------------------------


def finding_key(session_id: str, heading: str, anchor_step_uuid: str) -> str:
    """The key under which two rows are the same finding: 16 hex digits.

    The session, the checklist heading, and the anchor: the earliest step
    the finding quotes. A second review of the same session (after it grew,
    or under a new prompt) that finds the same fault at the same step under
    the same heading writes a row with the same key, and the view keeps the
    newest. The wording of ``expected`` and ``actual`` is left out on
    purpose, since two runs never word a finding the same way. Whether two
    findings in different sessions share a cause is a clustering question,
    and no part of this key.
    """
    digest = hashlib.sha256()
    for part in (session_id, heading, anchor_step_uuid):
        digest.update(part.encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Packing steps into calls
# ---------------------------------------------------------------------------


def pack_groups(sizes: Sequence[int], max_chars: int) -> list[tuple[int, int]]:
    """Split consecutive groups into calls of at most ``max_chars``: ``(start, stop)`` index pairs.

    ``sizes`` is the rendered size of each group (a window, as a rule) in
    order. Groups are never reordered or split, so a group larger than the
    ceiling gets a call of its own. Every group lands in exactly one call.
    """
    calls: list[tuple[int, int]] = []
    start = 0
    running = 0
    for index, size in enumerate(sizes):
        if index > start and running + size > max_chars:
            calls.append((start, index))
            start = index
            running = 0
        running += size
    if start < len(sizes):
        calls.append((start, len(sizes)))
    return calls


__all__ = [
    "CHECKLIST",
    "DROP_REASONS",
    "HEADINGS",
    "MIN_QUOTE_CHARS",
    "Citation",
    "CitationResult",
    "DropReason",
    "Heading",
    "LabeledStep",
    "check_citations",
    "checklist_text",
    "escape_step_headers",
    "finding_key",
    "normalize_quote",
    "pack_groups",
    "quote_in_step",
    "render_labeled",
    "step_haystacks",
]
