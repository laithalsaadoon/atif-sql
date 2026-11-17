# SPDX-License-Identifier: Apache-2.0

"""What a narration call is told: who wrote each line, where its window sits, what the view leaves out.

Three ways a narration can state something its steps contradict, and the
text that heads each one off:

* a window in the middle of a session narrated as the end of the transcript;
* machine-written text (a goal check-in, a notification) narrated as the
  human asking;
* this renderer's own cuts narrated as output the session truncated.

Pure tests over hand-built steps and the prompt builders.
"""

from __future__ import annotations

import re

import pytest

from atif_analytics.application.prompts import (
    NARRATE_SYSTEM_PROMPT,
    STORY_SYSTEM_PROMPT,
    SUBAGENT_STORY_SYSTEM_PROMPT,
    narration_prompt,
    subagent_report_note,
    window_end_note,
    window_frame,
)
from atif_analytics.application.use_cases import narrate as narrate_module
from atif_analytics.application.use_cases.narrate import (
    _story_status,
    _window_status,
    narrate_prompt_digest,
)
from atif_analytics.domain.authorship import step_author
from atif_analytics.domain.models import WindowNarration
from atif_analytics.domain.narration import (
    BOUNDARY_KINDS,
    DEFAULT_MACHINE_TEXT_CHARS,
    DEFAULT_RESULT_FAILED_TAIL_CHARS,
    DEFAULT_RESULT_TAIL_CHARS,
    GOAL_CHECKIN,
    HUMAN_LABEL,
    NARRATE_CLIPS,
    PARENT_AUTHOR,
    QUEUED_COMMAND,
    SubagentLink,
    SubagentRef,
    Window,
    clip_step,
    narration_source,
    plan_windows,
    render_window,
    result_excerpt,
)
from atif_analytics.domain.transcript import (
    DEFAULT_CLIPS,
    NON_HUMAN_PREVIEW_CHARS,
    StepEvent,
    render_session_text,
    tool_input_preview,
    tool_result_preview,
)
from atif_analytics.infrastructure.settings import AnalyticsSettings

TS = "2026-11-01T10:00:00.000Z"

GOAL_STEP = "Goal check-in: «finish the migration» is still active. Check on the background work."
GOAL_NOTICE = (
    "<task-notification>\n<summary>Goal check-in: background work still running</summary>\n"
    "</task-notification>\nCheck on the background work."
)
TASK_NOTICE = (
    "<task-notification>\n<task-id>b12</task-id>\n<status>completed</status>\n"
    "<summary>Background command finished</summary>\n</task-notification>"
)
PEER_MESSAGE = "Another Claude session sent a message:\nthe disk is full"
COORDINATOR_MESSAGE = "The coordinator sent a message while you were working:\nwrite the report"


def _user(text: str, **kw: object) -> StepEvent:
    return StepEvent(ts=TS, role="user", text=text, **kw)  # type: ignore[arg-type]


def _agent(text: str, **kw: object) -> StepEvent:
    return StepEvent(ts=TS, role="assistant", text=text, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# who wrote a line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "opening",
    [
        "Goal check-in: «finish the migration» is still active",
        'A session-scoped Stop hook is now active with condition: "finish the migration"',
    ],
)
def test_goal_text_is_the_harness_not_a_human(opening: str) -> None:
    """What the harness writes about a session's goal isn't a human turn."""
    assert step_author("user", opening) == "harness"
    assert step_author("user", "  \n" + opening) == "harness"


@pytest.mark.parametrize(
    ("step", "source"),
    [
        (_user("fix the export job"), "human"),
        (_user(GOAL_STEP), GOAL_CHECKIN),
        (_user(GOAL_NOTICE), GOAL_CHECKIN),
        (_user(TASK_NOTICE), "task_notification"),
        (_user(PEER_MESSAGE), QUEUED_COMMAND),
        (_user(COORDINATOR_MESSAGE), QUEUED_COMMAND),
        (_user("Stop hook feedback:\nshow the test output"), "stop_hook"),
        (_user("<system-reminder>\nnote\n</system-reminder>"), "harness"),
        (_user("build the parser", author=PARENT_AUTHOR), PARENT_AUTHOR),
        (_agent("Reading the file."), None),
    ],
)
def test_every_user_role_step_has_a_source(step: StepEvent, source: str | None) -> None:
    assert narration_source(step) == source


def test_a_task_notification_that_only_mentions_a_goal_later_keeps_its_label() -> None:
    """Only a notification whose own summary is a goal check-in is relabeled."""
    notice = _user(TASK_NOTICE + "\n<summary>Goal check-in: unrelated quoted text</summary>")
    assert narration_source(notice) == GOAL_CHECKIN  # the summary sits in its opening
    far = _user(TASK_NOTICE + "x" * 700 + "<summary>Goal check-in: quoted</summary>")
    assert narration_source(far) == "task_notification"


def test_a_window_labels_each_line_with_who_wrote_it() -> None:
    steps = [
        _user("fix the export job"),
        _user(GOAL_STEP),
        _user(GOAL_NOTICE),
        _user(TASK_NOTICE),
        _user(PEER_MESSAGE),
        _agent("On it."),
    ]
    lines = render_window(steps, total_max_chars=100_000).split("\n[")
    labels = [line.lstrip("[").split(" ", 1)[0] for line in lines]
    assert labels == [
        HUMAN_LABEL,
        GOAL_CHECKIN,
        GOAL_CHECKIN,
        "task_notification",
        QUEUED_COMMAND,
        "assistant",
    ]
    # Nothing in a window is labeled "user": a narrator reads that as a person.
    assert "[user " not in render_window(steps, total_max_chars=100_000)


def test_the_other_pipelines_still_read_user() -> None:
    """The judgment prompts are tuned on ``[user ...]``; only narrate relabels."""
    assert render_session_text([_user("fix the export job")]).startswith(f"[user {TS}] ")
    assert render_session_text([_user("hi")], human_label="person").startswith("[person ")


def test_a_goal_checkin_does_not_cut_a_window_as_a_human_turn() -> None:
    """A check-in mid-run used to close the window as ``human_turn``."""
    chain = [_user("start"), _agent("x" * 50), _user(GOAL_STEP), _agent("y" * 50)]
    windows = plan_windows(chain, max_chars=10_000, min_chars=10)
    assert [w.boundary for w in windows] == ["session_end"]
    # A person's message there still does.
    chain[2] = _user("stop, do it the other way")
    assert [w.boundary for w in plan_windows(chain, max_chars=10_000, min_chars=10)] == [
        "human_turn",
        "session_end",
    ]


def test_the_stories_keep_who_asked_and_what_was_counted() -> None:
    """A story folded an audit prompt's request into "the user asked"."""
    assert "Keep who asked for what as the narrations say it." in STORY_SYSTEM_PROMPT
    assert "don't turn it into the\n  user or the human asking" in STORY_SYSTEM_PROMPT
    assert "Don't write that the session or the transcript ended" in STORY_SYSTEM_PROMPT
    assert (
        "don't write that the human or the user asked the subagent for anything"
        in SUBAGENT_STORY_SYSTEM_PROMPT
    )
    for prompt in (STORY_SYSTEM_PROMPT, SUBAGENT_STORY_SYSTEM_PROMPT):
        assert "Copy numbers, names and identifiers as the narrations give them" in prompt


def test_the_prompt_names_the_two_harness_lines_that_are_the_humans_act() -> None:
    assert "(<command-name>) is a command the\n  human typed" in NARRATE_SYSTEM_PROMPT
    assert '"[Request interrupted by user]" is the human\n  interrupting' in NARRATE_SYSTEM_PROMPT


def test_the_prompt_says_a_failed_check_did_not_pass() -> None:
    assert "When a result reports a failure or a non-zero exit, say so" in NARRATE_SYSTEM_PROMPT
    assert "without remarking that a summary, a result or a\n  final line wasn't shown" in (
        NARRATE_SYSTEM_PROMPT
    )


def test_the_prompt_says_only_human_lines_are_a_person() -> None:
    assert "Only [human ...] lines are a person's words." in NARRATE_SYSTEM_PROMPT
    assert "[human ...] lines are a person's words. No other line is." in NARRATE_SYSTEM_PROMPT
    for label in (GOAL_CHECKIN, QUEUED_COMMAND, "task_notification", "stop_hook", "harness"):
        assert f"[{label} ...]" in NARRATE_SYSTEM_PROMPT, label
    assert "[user ...]" not in NARRATE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# what this view leaves out
# ---------------------------------------------------------------------------


def test_narrates_cuts_are_worded_as_not_shown() -> None:
    assert NARRATE_CLIPS.dropped.format(n=7) == "…(+7 chars not shown here)"
    for wording in (NARRATE_CLIPS.dropped, NARRATE_CLIPS.arguments):
        assert "truncat" not in wording
        assert "dropped" not in wording
        assert "omitted" not in wording


def test_a_cut_result_step_and_argument_never_say_truncated() -> None:
    long = "HEAD" + "x" * 2_000 + "TAIL"
    assert result_excerpt(long, failed=False, head_chars=100, failed_tail_chars=50) == (
        long[:100] + "\n…(+1908 chars not shown here)"
    )
    assert result_excerpt(long, failed=True, head_chars=100, failed_tail_chars=50) == (
        "[failed] " + long[:100] + "\n…(+1858 chars not shown here)\n" + long[-50:]
    )
    assert clip_step(_user(long), 100).text == long[:100] + "\n…(+1908 chars not shown here)"
    step = _agent(
        "running",
        tool_calls=[("Bash", '{"command": "' + "c" * 3_000 + '"}')],
        tool_call_ids=["t1"],
        tool_results=[
            ("t1", result_excerpt(long, failed=False, head_chars=100, failed_tail_chars=0))
        ],
        tool_result_errors=[False],
    )
    rendered = render_window(
        [_user(TASK_NOTICE + "n" * 5_000), step],
        total_max_chars=100_000,
        tool_result_bodies=True,
        tool_input_max_chars=200,
    )
    assert rendered.count("chars not shown here)") == 3
    for word in ("truncated", "dropped", "omitted"):
        assert word not in rendered


def test_a_result_that_did_not_fail_keeps_a_shorter_ending() -> None:
    """The last lines are where a command's summary is; without them a narration said none appeared."""
    content = "collected 41 items\n" + "." * 900 + "\n41 passed, 0 failed\nexit=0"
    opening_only = result_excerpt(content, failed=False, head_chars=40, failed_tail_chars=300)
    assert "41 passed" not in opening_only
    shown = result_excerpt(
        content, failed=False, head_chars=40, failed_tail_chars=300, tail_chars=40
    )
    assert shown.endswith("\n41 passed, 0 failed\nexit=0")
    assert DEFAULT_RESULT_TAIL_CHARS < DEFAULT_RESULT_FAILED_TAIL_CHARS
    assert AnalyticsSettings().narrate_tool_result_tail_chars == DEFAULT_RESULT_TAIL_CHARS


def test_a_results_ending_starts_at_a_line_start() -> None:
    """An ending that opened mid-hash read as a second, different hash."""
    digest = "0123456789abcdef0123456789abcdef"
    content = "HEAD" + "x" * 400 + f"\nsha256 {digest}\nexit=0"
    assert content[-30:].startswith(digest[-23:])  # 30 characters back lands inside the hash
    shown = result_excerpt(content, failed=False, head_chars=20, failed_tail_chars=0, tail_chars=30)
    assert shown == content[:20] + f"\n…(+{len(content) - 20 - 6} chars not shown here)\nexit=0"
    assert digest[-23:] not in shown
    # An ending that already starts a line is kept whole.
    whole = result_excerpt(content, failed=False, head_chars=20, failed_tail_chars=0, tail_chars=6)
    assert whole.endswith("not shown here)\nexit=0")
    aligned = result_excerpt(
        content, failed=False, head_chars=20, failed_tail_chars=0, tail_chars=len(digest) + 14
    )
    assert aligned.endswith(f"not shown here)\nsha256 {digest}\nexit=0")
    # One long line has no line start to move to.
    line = "y" * 500
    assert result_excerpt(line, failed=True, head_chars=20, failed_tail_chars=30) == (
        "[failed] " + "y" * 20 + "\n…(+450 chars not shown here)\n" + "y" * 30
    )


def test_the_schema_asks_for_where_things_stood_not_for_an_ending() -> None:
    """The field descriptions reach the model with the schema."""
    outcome = WindowNarration.model_fields["outcome"].description or ""
    assert "never that the session or the transcript ended" in outcome
    intent = WindowNarration.model_fields["intent"].description or ""
    assert "a machine source such as a goal check-in or a notification" in intent


def test_the_other_pipelines_keep_their_wording() -> None:
    assert DEFAULT_CLIPS.dropped.format(n=5) == "…(truncated, 5 chars dropped)"
    assert tool_result_preview("x" * 15, 10) == "x" * 10 + "\n…(truncated, 5 chars dropped)"
    assert tool_input_preview("x" * 15, 10) == "x" * 10 + "…(truncated)"
    assert tool_result_preview("x" * 15, 10, clips=NARRATE_CLIPS).endswith(
        "(+5 chars not shown here)"
    )
    assert tool_input_preview("x" * 15, 10, clips=NARRATE_CLIPS).endswith(
        "(+5 chars not shown here)"
    )
    machine = _user("<system-reminder>" + "r" * 1_000)
    assert "…(truncated, " in render_session_text([machine])


def test_a_notification_is_shown_past_its_file_path() -> None:
    """At the judgment pipelines' 300 characters a notification stops inside its path."""
    notice = (
        "<task-notification>\n<task-id>b12</task-id>\n<output-file>/tmp/"
        + "p" * 320
        + "</output-file>\n"
        "<status>completed</status>\n<summary>Background command finished (exit code 0)</summary>\n"
        "</task-notification>"
    )
    assert len(notice) > NON_HUMAN_PREVIEW_CHARS
    assert len(notice) < DEFAULT_MACHINE_TEXT_CHARS
    shown = render_window([_user(notice)], total_max_chars=100_000)
    assert "<status>completed</status>" in shown
    assert "not shown here" not in shown
    # The whole-session renderer still stops at its own budget.
    assert "<status>completed</status>" not in render_session_text([_user(notice)])
    # And a narrate caller can set the budget.
    assert "<status>" not in render_window(
        [_user(notice)], total_max_chars=100_000, machine_text_max_chars=100
    )


def test_the_prompt_says_a_cut_in_this_view_is_not_a_truncation() -> None:
    assert '"…(+N chars not shown here)" marks text this view leaves out' in NARRATE_SYSTEM_PROMPT
    assert "Don't call it truncated, cut off, clipped or missing" in NARRATE_SYSTEM_PROMPT
    assert "Say output was truncated only when the step itself\n  says so" in NARRATE_SYSTEM_PROMPT
    # The old wording told the narrator to report the cut.
    assert "say that the rest\n  wasn't" not in NARRATE_SYSTEM_PROMPT
    assert "chars dropped" not in NARRATE_SYSTEM_PROMPT


def test_the_prompt_says_to_report_a_number_as_printed() -> None:
    assert "Say that two values match or\n  differ only when both are shown whole" in (
        NARRATE_SYSTEM_PROMPT
    )
    assert "Report a number the way the step prints it" in NARRATE_SYSTEM_PROMPT
    assert "Don't convert a unit" in NARRATE_SYSTEM_PROMPT
    assert "don't infer what a bare number\n  measures" in NARRATE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# where a window sits
# ---------------------------------------------------------------------------


def test_a_windows_transcript_opens_and_closes_with_where_it_sits() -> None:
    opens, ends = window_frame(7, "size_cap")
    assert opens == "[window 7 of the session; not the last window: more steps follow it]"
    assert ends == "[end of window 7; next, not shown: the agent's steps go on]"
    opens, ends = window_frame(7, "session_end")
    assert opens == "[window 7 of the session; the last window recorded so far]"
    assert ends == "[end of window 7; no later steps are recorded]"
    assert window_frame(2, "human_turn", subagent_stream=True)[0].startswith(
        "[window 2 of the subagent's steps; not the last window"
    )


@pytest.mark.parametrize("kind", BOUNDARY_KINDS)
def test_the_frame_is_inside_the_transcript_fence(kind: str) -> None:
    opens, ends = window_frame(3, kind)
    prompt = narration_prompt(
        "[assistant t] working", window_index=3, boundary=kind, closes=window_end_note(kind)
    )
    body = prompt.split("<transcript id=", 1)[1].split("</transcript id=", 1)[0]
    lines = body.split("\n")[1:-1]
    assert lines[0] == opens
    assert lines[-1] == ends
    assert lines[1:-1] == ["[assistant t] working"]
    # Without a boundary the transcript is the window alone.
    assert opens not in narration_prompt("[assistant t] working", window_index=3)


def test_only_the_last_window_is_called_the_last_and_only_so_far() -> None:
    for kind in BOUNDARY_KINDS:
        for stream in (False, True):
            opens, ends = window_frame(1, kind, subagent_stream=stream)
            note = window_end_note(kind, subagent_stream=stream)
            if kind == "session_end":
                assert "last window recorded so far" in opens
                assert "recorded so far" in note
            else:
                assert "not the last window" in opens
                assert "next, not shown" in ends
                assert note.startswith("This isn't the last window")


def _handed_to_the_model() -> list[str]:
    """Every sentence the pipeline writes about a window's or a subagent's end."""
    said: list[str] = []
    for kind in BOUNDARY_KINDS:
        for stream in (False, True):
            said.extend(window_frame(0, kind, subagent_stream=stream))
            said.extend(
                window_end_note(kind, subagent_stream=stream, report=state)
                for state in ("received", "none", "running")
            )
    said.extend(subagent_report_note(state) for state in ("received", "none", "running"))
    ref = SubagentRef(agent_id="a")
    window = Window(index=0, start=0, stop=5, boundary="size_cap", chars=10)
    for link in (
        SubagentLink(ref=ref, launch=1),
        SubagentLink(ref=ref, launch=1, returned=3),
        SubagentLink(ref=ref, launch=1, returned=9),
        SubagentLink(ref=ref, launch=1, ended=3),
        SubagentLink(ref=ref, launch=None, ended=3),
    ):
        status = _window_status(link, window)
        if status is not None:
            said.append(status)
        said.append(_story_status(link))
    return said


#: "the transcript ends", "the session ended": what a narration must not say
#: of a window, and so what the pipeline must not say to the narrator.
_ENDED_RE = re.compile(r"\b(?:transcript|session)\s+(?:ends|ended|is over|stops|stopped)\b")

#: The one place the words appear: the instruction not to write them.
_PROHIBITION = "don't say that the session or the transcript ended"


def test_nothing_the_pipeline_says_tells_the_narrator_the_transcript_ended() -> None:
    """A status that read "the transcript ends before it finishes" was copied into narrations."""
    for text in _handed_to_the_model():
        assert not _ENDED_RE.search(text.replace(_PROHIBITION, "")), text


def test_the_prompt_never_asks_for_the_transcript_ended() -> None:
    assert (
        "Never write that the session or the\n  transcript ended, stopped or was cut off, in any window."
        in NARRATE_SYSTEM_PROMPT
    )
    assert "only the last one recorded so far" in NARRATE_SYSTEM_PROMPT
    # The examples practice it: no example output says the session ended.
    outputs = re.findall(r"<output>(.*?)</output>", NARRATE_SYSTEM_PROMPT, flags=re.DOTALL)
    assert len(outputs) >= 4
    for output in outputs:
        assert not _ENDED_RE.search(output)
        assert "truncated" not in output


def test_the_digest_moves_with_the_frame_and_the_cut_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row's digest names the prompt that wrote it, so a change to either re-stamps the rows."""
    before = narrate_prompt_digest()

    def unframed(
        window_index: int, boundary: str, *, subagent_stream: bool = False
    ) -> tuple[str, str]:
        del window_index, boundary, subagent_stream
        return "[opens]", "[ends]"

    monkeypatch.setattr(narrate_module, "window_frame", unframed)
    framed = narrate_prompt_digest()
    assert framed != before
    monkeypatch.setattr(narrate_module, "NARRATE_CLIPS", DEFAULT_CLIPS)
    assert narrate_prompt_digest() not in (before, framed)
