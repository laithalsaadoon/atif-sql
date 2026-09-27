# SPDX-License-Identifier: Apache-2.0

"""Who wrote a user-role step, and what kind of session the steps make.

This is the atif-analytics TWIN of ``atif_duck.domain.authorship``. The
independence contract forbids importing atif-duck, so the rule tables below
are a copy, and ``packages/atif-duck/tests/test_authorship_twin_pin.py``
reads this file as source text and fails on any difference from the
canonical ones. Edit both or neither.

Claude Code and Codex deliver machine-written text in the USER role: Stop
hook feedback, task notifications, retry and continuation nudges, skill
bodies, slash-command wrappers, screenshot metadata, and the prompts of
automated review sessions. The LLM pipelines read a session through
:func:`step_author` so that only ``human`` steps count as the user speaking:
friction candidates, the perceived-error pair gate and uuid universe, the
rendered transcript's role labels, and the session-kind gate every pipeline
applies before it spends anything.

Matching is a prefix test over the message with leading whitespace
stripped; first rule wins; an empty message is ``harness``. A step flagged
``is_compact_summary`` is ``harness`` whatever its text; the corpus reader
applies that when it projects a step (mirroring atif-duck's
``user_steps.author``), so :func:`step_author` itself sees only source and
text, like the SQL macro.

Pure domain module: stdlib only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

AUTHOR_VALUES: tuple[str, ...] = (
    "human",
    "stop_hook",
    "task_notification",
    "harness",
    "audit_prompt",
)

#: Characters stripped from the left of a message before matching.
AUTHOR_STRIP_CHARS: str = " \t\r\n"

#: ``(author, prefix)``, first match wins. Every prefix was read off real
#: Claude Code and Codex corpora in September 2026 as a most-common opening of
#: user-role steps; the comment beside each says what writes it.
AUTHOR_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    # An automated review session: a hook that audits another agent's turn
    # opens a fresh session whose whole content is its prompt and a verdict.
    ("audit_prompt", "You are auditing an agent turn"),
    # Claude Code Stop / SubagentStop hooks that blocked the agent's stop.
    ("stop_hook", "Stop hook feedback"),
    ("stop_hook", "SubagentStop hook feedback"),
    # Background task completion notices.
    ("task_notification", "<task-notification>"),
    ("task_notification", "[SYSTEM NOTIFICATION"),
    # Claude Code bookkeeping: interrupts, continuation and retry nudges,
    # compaction summaries, skill bodies, image metadata for a screenshot the
    # agent read, workflow-computed tasks, reminders and hook output.
    ("harness", "[Request interrupted"),
    ("harness", "Continue from where you left off"),
    ("harness", "Your previous attempt hit a transient"),
    ("harness", "This session is being continued from a previous conversation"),
    ("harness", "Base directory for this skill:"),
    ("harness", "[Image: original "),
    ("harness", '{"type": "image"'),
    ("harness", "[Workflow harness"),
    ("harness", "[structured-output-enforce]"),
    ("harness", "<system-reminder>"),
    ("harness", "<user-prompt-submit-hook>"),
    ("harness", "UserPromptSubmit operation blocked by hook"),
    ("harness", "<fork-boilerplate>"),
    ("harness", "Another Claude session sent a message"),
    ("harness", "The coordinator sent a message"),
    # Slash-command and local-shell wrappers.
    ("harness", "<command-name>"),
    ("harness", "<command-message>"),
    ("harness", "<command-args>"),
    ("harness", "<local-command-stdout>"),
    ("harness", "<local-command-stderr>"),
    ("harness", "<local-command-caveat>"),
    ("harness", "Caveat: The messages below were generated"),
    ("harness", "<bash-input>"),
    ("harness", "<bash-stdout>"),
    ("harness", "<bash-stderr>"),
    # Codex CLI context injections.
    ("harness", "<environment_context>"),
    ("harness", "<codex_internal_context"),
    ("harness", "<user_instructions>"),
    ("harness", "# AGENTS.md instructions"),
    ("harness", "<turn_aborted>"),
    ("harness", "The following is the Codex agent history"),
)

#: Prefixes that mark an interrupted turn (``session_outcomes.interrupts``).
#: Both are also ``harness`` rules above, so an interrupt never counts as a
#: human turn.
INTERRUPT_PREFIXES: tuple[str, ...] = ("[Request interrupted", "<turn_aborted>")

#: ``session_outcomes.kind`` values (atif-duck's view computes the same).
SessionKind = Literal["interactive", "one_shot_job", "turn_audit"]


def step_author(source: str, message: str | None) -> str | None:
    """The author label for one step; ``None`` when ``source`` is not ``user``.

    ``source`` is the ATIF ``Step.source`` (``user`` / ``agent`` /
    ``system``). Mirrors atif-duck's ``step_author(src, msg)`` macro.
    """
    if source != "user":
        return None
    text = (message or "").lstrip(AUTHOR_STRIP_CHARS)
    if not text:
        return "harness"
    for author, prefix in AUTHOR_PREFIX_RULES:
        if text.startswith(prefix):
            return author
    return "human"


def is_interrupt(message: str | None) -> bool:
    """True when ``message`` opens with one of :data:`INTERRUPT_PREFIXES`."""
    return (message or "").lstrip(AUTHOR_STRIP_CHARS).startswith(INTERRUPT_PREFIXES)


def session_kind(first_user_author: str | None, human_turns: int) -> SessionKind:
    """``turn_audit`` | ``one_shot_job`` | ``interactive``, as ``session_outcomes`` computes it.

    ``first_user_author`` is the author of the session's first main-chain
    user step (``None`` when it has none); ``human_turns`` counts main-chain
    human steps.
    """
    if first_user_author == "audit_prompt":
        return "turn_audit"
    if human_turns <= 1:
        return "one_shot_job"
    return "interactive"


def kind_of(authors: Iterable[str]) -> SessionKind:
    """:func:`session_kind` over one session's main-chain user-step authors, in order."""
    first: str | None = None
    humans = 0
    for author in authors:
        if first is None:
            first = author
        if author == "human":
            humans += 1
    return session_kind(first, humans)


__all__ = [
    "AUTHOR_PREFIX_RULES",
    "AUTHOR_STRIP_CHARS",
    "AUTHOR_VALUES",
    "INTERRUPT_PREFIXES",
    "SessionKind",
    "is_interrupt",
    "kind_of",
    "session_kind",
    "step_author",
]
