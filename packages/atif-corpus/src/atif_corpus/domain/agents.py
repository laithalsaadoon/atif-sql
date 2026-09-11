# SPDX-License-Identifier: Apache-2.0

"""Which agent produced a transcript — atif-corpus's copy of the one enum.

DELIBERATE TWIN of :mod:`atif_converter.domain.agents`, and the same kind of
twin as ``sql_literal`` and ``embedding_guard`` elsewhere in this workspace:
the import-linter independence contract forbids atif-corpus from importing
atif-converter, and the agent identity is needed on both sides of that
boundary — the converter picks an adapter with it, the corpus picks a discovery
layout and a corpus slug with it.

``packages/atif-corpus/tests/test_agents_twin_pin.py`` reads the atif-converter
copy as SOURCE TEXT (AST, never imported) and fails if the two disagree on a
member name or a value, so the twin cannot drift silently.

The string values are WIRE CONTRACT: the ``--agent`` flag's spellings, harbor's
``Trajectory.agent.name``, and the ``agent`` key in ``meta.json``.
"""

from __future__ import annotations

from enum import StrEnum


class AgentSource(StrEnum):
    """A coding agent whose transcripts this workspace can materialize."""

    CLAUDE_CODE = "claude-code"
    CODEX = "codex"


#: The agent assumed when a caller names none.
DEFAULT_AGENT: AgentSource = AgentSource.CLAUDE_CODE
