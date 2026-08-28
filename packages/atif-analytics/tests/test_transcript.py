# SPDX-License-Identifier: Apache-2.0

"""Transcript renderer over the fixture corpus — byte-shape assertions."""

from __future__ import annotations

from analytics_fixtures import SESSION_IDS

from atif_analytics.domain.transcript import (
    StepEvent,
    render_session_text,
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


def test_text_windows_shape(reader: CorpusReader) -> None:
    windows = reader.text_windows(SESSION_IDS[0])
    # 7 main-chain text steps with uuids (marker step included as a text
    # turn; sidechain + compact-summary excluded) → one window per turn.
    curr_uuids = [w[2] for w in windows]
    assert "u-sc" not in curr_uuids
    assert "u-cs" not in curr_uuids
    # Session-first window has null prevs.
    first = windows[0]
    assert first[1] is None
    assert first[3] is None
    assert first[5] is None
    assert first[2] == "u-01"
    # Adjacency: each window's prev is the prior window's curr.
    import itertools

    for prev_win, curr_win in itertools.pairwise(windows):
        assert curr_win[1] == prev_win[2]
    # ContentPart[] messages flatten with blank lines.
    last = windows[-1]
    assert last[2] == "u-07"
    assert last[6] == "fixed the race\n\nre-ran twice, green"


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
