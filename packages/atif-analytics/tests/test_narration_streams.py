# SPDX-License-Identifier: Apache-2.0

"""Narration streams: splitting a session by subagent, linking each launch, and tool-result excerpts.

Pure domain tests over hand-built steps.
"""

from __future__ import annotations

from atif_analytics.domain.narration import (
    PARENT_AUTHOR,
    UNATTRIBUTED_AGENT_ID,
    SubagentLink,
    SubagentRef,
    TaskNotice,
    excerpt_results,
    link_subagents,
    persisted_output,
    plan_windows,
    render_window,
    result_excerpt,
    split_streams,
    task_notices,
)
from atif_analytics.domain.transcript import StepEvent, render_session_text


def _main(text: str, *, role: str = "assistant", minute: int = 0, **kw: object) -> StepEvent:
    return StepEvent(ts=f"2026-11-01T10:{minute:02d}:00.000Z", role=role, text=text, **kw)  # type: ignore[arg-type]


def _side(
    text: str, agent: str | None, *, role: str = "assistant", minute: int = 0, **kw: object
) -> StepEvent:
    return StepEvent(
        ts=f"2026-11-01T10:{minute:02d}:01.000Z",
        role=role,
        text=text,
        is_sidechain=True,
        agent_id=agent,
        **kw,  # type: ignore[arg-type]
    )


def _launch(call_id: str, result: str | None = "[Subagent hand-back] done") -> StepEvent:
    """A step that launches a subagent; ``result`` is what the call returned on that step."""
    return _main(
        "launching",
        tool_calls=[("Agent", "{}")],
        tool_call_ids=[call_id],
        tool_results=[(call_id, result)] if result is not None else [],
        tool_result_errors=[False] if result is not None else [],
    )


def _notice(agent: str) -> StepEvent:
    return _main(
        f"<task-notification>\n<task-id>{agent}</task-id>\n</task-notification>", role="user"
    )


ASYNC = "Async agent launched successfully. agentId: x"


# ---------------------------------------------------------------------------
# split_streams
# ---------------------------------------------------------------------------


def test_parallel_subagents_come_apart_into_their_own_streams() -> None:
    """Two subagents running side by side interleave in the flat list; each stream is in order."""
    steps = [
        _main("ask", role="user"),
        _side("a prompt", "a", role="user"),
        _side("b prompt", "b", role="user"),
        _side("a works", "a"),
        _side("b works", "b"),
        _main("done"),
        _side("a ends", "a"),
    ]
    streams = split_streams(steps)
    assert list(streams) == [None, "a", "b"]
    assert [s.text for s in streams[None]] == ["ask", "done"]
    assert [s.text for s in streams["a"]] == ["a prompt", "a works", "a ends"]
    assert [s.text for s in streams["b"]] == ["b prompt", "b works"]


def test_a_subagents_steps_become_a_chain_of_their_own() -> None:
    steps = [
        _side("do the task", "a", role="user"),
        _side("Stop hook feedback: no evidence", "a", role="user"),
        _side("working", "a"),
    ]
    stream = split_streams(steps)["a"]
    # No longer filtered out as sidechain, so the renderer shows them.
    assert not any(s.is_sidechain for s in stream)
    # The task is the launching agent's; a hook's text keeps its own label.
    assert [s.author for s in stream] == [PARENT_AUTHOR, "stop_hook", None]
    rendered = render_window(stream, total_max_chars=10_000)
    assert "[parent_agent 2026-11-01T10:00:01.000Z] do the task" in rendered
    assert "[stop_hook " in rendered
    assert "[user " not in rendered
    # The whole-session renderer every other pipeline uses still drops them.
    assert render_session_text(steps) == ""


def test_a_parents_instructions_are_shown_whole_and_cut_a_window() -> None:
    task = "x" * 2_000
    stream = split_streams(
        [
            _side(task, "a", role="user"),
            _side("working", "a"),
            _side("one more thing", "a", role="user"),
            _side("ok", "a"),
        ]
    )["a"]
    assert task in render_window(stream, total_max_chars=100_000)
    windows = plan_windows(stream, max_chars=100_000, min_chars=0)
    assert [(w.start, w.stop, w.boundary) for w in windows] == [
        (0, 2, "parent_message"),
        (2, 4, "session_end"),
    ]


def test_sidechain_steps_without_an_agent_id_share_one_stream() -> None:
    streams = split_streams([_main("m"), _side("old one", None), _side("old two", None)])
    assert [s.text for s in streams[UNATTRIBUTED_AGENT_ID]] == ["old one", "old two"]


def test_the_main_chain_is_always_a_stream() -> None:
    assert split_streams([]) == {None: []}
    assert list(split_streams([_side("only a subagent", "a")])) == [None, "a"]


# ---------------------------------------------------------------------------
# link_subagents
# ---------------------------------------------------------------------------


def _links(steps: list[StepEvent], refs: list[SubagentRef]) -> dict[str, SubagentLink]:
    return {link.ref.agent_id: link for link in link_subagents(split_streams(steps), refs)}


def test_a_foreground_subagent_returns_on_its_launching_step() -> None:
    steps = [_main("ask", role="user"), _launch("tc-a"), _side("work", "a"), _main("after")]
    link = _links(steps, [SubagentRef("a", parent_tool_call_id="tc-a")])["a"]
    assert (link.parent, link.launch, link.returned) == (None, 1, 1)


def test_a_background_subagent_returns_on_its_task_notification() -> None:
    steps = [
        _launch("tc-a", ASYNC),
        _side("work", "a"),
        _main("meanwhile"),
        _notice("someone-else"),
        _notice("a"),
        _main("after"),
    ]
    link = _links(steps, [SubagentRef("a", parent_tool_call_id="tc-a")])["a"]
    # Indexes are into the main chain: the subagent's own step isn't in it.
    assert (link.launch, link.returned) == (0, 3)


def test_a_subagent_that_never_reports_has_no_return() -> None:
    background = [_launch("tc-a", ASYNC), _side("work", "a"), _main("moved on")]
    assert _links(background, [SubagentRef("a", parent_tool_call_id="tc-a")])["a"].returned is None
    # A foreground launch cut off before its result came back.
    interrupted = [_launch("tc-a", None), _side("work", "a")]
    link = _links(interrupted, [SubagentRef("a", parent_tool_call_id="tc-a")])["a"]
    assert (link.launch, link.returned) == (0, None)


def test_with_no_return_step_the_end_is_placed_by_the_clock() -> None:
    """A background subagent whose output the agent read from a file: no step is its return."""
    steps = [
        _launch("tc-a", ASYNC),
        _side("work", "a", minute=1),
        _main("meanwhile", minute=2),
        _side("final report", "a", minute=3),
        _main("reading its output file", minute=4),
        _main("later", minute=5),
    ]
    link = _links(steps, [SubagentRef("a", parent_tool_call_id="tc-a")])["a"]
    # Main chain: launch 0, meanwhile 1, reading 2, later 3. The first of
    # those not before the subagent's last step (minute 3) is "reading".
    assert (link.launch, link.returned, link.ended, link.back) == (0, None, 2, 2)


def test_a_subagent_the_launching_stream_never_outlasts_has_no_end() -> None:
    steps = [
        _launch("tc-a", ASYNC),
        _main("moved on", minute=1),
        _side("still working", "a", minute=9),
    ]
    link = _links(steps, [SubagentRef("a", parent_tool_call_id="tc-a")])["a"]
    assert (link.launch, link.returned, link.ended, link.back) == (0, None, None, None)


def test_a_return_step_wins_over_the_clock() -> None:
    steps = [
        _launch("tc-a", ASYNC),
        _side("final report", "a", minute=1),
        _main("busy with something else", minute=2),
        _notice("a"),
    ]
    link = _links(steps, [SubagentRef("a", parent_tool_call_id="tc-a")])["a"]
    assert (link.returned, link.ended, link.back) == (2, 1, 2)


def _queued(agent: str, minute: int, second: int = 30) -> TaskNotice:
    return TaskNotice(ts=f"2026-11-01T10:{minute:02d}:{second:02d}.000Z", task_id=agent)


def test_a_queued_notice_is_the_return_at_the_first_step_after_it() -> None:
    """Most background reports arrive beside the transcript, not as a step."""
    steps = [
        _launch("tc-a", ASYNC),
        _side("final report", "a", minute=1),
        _main("busy", minute=2),
        _main("still busy", minute=3),
        _main("uses the report", minute=5),
    ]
    refs = [SubagentRef("a", parent_tool_call_id="tc-a")]
    # Without the notice the end is only where the clock puts it.
    clock = link_subagents(split_streams(steps), refs)[0]
    assert (clock.returned, clock.ended, clock.back) == (None, 1, 1)
    # The notice is queued at 10:04:30; the agent's next step is at 10:05.
    link = link_subagents(split_streams(steps), refs, [_queued("a", 4)])[0]
    assert (link.returned, link.back) == (3, 3)


def test_a_notice_for_another_task_or_from_before_the_launch_is_not_the_return() -> None:
    steps = [
        _main("earlier work", minute=1),
        _main(
            "launching",
            minute=5,
            tool_calls=[("Agent", "{}")],
            tool_call_ids=["tc-a"],
            tool_results=[("tc-a", ASYNC)],
        ),
        _side("report", "a", minute=6),
        _main("later", minute=9),
    ]
    refs = [SubagentRef("a", parent_tool_call_id="tc-a")]

    def returned(*notices: TaskNotice) -> int | None:
        return link_subagents(split_streams(steps), refs, notices)[0].returned

    assert returned(_queued("some-shell-task", 7)) is None
    assert returned(_queued("a-longer-id", 7)) is None
    # The same agent id notifying before this launch belongs to an earlier run of it.
    assert returned(_queued("a", 2)) is None
    assert returned(_queued("a", 2), _queued("a", 7)) == 2


def test_a_notice_after_the_last_step_lands_on_the_last_step() -> None:
    steps = [_launch("tc-a", ASYNC), _side("report", "a", minute=1), _main("last", minute=2)]
    link = link_subagents(
        split_streams(steps), [SubagentRef("a", parent_tool_call_id="tc-a")], [_queued("a", 30)]
    )[0]
    assert link.returned == 1


def test_the_earliest_sign_of_a_report_wins() -> None:
    steps = [
        _launch("tc-a", ASYNC),
        _side("report", "a", minute=1),
        _main("busy", minute=2),
        _notice("a"),  # a notification delivered as a step, stamped 10:00
        _main("later", minute=8),
    ]
    refs = [SubagentRef("a", parent_tool_call_id="tc-a")]
    # The step notification is at index 2; a queued notice at 10:07 would
    # land on index 3. The earlier one is the return.
    assert link_subagents(split_streams(steps), refs, [_queued("a", 7)])[0].returned == 2


def test_task_notices_reads_each_notice_once_with_its_status() -> None:
    block = (
        "<task-notification>\\n<task-id>a0000001</task-id>\\n<status>completed</status>\\n"
        "<result>found it</result>\\n</task-notification>"
    )
    # A queued-command record holds the notice twice: as the prompt, and as
    # it was shown to the agent.
    body = f'{{"renderedInHumanTurn": [{{"content": "{block}"}}], "prompt": "{block}"}}'
    assert task_notices(body, "2026-01-02T03:04:05.000Z") == [
        TaskNotice(ts="2026-01-02T03:04:05.000Z", task_id="a0000001", status="completed")
    ]
    two = block + block.replace("a0000001", "bshell01").replace("completed", "failed")
    assert [(n.task_id, n.status) for n in task_notices(two, "t")] == [
        ("a0000001", "completed"),
        ("bshell01", "failed"),
    ]
    # A queued message that isn't a task notification (the agent was sent a
    # note) holds none.
    assert task_notices('{"prompt": "Priority lead: check the filing"}', "t") == []
    assert task_notices("<task-notification>no id here</task-notification>", "t") == []
    # The converter cuts a stored record at 8 KB, so a long report loses its
    # closing tag. The id at its start still names the task.
    cut = block[: block.index("</status>") + len("</status>")] + "<result>a long report that was cu"
    assert [(n.task_id, n.status) for n in task_notices(cut, "t")] == [("a0000001", "completed")]
    assert [n.task_id for n in task_notices(block + cut.replace("a0000001", "bshell01"), "t")] == [
        "a0000001",
        "bshell01",
    ]


def test_a_nested_subagent_hangs_off_the_subagent_that_launched_it() -> None:
    nested_launch = _side(
        "delegating",
        "a",
        tool_calls=[("Agent", "{}")],
        tool_call_ids=["tc-a1"],
        tool_results=[("tc-a1", "[Subagent hand-back] ok")],
        tool_result_errors=[False],
    )
    steps = [_launch("tc-a"), _side("start", "a"), nested_launch, _side("deep work", "a1")]
    declared = [
        SubagentRef("a", parent_tool_call_id="tc-a"),
        SubagentRef("a1", parent_tool_call_id="tc-a1", parent_agent_id="a"),
    ]
    ordered = link_subagents(split_streams(steps), declared)
    # Deepest first: a caller narrating in order has the child's story
    # before the window that launched the child.
    assert [link.ref.agent_id for link in ordered] == ["a1", "a"]
    nested = ordered[0]
    assert (nested.parent, nested.launch, nested.returned) == ("a", 1, 1)

    # With the parent's id unrecorded, the launching call still places it.
    declared[1] = SubagentRef("a1", parent_tool_call_id="tc-a1")
    assert _links(steps, declared)["a1"].parent == "a"


def test_a_subagent_nobody_declared_is_still_a_stream_to_narrate() -> None:
    steps = [_main("m"), _side("orphan work", "ghost")]
    link = _links(steps, [])["ghost"]
    assert link == SubagentLink(ref=SubagentRef("ghost"))
    # Declared, but no step holds the launching call: the same.
    declared = [SubagentRef("ghost", agent_type="researcher", parent_tool_call_id="tc-gone")]
    link = _links(steps, declared)["ghost"]
    assert (link.parent, link.launch, link.returned) == (None, None, None)
    assert link.ref.agent_type == "researcher"


def test_a_declared_subagent_with_no_steps_gets_no_link() -> None:
    assert _links([_launch("tc-a")], [SubagentRef("a", parent_tool_call_id="tc-a")]) == {}


def test_a_launch_loop_is_cut_rather_than_followed() -> None:
    """Two subagents that each claim the other as parent can't make the order recurse."""
    a_launches_b = _side(
        "a", "a", tool_calls=[("Agent", "{}")], tool_call_ids=["tc-b"], tool_results=[("tc-b", "x")]
    )
    b_launches_a = _side(
        "b", "b", tool_calls=[("Agent", "{}")], tool_call_ids=["tc-a"], tool_results=[("tc-a", "x")]
    )
    declared = [
        SubagentRef("a", parent_tool_call_id="tc-a", parent_agent_id="b"),
        SubagentRef("b", parent_tool_call_id="tc-b", parent_agent_id="a"),
    ]
    links = _links([a_launches_b, b_launches_a], declared)
    # The loop is opened at one end: one of the two becomes a root.
    assert (links["a"].parent, links["b"].parent) == (None, "a")


# ---------------------------------------------------------------------------
# tool-result excerpts
# ---------------------------------------------------------------------------


def test_a_short_result_is_shown_whole() -> None:
    assert result_excerpt("ok", failed=False, head_chars=300, failed_tail_chars=300) == "ok"
    assert (
        result_excerpt("boom", failed=True, head_chars=300, failed_tail_chars=300)
        == "[failed] boom"
    )


def test_a_long_result_keeps_its_opening() -> None:
    content = "HEAD" + "x" * 996 + "TAIL"
    excerpt = result_excerpt(content, failed=False, head_chars=100, failed_tail_chars=100)
    assert excerpt == content[:100] + "\n…(truncated, 904 chars dropped)"


def test_a_long_failed_result_keeps_its_opening_and_its_ending() -> None:
    content = "HEAD" + "x" * 996 + "TAIL"
    excerpt = result_excerpt(content, failed=True, head_chars=100, failed_tail_chars=50)
    assert excerpt == "[failed] " + content[:100] + "\n…(854 chars omitted)…\n" + content[-50:]
    # With no tail asked for, a failed result is cut like any other.
    assert (
        result_excerpt(content, failed=True, head_chars=100, failed_tail_chars=0)
        == "[failed] " + content[:100] + "\n…(truncated, 904 chars dropped)"
    )


def test_excerpts_follow_each_results_own_flag() -> None:
    long = "y" * 500
    step = _main(
        "ran two",
        tool_calls=[("Bash", "{}"), ("Bash", "{}")],
        tool_results=[("c1", long), ("c2", long)],
        tool_result_errors=[False, True],
        has_error_result=True,
    )
    shown = excerpt_results(step, head_chars=20, failed_tail_chars=10)
    assert shown.tool_results[0] == ("c1", "y" * 20 + "\n…(truncated, 480 chars dropped)")
    assert shown.tool_results[1][1].startswith("[failed] ")
    assert shown.tool_results[1][1].endswith("y" * 10)
    # The step itself isn't changed, and a step with no results is handed back.
    assert step.tool_results[0][1] == long
    bare = _main("no tools")
    assert excerpt_results(bare, head_chars=20, failed_tail_chars=10) is bare


def test_a_step_built_without_flags_reads_as_not_failed() -> None:
    step = _main("old shape", tool_results=[("c1", "z" * 100)])
    assert excerpt_results(step, head_chars=10, failed_tail_chars=10).tool_results == [
        ("c1", "z" * 10 + "\n…(truncated, 90 chars dropped)")
    ]


# ---------------------------------------------------------------------------
# tool results the harness saved to a file
# ---------------------------------------------------------------------------

SAVED_PATH = "/home/u/.claude/projects/-work/5e55-1d/tool-results/a1b2c3d4e.txt"
STUB = (
    f"<persisted-output>\nOutput too large (32.8KB). Full output saved to: {SAVED_PATH}\n\n"
    "Preview (first 2KB):\nPREVIEW-HEAD " + "p" * 400
)


def _no_file(_path: str) -> str | None:
    return None


MCP_PATH = (
    "/home/u/.claude/projects/-work/5e55-1d/tool-results/mcp-example-get_item-1700000000000.txt"
)
MCP_STUB = (
    "Error: result (109,264 characters across 1,402 lines) exceeds maximum allowed tokens. "
    f"Output has been saved to {MCP_PATH}.\nFormat: Plain text\nUse offset and limit parameters "
    "to read specific portions of the file."
)


def test_an_mcp_result_saved_for_its_size_is_read_from_the_file_too() -> None:
    saved = persisted_output(MCP_STUB)
    assert saved is not None
    assert (saved.path, saved.size, saved.preview) == (MCP_PATH, "109,264 characters", "")
    escaped = persisted_output('"' + MCP_STUB.replace("\n", "\\n") + '"')
    assert escaped is not None
    assert escaped.path == MCP_PATH
    step = _main("looked", tool_calls=[("slack", "{}")], tool_results=[("c1", MCP_STUB)])
    ((_, shown),) = excerpt_results(
        step,
        head_chars=20,
        failed_tail_chars=10,
        load=lambda _p: "THREAD-HEAD " + "t" * 500 + " THREAD-END",
    ).tool_results
    assert shown.startswith(f"[persisted to {MCP_PATH}, 109,264 characters] THREAD-HEAD ")
    assert shown.endswith("THREAD-END")
    # With no file and no preview, the marker says so and shows nothing else.
    ((_, bare),) = excerpt_results(step, head_chars=20, failed_tail_chars=10).tool_results
    assert bare == (
        f"[persisted to {MCP_PATH}, 109,264 characters] "
        "(the saved file isn't in the corpus, and the transcript has no preview)"
    )
    assert "Use offset" not in bare


def test_a_persisted_stub_is_read_for_its_path_and_size() -> None:
    saved = persisted_output(STUB)
    assert saved is not None
    assert (saved.path, saved.size) == (SAVED_PATH, "32.8KB")
    assert saved.preview.startswith("PREVIEW-HEAD ")
    # Stored JSON-escaped, the way a structured result keeps it.
    escaped = STUB.replace("\n", "\\n")
    again = persisted_output(f'[{{"type": "text", "text": "{escaped}"}}]')
    assert again is not None
    assert (again.path, again.size) == (SAVED_PATH, "32.8KB")
    # An ordinary result, and one that merely mentions the words, are not stubs.
    assert persisted_output("exit 0") is None
    assert persisted_output("the docs say Output too large is saved to a file") is None


def test_a_persisted_result_is_marked_and_cut_from_the_saved_file() -> None:
    body = "FILE-HEAD " + "f" * 2_000 + " FILE-TAIL"
    step = _main("ran it", tool_calls=[("Bash", "{}")], tool_results=[("c1", STUB)])
    asked: list[str] = []

    def load(path: str) -> str | None:
        asked.append(path)
        return body

    ((_, shown),) = excerpt_results(
        step, head_chars=20, failed_tail_chars=10, load=load
    ).tool_results
    assert asked == [SAVED_PATH]
    assert shown.startswith(f"[persisted to {SAVED_PATH}, 32.8KB] FILE-HEAD ")
    # The ending of the file is kept even though the call didn't fail: the
    # last lines of a long output are where it says how it went.
    assert shown.endswith(" FILE-TAIL")
    assert "…(1990 chars omitted)…" in shown
    assert "PREVIEW-HEAD" not in shown


def test_without_the_saved_file_the_preview_is_shown_and_said_to_be_that() -> None:
    step = _main("ran it", tool_calls=[("Bash", "{}")], tool_results=[("c1", STUB)])
    for load in (None, _no_file):
        ((_, shown),) = excerpt_results(
            step, head_chars=20, failed_tail_chars=10, load=load
        ).tool_results
        assert shown.startswith(f"[persisted to {SAVED_PATH}, 32.8KB] PREVIEW-HEAD ")
        assert shown.endswith(
            "(the saved file isn't in the corpus; this is the transcript's preview)"
        )


def test_an_ordinary_result_is_never_looked_up() -> None:
    step = _main("ran it", tool_calls=[("Bash", "{}")], tool_results=[("c1", "plain output")])

    def load(path: str) -> str | None:
        raise AssertionError(path)

    assert excerpt_results(step, head_chars=20, failed_tail_chars=10, load=load).tool_results == [
        ("c1", "plain output")
    ]
