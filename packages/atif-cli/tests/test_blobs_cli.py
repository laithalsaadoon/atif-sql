# SPDX-License-Identifier: Apache-2.0

"""End to end through the composition root: an inline image becomes a stored blob.

The real converter, the real corpus writer and the real views, over one
Claude Code session whose ``Read`` result carries a PNG: after ``materialize``
the base64 is gone from ``trajectory.json``, the bytes sit in the blob store,
and ``images.blob_path`` names the stored file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from atif_cli.app import materialize, query
from atif_cli.output import OutputFormat

SESSION = "cccccccc-cccc-cccc-cccc-cccccccccccc"
#: A PNG signature plus a 7 x 9 IHDR: all an image header reader needs.
IMAGE = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    + (7).to_bytes(4, "big")
    + (9).to_bytes(4, "big")
    + b"\x08\x02\x00\x00\x00"
    + b"\x00" * 8
)


def _write_session(root: Path) -> Path:
    main = root / "-w" / f"{SESSION}.jsonl"
    main.parent.mkdir(parents=True)
    base = {"sessionId": SESSION, "version": "2.1.300", "cwd": "/w"}
    records = [
        {
            **base,
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-09-27T00:00:01Z",
            "message": {"role": "user", "content": "read the screenshot"},
        },
        {
            **base,
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-09-27T00:00:02Z",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-test-1",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "/w/s.png"},
                    }
                ],
            },
        },
        {
            **base,
            "type": "user",
            "uuid": "u2",
            "timestamp": "2026-09-27T00:00:03Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": base64.b64encode(IMAGE).decode(),
                                },
                            }
                        ],
                    }
                ],
            },
        },
    ]
    main.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    stale = time.time_ns() - 3_600 * 1_000_000_000
    os.utime(main, ns=(stale, stale))
    return main


@pytest.mark.integration
def test_an_inline_image_is_stored_once_and_queryable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("ATIF_SQL_CORPUS_ROOT", "ATIF_SQL_LANCE_URI", "ATIF_SQL_EMBED_MODEL_ID"):
        monkeypatch.delenv(var, raising=False)
    source = tmp_path / "projects"
    _write_session(source)
    corpus = tmp_path / "corpus"
    materialize(source_root=source, corpus_root=corpus, fmt=OutputFormat.JSON)
    report = json.loads(capsys.readouterr().out)
    assert report["materialized"] == 1
    assert report["failed"] == 0

    trajectory = (corpus / "sessions" / SESSION / "trajectory.json").read_text()
    assert base64.b64encode(IMAGE).decode() not in trajectory
    digest = hashlib.sha256(IMAGE).hexdigest()
    assert f"[image sha256:{digest} image/png {len(IMAGE)} bytes]" in trajectory

    query(
        "SELECT sha256, width, height, blob_path FROM images",
        corpus_root=corpus,
        fmt=OutputFormat.JSON,
    )
    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {
            "sha256": digest,
            "width": 7,
            "height": 9,
            "blob_path": f"blobs/sha256/{digest[:2]}/{digest}.png",
        }
    ]
    assert (corpus / rows[0]["blob_path"]).read_bytes() == IMAGE
