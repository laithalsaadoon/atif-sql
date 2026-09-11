# SPDX-License-Identifier: Apache-2.0

"""Our Claude Code converter against harbor's, on the synthetic fixture.

Three readings of the same session. The LIVE oracle (skipped, loudly, once
harbor drops the private method), the FROZEN golden (never skipped: it is the
oracle that outlives harbor's private API), and the live oracle again with
``include_subagents=False``, so the flag is measured on both sides rather than
assumed to mean the same thing.
"""

from __future__ import annotations

from pathlib import Path

from harbor_oracle import (
    diff_paths,
    harbor_claude_code_trajectory,
    load_golden,
    require_harbor_private_api,
)

from atif_converter.infrastructure.claude_code_converter import convert_claude_code_session


class TestParityClaudeCode:
    def test_synthetic_matches_live_oracle(self, synthetic_session: Path) -> None:
        require_harbor_private_api("claude-code")
        theirs = harbor_claude_code_trajectory(synthetic_session)
        ours = convert_claude_code_session(synthetic_session)
        assert theirs is not None
        assert ours is not None
        assert diff_paths(theirs, ours.to_json_dict()) == []

    def test_synthetic_matches_frozen_golden(self, synthetic_session: Path) -> None:
        ours = convert_claude_code_session(synthetic_session)
        assert ours is not None
        assert diff_paths(load_golden("claude_code.synthetic"), ours.to_json_dict()) == []

    def test_without_subagents_matches_live_oracle(self, synthetic_session: Path) -> None:
        require_harbor_private_api("claude-code")
        theirs = harbor_claude_code_trajectory(synthetic_session, include_subagents=False)
        ours = convert_claude_code_session(synthetic_session, include_subagents=False)
        assert theirs is not None
        assert ours is not None
        assert diff_paths(theirs, ours.to_json_dict()) == []
        # The flag has to change the answer, or the test above proves nothing.
        assert len(ours.steps) < len(load_golden("claude_code.synthetic")["steps"])
        assert all(step.extra is not None and not step.extra["is_sidechain"] for step in ours.steps)
