# SPDX-License-Identifier: Apache-2.0

"""The process pool behind ``materialize(workers=N)``.

Three claims, each pinned against the inline ``workers=1`` reference path:
the artifacts a pool writes are the same BYTES, a failure is reported the
same way and holds the watermark back the same way, and the work really
does leave this process. The last one matters most: a pool that quietly
ran everything inline would pass the first two and still be a lie, so the
fake converter stamps its pid into every trajectory and the test reads
those pids back off disk.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, Unpack

import pytest
from corpus_fixtures import NOW_NS, STALE_NS, write_session

from atif_corpus.application.materialize import MaterializationReport, materialize
from atif_corpus.domain.layout import CorpusLayout
from atif_corpus.infrastructure.fake_converter import FakeConverter

SESSIONS = tuple(f"{i:08d}-aaaa-bbbb-cccc-{i:012d}" for i in range(1, 7))
ARTIFACTS = ("trajectory.json", "edges.jsonl", "loss_report.json", "meta.json")


class _Provenance(TypedDict):
    materialized_at: str
    harbor_version: str
    converter_version: str


_PROVENANCE: _Provenance = {
    "materialized_at": "2026-01-02T00:00:00+00:00",
    "harbor_version": "0.22.0",
    "converter_version": "0.1.0",
}


class _RunOverrides(TypedDict, total=False):
    """The `materialize` keywords these tests vary, with `materialize`'s types."""

    worker_setup: Callable[[], None]


def _run(
    source_root: Path,
    corpus_root: Path,
    converter: FakeConverter,
    *,
    workers: int,
    **overrides: Unpack[_RunOverrides],
) -> MaterializationReport:
    return materialize(
        source_root=source_root,
        corpus_root=corpus_root,
        converter=converter,
        now_ns=NOW_NS,
        workers=workers,
        **_PROVENANCE,
        **overrides,
    )


def _write_fixture(source_root: Path) -> None:
    for session_id in SESSIONS:
        write_session(source_root, session_id, mtime_ns=STALE_NS, with_side_files=True)


def _digests(corpus_root: Path) -> dict[str, str]:
    """``{"<sid>/<artifact>": sha256}`` for every artifact under ``sessions/``."""
    layout = CorpusLayout(corpus_root=corpus_root)
    digests: dict[str, str] = {}
    for session_dir in sorted(layout.sessions_dir.iterdir()):
        for name in ARTIFACTS:
            digests[f"{session_dir.name}/{name}"] = hashlib.sha256(
                (session_dir / name).read_bytes()
            ).hexdigest()
    return digests


def _worker_pids(corpus_root: Path) -> dict[str, int]:
    layout = CorpusLayout(corpus_root=corpus_root)
    return {
        session_dir.name: int(
            json.loads(layout.trajectory_path(session_dir.name).read_text())["worker_pid"]
        )
        for session_dir in sorted(layout.sessions_dir.iterdir())
    }


def _touch_pid_file(directory: str) -> None:
    """A picklable ``worker_setup``: leave one file named by the process that ran it."""
    Path(directory, str(os.getpid())).touch()


class TestByteIdentity:
    def test_pool_writes_the_same_bytes_as_the_inline_path(self, tmp_path: Path) -> None:
        source_root = tmp_path / "projects"
        _write_fixture(source_root)

        inline = _run(source_root, tmp_path / "inline", FakeConverter(), workers=1)
        pooled = _run(source_root, tmp_path / "pooled", FakeConverter(), workers=3)

        assert inline.workers == 1
        assert pooled.workers == 3
        assert inline.materialized_count == pooled.materialized_count == len(SESSIONS)
        inline_digests = _digests(tmp_path / "inline")
        assert len(inline_digests) == len(SESSIONS) * len(ARTIFACTS)
        assert _digests(tmp_path / "pooled") == inline_digests
        # The watermark is derived from the same scan, so it is the same bytes too.
        assert (tmp_path / "pooled" / "watermark.json").read_bytes() == (
            tmp_path / "inline" / "watermark.json"
        ).read_bytes()

    def test_pool_leaves_no_staging_residue(self, tmp_path: Path) -> None:
        """Every worker cleans up under its own pid; nothing waits for the sweep."""
        source_root = tmp_path / "projects"
        _write_fixture(source_root)

        _run(source_root, tmp_path / "corpus", FakeConverter(), workers=3)

        staging = CorpusLayout(corpus_root=tmp_path / "corpus").staging_dir
        assert not staging.is_dir() or list(staging.iterdir()) == []


class TestFailures:
    def test_failures_are_reported_in_plan_order_and_hold_the_watermark(
        self, tmp_path: Path
    ) -> None:
        source_root = tmp_path / "projects"
        _write_fixture(source_root)
        failing = frozenset({SESSIONS[4], SESSIONS[1]})

        inline = _run(
            source_root, tmp_path / "inline", FakeConverter(fail_sessions=failing), workers=1
        )
        pooled = _run(
            source_root, tmp_path / "pooled", FakeConverter(fail_sessions=failing), workers=3
        )

        # Same failures, same text, same (plan) order, whatever finished first.
        assert pooled.failures == inline.failures
        assert [f.session_id for f in pooled.failures] == [SESSIONS[1], SESSIONS[4]]
        assert pooled.materialized_count == inline.materialized_count == len(SESSIONS) - 2
        # A failed session has no artifact dir and no watermark entry, so it retries.
        layout = CorpusLayout(corpus_root=tmp_path / "pooled")
        for session_id in failing:
            assert not layout.session_dir(session_id).exists()
        watermark = json.loads(layout.watermark_path.read_text())
        assert not any(session_id in path for path in watermark for session_id in failing)
        assert (tmp_path / "pooled" / "watermark.json").read_bytes() == (
            tmp_path / "inline" / "watermark.json"
        ).read_bytes()

        retry = _run(source_root, tmp_path / "pooled", FakeConverter(), workers=3)
        assert retry.materialized_count == 2
        assert retry.up_to_date_count == len(SESSIONS) - 2
        assert retry.failures == ()

    def test_convert_seconds_is_the_per_session_sum(self, tmp_path: Path) -> None:
        """Three workers sleeping 0.2s each: the sum beats the wall clock."""
        source_root = tmp_path / "projects"
        _write_fixture(source_root)

        report = _run(source_root, tmp_path / "corpus", FakeConverter(delay_seconds=0.2), workers=3)

        assert report.convert_seconds >= 0.2 * len(SESSIONS)
        assert report.convert_seconds > report.total_seconds


class TestTheWorkLeavesThisProcess:
    def test_pool_converts_in_distinct_worker_processes(self, tmp_path: Path) -> None:
        """Fails if the pool fell back to serial: every pid would be ours."""
        source_root = tmp_path / "projects"
        _write_fixture(source_root)

        report = _run(
            source_root,
            tmp_path / "corpus",
            FakeConverter(record_pid=True, delay_seconds=0.3),
            workers=3,
        )

        pids = _worker_pids(tmp_path / "corpus")
        assert len(pids) == len(SESSIONS)
        assert os.getpid() not in pids.values()
        assert len(set(pids.values())) >= 2
        assert report.workers == 3

    def test_workers_one_is_the_inline_reference_path(self, tmp_path: Path) -> None:
        source_root = tmp_path / "projects"
        _write_fixture(source_root)
        converter = FakeConverter(record_pid=True)

        report = _run(source_root, tmp_path / "corpus", converter, workers=1)

        assert set(_worker_pids(tmp_path / "corpus").values()) == {os.getpid()}
        assert report.workers == 1
        # The inline path uses THIS instance, so its call log is populated.
        assert [p.stem for p in converter.converted] == list(SESSIONS)

    def test_a_single_planned_session_runs_inline(self, tmp_path: Path) -> None:
        """A pool of one buys nothing; the report says so."""
        source_root = tmp_path / "projects"
        write_session(source_root, SESSIONS[0], mtime_ns=STALE_NS)

        report = _run(source_root, tmp_path / "corpus", FakeConverter(record_pid=True), workers=4)

        assert report.workers == 1
        assert set(_worker_pids(tmp_path / "corpus").values()) == {os.getpid()}

    def test_worker_setup_runs_once_in_every_worker(self, tmp_path: Path) -> None:
        source_root = tmp_path / "projects"
        _write_fixture(source_root)
        marks = tmp_path / "marks"
        marks.mkdir()

        _run(
            source_root,
            tmp_path / "corpus",
            FakeConverter(record_pid=True, delay_seconds=0.3),
            workers=3,
            worker_setup=functools.partial(_touch_pid_file, str(marks)),
        )

        setup_pids = {int(p.name) for p in marks.iterdir()}
        assert os.getpid() not in setup_pids
        assert set(_worker_pids(tmp_path / "corpus").values()) <= setup_pids

    def test_workers_below_one_is_refused(self, tmp_path: Path) -> None:
        source_root = tmp_path / "projects"
        _write_fixture(source_root)

        with pytest.raises(ValueError, match="workers must be >= 1"):
            _run(source_root, tmp_path / "corpus", FakeConverter(), workers=0)
