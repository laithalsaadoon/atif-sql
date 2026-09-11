# SPDX-License-Identifier: Apache-2.0

"""The oracle itself must be trustworthy before a port is measured against it.

Three properties, each a precondition for the parity tests meaning anything:
the live oracle converts both synthetic fixtures; it is deterministic (two runs
agree byte for byte, so a diff is a real divergence and never noise); and it
still agrees with the frozen goldens, so an upstream behavior change in harbor
surfaces here as a named diff rather than as a port that "fails parity".
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from harbor_oracle import (
    diff_paths,
    golden_path,
    harbor_claude_code_trajectory,
    harbor_codex_trajectory,
    load_golden,
    require_harbor_private_api,
    write_golden,
)

#: Set to re-freeze the goldens from the live oracle. Deliberately an env var
#: and not a default: a golden is re-frozen after a DECISION about an upstream
#: change, never as a side effect of a normal test run.
FREEZE_ENV = "ATIF_FREEZE_GOLDENS"


class TestLiveOracle:
    def test_claude_code_fixture_converts_and_is_deterministic(
        self, synthetic_session: Path
    ) -> None:
        require_harbor_private_api("claude-code")
        first = harbor_claude_code_trajectory(synthetic_session)
        second = harbor_claude_code_trajectory(synthetic_session)
        assert first is not None
        assert diff_paths(first, second) == []

    def test_codex_fixture_converts_and_is_deterministic(self, codex_rollout: Path) -> None:
        require_harbor_private_api("codex")
        first = harbor_codex_trajectory(codex_rollout)
        second = harbor_codex_trajectory(codex_rollout)
        assert first is not None
        assert diff_paths(first, second) == []


class TestFreeze:
    """Re-freeze the goldens from the live oracle; runs only under FREEZE_ENV=1."""

    def test_freeze(self, synthetic_session: Path, codex_rollout: Path) -> None:
        if os.environ.get(FREEZE_ENV) != "1":
            pytest.skip(f"set {FREEZE_ENV}=1 to re-freeze the goldens")
        require_harbor_private_api("claude-code")
        require_harbor_private_api("codex")
        claude = harbor_claude_code_trajectory(synthetic_session)
        codex = harbor_codex_trajectory(codex_rollout)
        assert claude is not None and codex is not None
        write_golden("claude_code.synthetic", claude)
        write_golden("codex.synthetic", codex)


class TestFrozenGoldens:
    """The live oracle and the frozen one must agree, or the freeze is stale.

    A diff here means harbor changed conversion behavior under the pin. The
    response is a decision, not a re-freeze: either the port follows upstream
    (re-freeze and port the change) or it deliberately does not (record why).
    """

    @pytest.mark.parametrize("name", ["claude_code.synthetic", "codex.synthetic"])
    def test_golden_exists(self, name: str) -> None:
        assert golden_path(name).is_file(), f"freeze it: {FREEZE_ENV}=1 uv run pytest -k freeze"

    def test_claude_code_live_matches_frozen(self, synthetic_session: Path) -> None:
        require_harbor_private_api("claude-code")
        live = harbor_claude_code_trajectory(synthetic_session)
        assert live is not None
        assert diff_paths(load_golden("claude_code.synthetic"), live) == []

    def test_codex_live_matches_frozen(self, codex_rollout: Path) -> None:
        require_harbor_private_api("codex")
        live = harbor_codex_trajectory(codex_rollout)
        assert live is not None
        assert diff_paths(load_golden("codex.synthetic"), live) == []
