# SPDX-License-Identifier: Apache-2.0

"""Raw source archive, empty sessions, generation staleness, and the pass clock.

Each class pins one rule the materialize use case gained when the corpus
stopped deleting sessions whose source expired:

* every live conversion leaves a verified zstd archive of its source files,
  and :func:`restore_session_sources` gives the original tree back;
* a transcript with nothing to convert is recorded as empty, not failed, and
  is tried again only when its source or the converter moves;
* a session whose recorded ``converter_schema`` (or, without one,
  ``converter_version``) or expected extra meta key differs from the running
  one is stale, and a source-removed one re-converts from its archive;
* "now" is read after the scan, so a file written during the scan never trips
  the future-mtime clock warning.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, override

import pytest
import zstandard
from corpus_fixtures import NOW_NS, SESSION_A, SESSION_B, STALE_NS, write_session
from loguru import logger

from atif_corpus.application import materialize as materialize_module
from atif_corpus.application.materialize import (
    MaterializationReport,
    expected_generation,
    materialize,
    preview_pass,
    read_empty_sessions,
)
from atif_corpus.domain.generation import generation_matches
from atif_corpus.domain.layout import CorpusLayout
from atif_corpus.domain.ports import ArchivedSource, ConversionOutput
from atif_corpus.domain.sessions import NANOS_PER_SECOND, QuiescencePolicy
from atif_corpus.infrastructure.fake_converter import FakeConverter
from atif_corpus.infrastructure.scanner import SourceScan, scan_sources
from atif_corpus.infrastructure.source_archive import (
    SourceArchiveError,
    archive_path_rejection,
    restore_session_sources,
)

MATERIALIZED_AT = "2026-01-02T00:00:00+00:00"


def run(
    source_root: Path,
    corpus_root: Path,
    converter: Any,
    *,
    converter_version: str = "0.1.0",
    converter_schema: int | None = None,
    expected_meta: Mapping[str, object] | None = None,
    force: bool = False,
    workers: int = 1,
    artifact_producer: Any = None,
    materialized_at: str = MATERIALIZED_AT,
    now_ns: int | None = NOW_NS,
) -> MaterializationReport:
    return materialize(
        source_root=source_root,
        corpus_root=corpus_root,
        converter=converter,
        materialized_at=materialized_at,
        harbor_version="0.22.0",
        converter_version=converter_version,
        now_ns=now_ns,
        force=force,
        workers=workers,
        expected_meta=expected_meta,
        artifact_producer=artifact_producer,
        converter_schema=converter_schema,
    )


def meta_of(corpus_root: Path, session_id: str) -> dict[str, Any]:
    meta = json.loads(
        CorpusLayout(corpus_root=corpus_root).meta_path(session_id).read_text(encoding="utf-8")
    )
    assert isinstance(meta, dict)
    return meta


def source_tree(root: Path) -> dict[str, bytes]:
    """``{relative posix path: bytes}`` for every regular file under ``root``."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def delete_session_source(source_root: Path, session_id: str) -> None:
    """What Claude Code's retention does: the transcript and its side dir go."""
    project = source_root / "-proj-a"
    (project / f"{session_id}.jsonl").unlink()
    shutil.rmtree(project / session_id, ignore_errors=True)


class TestSourceArchive:
    def test_every_source_file_is_archived_and_decompresses_to_the_original(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        originals = source_tree(source_root / "-proj-a")

        run(source_root, corpus_root, FakeConverter())

        archive_dir = CorpusLayout(corpus_root=corpus_root).source_archive_dir(SESSION_A)
        archived = {
            path.relative_to(archive_dir).as_posix().removesuffix(".zst"): (
                zstandard.ZstdDecompressor().decompressobj().decompress(path.read_bytes())
            )
            for path in sorted(archive_dir.rglob("*.zst"))
        }
        # Main transcript, both side-file transcripts, AND the meta.json sidecar
        # the converter never parses: the archive is the whole source tree.
        assert archived == originals
        assert f"{SESSION_A}/subagents/agent-aaaa.meta.json" in archived

    def test_meta_lists_the_archive_with_sizes_and_digests(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        originals = source_tree(source_root / "-proj-a")

        run(source_root, corpus_root, FakeConverter())

        manifest = meta_of(corpus_root, SESSION_A)["source_archive"]
        assert manifest["codec"] == "zstd"
        assert manifest["source_dir"] == "-proj-a"
        assert manifest["main"] == f"{SESSION_A}.jsonl"
        assert {entry["path"]: (entry["size"], entry["sha256"]) for entry in manifest["files"]} == {
            path: (len(data), hashlib.sha256(data).hexdigest()) for path, data in originals.items()
        }

    def test_restore_rebuilds_the_tree_after_the_source_is_gone(
        self, source_root: Path, corpus_root: Path, tmp_path: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        originals = {
            path: data for path, data in source_tree(source_root).items() if SESSION_A in path
        }
        run(source_root, corpus_root, FakeConverter())
        delete_session_source(source_root, SESSION_A)
        run(source_root, corpus_root, FakeConverter())

        dest = tmp_path / "restored"
        main = restore_session_sources(
            CorpusLayout(corpus_root=corpus_root).session_dir(SESSION_A), dest
        )

        assert main == dest / "-proj-a" / f"{SESSION_A}.jsonl"
        assert source_tree(dest) == originals

    def test_restore_refuses_bytes_that_do_not_match_the_manifest(
        self, source_root: Path, corpus_root: Path, tmp_path: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        member = (
            CorpusLayout(corpus_root=corpus_root).source_archive_dir(SESSION_A)
            / f"{SESSION_A}.jsonl.zst"
        )
        # A valid zstd frame of the wrong bytes: only the digest check catches it.
        member.write_bytes(zstandard.ZstdCompressor().compress(b'{"type":"user","uuid":"x"}\n'))

        with pytest.raises(SourceArchiveError, match="expected"):
            restore_session_sources(
                CorpusLayout(corpus_root=corpus_root).session_dir(SESSION_A), tmp_path / "r"
            )

    def test_restore_of_a_session_without_an_archive_says_so(
        self, source_root: Path, corpus_root: Path, tmp_path: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, NonArchivingConverter())
        with pytest.raises(SourceArchiveError, match="no source archive"):
            restore_session_sources(
                CorpusLayout(corpus_root=corpus_root).session_dir(SESSION_A), tmp_path / "r"
            )

    @pytest.mark.parametrize(
        "path",
        ["", "/etc/passwd", "../x.jsonl", "a/../../x", "a//b", "./a", "a\\b", "a\x00b"],
    )
    def test_manifest_paths_that_could_escape_are_rejected(self, path: str) -> None:
        assert archive_path_rejection(path) is not None

    def test_ordinary_manifest_paths_pass(self) -> None:
        assert archive_path_rejection(f"{SESSION_A}/subagents/agent-a.jsonl") is None

    def test_a_converter_that_lists_a_file_it_did_not_write_fails_the_session(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        report = run(source_root, corpus_root, LyingArchiveConverter())
        assert [f.session_id for f in report.failures] == [SESSION_A]
        assert "wrote no" in report.failures[0].error
        assert not CorpusLayout(corpus_root=corpus_root).session_dir(SESSION_A).exists()

    def test_a_converter_that_archives_nothing_leaves_no_archive_behind(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        report = run(source_root, corpus_root, NonArchivingConverter())
        assert report.materialized_count == 1
        assert "source_archive" not in meta_of(corpus_root, SESSION_A)
        assert not CorpusLayout(corpus_root=corpus_root).source_archive_dir(SESSION_A).exists()


class TestSharedCorpusDirectories:
    def test_nothing_outside_sessions_is_touched_by_retention_or_archiving(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """A shared content-addressed blob store lives at ``<corpus>/blobs/``. No
        pass may delete, move or write anything there, and the source archive
        stays inside the session dir."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        blob = corpus_root / "blobs" / "sha256" / "ab" / f"{'ab' * 32}.png"
        blob.parent.mkdir(parents=True)
        blob.write_bytes(b"\x89PNG not really")
        before = source_tree(corpus_root / "blobs")

        run(source_root, corpus_root, FakeConverter())
        delete_session_source(source_root, SESSION_A)
        run(source_root, corpus_root, FakeConverter())
        run(source_root, corpus_root, FakeConverter(), converter_version="0.2.0", workers=2)

        assert source_tree(corpus_root / "blobs") == before
        assert not list((corpus_root / "blobs").rglob("*.zst"))
        top_level = sorted(p.name for p in corpus_root.iterdir())
        assert top_level == [".staging", "blobs", "sessions", "watermark.json"]


class NonArchivingConverter(FakeConverter):
    """A port adapter that ignores ``archive_dir``, as a third-party one may."""

    @override
    def convert(self, session_jsonl: Path, *, archive_dir: Path | None = None) -> ConversionOutput:
        del archive_dir
        return super().convert(session_jsonl)


class LyingArchiveConverter(FakeConverter):
    """Claims an archive member it never wrote."""

    @override
    def convert(self, session_jsonl: Path, *, archive_dir: Path | None = None) -> ConversionOutput:
        output = super().convert(session_jsonl)
        return ConversionOutput(
            trajectory_dict=output.trajectory_dict,
            loss_report_dict=output.loss_report_dict,
            edges_lines=output.edges_lines,
            source_archive=(
                ArchivedSource(relative_path=session_jsonl.name, size=1, sha256="0" * 64),
            ),
        )


class TestEmptySessions:
    def test_an_empty_session_is_counted_empty_not_failed(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)

        report = run(source_root, corpus_root, FakeConverter(empty_sessions=frozenset({SESSION_A})))

        assert report.empty_session_ids == (SESSION_A,)
        assert report.failed_count == 0
        assert report.materialized_count == 1
        layout = CorpusLayout(corpus_root=corpus_root)
        assert not layout.session_dir(SESSION_A).exists()
        assert read_empty_sessions(layout.empty_sessions_path) == {
            SESSION_A: {"converter_version": "0.1.0"}
        }

    def test_a_recorded_empty_session_is_not_retried_while_nothing_moves(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter(empty_sessions=frozenset({SESSION_A})))

        converter = FakeConverter(empty_sessions=frozenset({SESSION_A}))
        report = run(source_root, corpus_root, converter)

        assert converter.converted == []
        assert report.empty_count == 0
        assert report.failed_count == 0
        assert report.up_to_date_count == 1

    def test_a_write_to_an_empty_session_converts_it(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        main = write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter(empty_sessions=frozenset({SESSION_A})))

        os.utime(main, ns=(STALE_NS + 1, STALE_NS + 1))
        report = run(source_root, corpus_root, FakeConverter())

        assert report.materialized_count == 1
        layout = CorpusLayout(corpus_root=corpus_root)
        assert layout.trajectory_path(SESSION_A).is_file()
        assert read_empty_sessions(layout.empty_sessions_path) == {}

    def test_a_converter_upgrade_retries_an_empty_session(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter(empty_sessions=frozenset({SESSION_A})))

        converter = FakeConverter()
        report = run(source_root, corpus_root, converter, converter_version="0.2.0")

        assert [p.stem for p in converter.converted] == [SESSION_A]
        assert report.materialized_count == 1

    def test_a_vanished_empty_session_leaves_the_record(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        main = write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter(empty_sessions=frozenset({SESSION_A})))
        main.unlink()
        run(source_root, corpus_root, FakeConverter())
        layout = CorpusLayout(corpus_root=corpus_root)
        assert read_empty_sessions(layout.empty_sessions_path) == {}


class TestGenerationStaleness:
    def test_a_converter_version_bump_reconverts_every_session(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter(output_tag="old"))

        report = run(
            source_root, corpus_root, FakeConverter(output_tag="new"), converter_version="0.2.0"
        )

        assert report.materialized_count == 2
        for session_id in (SESSION_A, SESSION_B):
            assert meta_of(corpus_root, session_id)["converter_version"] == "0.2.0"
            trajectory = json.loads(
                CorpusLayout(corpus_root=corpus_root).trajectory_path(session_id).read_text()
            )
            assert trajectory["converter_tag"] == "new"

    def test_the_same_version_stays_up_to_date(self, source_root: Path, corpus_root: Path) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        report = run(source_root, corpus_root, FakeConverter())
        assert report.materialized_count == 0
        assert report.up_to_date_count == 1

    def test_a_missing_expected_meta_key_reconverts_once(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """A --no-columnar session meets a columnar pass: converted once, then current."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        producer = SchemaProducer(schema=1)

        first = run(
            source_root,
            corpus_root,
            FakeConverter(),
            expected_meta={"columnar_schema": 1},
            artifact_producer=producer,
        )
        second = run(
            source_root,
            corpus_root,
            FakeConverter(),
            expected_meta={"columnar_schema": 1},
            artifact_producer=producer,
        )

        assert first.materialized_count == 1
        assert second.materialized_count == 0
        assert meta_of(corpus_root, SESSION_A)["columnar_schema"] == 1

    def test_a_schema_bump_reconverts(self, source_root: Path, corpus_root: Path) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(
            source_root,
            corpus_root,
            FakeConverter(),
            expected_meta={"columnar_schema": 1},
            artifact_producer=SchemaProducer(schema=1),
        )
        report = run(
            source_root,
            corpus_root,
            FakeConverter(),
            expected_meta={"columnar_schema": 2},
            artifact_producer=SchemaProducer(schema=2),
        )
        assert report.materialized_count == 1

    def test_a_pass_that_expects_no_schema_leaves_columnar_sessions_alone(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(
            source_root,
            corpus_root,
            FakeConverter(),
            expected_meta={"columnar_schema": 1},
            artifact_producer=SchemaProducer(schema=1),
        )
        assert run(source_root, corpus_root, FakeConverter()).materialized_count == 0

    def test_an_unreadable_meta_is_stale(self, source_root: Path, corpus_root: Path) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        CorpusLayout(corpus_root=corpus_root).meta_path(SESSION_A).write_text("{not json")
        assert run(source_root, corpus_root, FakeConverter()).materialized_count == 1

    def test_the_converter_schema_is_stamped_and_decides(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """With a schema number, a release that changes no output re-converts
        nothing, and a schema bump re-converts everything."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter(), converter_schema=1)
        assert meta_of(corpus_root, SESSION_A)["converter_schema"] == 1

        new_release = run(
            source_root, corpus_root, FakeConverter(), converter_version="0.9.0", converter_schema=1
        )
        bumped = run(
            source_root, corpus_root, FakeConverter(), converter_version="0.9.0", converter_schema=2
        )

        assert new_release.materialized_count == 0
        assert bumped.materialized_count == 1
        assert meta_of(corpus_root, SESSION_A)["converter_schema"] == 2

    def test_a_session_from_before_the_schema_key_is_stale(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """The live corpus has no converter_schema anywhere; the first pass after
        deploy has to reach every session."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        report = run(source_root, corpus_root, FakeConverter(), converter_schema=1)
        assert report.materialized_count == 1

    def test_expected_meta_may_not_contradict_the_stamped_version(self) -> None:
        with pytest.raises(ValueError, match="converter_version"):
            expected_generation("0.2.0", {"converter_version": "0.1.0"})
        with pytest.raises(ValueError, match="converter_schema"):
            expected_generation("0.2.0", {"converter_schema": 1}, converter_schema=2)

    def test_generation_matches_only_compares_expected_keys(self) -> None:
        recorded = {"converter_version": "1", "columnar_schema": 1, "other": "x"}
        assert generation_matches(recorded, {"converter_version": "1"})
        assert not generation_matches(recorded, {"converter_version": "2"})
        assert not generation_matches({"converter_version": "1"}, {"columnar_schema": 1})


class SchemaProducer:
    """Stamps ``columnar_schema`` the way atif-duck's producer does, writing nothing."""

    def __init__(self, *, schema: int) -> None:
        self.schema = schema

    def produce(
        self, session_dir: Path, *, session_id: str, trajectory: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        del session_dir, session_id, trajectory
        return {"columnar_schema": self.schema}


class TestArchiveReconversion:
    def _retained(self, source_root: Path, corpus_root: Path) -> dict[str, Any]:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter(output_tag="old"))
        delete_session_source(source_root, SESSION_A)
        run(source_root, corpus_root, FakeConverter(), materialized_at="2026-01-03T00:00:00+00:00")
        return meta_of(corpus_root, SESSION_A)

    def test_a_stale_source_removed_session_reconverts_from_its_archive(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        before = self._retained(source_root, corpus_root)
        layout = CorpusLayout(corpus_root=corpus_root)
        archive_before = source_tree(layout.source_archive_dir(SESSION_A))
        converter = FakeConverter(output_tag="new")

        report = run(source_root, corpus_root, converter, converter_version="0.2.0")

        assert report.archive_session_ids == (SESSION_A,)
        assert report.failed_count == 0
        # The live session B re-converts too; A came from its archive.
        assert report.materialized_count == 2
        restored_from = next(p for p in converter.converted if p.stem == SESSION_A)
        assert ".staging" in restored_from.parts
        assert restored_from.parent.name == "-proj-a"
        after = meta_of(corpus_root, SESSION_A)
        assert after["converter_version"] == "0.2.0"
        assert after["source_present"] is False
        for key in ("source_removed_at", "source_files", "source_mtime_ns", "source_archive"):
            assert after[key] == before[key], key
        trajectory = json.loads(layout.trajectory_path(SESSION_A).read_text())
        assert trajectory["converter_tag"] == "new"
        assert source_tree(layout.source_archive_dir(SESSION_A)) == archive_before
        assert not list(layout.staging_dir.iterdir())

    def test_a_current_source_removed_session_is_left_alone(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        self._retained(source_root, corpus_root)
        converter = FakeConverter()
        report = run(source_root, corpus_root, converter)
        assert report.archive_session_ids == ()
        assert converter.converted == []

    def test_a_source_removed_session_without_an_archive_keeps_its_artifacts(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """Sessions retained before archives existed cannot re-convert; they stay."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, NonArchivingConverter())
        delete_session_source(source_root, SESSION_A)
        run(source_root, corpus_root, FakeConverter())

        report = run(source_root, corpus_root, FakeConverter(), converter_version="0.2.0")

        assert report.archive_session_ids == ()
        assert report.failed_count == 0
        assert report.retained_count == 1
        assert meta_of(corpus_root, SESSION_A)["converter_version"] == "0.1.0"

    def test_archive_reconversion_runs_on_the_pool(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        self._retained(source_root, corpus_root)
        report = run(
            source_root,
            corpus_root,
            FakeConverter(output_tag="pool"),
            converter_version="0.2.0",
            workers=2,
        )
        assert report.workers == 2
        assert report.archive_session_ids == (SESSION_A,)
        assert meta_of(corpus_root, SESSION_A)["converter_version"] == "0.2.0"

    def test_dead_restore_residue_is_swept(self, source_root: Path, corpus_root: Path) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        residue = CorpusLayout(corpus_root=corpus_root).staging_dir / f"{SESSION_A}.src-999999"
        (residue / "-proj-a").mkdir(parents=True)
        run(source_root, corpus_root, FakeConverter())
        assert not residue.exists()

    def test_status_preview_counts_what_the_pass_would_do(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        self._retained(source_root, corpus_root)
        preview = preview_pass(
            source_root=source_root,
            corpus_root=corpus_root,
            converter_version="0.2.0",
            now_ns=NOW_NS,
        )
        assert preview.retained_session_ids == (SESSION_A,)
        assert preview.archive_session_ids == (SESSION_A,)
        assert preview.generation_stale_session_ids == (SESSION_B,)
        assert [s.session_id for s in preview.plan.to_materialize] == [SESSION_B]


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    """Every WARNING-or-higher loguru message emitted while the test runs."""
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    yield messages
    logger.remove(sink_id)


class TestPassClock:
    def test_a_file_written_during_the_scan_does_not_warn_about_the_clock(
        self,
        source_root: Path,
        corpus_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        warnings: list[str],
    ) -> None:
        """The scan is slow on a real corpus; a transcript appended while it runs
        carries an mtime later than any "now" read before the scan started.

        The clock is scripted so the write lands three seconds after the pass
        began, well past the one-second warning tolerance: only the ORDER of
        the clock read and the scan decides whether this warns.
        """
        main = write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        started = NOW_NS
        scanned = {"done": False}

        def clock() -> int:
            return started + 5 * NANOS_PER_SECOND if scanned["done"] else started

        def scan_during_a_write(root: Path, layout: Any) -> SourceScan:
            written = started + 3 * NANOS_PER_SECOND
            os.utime(main, ns=(written, written))
            scan = scan_sources(root, layout)
            scanned["done"] = True
            return scan

        monkeypatch.setattr(time, "time_ns", clock)
        monkeypatch.setattr(materialize_module, "scan_sources", scan_during_a_write)
        report = run(source_root, corpus_root, FakeConverter(), now_ns=None)

        assert report.skipped_live_count == 1
        assert not [m for m in warnings if "FUTURE" in m]

    def test_sub_second_future_mtimes_do_not_warn(self, warnings: list[str]) -> None:
        policy = QuiescencePolicy(quiesce_seconds=300)
        assert not policy.is_quiescent(NOW_NS + NANOS_PER_SECOND // 2, NOW_NS)
        assert not [m for m in warnings if "FUTURE" in m]

    def test_a_genuinely_future_mtime_still_warns(self, warnings: list[str]) -> None:
        policy = QuiescencePolicy(quiesce_seconds=300)
        assert not policy.is_quiescent(NOW_NS + 5 * NANOS_PER_SECOND, NOW_NS)
        assert [m for m in warnings if "FUTURE" in m]
