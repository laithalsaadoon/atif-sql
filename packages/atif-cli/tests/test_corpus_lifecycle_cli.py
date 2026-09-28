# SPDX-License-Identifier: Apache-2.0

"""Retention, archive, empties and version staleness through the REAL converter.

The atif-corpus suite pins these rules against a fake converter; this one runs
them through ``atif-sql materialize`` / ``status`` / ``query`` with harbor in
the loop, which is where the version pin, the archive-from-the-verifying-read
seam and the ``EmptySessionError`` translation actually live.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
import zstandard
from cli_fixtures import read_artifact_bytes, write_synthetic_session

from atif_cli.app import materialize, query, status
from atif_cli.converter_adapter import RealConverter
from atif_cli.output import OutputFormat
from atif_converter.domain import schema_version as schema_version_module
from atif_converter.domain.errors import EmptySessionError
from atif_converter.domain.schema_version import CONVERTER_SCHEMA_VERSION
from atif_converter.infrastructure import source_archive as converter_archive
from atif_corpus.domain.ports import EmptySourceError
from atif_corpus.infrastructure import source_archive as corpus_archive

SESSION_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SESSION_EMPTY = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def _materialize(
    source_root: Path, corpus_root: Path, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    materialize(source_root=source_root, corpus_root=corpus_root, workers=1, fmt=OutputFormat.JSON)
    report = json.loads(capsys.readouterr().out)
    assert isinstance(report, dict)
    return report


def _status(
    source_root: Path, corpus_root: Path, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    status(source_root=source_root, corpus_root=corpus_root, fmt=OutputFormat.JSON)
    reported = json.loads(capsys.readouterr().out)
    assert isinstance(reported, dict)
    return reported


def _meta(corpus_root: Path, session_id: str) -> dict[str, Any]:
    meta = json.loads((corpus_root / "sessions" / session_id / "meta.json").read_text())
    assert isinstance(meta, dict)
    return meta


def _write_empty_session(source_root: Path, session_id: str) -> Path:
    """A well-formed transcript with no user or assistant record."""
    main = source_root / "-tmp-proj" / f"{session_id}.jsonl"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(json.dumps({"type": "summary", "summary": "s", "leafUuid": "x"}) + "\n")
    stale = time.time_ns() - 3_600 * 1_000_000_000
    os.utime(main, ns=(stale, stale))
    return main


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.delenv("ATIF_SQL_CORPUS_ROOT", raising=False)
    monkeypatch.delenv("ATIF_SQL_SOURCE_ROOT", raising=False)
    source_root = tmp_path / "projects"
    write_synthetic_session(source_root, SESSION_A)
    write_synthetic_session(source_root, SESSION_B)
    return source_root, tmp_path / "corpus"


@pytest.mark.integration
class TestLifecycleThroughTheRealConverter:
    def test_meta_carries_a_real_release_and_the_converter_schema(
        self, roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        source_root, corpus_root = roots
        _materialize(source_root, corpus_root, capsys)
        meta = _meta(corpus_root, SESSION_A)
        assert meta["converter_version"] == importlib.metadata.version("atif-sql")
        assert meta["converter_version"] != "unknown"
        assert meta["converter_schema"] == CONVERTER_SCHEMA_VERSION

    def test_the_archive_holds_the_parsed_transcript(
        self, roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        source_root, corpus_root = roots
        original = (source_root / "-tmp-proj" / f"{SESSION_A}.jsonl").read_bytes()
        _materialize(source_root, corpus_root, capsys)

        member = corpus_root / "sessions" / SESSION_A / "source" / f"{SESSION_A}.jsonl.zst"
        restored = zstandard.ZstdDecompressor().decompressobj().decompress(member.read_bytes())
        assert restored == original
        manifest = _meta(corpus_root, SESSION_A)["source_archive"]
        assert manifest["source_dir"] == "-tmp-proj"
        assert [entry["path"] for entry in manifest["files"]] == [f"{SESSION_A}.jsonl"]

    def test_a_deleted_source_stays_queryable_and_is_reported(
        self, roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        source_root, corpus_root = roots
        _materialize(source_root, corpus_root, capsys)
        (source_root / "-tmp-proj" / f"{SESSION_A}.jsonl").unlink()

        report = _materialize(source_root, corpus_root, capsys)

        assert report["removed"] == 1
        assert report["removed_session_ids"] == [SESSION_A]
        assert report["retained"] == 1
        assert _meta(corpus_root, SESSION_A)["source_present"] is False
        query("SELECT count(*) AS n FROM sessions", corpus_root=corpus_root, fmt=OutputFormat.JSON)
        assert json.loads(capsys.readouterr().out) == [{"n": 2}]
        reported = _status(source_root, corpus_root, capsys)
        assert reported["retained_sessions"] == 1
        assert reported["materialized_sessions"] == 2

    def test_an_empty_transcript_is_empty_not_failed_and_not_retried(
        self, roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        source_root, corpus_root = roots
        _write_empty_session(source_root, SESSION_EMPTY)

        first = _materialize(source_root, corpus_root, capsys)
        second = _materialize(source_root, corpus_root, capsys)

        assert first["failed"] == 0
        assert first["empty"] == 1
        assert first["empty_session_ids"] == [SESSION_EMPTY]
        assert second["failed"] == 0
        assert second["empty"] == 0
        assert second["materialized"] == 0
        assert _status(source_root, corpus_root, capsys)["empty_sessions"] == 1

    def test_a_version_bump_reconverts_live_and_archived_sessions(
        self,
        roots: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source_root, corpus_root = roots
        _materialize(source_root, corpus_root, capsys)
        (source_root / "-tmp-proj" / f"{SESSION_A}.jsonl").unlink()
        _materialize(source_root, corpus_root, capsys)
        trajectory_before = read_artifact_bytes(
            corpus_root / "sessions" / SESSION_A / "trajectory.json.zst"
        )

        monkeypatch.setattr(schema_version_module, "CONVERTER_SCHEMA_VERSION", 99)
        assert _status(source_root, corpus_root, capsys)["staleness"]["generation_stale"] == 1
        report = _materialize(source_root, corpus_root, capsys)

        assert report["failed"] == 0
        assert report["materialized"] == 2
        assert report["from_archive_session_ids"] == [SESSION_A]
        for session_id in (SESSION_A, SESSION_B):
            assert _meta(corpus_root, session_id)["converter_schema"] == 99
        # Converted from the restored archive by the real converter: same input,
        # same converter code, so the same trajectory bytes.
        trajectory_after = read_artifact_bytes(
            corpus_root / "sessions" / SESSION_A / "trajectory.json.zst"
        )
        assert trajectory_after == trajectory_before
        assert _meta(corpus_root, SESSION_A)["source_present"] is False
        assert _materialize(source_root, corpus_root, capsys)["materialized"] == 0


class TestAdapterSeams:
    def test_empty_session_error_becomes_the_ports_empty_source_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _empty(path: Path, **_kwargs: object) -> Any:
            msg = f"no convertible events in {path}"
            raise EmptySessionError(msg)

        monkeypatch.setattr("atif_cli.converter_adapter.convert_and_audit", _empty)
        with pytest.raises(EmptySourceError, match="no convertible events"):
            RealConverter().convert(Path("x.jsonl"))

    def test_the_archive_suffix_twins_agree(self) -> None:
        """The converter writes ``<path>.zst`` and the corpus reads ``<path>.zst``;
        the two packages may not import each other, so the one place that sees
        both pins them together."""
        assert converter_archive.ARCHIVE_SUFFIX == corpus_archive.ARCHIVE_SUFFIX
