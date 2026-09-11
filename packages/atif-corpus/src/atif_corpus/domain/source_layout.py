# SPDX-License-Identifier: Apache-2.0

"""Where an agent's transcripts live, as one value object per agent.

Discovery used to be a single hardcoded shape — project directories one level
under the source root, one JSONL per session — because Claude Code was the only
agent. Codex CLI writes nothing like it: rollouts sit three levels deep under
``$CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/`` and the session id is a uuid inside
the FILENAME rather than the whole stem. Both shapes are now described here, as
pure data plus pure functions, so the scanner walks one algorithm and the
per-agent knowledge lives in exactly one place.

Pure module: no filesystem access, no env, no logging. Everything it returns is
derived from a path's TEXT.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from atif_corpus.domain.agents import AgentSource

#: A Codex rollout filename's fixed prefix, and the number of hyphen-separated
#: groups its trailing uuid occupies: ``rollout-<ts>-<8>-<4>-<4>-<4>-<12>.jsonl``.
_ROLLOUT_PREFIX = "rollout-"
_UUID_GROUPS = 5


@dataclass(frozen=True, slots=True)
class SourceLayout:
    """How one agent's transcripts are arranged under a source root.

    ``transcript_depth`` is how many directory levels sit between the source
    root and a transcript file: 1 for Claude Code (``<project>/<session>.jsonl``)
    and 3 for Codex (``<YYYY>/<MM>/<DD>/rollout-*.jsonl``). The scanner descends
    exactly that many levels, listing each one explicitly, which is what keeps a
    directory it could not OPEN distinguishable from an empty one at every level
    rather than only at the first.

    ``has_side_files`` says whether a session can own a sibling directory of
    additional transcripts. Claude Code sessions do (subagent and workflow
    side-files, which the watermark must notice); a Codex sub-agent writes its
    own rollout under its own session id, so a rollout is always alone.
    """

    agent: AgentSource
    transcript_depth: int
    has_side_files: bool

    def is_transcript(self, name: str) -> bool:
        """Whether a filename in a transcript directory is one of this agent's."""
        if not name.endswith(".jsonl"):
            return False
        if self.agent is AgentSource.CODEX:
            return name.startswith(_ROLLOUT_PREFIX)
        return True

    def session_id(self, name: str) -> str | None:
        """The session id a transcript filename carries, or ``None`` if it carries none.

        Claude Code names the file after the session, so the stem IS the id.
        Codex prefixes a timestamp, so the id is the stem's trailing uuid — and
        a name whose shape does not yield one is not a session this layout can
        materialize, which the caller must skip rather than guess at.
        """
        if not self.is_transcript(name):
            return None
        stem = name.removesuffix(".jsonl")
        if self.agent is not AgentSource.CODEX:
            return stem or None
        parts = stem.split("-")
        if len(parts) < _UUID_GROUPS + 1:
            return None
        return "-".join(parts[-_UUID_GROUPS:])

    def session_from_watermark_path(self, source_root: str, path: str) -> tuple[str, str] | None:
        """``(session_id, main transcript path)`` for a recorded watermark path.

        Used when a directory could not be LISTED: nothing was discovered
        there, so the watermark is the only record of which sessions lived
        inside it, and each recorded path must resolve back to a session id
        WITHOUT touching the filesystem.

        Returns ``None`` when the path is not under ``source_root`` or its shape
        does not name a session.
        """
        root_prefix = f"{source_root.rstrip('/')}/"
        if not path.startswith(root_prefix):
            return None
        segments = PurePosixPath(path[len(root_prefix) :]).parts
        if len(segments) <= self.transcript_depth:
            return None
        if self.agent is AgentSource.CODEX:
            # A rollout owns no side-files, so a recorded path IS a main
            # transcript and its own name carries the id.
            session_id = self.session_id(segments[self.transcript_depth])
            return (session_id, path) if session_id else None
        # Claude Code: `<project>/<session>.jsonl`, or a side-file under
        # `<project>/<session>/...`. Either way the second segment names the
        # session, so the main transcript is derivable from position alone.
        project, entry = segments[0], segments[1]
        session_id = entry.removesuffix(".jsonl")
        if not session_id:
            return None
        return session_id, f"{root_prefix}{project}/{session_id}.jsonl"


#: Claude Code: `<source_root>/<project>/<session>.jsonl` plus a side-file tree
#: under `<source_root>/<project>/<session>/`.
CLAUDE_CODE_LAYOUT = SourceLayout(
    agent=AgentSource.CLAUDE_CODE,
    transcript_depth=1,
    has_side_files=True,
)

#: Codex CLI: `<source_root>/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl`, one
#: file per session and no side-files.
CODEX_LAYOUT = SourceLayout(
    agent=AgentSource.CODEX,
    transcript_depth=3,
    has_side_files=False,
)

_LAYOUTS: dict[AgentSource, SourceLayout] = {
    AgentSource.CLAUDE_CODE: CLAUDE_CODE_LAYOUT,
    AgentSource.CODEX: CODEX_LAYOUT,
}


def layout_for(agent: AgentSource) -> SourceLayout:
    """The layout for ``agent``.

    Every :class:`~atif_corpus.domain.agents.AgentSource` member has one, and
    the lookup raises rather than defaulting: a new agent added to the enum
    without a layout must fail loudly at the first scan instead of silently
    scanning with Claude Code's shape and reporting an empty corpus.
    """
    try:
        return _LAYOUTS[agent]
    except KeyError as exc:  # pragma: no cover — unreachable while the enum is closed
        msg = f"no source layout declared for agent {agent!r}"
        raise ValueError(msg) from exc
