# SPDX-License-Identifier: Apache-2.0

"""The lake session reader's rows for the narrate pipeline: whose step, which call, which subagents.

The reader's other rows are covered end to end by atif-cli's
``test_analyze_lake.py`` (lake against files, step for step). These three
columns matter only to narrate, and atif-cli's fixture corpus has no
subagent, so they're pinned here against the fixture session that has one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from duck_fixtures import SESSION_IDS, build_corpus

from atif_duck.infrastructure.lake import LakeCorpus, LakeLayout, rebuild_lake
from atif_duck.infrastructure.lake_sessions import LakeSessionReader

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def reader(corpus_root: Path, tmp_path: Path) -> Iterator[LakeSessionReader]:
    layout = LakeLayout(tmp_path / "lake")
    rebuild_lake(layout, [LakeCorpus(root=corpus_root, agent="claude-code")])
    opened = LakeSessionReader.open(layout, corpus_root)
    assert isinstance(opened, LakeSessionReader), opened
    try:
        yield opened
    finally:
        opened.close()


def test_turns_say_which_subagent_a_step_belongs_to(reader: LakeSessionReader) -> None:
    rows = reader.turns([SESSION_IDS[0]])
    # Column 5 is the sidechain flag and column 8, the last, the agent id.
    by_sidechain = {(bool(row[5]), row[8]) for row in rows}
    assert by_sidechain == {(False, None), (True, "agent-r1")}


def test_tool_calls_carry_their_call_id(reader: LakeSessionReader) -> None:
    """The id a subagent's ``parent_tool_call_id`` names is on the launching call's row."""
    rows = reader.tool_calls([SESSION_IDS[0]])
    assert all(len(row) == 5 for row in rows)
    ids = [row[4] for row in rows]
    assert "toolu_02" in ids
    assert all(isinstance(call_id, str) and call_id for call_id in ids)


def test_subagents_json_is_the_converters_declared_list(reader: LakeSessionReader) -> None:
    declared = json.loads(reader.subagents_json(SESSION_IDS[0]) or "null")
    assert declared == [
        {
            "agent_id": "agent-r1",
            "agent_type": "general-purpose",
            "parent_tool_call_id": "toolu_02",
            "spawn_depth": 1,
            "link_source": "meta",
        }
    ]
    # A session that declared none, and one the lake doesn't hold.
    assert reader.subagents_json(SESSION_IDS[1]) is None
    assert reader.subagents_json("no-such-session") is None


def test_queued_commands_are_the_sessions_queued_attachment_events(tmp_path: Path) -> None:
    """A background task's notice is a ``queued_command`` event, and only those rows come back."""
    corpus = build_corpus(tmp_path / "corpus")
    notice = {
        "seq": 99,
        "ts": "2026-08-20T10:00:45.500Z",
        "event_type": "attachment",
        "subtype": "queued_command",
        "uuid": "q-1",
        "parent_uuid": None,
        "tool_use_id": None,
        "is_sidechain": False,
        "source_file": "transcript.jsonl",
        "payload": {
            "prompt": "<task-notification>\n<task-id>agent-r1</task-id>\n</task-notification>"
        },
        "payload_bytes": 80,
        "payload_truncated": False,
    }
    events = corpus / "sessions" / SESSION_IDS[0] / "session_events.jsonl"
    events.write_text(
        events.read_text(encoding="utf-8") + json.dumps(notice) + "\n", encoding="utf-8"
    )
    layout = LakeLayout(tmp_path / "lake")
    rebuild_lake(layout, [LakeCorpus(root=corpus, agent="claude-code")])
    opened = LakeSessionReader.open(layout, corpus)
    assert isinstance(opened, LakeSessionReader), opened
    try:
        rows = opened.queued_commands(SESSION_IDS[0])
        assert len(rows) == 1  # the hook and cost-state events aren't queued commands
        ts, payload = rows[0]
        assert ts == datetime(2026, 8, 20, 10, 0, 45, 500000, tzinfo=UTC)
        assert json.loads(payload) == notice["payload"]
        assert opened.queued_commands(SESSION_IDS[1]) == []
    finally:
        opened.close()
