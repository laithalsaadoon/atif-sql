# SPDX-License-Identifier: Apache-2.0

"""The file source over the compressed layout reads what it reads over the plain one.

materialize stores ``trajectory.json`` and ``edges.jsonl`` as ``<name>.zst``;
a corpus written before that keeps the plain files until ``atif-sql corpus
slim`` compresses them, keeping each file's mtime. The pipelines' checkpoints
key on the session bounds (last edge time, trajectory mtime), so the bounds,
the steps and the edge uuids must all come out the same.
"""

from __future__ import annotations

import os
from pathlib import Path

import zstandard
from analytics_fixtures import build_fixture_corpus

from atif_analytics.infrastructure.corpus_reader import TrajectoryFileSource, stored_file

_COMPRESSED = ("trajectory.json", "edges.jsonl")


def _compress_in_place(session_dir: Path, *, keep_plain: bool = False) -> None:
    for name in _COMPRESSED:
        plain = session_dir / name
        st = plain.stat()
        target = session_dir / f"{name}.zst"
        target.write_bytes(zstandard.ZstdCompressor().compress(plain.read_bytes()))
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        if not keep_plain:
            plain.unlink()


def _snapshot(corpus_root: Path) -> tuple[object, ...]:
    source = TrajectoryFileSource(corpus_root)
    bounds = source.session_bounds()
    ids = sorted(bounds)
    return (
        bounds,
        source.load_steps(ids),
        {sid: source.edges_uuids(sid) for sid in ids},
    )


def test_a_compressed_corpus_reads_like_the_plain_one(tmp_path: Path) -> None:
    root = build_fixture_corpus(tmp_path / "corpus")
    expected = _snapshot(root)
    assert expected[1], "fixture has sessions with steps"
    for session_dir in sorted((root / "sessions").iterdir()):
        _compress_in_place(session_dir)
        assert stored_file(session_dir, "trajectory.json") == session_dir / "trajectory.json.zst"
    assert _snapshot(root) == expected


def test_a_session_caught_mid_slim_reads_once(tmp_path: Path) -> None:
    root = build_fixture_corpus(tmp_path / "corpus")
    expected = _snapshot(root)
    for session_dir in sorted((root / "sessions").iterdir()):
        _compress_in_place(session_dir, keep_plain=True)
    assert _snapshot(root) == expected
