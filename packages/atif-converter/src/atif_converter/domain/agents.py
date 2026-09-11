# SPDX-License-Identifier: Apache-2.0

"""Which agent produced a transcript — the one enum every layer keys off.

An ATIF corpus can hold trajectories from more than one coding agent, and the
two this workspace converts write nothing alike: Claude Code appends one JSONL
per session under ``<config>/projects/<project>/``, Codex CLI writes one
rollout per session under ``$CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/``. Discovery,
the record taxonomy, the fidelity gaps and the harbor adapter all differ, so
the agent is a first-class value carried from the CLI flag down to the census
rather than inferred from a path shape at each layer.

Pure value object: no harbor import, no I/O. The string values are WIRE
CONTRACT — they are the ``--agent`` flag's accepted spellings, the
``agent.name`` harbor stamps into a trajectory, and the ``agent`` key in
``meta.json``.
"""

from __future__ import annotations

from enum import StrEnum


class AgentSource(StrEnum):
    """A coding agent whose transcripts this workspace can convert.

    The value doubles as harbor's ``Trajectory.agent.name`` for that agent
    (verified 2026-09-11 against harbor 0.22.0: ``ClaudeCode`` stamps
    ``claude-code`` and ``Codex`` stamps ``codex``), which is what lets
    atif-duck's ``sessions.agent`` column come straight from the trajectory
    instead of from a second provenance file.
    """

    CLAUDE_CODE = "claude-code"
    CODEX = "codex"


#: The agent assumed when a caller names none — the interactive corpus nearly
#: every user has, and the only agent this workspace supported before 2026-09-11.
DEFAULT_AGENT: AgentSource = AgentSource.CLAUDE_CODE
