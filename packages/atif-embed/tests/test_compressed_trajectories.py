# SPDX-License-Identifier: Apache-2.0

"""The per-session reader over ``trajectory.json.zst`` yields the plain corpus's rows.

Batches are sized by the decompressed bytes (from the frame header), not by the
compressed file's size, so a batch still bounds what one statement holds.
"""

from __future__ import annotations

from pathlib import Path

import zstandard
from embed_fixtures import write_corpus

from atif_embed.infrastructure.corpus_text_rows import DuckDbTextRows, _complete_trajectory_paths


def _compress(root: Path) -> None:
    for session_dir in sorted((root / "sessions").iterdir()):
        plain = session_dir / "trajectory.json"
        if plain.is_file():
            (session_dir / "trajectory.json.zst").write_bytes(
                zstandard.ZstdCompressor(write_content_size=True).compress(plain.read_bytes())
            )
            plain.unlink()


def _rows(root: Path) -> list[tuple[str, str]]:
    return [(p.uuid, p.text) for p in DuckDbTextRows().iter_unembedded(root)]


def test_compressed_trajectories_yield_the_same_rows(tmp_path: Path) -> None:
    root = write_corpus(tmp_path / "corpus", torn_session=True)
    expected = _rows(root)
    sizes = dict(_complete_trajectory_paths(root))
    assert expected
    _compress(root)
    assert _rows(root) == expected
    compressed = _complete_trajectory_paths(root)
    assert all(path.endswith("trajectory.json.zst") for path, _ in compressed)
    # The batch budget counts decompressed bytes.
    assert [size for _, size in compressed] == [
        sizes[path.removesuffix(".zst")] for path, _ in compressed
    ]
