# SPDX-License-Identifier: Apache-2.0

"""Narration windowing: where windows are cut, and the properties the pipeline leans on.

Pure domain tests. The unit tests pin each boundary kind on a hand-built
chain; the property tests run :func:`plan_windows` over generated chains
and assert the partition, the size bounds, the thin-window rule, and
prefix stability (the property a resumed session depends on).
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings as hyp_settings, strategies as st

from atif_analytics.domain.narration import (
    BOUNDARY_KINDS,
    Window,
    carry_tail,
    clip_step,
    launches_subagent,
    narratable_chain,
    plan_windows,
    render_window,
    resume_position,
    step_chars,
)
from atif_analytics.domain.transcript import StepEvent

#: Boundaries the transcript marks, as opposed to the two the planner adds.
_MARKS = {"human_turn", "compaction", "subagent_start", "subagent_end"}


def _human(text: str = "do the thing", uuid: str | None = None) -> StepEvent:
    return StepEvent(ts="2026-11-01T10:00:00.000Z", role="user", text=text, uuid=uuid)


def _agent(text: str = "working", uuid: str | None = None, **kw: object) -> StepEvent:
    return StepEvent(ts="2026-11-01T10:00:01.000Z", role="assistant", text=text, uuid=uuid, **kw)  # type: ignore[arg-type]


def _tool(name: str = "Bash", uuid: str | None = None) -> StepEvent:
    return _agent("", uuid, tool_calls=[(name, '{"command": "ls"}')], tool_results=[("c1", "ok")])


def _compact() -> StepEvent:
    return StepEvent(
        ts="2026-11-01T10:00:02.000Z",
        role="user",
        text="This session is being continued from a previous conversation",
        is_compact_summary=True,
    )


def _notification() -> StepEvent:
    return StepEvent(
        ts="2026-11-01T10:00:03.000Z", role="user", text="<task-notification> agent done"
    )


def _unit(step: StepEvent) -> int:
    """Every step measures 10, so the caps read as step counts times ten."""
    del step
    return 10


def _spans(windows: list[Window]) -> list[tuple[int, int, str]]:
    return [(w.start, w.stop, w.boundary) for w in windows]


# ---------------------------------------------------------------------------
# unit: each boundary kind
# ---------------------------------------------------------------------------


def test_a_human_turn_closes_the_window_before_it() -> None:
    chain = [_human(), _agent(), _tool(), _human("now the other thing"), _agent()]
    windows = plan_windows(chain, max_chars=1000, min_chars=0, size_of=_unit)
    assert _spans(windows) == [(0, 3, "human_turn"), (3, 5, "session_end")]
    assert [w.index for w in windows] == [0, 1]
    assert [w.step_count for w in windows] == [3, 2]
    assert [w.chars for w in windows] == [30, 20]


def test_a_compaction_summary_opens_a_new_window() -> None:
    chain = [_human(), _agent(), _compact(), _agent()]
    windows = plan_windows(chain, max_chars=1000, min_chars=0, size_of=_unit)
    assert _spans(windows) == [(0, 2, "compaction"), (2, 4, "session_end")]


def test_a_subagent_launch_is_cut_before_and_after() -> None:
    """A foreground subagent starts and ends on its launching step (the result rides on it)."""
    chain = [_human(), _agent(), _tool("Agent"), _agent("the subagent says"), _tool()]
    assert launches_subagent(chain[2])
    assert not launches_subagent(chain[4])
    windows = plan_windows(chain, max_chars=1000, min_chars=0, size_of=_unit)
    assert _spans(windows) == [
        (0, 2, "subagent_start"),
        (2, 3, "subagent_end"),
        (3, 5, "session_end"),
    ]


def test_the_older_task_tool_name_also_launches_a_subagent() -> None:
    chain = [_agent(), _tool("Task"), _agent()]
    windows = plan_windows(chain, max_chars=1000, min_chars=0, size_of=_unit)
    assert [w.boundary for w in windows] == ["subagent_start", "subagent_end", "session_end"]


def test_a_background_task_notification_closes_as_subagent_end() -> None:
    chain = [_human(), _agent(), _notification(), _agent()]
    windows = plan_windows(chain, max_chars=1000, min_chars=0, size_of=_unit)
    assert _spans(windows) == [(0, 2, "subagent_end"), (2, 4, "session_end")]


def test_a_human_turn_outranks_a_returning_subagent() -> None:
    chain = [_agent(), _tool("Agent"), _human()]
    windows = plan_windows(chain, max_chars=1000, min_chars=0, size_of=_unit)
    assert windows[1].boundary == "human_turn"


def test_a_long_run_of_tool_calls_splits_at_the_size_cap() -> None:
    chain = [_human(), *[_tool() for _ in range(9)]]
    windows = plan_windows(chain, max_chars=40, min_chars=0, size_of=_unit)
    assert _spans(windows) == [(0, 4, "size_cap"), (4, 8, "size_cap"), (8, 10, "session_end")]
    assert all(w.chars <= 40 for w in windows)


def test_a_thin_window_runs_on_into_the_next_one() -> None:
    """A short exchange isn't narrated alone: the mark is passed over."""
    chain = [_human("hi"), _agent("hello"), _human("fix the test"), _tool(), _tool(), _human()]
    windows = plan_windows(chain, max_chars=1000, min_chars=30, size_of=_unit)
    # The first human turn's window is 20 chars at the second human turn
    # (under the floor of 30), so it keeps going and closes at the third.
    assert _spans(windows) == [(0, 5, "human_turn"), (5, 6, "session_end")]


def test_the_last_window_is_kept_however_thin() -> None:
    chain = [_human(), _tool(), _tool(), _tool(), _human("thanks")]
    windows = plan_windows(chain, max_chars=1000, min_chars=30, size_of=_unit)
    assert _spans(windows) == [(0, 4, "human_turn"), (4, 5, "session_end")]


def test_a_step_larger_than_the_cap_gets_its_own_window() -> None:
    sizes = {0: 10, 1: 500, 2: 10}
    chain = [_agent("a"), _agent("b"), _agent("c")]
    by_id = {id(step): sizes[i] for i, step in enumerate(chain)}
    windows = plan_windows(chain, max_chars=100, min_chars=0, size_of=lambda s: by_id[id(s)])
    assert _spans(windows) == [(0, 1, "size_cap"), (1, 2, "size_cap"), (2, 3, "session_end")]
    assert windows[1].chars == 500


def test_start_and_first_index_continue_a_session() -> None:
    chain = [_human(), _agent(), _human(), _agent(), _human(), _agent()]
    whole = plan_windows(chain, max_chars=1000, min_chars=0, size_of=_unit)
    assert [w.index for w in whole] == [0, 1, 2]
    rest = plan_windows(chain, max_chars=1000, min_chars=0, start=4, first_index=2, size_of=_unit)
    assert _spans(rest) == [(4, 6, "session_end")]
    assert rest[0].index == 2


def test_nothing_left_plans_no_window() -> None:
    chain = [_human(), _agent()]
    assert plan_windows(chain, start=2) == []
    assert plan_windows([]) == []


@pytest.mark.parametrize(
    ("max_chars", "min_chars", "start"),
    [(0, 0, 0), (10, 11, 0), (10, -1, 0), (10, 0, 3), (10, 0, -1)],
)
def test_bad_arguments_are_refused(max_chars: int, min_chars: int, start: int) -> None:
    with pytest.raises(ValueError, match="must be"):
        plan_windows([_human(), _agent()], max_chars=max_chars, min_chars=min_chars, start=start)


def test_step_chars_measures_the_rendered_step() -> None:
    step = _agent("hello", tool_calls=[("Bash", '{"command": "ls"}')], tool_results=[("c1", "x")])
    rendered = render_window([step], total_max_chars=10_000)
    assert step_chars(step) == len(rendered) + 1
    assert rendered.count("\n") == 2  # text line, tool_use line, tool_result stub
    assert "(1 chars omitted)" in rendered
    # A step with nothing to render adds nothing to a window.
    assert step_chars(_agent("")) == 0


def test_clip_step_keeps_the_opening_of_an_oversized_body() -> None:
    short = _human("a short ask", uuid="u-1")
    assert clip_step(short, 100) is short
    long = _human("x" * 250 + "TAIL", uuid="u-2")
    clipped = clip_step(long, 100)
    assert clipped.text == "x" * 100 + "\n…(+154 chars not shown here)"
    # Everything else about the step is kept: it's still that human turn.
    assert (clipped.uuid, clipped.role, clipped.author, clipped.ts) == (
        "u-2",
        "user",
        "human",
        long.ts,
    )
    assert step_chars(clipped) < step_chars(long)


def test_narratable_chain_drops_sidechain_steps() -> None:
    side = StepEvent(ts="t", role="assistant", text="subagent work", is_sidechain=True)
    main = _agent("main work")
    assert narratable_chain([side, main, side]) == [main]


# ---------------------------------------------------------------------------
# unit: the read position and the carry-over
# ---------------------------------------------------------------------------


def test_resume_position_is_the_step_after_the_last_narrated_uuid() -> None:
    chain = [_human(uuid="a"), _agent(uuid="b"), _human(uuid="c"), _agent(uuid="d")]
    assert resume_position(chain, last_uuid="b", steps_narrated=2) == 2
    # The uuid wins over a count that disagrees with it.
    assert resume_position(chain, last_uuid="b", steps_narrated=0) == 2
    assert resume_position(chain, last_uuid="d", steps_narrated=4) == 4


def test_resume_position_falls_back_to_the_count() -> None:
    chain = [_human(uuid="a"), _agent(uuid="b"), _human(uuid="c")]
    assert resume_position(chain, last_uuid="gone", steps_narrated=2) == 2
    assert resume_position(chain, last_uuid=None, steps_narrated=1) == 1
    # Clamped: a count past the end reads as "everything is narrated".
    assert resume_position(chain, last_uuid=None, steps_narrated=99) == 3
    assert resume_position(chain, last_uuid=None, steps_narrated=0) == 0


def test_carry_tail_keeps_the_end_of_the_previous_window() -> None:
    steps = [_agent(f"step {i}") for i in range(10)]
    tail = carry_tail(steps, max_steps=3, max_chars=10_000)
    assert [line.rsplit(" ", 1)[-1] for line in tail.splitlines()] == ["7", "8", "9"]
    # The character ceiling cuts from the front, at a line start.
    short = carry_tail(steps, max_steps=3, max_chars=60)
    assert short.startswith("[assistant ")
    assert short.endswith("step 9")
    assert len(short) <= 60
    assert carry_tail(steps, max_steps=0, max_chars=100) == ""
    assert carry_tail([], max_steps=3, max_chars=100) == ""


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------

_KINDS = st.sampled_from(["human", "agent", "tool", "spawn", "compact", "notification", "empty"])


def _build(kind: str, size: int) -> StepEvent:
    body = "x" * size
    builders = {
        "human": lambda: _human(body),
        "agent": lambda: _agent(body),
        "tool": lambda: _agent(body, tool_calls=[("Bash", "{}")], tool_results=[("c", "r")]),
        "spawn": lambda: _agent(body, tool_calls=[("Agent", "{}")], tool_results=[("c", "r")]),
        "compact": _compact,
        "notification": _notification,
        "empty": lambda: _agent(""),
    }
    return builders[kind]()


_CHAINS = st.lists(st.tuples(_KINDS, st.integers(min_value=1, max_value=400)), max_size=60).map(
    lambda specs: [_build(kind, size) for kind, size in specs]
)
_CAPS = st.integers(min_value=1, max_value=2_000).flatmap(
    lambda max_chars: st.tuples(st.just(max_chars), st.integers(min_value=0, max_value=max_chars))
)


@given(chain=_CHAINS, caps=_CAPS, data=st.data())
@hyp_settings(max_examples=300, deadline=None)
def test_windows_partition_the_steps_from_start(
    chain: list[StepEvent], caps: tuple[int, int], data: st.DataObject
) -> None:
    max_chars, min_chars = caps
    start = data.draw(st.integers(min_value=0, max_value=len(chain)))
    first_index = data.draw(st.integers(min_value=0, max_value=50))
    windows = plan_windows(
        chain, max_chars=max_chars, min_chars=min_chars, start=start, first_index=first_index
    )
    # Contiguous, in order, none empty, covering exactly chain[start:].
    cursor = start
    for offset, window in enumerate(windows):
        assert window.start == cursor
        assert window.stop > window.start
        assert window.index == first_index + offset
        assert window.boundary in BOUNDARY_KINDS
        assert window.chars == sum(step_chars(s) for s in chain[window.start : window.stop])
        cursor = window.stop
    assert cursor == len(chain)
    assert (windows == []) == (start == len(chain))
    # Only the last window ends the session, and it always does.
    assert [w.boundary == "session_end" for w in windows] == [False] * (len(windows) - 1) + [True][
        : len(windows)
    ]


@given(chain=_CHAINS, caps=_CAPS)
@hyp_settings(max_examples=300, deadline=None)
def test_size_and_thin_window_bounds(chain: list[StepEvent], caps: tuple[int, int]) -> None:
    max_chars, min_chars = caps
    for window in plan_windows(chain, max_chars=max_chars, min_chars=min_chars):
        # Over the ceiling only as one step that is itself over it (steps
        # that render nothing may sit in front of it; they add no size).
        if window.chars > max_chars:
            sized = [s for s in chain[window.start : window.stop] if step_chars(s) > 0]
            assert len(sized) == 1
        # A transcript mark never closes a window under the floor.
        if window.boundary in _MARKS:
            assert window.chars >= min_chars
        # And a mark is only claimed where the transcript has one.
        if window.boundary in _MARKS:
            after, before = chain[window.stop], chain[window.stop - 1]
            assert (
                (after.role == "user" and after.author == "human")
                or after.is_compact_summary
                or launches_subagent(after)
                or launches_subagent(before)
                or after.author == "task_notification"
            )


@given(chain=_CHAINS, caps=_CAPS, data=st.data())
@hyp_settings(max_examples=300, deadline=None)
def test_planning_is_prefix_stable(
    chain: list[StepEvent], caps: tuple[int, int], data: st.DataObject
) -> None:
    """Windows closed before the end of a shorter transcript don't move when it grows.

    This is what lets a session that resumes later keep its narrated rows:
    the windows it already has are the windows a from-scratch plan of the
    longer transcript would also cut.
    """
    max_chars, min_chars = caps
    cut = data.draw(st.integers(min_value=0, max_value=len(chain)))
    shorter = plan_windows(chain[:cut], max_chars=max_chars, min_chars=min_chars)
    longer = plan_windows(chain, max_chars=max_chars, min_chars=min_chars)
    closed = shorter[:-1]  # the last window of the shorter plan is still open
    assert longer[: len(closed)] == closed


@given(chain=_CHAINS, caps=_CAPS)
@hyp_settings(max_examples=200, deadline=None)
def test_resuming_from_a_closed_window_replans_the_same_cuts(
    chain: list[StepEvent], caps: tuple[int, int]
) -> None:
    """Planning from any window's start gives the windows the whole plan has from there."""
    max_chars, min_chars = caps
    whole = plan_windows(chain, max_chars=max_chars, min_chars=min_chars)
    for window in whole:
        rest = plan_windows(
            chain,
            max_chars=max_chars,
            min_chars=min_chars,
            start=window.start,
            first_index=window.index,
        )
        assert rest == whole[window.index :]
