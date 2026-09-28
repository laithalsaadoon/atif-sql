# SPDX-License-Identifier: Apache-2.0

"""The ``SessionSink`` port: what materialize hands it, when, and what a failure leaves.

atif-corpus knows nothing about what the sink keeps (atif-cli plugs in
atif-duck's DuckLake writer), so these tests use a recording fake and pin the
contract any sink gets: every session published or marked source-removed this
pass, in plan order and in batches, after the swaps and in this process; a
sink failure never fails the pass, and every session it did not take is
recorded in ``sink_pending.json`` BEFORE anything publishes, and handed over
again by the next pass even when no source moved.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, override

import pytest
from corpus_fixtures import NOW_NS, SESSION_A, SESSION_B, STALE_NS, write_session

from atif_corpus.application.materialize import (
    MaterializationReport,
    materialize,
    read_sink_pending,
)
from atif_corpus.domain.layout import CorpusLayout
from atif_corpus.infrastructure.fake_converter import FakeConverter

SESSION_C = "33333333-3333-3333-3333-333333333333"
SESSION_D = "44444444-4444-4444-4444-444444444444"
SESSIONS = (SESSION_A, SESSION_B, SESSION_C, SESSION_D)

VERSIONS: dict[str, Any] = {
    "materialized_at": "2026-01-02T00:00:00+00:00",
    "harbor_version": "0.23.0",
    "converter_version": "0.1.0",
}


class RecordingSink:
    """Records every batch; raises for the first ``fail_calls`` calls."""

    def __init__(self, *, fail_calls: int = 0, error: type[BaseException] = RuntimeError) -> None:
        self.calls: list[tuple[Path, str, tuple[str, ...]]] = []
        self.fail_calls = fail_calls
        self.error = error

    def sync_sessions(self, *, corpus_root: Path, agent: str, session_ids: Sequence[str]) -> None:
        # The contract: every session handed over is already published.
        layout = CorpusLayout(corpus_root=corpus_root)
        for session_id in session_ids:
            assert layout.meta_path(session_id).is_file(), session_id
        self.calls.append((corpus_root, agent, tuple(session_ids)))
        if len(self.calls) <= self.fail_calls:
            msg = f"scripted sink failure on call {len(self.calls)}"
            raise self.error(msg)

    @property
    def handed(self) -> list[str]:
        return [sid for _, _, batch in self.calls for sid in batch]


def run(
    source_root: Path,
    corpus_root: Path,
    sink: RecordingSink | None,
    *,
    converter: FakeConverter | None = None,
    batch: int = 2,
    workers: int = 1,
) -> MaterializationReport:
    return materialize(
        source_root=source_root,
        corpus_root=corpus_root,
        converter=converter or FakeConverter(),
        now_ns=NOW_NS,
        session_sink=sink,
        sink_batch_size=batch,
        workers=workers,
        **VERSIONS,
    )


def _write_all(source_root: Path) -> None:
    for session_id in SESSIONS:
        write_session(source_root, session_id, mtime_ns=STALE_NS)


def _pending(corpus_root: Path) -> tuple[str, ...]:
    return read_sink_pending(CorpusLayout(corpus_root=corpus_root).sink_pending_path)


class TestWhatTheSinkIsHanded:
    def test_published_sessions_in_plan_order_and_batches(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        _write_all(source_root)
        sink = RecordingSink()
        report = run(source_root, corpus_root, sink, batch=3)
        assert [batch for _, _, batch in sink.calls] == [SESSIONS[:3], SESSIONS[3:]]
        assert {(root, agent) for root, agent, _ in sink.calls} == {(corpus_root, "claude-code")}
        assert report.sink_synced_count == len(SESSIONS)
        assert report.sink_pending_session_ids == ()
        assert report.sink_error is None
        assert not CorpusLayout(corpus_root=corpus_root).sink_pending_path.exists()

    def test_failed_and_empty_sessions_are_not_handed_over(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        _write_all(source_root)
        sink = RecordingSink()
        converter = FakeConverter(
            fail_sessions=frozenset({SESSION_B}), empty_sessions=frozenset({SESSION_C})
        )
        run(source_root, corpus_root, sink, converter=converter)
        assert sink.handed == [SESSION_A, SESSION_D]
        assert _pending(corpus_root) == ()

    def test_an_up_to_date_pass_hands_over_nothing(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        _write_all(source_root)
        run(source_root, corpus_root, RecordingSink())
        again = RecordingSink()
        run(source_root, corpus_root, again)
        assert again.calls == []

    def test_a_newly_source_removed_session_is_handed_over(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        _write_all(source_root)
        run(source_root, corpus_root, RecordingSink())
        (source_root / "-proj-a" / f"{SESSION_B}.jsonl").unlink()
        sink = RecordingSink()
        report = run(source_root, corpus_root, sink)
        assert report.removed_session_ids == (SESSION_B,)
        assert sink.handed == [SESSION_B]
        # Marked once: the pass after that hands it over no more.
        later = RecordingSink()
        run(source_root, corpus_root, later)
        assert later.calls == []

    def test_the_pool_hands_over_exactly_what_the_inline_path_does(self, tmp_path: Path) -> None:
        source_root = tmp_path / "projects"
        _write_all(source_root)
        inline, pooled = RecordingSink(), RecordingSink()
        run(source_root, tmp_path / "inline", inline, workers=1)
        run(source_root, tmp_path / "pooled", pooled, workers=3)
        assert [batch for _, _, batch in pooled.calls] == [batch for _, _, batch in inline.calls]


class TestAFailingSink:
    def test_the_failed_batch_and_every_later_one_stay_pending(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        _write_all(source_root)
        sink = RecordingSink(fail_calls=1)
        report = run(source_root, corpus_root, sink, batch=2)
        # The first batch failed; the pass stopped handing over and kept all.
        assert [batch for _, _, batch in sink.calls] == [SESSIONS[:2]]
        assert report.sink_pending_session_ids == SESSIONS
        assert report.sink_synced_count == 0
        assert report.sink_error is not None
        assert "scripted sink failure" in report.sink_error
        assert _pending(corpus_root) == SESSIONS
        # The pass itself succeeded: artifacts published, watermark advanced.
        assert report.failed_count == 0
        assert report.materialized_count == len(SESSIONS)

    def test_the_next_pass_hands_pending_sessions_over_with_no_source_change(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        _write_all(source_root)
        run(source_root, corpus_root, RecordingSink(fail_calls=1))
        retry = RecordingSink()
        report = run(source_root, corpus_root, retry)
        assert report.materialized_count == 0
        assert retry.handed == list(SESSIONS)
        assert report.sink_pending_session_ids == ()
        assert not CorpusLayout(corpus_root=corpus_root).sink_pending_path.exists()

    def test_a_pass_without_a_sink_leaves_the_pending_record_alone(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        _write_all(source_root)
        run(source_root, corpus_root, RecordingSink(fail_calls=1))
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS + 1)
        report = run(source_root, corpus_root, None)
        assert report.materialized_count == 1
        assert _pending(corpus_root) == SESSIONS

    def test_sessions_are_recorded_before_they_publish(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """A pass killed between a session's swap and its sync still leaves it recorded."""
        _write_all(source_root)
        sink = RecordingSink(fail_calls=1, error=KeyboardInterrupt)
        with pytest.raises(KeyboardInterrupt):
            run(source_root, corpus_root, sink)
        assert _pending(corpus_root) == SESSIONS
        retry = RecordingSink()
        run(source_root, corpus_root, retry)
        assert retry.handed == list(SESSIONS)

    def test_a_converter_sees_its_session_recorded_before_the_swap(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        layout = CorpusLayout(corpus_root=corpus_root)
        seen: list[tuple[str, ...]] = []

        class Watching(FakeConverter):
            @override
            def convert(self, session_jsonl: Path, *, archive_dir: Path | None = None) -> Any:
                seen.append(read_sink_pending(layout.sink_pending_path))
                return super().convert(session_jsonl, archive_dir=archive_dir)

        run(source_root, corpus_root, RecordingSink(), converter=Watching())
        assert seen == [(SESSION_A,)]

    def test_an_unreadable_record_reads_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "sink_pending.json"
        path.write_text("{not json")
        assert read_sink_pending(path) == ()
        path.write_text(json.dumps({"session_ids": "not-a-list"}))
        assert read_sink_pending(path) == ()

    def test_a_batch_size_below_one_is_refused(self, source_root: Path, corpus_root: Path) -> None:
        with pytest.raises(ValueError, match="sink_batch_size"):
            run(source_root, corpus_root, RecordingSink(), batch=0)
