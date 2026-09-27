# SPDX-License-Identifier: Apache-2.0

"""The shared content-addressed blob store the converter's attachments land in.

Pins the contract ``_store_blobs`` offers: one file per hash under
``blobs/sha256/<ab>/``, shared across sessions (stored once), written before
the session that references it publishes, read-only, and never built from a
name that is not a hash.
"""

from __future__ import annotations

import dataclasses
import hashlib
import stat
from pathlib import Path
from typing import override

import pytest
from corpus_fixtures import NOW_NS, SESSION_A, SESSION_B, STALE_NS, write_session

from atif_corpus.application.materialize import materialize
from atif_corpus.domain.layout import CorpusLayout, InvalidBlobNameError
from atif_corpus.domain.ports import BlobOutput, ConversionOutput
from atif_corpus.infrastructure.atomic import write_blob_atomic
from atif_corpus.infrastructure.fake_converter import FakeConverter

SHARED = b"\x89PNG shared screenshot"
ONLY_B = b"\x89PNG only in b"


def _blob(data: bytes) -> BlobOutput:
    return BlobOutput(sha256=hashlib.sha256(data).hexdigest(), extension="png", data=data)


class BlobConverter(FakeConverter):
    """The fake converter, plus attachments: both sessions share one image."""

    @override
    def convert(self, session_jsonl: Path, *, archive_dir: Path | None = None) -> ConversionOutput:
        base = super().convert(session_jsonl, archive_dir=archive_dir)
        blobs = (
            (_blob(SHARED),) if session_jsonl.stem == SESSION_A else (_blob(SHARED), _blob(ONLY_B))
        )
        return dataclasses.replace(base, blobs=blobs)


def _run(source_root: Path, corpus_root: Path) -> None:
    report = materialize(
        source_root=source_root,
        corpus_root=corpus_root,
        converter=BlobConverter(),
        now_ns=NOW_NS,
        materialized_at="2026-01-02T00:00:00+00:00",
        harbor_version="0.22.0",
        converter_version="0.1.0",
    )
    assert report.failed_count == 0


def test_blobs_are_stored_once_per_hash_across_sessions(
    source_root: Path, corpus_root: Path
) -> None:
    write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
    write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
    _run(source_root, corpus_root)

    layout = CorpusLayout(corpus_root=corpus_root)
    stored = sorted(p for p in layout.blobs_dir.rglob("*") if p.is_file())
    assert stored == sorted(
        layout.blob_path(_blob(data).sha256, "png") for data in (SHARED, ONLY_B)
    )
    shared = layout.blob_path(_blob(SHARED).sha256, "png")
    assert shared.read_bytes() == SHARED
    assert shared.parent.name == _blob(SHARED).sha256[:2]
    assert stat.S_IMODE(shared.stat().st_mode) == 0o444
    # Outside sessions/: the per-session swap and every reader glob skip it.
    assert layout.sessions_dir not in shared.parents


def test_a_second_pass_rewrites_nothing(source_root: Path, corpus_root: Path) -> None:
    write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
    _run(source_root, corpus_root)
    layout = CorpusLayout(corpus_root=corpus_root)
    path = layout.blob_path(_blob(SHARED).sha256, "png")
    before = path.stat().st_mtime_ns
    assert write_blob_atomic(path, SHARED) is False
    assert path.stat().st_mtime_ns == before


def test_a_torn_blob_is_replaced(corpus_root: Path) -> None:
    layout = CorpusLayout(corpus_root=corpus_root)
    path = layout.blob_path(_blob(SHARED).sha256, "png")
    path.parent.mkdir(parents=True)
    path.write_bytes(SHARED[:3])
    assert write_blob_atomic(path, SHARED) is True
    assert path.read_bytes() == SHARED
    assert [p.name for p in path.parent.iterdir()] == [path.name], "no tmp file left behind"


def test_blobs_land_before_the_session_publishes(
    source_root: Path, corpus_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the moment of the directory swap, every blob the session names is on disk."""
    from atif_corpus.application import materialize as materialize_module

    layout = CorpusLayout(corpus_root=corpus_root)
    expected = layout.blob_path(_blob(SHARED).sha256, "png")
    observed: list[bool] = []
    real_swap = materialize_module.replace_dir_atomic

    def checking_swap(tmp_dir: Path, dst_dir: Path) -> None:
        observed.append(expected.is_file())
        real_swap(tmp_dir, dst_dir)

    monkeypatch.setattr(materialize_module, "replace_dir_atomic", checking_swap)
    write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
    _run(source_root, corpus_root)
    assert observed == [True]


@pytest.mark.parametrize(
    ("sha256", "extension"),
    [
        ("../" + "0" * 61, "png"),
        ("A" * 64, "png"),
        ("0" * 63, "png"),
        ("0" * 64, "p/ng"),
        ("0" * 64, ""),
    ],
)
def test_a_name_that_is_not_a_hash_never_becomes_a_path(
    corpus_root: Path, sha256: str, extension: str
) -> None:
    with pytest.raises(InvalidBlobNameError):
        CorpusLayout(corpus_root=corpus_root).blob_path(sha256, extension)
