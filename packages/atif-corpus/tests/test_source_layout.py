# SPDX-License-Identifier: Apache-2.0

"""The per-agent discovery layouts: names, ids, and watermark resolution.

Pure-function tests — no filesystem. The scanner's use of these layouts is
covered in ``test_codex_corpus.py`` and ``test_infrastructure.py``.
"""

from __future__ import annotations

import pytest

from atif_corpus.domain.agents import AgentSource
from atif_corpus.domain.source_layout import (
    CLAUDE_CODE_LAYOUT,
    CODEX_LAYOUT,
    layout_for,
)

CODEX_ROLLOUT = "rollout-2026-09-11T17-27-01-01a09182-2858-7f42-936b-7f027b341fdf.jsonl"
CODEX_SESSION = "01a09182-2858-7f42-936b-7f027b341fdf"
CLAUDE_SESSION = "11111111-1111-1111-1111-111111111111"


class TestLayoutRegistry:
    def test_every_agent_has_a_layout(self) -> None:
        """A new agent without a layout must fail loudly, not scan as Claude Code."""
        for agent in AgentSource:
            assert layout_for(agent).agent is agent

    def test_depths_and_side_file_policy(self) -> None:
        assert CLAUDE_CODE_LAYOUT.transcript_depth == 1
        assert CLAUDE_CODE_LAYOUT.has_side_files is True
        # A Codex sub-agent writes its own rollout under its own session id.
        assert CODEX_LAYOUT.transcript_depth == 3
        assert CODEX_LAYOUT.has_side_files is False


class TestTranscriptNames:
    def test_claude_code_takes_any_jsonl(self) -> None:
        assert CLAUDE_CODE_LAYOUT.is_transcript(f"{CLAUDE_SESSION}.jsonl")
        assert CLAUDE_CODE_LAYOUT.session_id(f"{CLAUDE_SESSION}.jsonl") == CLAUDE_SESSION

    def test_codex_takes_only_a_rollout(self) -> None:
        assert CODEX_LAYOUT.is_transcript(CODEX_ROLLOUT)
        assert not CODEX_LAYOUT.is_transcript(f"{CLAUDE_SESSION}.jsonl")
        assert CODEX_LAYOUT.session_id(CODEX_ROLLOUT) == CODEX_SESSION

    @pytest.mark.parametrize("name", ["notes.md", "session.json", "history.jsonl.bak"])
    def test_non_jsonl_is_never_a_transcript(self, name: str) -> None:
        assert not CLAUDE_CODE_LAYOUT.is_transcript(name)
        assert not CODEX_LAYOUT.is_transcript(name)

    @pytest.mark.parametrize("name", ["rollout-2026.jsonl", "rollout-.jsonl", "other.jsonl"])
    def test_codex_name_without_a_uuid_yields_no_session_id(self, name: str) -> None:
        assert CODEX_LAYOUT.session_id(name) is None


class TestWatermarkResolution:
    def test_claude_code_resolves_a_side_file_to_its_main_transcript(self) -> None:
        root = "/home/alice/.claude/projects"
        side = f"{root}/-proj-a/{CLAUDE_SESSION}/subagents/workflows/wf_1/agent-x.jsonl"
        assert CLAUDE_CODE_LAYOUT.session_from_watermark_path(root, side) == (
            CLAUDE_SESSION,
            f"{root}/-proj-a/{CLAUDE_SESSION}.jsonl",
        )

    def test_claude_code_resolves_a_main_transcript_to_itself(self) -> None:
        root = "/home/alice/.claude/projects"
        main = f"{root}/-proj-a/{CLAUDE_SESSION}.jsonl"
        assert CLAUDE_CODE_LAYOUT.session_from_watermark_path(root, main) == (CLAUDE_SESSION, main)

    def test_codex_resolves_a_rollout_to_itself(self) -> None:
        """A rollout owns no side-files, so a recorded path IS a main transcript."""
        root = "/home/alice/.codex/sessions"
        path = f"{root}/2026/09/11/{CODEX_ROLLOUT}"
        assert CODEX_LAYOUT.session_from_watermark_path(root, path) == (CODEX_SESSION, path)

    def test_a_path_outside_the_root_resolves_to_nothing(self) -> None:
        assert (
            CODEX_LAYOUT.session_from_watermark_path("/a/sessions", "/b/2026/09/11/x.jsonl") is None
        )

    def test_a_path_too_shallow_to_name_a_session_resolves_to_nothing(self) -> None:
        """A root-level entry names no session, whichever layout reads it."""
        root = "/home/alice/.codex/sessions"
        assert CODEX_LAYOUT.session_from_watermark_path(root, f"{root}/stray.jsonl") is None
        claude_root = "/home/alice/.claude/projects"
        assert (
            CLAUDE_CODE_LAYOUT.session_from_watermark_path(
                claude_root, f"{claude_root}/stray.jsonl"
            )
            is None
        )

    def test_a_trailing_slash_on_the_root_is_tolerated(self) -> None:
        root = "/home/alice/.codex/sessions"
        path = f"{root}/2026/09/11/{CODEX_ROLLOUT}"
        assert CODEX_LAYOUT.session_from_watermark_path(f"{root}/", path) == (CODEX_SESSION, path)
