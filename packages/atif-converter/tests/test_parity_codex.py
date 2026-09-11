# SPDX-License-Identifier: Apache-2.0

"""The ported Codex converter agrees with harbor's, field for field.

Two oracles, one fixture. The LIVE one is harbor's private method, consulted
while harbor still ships it and skipped the day it is gone; the FROZEN one is
the golden captured from that method under the 0.22.0 pin, never skipped, so
parity stays a hard gate after the private API disappears.
"""

from __future__ import annotations

from pathlib import Path

from harbor_oracle import (
    diff_paths,
    harbor_codex_trajectory,
    load_golden,
    require_harbor_private_api,
)

from atif_converter.infrastructure.codex_converter import convert_codex_rollout


class TestCodexParity:
    def test_matches_live_harbor(self, codex_rollout: Path) -> None:
        require_harbor_private_api("codex")
        ours = convert_codex_rollout(codex_rollout)
        assert ours is not None
        assert diff_paths(harbor_codex_trajectory(codex_rollout), ours.to_json_dict()) == []

    def test_matches_frozen_golden(self, codex_rollout: Path) -> None:
        ours = convert_codex_rollout(codex_rollout)
        assert ours is not None
        assert diff_paths(load_golden("codex.synthetic"), ours.to_json_dict()) == []
