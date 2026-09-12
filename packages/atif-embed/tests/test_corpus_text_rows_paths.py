# SPDX-License-Identifier: Apache-2.0

"""DuckDbTextRows binds its paths: a hostile corpus root and hostile text are both data."""

from __future__ import annotations

import json
from pathlib import Path

from atif_embed.infrastructure.corpus_text_rows import DuckDbTextRows

INJECTION = "'); DROP TABLE x; -- " + "?" * 8 + " $1 \\n \\' " + "\n" * 3 + "x" * 40


def test_hostile_root_and_hostile_text_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "o'brien ?; --$1\\x"
    session_dir = root / "sessions" / "11111111-1111-1111-1111-111111111111"
    session_dir.mkdir(parents=True)
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-20T10:00:00.000Z",
                "source": "user",
                "message": INJECTION,
                "extra": {"is_sidechain": False, "source_uuids": ["u-1"]},
            }
        ],
    }
    (session_dir / "trajectory.json").write_text(json.dumps(trajectory, separators=(",", ":")))
    (session_dir / "meta.json").write_text("{}")
    rows = list(DuckDbTextRows().iter_unembedded(root))
    assert [(row.uuid, row.text) for row in rows] == [("u-1", INJECTION)]
