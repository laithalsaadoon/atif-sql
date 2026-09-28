# SPDX-License-Identifier: Apache-2.0

"""The session port: the shared step projection, and how the reader drives a source.

The lake adapter itself lives in atif-cli (``tests/test_analyze_lake.py``
proves it reads what the files hold); these pin the half that lives here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import override

import pytest
from analytics_fixtures import SESSION_IDS, write_session

from atif_analytics.domain.transcript import StepEvent, format_step_ts, step_event
from atif_analytics.infrastructure.corpus_reader import CorpusReader, TrajectoryFileSource


class TestTheSharedProjection:
    @pytest.mark.parametrize(
        ("value", "spelled"),
        [
            (datetime(2026, 8, 20, 10, 0, 1, 250_000), "2026-08-20T10:00:01.250Z"),  # noqa: DTZ001 - the lake hands back naive UTC
            (datetime(2026, 8, 20, 10, 0, 1), "2026-08-20T10:00:01.000Z"),  # noqa: DTZ001 - as above
            (datetime(2026, 8, 20, 10, 0, 1, 250_123), "2026-08-20T10:00:01.250123Z"),  # noqa: DTZ001 - as above
            (
                datetime(2026, 8, 20, 12, 0, 1, tzinfo=timezone(timedelta(hours=2))),
                "2026-08-20T10:00:01.000Z",
            ),
            (datetime(2026, 8, 20, 10, 0, 1, tzinfo=UTC), "2026-08-20T10:00:01.000Z"),
            (None, ""),
        ],
    )
    def test_a_timestamp_is_spelled_the_way_both_agents_write_it(
        self, value: datetime | None, spelled: str
    ) -> None:
        assert format_step_ts(value) == spelled

    def test_the_projection_rules(self) -> None:
        event = step_event(
            ts=datetime(2026, 8, 20, 10, 0, 0),  # noqa: DTZ001 - naive UTC, as the lake gives it
            source="agent",
            text=None,
            source_uuids=["first", "second"],
            is_sidechain=False,
            is_compact_summary=False,
            tool_calls=[("Read", {"path": "café"}), (None, None)],
            tool_results=[("c1", "text", False), (None, None, True), ("c3", [1], False)],
        )
        assert event.role == "assistant"
        assert event.text == ""
        assert event.uuid == "first"
        assert event.author is None
        assert event.tool_calls == [("Read", '{"path": "café"}'), ("", "")]
        assert event.tool_results == [("c1", "text"), ("", "null"), ("c3", "[1]")]
        assert event.has_error_result is True

    def test_an_error_flag_can_arrive_without_the_results(self) -> None:
        def user(*, has_error_result: bool = False) -> StepEvent:
            return step_event(
                ts=None,
                source="user",
                text="why?",
                source_uuids=None,
                is_sidechain=False,
                is_compact_summary=False,
                has_error_result=has_error_result,
            )

        assert user(has_error_result=True).has_error_result is True
        assert user().has_error_result is False
        assert (user().role, user().author, user().uuid) == ("user", "human", None)

    def test_a_missing_source_reads_as_unknown(self) -> None:
        event = step_event(
            ts=None,
            source=None,
            text="x",
            source_uuids=[7],
            is_sidechain=True,
            is_compact_summary=False,
        )
        assert (event.role, event.uuid, event.is_sidechain) == ("unknown", None, True)


def test_the_file_source_reads_the_typed_error_flag(tmp_path: Path) -> None:
    """A Codex result carries only ``extra.is_error``; harbor's metadata flag is Claude Code's."""
    write_session(tmp_path, "cccccccc-3333-3333-3333-333333333333", [("user", "go")], day=22)
    path = tmp_path / "sessions" / "cccccccc-3333-3333-3333-333333333333" / "trajectory.json"
    doc = json.loads(path.read_text())
    doc["steps"].append(
        {
            "step_id": 2,
            "timestamp": "2026-08-22T08:01:00.000Z",
            "source": "agent",
            "message": "",
            "observation": {
                "results": [
                    {"source_call_id": "typed", "content": "x", "extra": {"is_error": True}},
                ]
            },
        }
    )
    doc["steps"].append(
        {
            "step_id": 3,
            "timestamp": "2026-08-22T08:02:00.000Z",
            "source": "agent",
            "message": "",
            "observation": {
                "results": [
                    {
                        "source_call_id": "raw",
                        "content": "y",
                        "extra": {"tool_result_metadata": {"is_error": True}},
                    },
                    {"source_call_id": "clean", "content": "z", "extra": {"is_error": False}},
                ]
            },
        }
    )
    path.write_text(json.dumps(doc))
    steps = TrajectoryFileSource(tmp_path).load_steps([path.parent.name])[path.parent.name]
    assert [s.has_error_result for s in steps] == [False, True, True]


class _CountingSource(TrajectoryFileSource):
    """The file source posing as a batching one, recording every call."""

    name = "counting"
    turns_are_complete = False

    def __init__(self, corpus_root: Path, *, turns_batch: int, steps_batch: int) -> None:
        super().__init__(corpus_root)
        self.turns_batch_size = turns_batch
        self.steps_batch_size = steps_batch
        self.turn_calls: list[list[str]] = []
        self.step_calls: list[list[str]] = []

    @override
    def load_turns(self, session_ids: Sequence[str]) -> dict[str, list[StepEvent]]:
        self.turn_calls.append(list(session_ids))
        return TrajectoryFileSource.load_steps(self, session_ids)

    @override
    def load_steps(self, session_ids: Sequence[str]) -> dict[str, list[StepEvent]]:
        self.step_calls.append(list(session_ids))
        return super().load_steps(session_ids)


@pytest.fixture
def five_sessions(corpus_root: Path) -> list[str]:
    """The fixture corpus plus three more, newest-first."""
    extra = [f"dddddddd-{n}{n}{n}{n}-4444-4444-444444444444" for n in range(3)]
    for day, sid in zip((23, 24, 25), extra, strict=True):
        write_session(corpus_root, sid, [("user", "hello"), ("agent", "hi")], day=day)
    return [*reversed(extra), SESSION_IDS[1], SESSION_IDS[0]]


def test_a_batching_source_is_read_ahead_in_walk_order(
    corpus_root: Path, five_sessions: list[str]
) -> None:
    source = _CountingSource(corpus_root, turns_batch=2, steps_batch=1)
    reader = CorpusReader(corpus_root, source=source)
    assert list(reader.session_bounds()) == five_sessions
    for sid in five_sessions:
        reader.load_steps(sid)
    assert source.turn_calls == [five_sessions[0:2], five_sessions[2:4], five_sessions[4:5]]
    assert source.step_calls == []


def test_the_file_source_is_never_read_ahead(corpus_root: Path, five_sessions: list[str]) -> None:
    source = _CountingSource(corpus_root, turns_batch=1, steps_batch=1)
    source.turns_are_complete = True
    reader = CorpusReader(corpus_root, source=source)
    reader.session_bounds()
    reader.load_steps(five_sessions[0])
    reader.session_text(five_sessions[0])
    # One parse serves the gate and the render.
    assert source.step_calls == [[five_sessions[0]]]
    assert source.turn_calls == []


def test_session_texts_loads_one_source_batch_at_a_time(
    corpus_root: Path, five_sessions: list[str]
) -> None:
    source = _CountingSource(corpus_root, turns_batch=1, steps_batch=2)
    reader = CorpusReader(corpus_root, source=source)
    texts = list(reader.session_texts(five_sessions, include_uuids=True))
    assert source.step_calls == [five_sessions[0:2], five_sessions[2:4], five_sessions[4:5]]
    plain = CorpusReader(corpus_root)
    assert texts == [plain.session_text(sid, include_uuids=True) for sid in five_sessions]
