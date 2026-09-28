# SPDX-License-Identifier: Apache-2.0

"""The compressed artifacts: what the writer stores, and slim's in-place compression.

Decompressed, a ``.zst`` artifact is byte for byte the plain file the writer
used to store, and its frame records that size. ``compress_in_place`` gives
an old-layout file the same bytes a fresh materialize would store, reads its
copy back before the plain file goes, and keeps the plain file's mtime.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import zstandard

from atif_corpus.domain.layout import (
    COMPRESSED_ARTIFACT_FILENAMES,
    COMPRESSED_SUFFIX,
    stored_filename,
)
from atif_corpus.infrastructure import compress_artifacts as compress_mod
from atif_corpus.infrastructure.atomic import (
    write_json_atomic,
    write_json_zstd_atomic,
    write_text_atomic,
    write_text_zstd_atomic,
)
from atif_corpus.infrastructure.compress_artifacts import (
    CompressionMismatchError,
    compress_in_place,
    measure_compressed,
    plain_artifacts,
)

DOC = {
    "schema_version": "ATIF-v1.7",
    "steps": [{"step_id": i, "message": "héllo " * (i % 7), "n": i / 3} for i in range(500)],
}


def _decompress(path: Path) -> bytes:
    with path.open("rb") as handle, zstandard.ZstdDecompressor().stream_reader(handle) as reader:
        return reader.readall()


def test_only_the_bulk_artifacts_are_stored_compressed() -> None:
    assert COMPRESSED_ARTIFACT_FILENAMES == (
        "trajectory.json",
        "edges.jsonl",
        "session_events.jsonl",
    )
    assert stored_filename("trajectory.json") == "trajectory.json.zst"
    assert stored_filename("meta.json") == "meta.json"
    assert stored_filename("loss_report.json") == "loss_report.json"


@pytest.mark.parametrize("compact", [True, False])
def test_the_json_writer_stores_the_plain_bytes_compressed(tmp_path: Path, compact: bool) -> None:
    write_json_atomic(tmp_path / "plain.json", DOC, compact=compact)
    write_json_zstd_atomic(tmp_path / "doc.json.zst", DOC, compact=compact)
    plain = (tmp_path / "plain.json").read_bytes()
    stored = (tmp_path / "doc.json.zst").read_bytes()
    assert _decompress(tmp_path / "doc.json.zst") == plain
    assert zstandard.frame_content_size(stored[:18]) == len(plain)
    assert len(stored) < len(plain)
    assert [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name] == []


def test_the_text_writer_stores_the_plain_bytes_compressed(tmp_path: Path) -> None:
    text = "".join(json.dumps({"uuid": f"u-{i}"}) + "\n" for i in range(200))
    write_text_atomic(tmp_path / "edges.jsonl", text)
    write_text_zstd_atomic(tmp_path / "edges.jsonl.zst", text)
    assert _decompress(tmp_path / "edges.jsonl.zst") == (tmp_path / "edges.jsonl").read_bytes()
    write_text_zstd_atomic(tmp_path / "empty.jsonl.zst", "")
    assert _decompress(tmp_path / "empty.jsonl.zst") == b""


def test_a_failed_serialization_leaves_no_destination(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        write_json_zstd_atomic(tmp_path / "doc.json.zst", {"bad": object()}, compact=True)
    assert list(tmp_path.iterdir()) == []


class TestCompressInPlace:
    def _plain(self, tmp_path: Path) -> Path:
        path = tmp_path / "trajectory.json"
        write_json_atomic(path, DOC, compact=True)
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_123_456_789))
        return path

    def test_it_stores_what_the_writer_stores_and_keeps_the_mtime(self, tmp_path: Path) -> None:
        plain = self._plain(tmp_path)
        original = plain.read_bytes()
        done = compress_in_place(plain)
        target = tmp_path / f"trajectory.json{COMPRESSED_SUFFIX}"
        assert not plain.exists()
        assert _decompress(target) == original
        assert target.stat().st_mtime_ns == 1_700_000_000_123_456_789
        assert (done.plain_bytes, done.compressed_bytes) == (len(original), target.stat().st_size)
        # A freshly converted session stores the same bytes for the same document.
        write_json_zstd_atomic(tmp_path / "fresh.json.zst", DOC, compact=True)
        assert (tmp_path / "fresh.json.zst").read_bytes() == target.read_bytes()

    def test_a_dry_run_measures_exactly_and_writes_nothing(self, tmp_path: Path) -> None:
        plain = self._plain(tmp_path)
        measured = measure_compressed(plain)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["trajectory.json"]
        assert measured.compressed_bytes == compress_in_place(plain).compressed_bytes

    def test_a_copy_that_reads_back_differently_leaves_the_plain_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain = self._plain(tmp_path)
        original = plain.read_bytes()

        def wrong_digest(path: Path) -> str:
            del path
            return "0" * 64

        monkeypatch.setattr(compress_mod, "_sha256_decompressed", wrong_digest)
        with pytest.raises(CompressionMismatchError):
            compress_in_place(plain)
        assert plain.read_bytes() == original
        assert sorted(p.name for p in tmp_path.iterdir()) == ["trajectory.json"]

    def test_plain_artifacts_lists_only_the_compressible_plain_files(self, tmp_path: Path) -> None:
        for name in ("trajectory.json", "edges.jsonl", "meta.json", "loss_report.json"):
            (tmp_path / name).write_text("{}")
        (tmp_path / "session_events.jsonl.zst").write_bytes(b"")
        assert plain_artifacts(tmp_path) == [tmp_path / "trajectory.json", tmp_path / "edges.jsonl"]
