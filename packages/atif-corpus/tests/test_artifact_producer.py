# SPDX-License-Identifier: Apache-2.0

"""The ``ArtifactProducer`` port: where it runs, what it may add, how it fails.

atif-corpus knows nothing about what a producer writes (atif-cli plugs in
atif-duck's columnar writer), so these tests use a recording fake and pin the
CONTRACT the use case offers any producer: it runs inside the staged dir after
the three JSON artifacts and before ``meta.json``, its files publish in the
same directory swap, its returned keys land in ``meta.json``, a raised
exception fails the session without touching the old generation, and a key
collision with the contract's own meta keys is an error.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from corpus_fixtures import NOW_NS, SESSION_A, SESSION_B, STALE_NS, write_session

from atif_corpus.application.materialize import MaterializationReport, materialize
from atif_corpus.domain.layout import CorpusLayout
from atif_corpus.infrastructure.fake_converter import FakeConverter

VERSIONS = {
    "materialized_at": "2026-01-02T00:00:00+00:00",
    "harbor_version": "0.22.0",
    "converter_version": "0.1.0",
}


class RecordingProducer:
    """A producer that writes one marker file and records what it was handed."""

    def __init__(
        self,
        *,
        extras: Mapping[str, Any] | None = None,
        fail_sessions: frozenset[str] = frozenset(),
    ) -> None:
        self.extras = dict(extras) if extras is not None else {"columnar_schema": 1}
        self.fail_sessions = fail_sessions
        #: (session_dir, session_id, step count) per call, in call order.
        self.calls: list[tuple[Path, str, int]] = []

    def produce(
        self,
        session_dir: Path,
        *,
        session_id: str,
        trajectory: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        # The contract the fake asserts on every call: staged dir, JSON
        # artifacts already there, meta.json not yet.
        assert ".staging" in session_dir.parts
        assert (session_dir / "trajectory.json").is_file()
        assert (session_dir / "edges.jsonl").is_file()
        assert (session_dir / "loss_report.json").is_file()
        assert not (session_dir / "meta.json").exists()
        assert json.loads((session_dir / "trajectory.json").read_text()) == trajectory
        self.calls.append((session_dir, session_id, len(trajectory["steps"])))
        (session_dir / "extra.parquet").write_text(session_id)
        if session_id in self.fail_sessions:
            msg = f"scripted producer failure for {session_id}"
            raise RuntimeError(msg)
        return self.extras


def run(
    source_root: Path,
    corpus_root: Path,
    producer: RecordingProducer | None,
    *,
    force: bool = False,
) -> MaterializationReport:
    return materialize(
        source_root=source_root,
        corpus_root=corpus_root,
        converter=FakeConverter(),
        now_ns=NOW_NS,
        force=force,
        artifact_producer=producer,
        materialized_at=VERSIONS["materialized_at"],
        harbor_version=VERSIONS["harbor_version"],
        converter_version=VERSIONS["converter_version"],
    )


class TestProducerRuns:
    def test_extra_file_publishes_with_the_session_and_extras_land_in_meta(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        producer = RecordingProducer(extras={"columnar_schema": 7, "note": "x"})

        report = run(source_root, corpus_root, producer)

        assert report.materialized_count == 1
        assert report.failed_count == 0
        layout = CorpusLayout(corpus_root=corpus_root)
        published = layout.session_dir(SESSION_A)
        assert (published / "extra.parquet").read_text() == SESSION_A
        meta = json.loads(layout.meta_path(SESSION_A).read_text())
        assert meta["columnar_schema"] == 7
        assert meta["note"] == "x"
        # The contract keys are untouched by the merge.
        assert meta["session_id"] == SESSION_A
        assert meta["agent"] == "claude-code"
        assert [(sid, n) for _, sid, n in producer.calls] == [(SESSION_A, 0)]
        # Nothing staged is left behind under the readers' tree.
        assert sorted(p.name for p in layout.sessions_dir.iterdir()) == [SESSION_A]

    def test_report_carries_artifact_seconds_only_when_a_producer_ran(
        self, source_root: Path, corpus_root: Path, tmp_path: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        with_producer = run(source_root, corpus_root, RecordingProducer())
        assert with_producer.artifact_seconds > 0.0

        without = run(source_root, tmp_path / "plain", None)
        assert without.artifact_seconds == 0.0
        assert not (tmp_path / "plain" / "sessions" / SESSION_A / "extra.parquet").exists()

    def test_producer_is_not_called_for_up_to_date_sessions(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, RecordingProducer())

        second = RecordingProducer()
        report = run(source_root, corpus_root, second)
        assert report.up_to_date_count == 2
        assert second.calls == []

        forced = RecordingProducer()
        report = run(source_root, corpus_root, forced, force=True)
        assert report.materialized_count == 2
        assert sorted(sid for _, sid, _ in forced.calls) == [SESSION_A, SESSION_B]


class TestProducerFailures:
    def test_raising_producer_fails_the_session_and_keeps_the_old_generation(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        main = write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, None)
        layout = CorpusLayout(corpus_root=corpus_root)
        old_meta = layout.meta_path(SESSION_A).read_text()
        os.utime(main, ns=(STALE_NS + 1, STALE_NS + 1))

        report = run(
            source_root, corpus_root, RecordingProducer(fail_sessions=frozenset({SESSION_A}))
        )

        assert report.failed_count == 1
        assert "scripted producer failure" in report.failures[0].error
        assert layout.meta_path(SESSION_A).read_text() == old_meta
        assert not (layout.session_dir(SESSION_A) / "extra.parquet").exists()
        assert not any(layout.staging_dir.iterdir()) if layout.staging_dir.is_dir() else True
        # The watermark did not advance, so the session retries next pass.
        retry = RecordingProducer()
        report = run(source_root, corpus_root, retry)
        assert report.materialized_count == 1
        assert [sid for _, sid, _ in retry.calls] == [SESSION_A]

    def test_one_failing_session_does_not_abort_the_others(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        report = run(
            source_root, corpus_root, RecordingProducer(fail_sessions=frozenset({SESSION_A}))
        )
        assert report.materialized_count == 1
        assert report.failed_count == 1
        layout = CorpusLayout(corpus_root=corpus_root)
        assert (layout.session_dir(SESSION_B) / "extra.parquet").exists()
        assert not layout.session_dir(SESSION_A).exists()

    @pytest.mark.parametrize("reserved", ["session_id", "agent", "materialized_at"])
    def test_colliding_meta_key_is_an_error_not_a_relabel(
        self, source_root: Path, corpus_root: Path, reserved: str
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        report = run(source_root, corpus_root, RecordingProducer(extras={reserved: "spoofed"}))
        assert report.failed_count == 1
        assert "reserved by the contract" in report.failures[0].error
        assert reserved in report.failures[0].error
        assert not CorpusLayout(corpus_root=corpus_root).session_dir(SESSION_A).exists()
