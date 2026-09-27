# SPDX-License-Identifier: Apache-2.0

"""Who wrote a user-role step: the ``author`` rule table and its SQL form.

Claude Code and Codex deliver a lot of machine-written text in the USER role:
Stop hook feedback, ``<task-notification>`` blocks, retry and continuation
nudges, skill bodies, slash-command wrappers, image metadata from a screenshot
the agent read, and the prompts of automated review sessions. Counted as
user turns they drown the human ones (on a busy agent corpus about half of
the main-chain user steps were machine-written), so every analytics reader
classifies a user step first and treats only ``human`` as the user
speaking.

The classification is a PREFIX match over the message with leading
whitespace stripped, first rule wins, in :data:`AUTHOR_PREFIX_RULES` order.
Prefixes only, so the SQL macro (``starts_with``) and the Python twin
(``str.startswith``) cannot disagree on a regex dialect. An empty message is
``harness``: nothing a human typed. One rule sits outside the text: a step the
converter flagged ``is_compact_summary`` is ``harness`` whatever it says
(``user_steps.author`` and the Python twin both apply it; the bare
``step_author`` macro sees only source and text).

This table is the ONE definition. atif-analytics may not import atif-duck
(independence contract), so ``atif_analytics.domain.authorship`` carries a
twin of the two tuples below, and ``tests/test_authorship_twin_pin.py`` reads
that twin as source text and fails on any difference (the ``session_id``
precedent).

Pure domain module: stdlib only, no duckdb.
"""

from __future__ import annotations

from atif_duck.domain.sql_literal import SqlFragment, sql_literal

#: The author labels, in the order the docs list them.
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


#: ``ltrim`` argument naming :data:`AUTHOR_STRIP_CHARS` by code point (space,
#: tab, CR, LF); ``test_authorship.py`` pins the two against each other.
_STRIP_CHARS_SQL = "chr(32) || chr(9) || chr(13) || chr(10)"

#: The stripped message inside the ``step_author(src, msg)`` macro body.
_MACRO_MSG_STRIPPED = f"ltrim(msg, {_STRIP_CHARS_SQL})"

#: The stripped ``message`` column, for predicates over ``steps`` rows.
_COLUMN_MSG_STRIPPED = f"ltrim(message, {_STRIP_CHARS_SQL})"


def _author_arms() -> SqlFragment:
    """One ``WHEN starts_with(...) THEN '<author>'`` arm per rule, in order."""
    return SqlFragment(
        "\n".join(
            f"    WHEN starts_with({_MACRO_MSG_STRIPPED}, {sql_literal(prefix)}) "
            f"THEN {sql_literal(author)}"
            for author, prefix in AUTHOR_PREFIX_RULES
        )
    )


def _step_author_case() -> SqlFragment:
    """The ``CASE`` expression behind the ``step_author(src, msg)`` macro.

    ``NULL`` for a step whose source is not ``user``; otherwise ``harness`` for
    an empty message, the first matching rule's author, and ``human`` when
    nothing matches.
    """
    return SqlFragment(
        "CASE\n    WHEN src IS DISTINCT FROM 'user' THEN NULL\n"
        f"    WHEN msg IS NULL OR {_MACRO_MSG_STRIPPED} = '' THEN 'harness'\n"
        f"{_author_arms()}\n    ELSE 'human'\nEND"
    )


def _interrupt_predicate() -> SqlFragment:
    """``starts_with`` over :data:`INTERRUPT_PREFIXES` for the ``message`` column."""
    return SqlFragment(
        "("
        + " OR ".join(
            f"starts_with({_COLUMN_MSG_STRIPPED}, {sql_literal(prefix)})"
            for prefix in INTERRUPT_PREFIXES
        )
        + ")"
    )


#: The macro body and the interrupt test, rendered once from the tables above.
STEP_AUTHOR_CASE_SQL: SqlFragment = _step_author_case()
INTERRUPT_PREDICATE_SQL: SqlFragment = _interrupt_predicate()


__all__ = [
    "AUTHOR_PREFIX_RULES",
    "AUTHOR_STRIP_CHARS",
    "AUTHOR_VALUES",
    "INTERRUPT_PREDICATE_SQL",
    "INTERRUPT_PREFIXES",
    "STEP_AUTHOR_CASE_SQL",
]
