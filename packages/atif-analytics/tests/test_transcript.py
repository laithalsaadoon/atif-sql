# SPDX-License-Identifier: Apache-2.0

"""Transcript renderer over the fixture corpus — byte-shape assertions."""

from __future__ import annotations

from analytics_fixtures import (
    IMAGE_META,
    RETRY_NUDGE,
    S1_HUMAN_UUIDS,
    S1_MACHINE_UUIDS,
    SESSION_IDS,
)

from atif_analytics.domain.transcript import (
    NON_HUMAN_PREVIEW_CHARS,
    StepEvent,
    is_human_turn,
    render_session_text,
    session_kind,
    tool_input_preview,
    tool_result_preview,
)
from atif_analytics.infrastructure.corpus_reader import CorpusReader


def test_text_line_shape(reader: CorpusReader) -> None:
    text = reader.session_text(SESSION_IDS[0])
    lines = text.split("\n")
    assert lines[0] == "[user 2026-08-20T10:00:00.000Z] please fix the flaky auth test"
    # ATIF "agent" renders as "assistant".
    assert "[assistant 2026-08-20T10:00:10.000Z] Reading the test file now." in lines


def test_tool_use_line_shape_and_400_char_preview(reader: CorpusReader) -> None:
    text = reader.session_text(SESSION_IDS[0])
    tool_lines = [ln for ln in text.split("\n") if ln.startswith("[tool_use:")]
    assert any(ln.startswith("[tool_use:Read 2026-08-20T10:00:10.000Z] ") for ln in tool_lines)
    # Args render as a JSON preview.
    read_line = next(ln for ln in tool_lines if ln.startswith("[tool_use:Read"))
    assert '"file_path"' in read_line


def test_tool_result_50k_cap_with_dropped_footer(reader: CorpusReader) -> None:
    text = reader.session_text(SESSION_IDS[0])
    result_lines_start = text.find("[tool_result toolu_01 ")
    assert result_lines_start != -1
    # The 60_000-char content clips at 50_000 with the footer.
    assert "…(truncated, 10000 chars dropped)" in text


def test_uuid_headers_only_when_requested(reader: CorpusReader) -> None:
    plain = reader.session_text(SESSION_IDS[0])
    assert "[uuid=" not in plain
    with_uuids = reader.session_text(SESSION_IDS[0], include_uuids=True)
    assert "[uuid=u-01 user 2026-08-20T10:00:00.000Z] please fix the flaky auth test" in with_uuids


def test_sidechain_steps_excluded(reader: CorpusReader) -> None:
    text = reader.session_text(SESSION_IDS[0])
    assert "subagent chatter" not in text


def test_session_total_cap_truncates_with_notice() -> None:
    steps = [
        StepEvent(ts="2026-08-20T10:00:00.000Z", role="user", text="a" * 300, uuid=f"u-{i}")
        for i in range(10)
    ]
    text = render_session_text(steps, total_max_chars=1000)
    assert "…(session truncated at 1000 chars, 10 events total)" in text
    assert len(text) < 1200


def test_tool_previews_pure() -> None:
    assert tool_input_preview(None) == ""
    assert tool_input_preview("x" * 500).endswith("…(truncated)")
    assert len(tool_input_preview("x" * 500)) == 400 + len("…(truncated)")
    assert tool_result_preview("y" * 30, 50) == "y" * 30
    clipped = tool_result_preview("y" * 100, 50)
    assert clipped.startswith("y" * 50)
    assert clipped.endswith("…(truncated, 50 chars dropped)")


def test_machine_user_text_renders_under_its_author(reader: CorpusReader) -> None:
    """Hook, retry and image text never reaches the model as ``[user ...]``."""
    text = reader.session_text(SESSION_IDS[0])
    lines = text.split("\n")
    assert any(ln.startswith("[stop_hook 2026-08-20T10:01:01.000Z] Stop hook") for ln in lines)
    assert any(ln.startswith("[harness 2026-08-20T10:01:02.000Z] Your previous") for ln in lines)
    assert any(ln.startswith("[harness 2026-08-20T10:01:02.500Z] [Image: original") for ln in lines)
    user_lines = [ln for ln in lines if ln.startswith("[user ")]
    assert not any(RETRY_NUDGE in ln or IMAGE_META in ln for ln in user_lines)
    assert not any("Stop hook feedback" in ln for ln in user_lines)


def test_non_human_body_is_clipped() -> None:
    body = "Stop hook feedback: " + "x" * (NON_HUMAN_PREVIEW_CHARS * 2)
    steps = [StepEvent(ts="t", role="user", text=body, uuid="u")]
    line = render_session_text(steps)
    assert line.startswith("[stop_hook t] Stop hook feedback: ")
    assert line.endswith("chars dropped)")
    assert len(line) < NON_HUMAN_PREVIEW_CHARS + 80


def test_step_event_author_derivation() -> None:
    assert StepEvent(ts="t", role="user", text="  fix it").author == "human"
    assert StepEvent(ts="t", role="user", text="<task-notification> x").author == (
        "task_notification"
    )
    compact = StepEvent(ts="t", role="user", text="fix it", is_compact_summary=True)
    assert compact.author == "harness"
    assert StepEvent(ts="t", role="agent", text="Stop hook feedback").author is None


def test_human_turns_and_session_kind(reader: CorpusReader) -> None:
    steps = reader.load_steps(SESSION_IDS[0])
    human = [s.uuid for s in steps if is_human_turn(s)]
    assert human == S1_HUMAN_UUIDS
    assert not set(S1_MACHINE_UUIDS) & set(human)
    assert session_kind(steps) == "interactive"
    assert reader.session_kind(SESSION_IDS[0]) == "interactive"
    audit = [
        StepEvent(ts="t", role="user", text="You are auditing an agent turn for x"),
        StepEvent(ts="t", role="agent", text='{"ok": true}'),
    ]
    assert session_kind(audit) == "turn_audit"
    job = [
        StepEvent(ts="t", role="user", text="Run the nightly brief."),
        StepEvent(ts="t", role="user", text=RETRY_NUDGE),
    ]
    assert session_kind(job) == "one_shot_job"


def test_edges_uuids(reader: CorpusReader) -> None:
    uuids = reader.edges_uuids(SESSION_IDS[0])
    # ``None`` means the edges file was unreadable, which would make every
    # membership assertion below vacuous.
    assert uuids is not None
    assert "u-01" in uuids
    assert "u-01-extra" in uuids
    assert "not-a-real-uuid" not in uuids


def test_session_bounds_newest_first(reader: CorpusReader) -> None:
    bounds = reader.session_bounds()
    sids = list(bounds)
    assert sids == [SESSION_IDS[1], SESSION_IDS[0]]  # session 2 is newer
    last_ts, mtime = bounds[SESSION_IDS[0]]
    assert last_ts is not None
    assert last_ts.isoformat().startswith("2026-08-20T10:01:10")
    assert mtime is not None


def test_session_bounds_limit(reader: CorpusReader) -> None:
    assert list(reader.session_bounds(limit=1)) == [SESSION_IDS[1]]
