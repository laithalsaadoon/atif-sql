# SPDX-License-Identifier: Apache-2.0

"""Windowing for the narrate pipeline: where one session's narration windows start and end.

A narration is a plain-language account of one stretch of a session. This
module decides the stretches. It's pure: it takes the session's main-chain
steps and returns :class:`Window` values, and nothing here reads a file, a
clock or a model.

Windows are cut where the transcript already marks a change of subject, and
:data:`BOUNDARY_KINDS` names each mark. A window's ``boundary`` is the mark
that CLOSED it, which is what the next window opens on:

* ``human_turn``: the next step is a turn a human wrote.
* ``compaction``: the next step is a compaction summary, so the agent's
  context was rebuilt there.
* ``subagent_start``: the next step launches a subagent (a tool call named
  in :data:`SUBAGENT_TOOL_NAMES`).
* ``subagent_end``: a subagent came back. Either the window's last step
  launched one (ATIF keeps a call's result on the calling step, so a
  foreground subagent starts and ends on one step), or the next step is a
  background task's completion notice.
* ``parent_message``: the next step is a message from the launching agent.
  Only a subagent's stream has these: its instructions arrive in the user
  role.
* ``size_cap``: no mark came, and one more step would take the window past
  ``max_chars`` of rendered transcript. This is what splits a long run of
  tool calls.
* ``session_end``: the steps ran out. Only the last window carries it.

A window too thin to narrate (under ``min_chars`` rendered) isn't closed at
a mark: it runs on into the following steps, so a one-line human turn and
the work it started land in one window. The last window is narrated however
thin it is, because nothing follows it.

Two properties the pipeline relies on, both pinned by the property tests:

* The windows partition the steps from ``start`` on: contiguous, in order,
  none empty, none overlapping.
* Planning is prefix-stable. Every cut is decided from the steps up to and
  including the step after it, so the windows closed before the end of a
  shorter transcript are the same windows when the transcript grows. A
  resumed session therefore keeps the windows it already narrated and only
  plans from the first step past them.

A step whose text is longer than a window (:func:`clip_step`) is cut to its
opening before it's measured or rendered, so one oversized step is narrated
from its start instead of being dropped by the renderer's size cap.

Streams. A session is narrated as several streams, each windowed on its own
(:func:`split_streams`): the main chain, and one stream per subagent, keyed
by the ``agent_id`` its sidechain steps carry. A subagent's stream is a
chain like any other, so the same marks cut it, nested launches included.
:func:`link_subagents` ties each subagent to the stream and step that
launched it and to the step where it came back, from the converter's
declared subagent list (:class:`SubagentRef`, the rows of atif-duck's
``subagents`` view) rather than from guesses about step order, since
parallel subagents interleave in the flat step list.

Who wrote a line. A window's transcript labels every user-role step with
its source (:func:`narration_source`): ``human`` for a person, and for
machine-written text the harness delivered in the user role one of
``stop_hook``, ``task_notification``, ``goal_checkin``, ``queued_command``,
``harness``, ``audit_prompt`` or ``parent_agent``. The narrator is told that
only ``human`` lines are a person, so a check-in or a notification isn't
narrated as something the human asked.

What this renderer leaves out. Every cut narrate makes is worded
:data:`NARRATE_CLIPS` (``…(+N chars not shown here)``), not "truncated": a
narrator shown "truncated" reports the session's output as truncated, which
it wasn't.

Tool results. :func:`excerpt_results` replaces each result with a bounded
opening and a shorter ending (a longer one for a failed result), which is
what lets a narration say whether a step worked and what its last lines
reported. A result the harness saved to a file
is marked as that, and its excerpt comes from the saved file when the
corpus holds it. It's narrate's own bound: the other
pipelines keep rendering a result as its size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast, get_args

from atif_analytics.domain.transcript import (
    TOOL_INPUT_PREVIEW_CHARS,
    ClipWording,
    StepEvent,
    is_human_turn,
    main_chain,
    render_session_text,
    tool_result_preview,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

BoundaryKind = Literal[
    "human_turn",
    "compaction",
    "subagent_start",
    "subagent_end",
    "parent_message",
    "size_cap",
    "session_end",
]

#: Every boundary kind, in the order the docs list them.
BOUNDARY_KINDS: tuple[str, ...] = cast("tuple[str, ...]", get_args(BoundaryKind))

#: Tool names that launch a subagent. Claude Code called the tool ``Task``
#: and renamed it ``Agent``; both appear in one corpus.
SUBAGENT_TOOL_NAMES: frozenset[str] = frozenset({"Agent", "Task"})

#: The author label of a background task's completion notice.
_TASK_NOTIFICATION = "task_notification"

#: The author label narrate gives a subagent's user-role steps a harness
#: didn't write: they're the launching agent's instructions. Not one of
#: :data:`~atif_analytics.domain.authorship.AUTHOR_VALUES`, which describe the
#: main chain; a subagent's stream is narrate's own reading of the steps.
PARENT_AUTHOR = "parent_agent"

#: The label a window's transcript gives a step a person wrote. The judgment
#: pipelines read ``user``; narrate says ``human``, the word its prompt uses
#: for the one source whose words are a person's.
HUMAN_LABEL = "human"

#: Narrate's own labels for two machine sources the authorship rules file
#: under a broader one. A goal check-in restates the session's goal while
#: background work runs, and reads like an instruction; a queued command is
#: a message another agent or session queued for this one.
GOAL_CHECKIN = "goal_checkin"
QUEUED_COMMAND = "queued_command"

#: Openings of a goal check-in delivered as its own step, and the summary
#: line of one delivered inside a task notification.
_GOAL_CHECKIN_PREFIX = "Goal check-in:"
_GOAL_CHECKIN_NOTICE_RE = re.compile(r"<summary>\s*Goal check-in", re.IGNORECASE)

#: Openings of a message another agent or session queued for this one.
_QUEUED_COMMAND_PREFIXES: tuple[str, ...] = (
    "Another Claude session sent a message",
    "The coordinator sent a message",
)

#: How much of a notification's opening is searched for its summary line.
_NOTICE_HEAD_CHARS = 600

#: How narrate words a cut it made itself. "Truncated" is what a harness
#: says of output it cut, so a narrator shown that word reports the output
#: as truncated; this says the text exists and this view leaves it out.
NARRATE_CLIPS = ClipWording(
    dropped="…(+{n} chars not shown here)",
    arguments="…(+{n} chars not shown here)",
)

#: Default body budget for machine-written user-role text in a narration
#: call. The judgment pipelines show 300 characters, which stops a task
#: notification inside its file path, before the status and summary a
#: narration needs; 1,200 shows those and the opening of the result.
DEFAULT_MACHINE_TEXT_CHARS: int = 1_200

#: Stream key for sidechain steps that carry no agent id (sessions converted
#: before the converter recorded one). They're narrated as one stream.
UNATTRIBUTED_AGENT_ID = "unattributed"

#: Claude Code's stub for a tool result it saved to a file. The newlines
#: may be real or JSON-escaped, depending on how the result was stored.
_PERSISTED_RE = re.compile(
    r"<persisted-output>(?:\\n|\s)*Output too large \((?P<size>[^)]{1,24})\)\. "
    r"Full output saved to: (?P<path>[^\s\\\"]+)"
    r"(?:(?:\\n|\s)*Preview \([^)]{0,40}\):(?:\\n|\n)?)?"
)

#: Its other stub, for an MCP tool result over the token limit: an error
#: line naming the saved file, then reading instructions and no preview.
_SAVED_RESULT_RE = re.compile(
    r"Error: result \((?P<size>[\d,]{1,16} characters)[^)]{0,40}\) exceeds maximum allowed "
    r"tokens\. Output has been saved to (?P<path>[^\s\\\"]+?)\.(?=\\n|\s|\"|$)"
)

#: One notice. The closing tag may be missing: the converter cuts a stored
#: record's body at 8 KB, and a subagent's report is often longer than that.
_NOTICE_BLOCK_RE = re.compile(
    r"<task-notification>.*?(?:</task-notification>|(?=<task-notification>)|\Z)", re.DOTALL
)
_NOTICE_TASK_RE = re.compile(r"<task-id>([^<\s]+)</task-id>")
_NOTICE_STATUS_RE = re.compile(r"<status>([^<]*)</status>")

#: How Claude Code's launch result reads when the subagent runs in the
#: background: the call returns at once and the subagent's report arrives
#: later as a task notification.
_ASYNC_LAUNCH_MARKER = "Async agent launched"

#: Default opening of each tool result a narration call is shown, and the
#: ending added for a failed result. Most results run to thousands of
#: characters, and the narrator needs only what happened: 300 shows the status
#: line, the first lines of output or the error name, and a failed command's
#: last lines, where the cause usually is.
DEFAULT_RESULT_HEAD_CHARS: int = 300
DEFAULT_RESULT_FAILED_TAIL_CHARS: int = 300

#: Default ending shown of a result that didn't fail. A command's last lines
#: are where its summary is ("41 passed", "exit=0", the commit it made), and
#: a narrator shown only the opening wrote that no final result appeared.
#: Shorter than a failed result's ending: one or two summary lines, not a
#: traceback.
DEFAULT_RESULT_TAIL_CHARS: int = 200

#: Default tool-call argument preview for narration. The judgment pipelines
#: read 400 characters. A sizable share of tool calls, most of them Bash
#: commands, run longer than that, so the command a narration should name
#: would often be cut; 1,200 shows most calls whole for under twice the
#: argument characters.
DEFAULT_TOOL_INPUT_CHARS: int = 1_200

#: Default window ceiling in rendered characters. About 60 tool-calling steps
#: with result bodies stubbed, small enough that three short fields can still
#: name the files and commands that mattered.
DEFAULT_WINDOW_MAX_CHARS: int = 30_000

#: Default floor under which a window isn't closed at a mark.
DEFAULT_WINDOW_MIN_CHARS: int = 2_000


@dataclass(frozen=True, slots=True)
class Window:
    """One narration window over a session's main-chain steps.

    ``start`` and ``stop`` index the main chain (``stop`` exclusive), so
    ``chain[start:stop]`` is the window's steps. ``index`` is the window's
    ordinal in the session, counted across runs. ``chars`` is the rendered
    size the planner measured.
    """

    index: int
    start: int
    stop: int
    boundary: BoundaryKind
    chars: int

    @property
    def step_count(self) -> int:
        """How many main-chain steps the window holds."""
        return self.stop - self.start


@dataclass(frozen=True, slots=True)
class SubagentRef:
    """One subagent as the converter declared it in ``trajectory.extra.subagents``.

    The row atif-duck's ``subagents`` view shows. ``parent_tool_call_id`` is
    the ``Agent`` / ``Task`` call that launched it, ``parent_agent_id`` the
    subagent that made that call when the launch was nested, and both can be
    absent.
    """

    agent_id: str
    agent_type: str | None = None
    description: str | None = None
    parent_tool_call_id: str | None = None
    parent_agent_id: str | None = None
    spawn_depth: int | None = None


@dataclass(frozen=True, slots=True)
class PersistedOutput:
    """A tool result the harness moved out of the transcript into a file.

    Claude Code replaces a result over its size limit with a stub: the size,
    the path of a file under the session's ``tool-results/`` directory, and
    the first 2 KB as a preview. An MCP result over the token limit gets a
    different stub: an error line with the size and the path, and no
    preview. ``path`` and ``size`` are the stub's own words, and ``preview``
    is what follows them in the first form and empty in the second.
    """

    path: str
    size: str
    preview: str


@dataclass(frozen=True, slots=True)
class TaskNotice:
    """A background task's completion notice, queued by the harness beside the transcript.

    Claude Code writes it as an ``attachment`` record of type
    ``queued_command`` whose prompt is a ``<task-notification>`` block: it
    isn't message content, so no step holds it, and the converter keeps it
    as a ``session_events`` row. ``task_id`` is the subagent's ``agent_id``
    when the task was a subagent (a background shell command has an id of
    its own kind), ``ts`` is when the notice was queued, and ``status`` is
    the block's ``<status>`` (``completed``, ``failed``, ``killed``).
    """

    ts: str
    task_id: str
    status: str | None = None


@dataclass(frozen=True, slots=True)
class SubagentLink:
    """Where one subagent's stream hangs off the stream that launched it.

    ``parent`` is the launching stream's key (``None`` for the main chain).
    ``launch``, ``returned`` and ``ended`` index that stream's chain.
    ``launch`` is the step holding the launching call, and is ``None`` when
    no step in the session holds the declared call (the subagent is
    narrated all the same).

    ``returned`` is the step where the subagent's report came back: its
    launching step for a foreground subagent, and for a background one the
    step its task notification reached the agent on. Claude Code delivers
    that notification as a user-role step or, far more often, queues it
    beside the transcript (:class:`TaskNotice`), in which case the step is
    the first one the agent took after it. It's ``None`` when the transcript
    holds no report for the subagent.

    ``ended`` places the subagent's end by the clock: the first step of the
    launching stream at or after the subagent's own last step. It's ``None``
    when the launching stream stops first, which is a subagent the
    transcript leaves still running. :attr:`back` is where a narration puts
    the subagent's end: ``returned`` when a step shows it, else ``ended``.
    """

    ref: SubagentRef
    parent: str | None = None
    launch: int | None = None
    returned: int | None = None
    ended: int | None = None

    @property
    def back(self) -> int | None:
        """The launching stream's step where the subagent's work is over, as far as the transcript says."""
        return self.returned if self.returned is not None else self.ended


def narration_source(step: StepEvent) -> str | None:
    """The label a window's transcript gives ``step``'s text: who wrote it.

    ``None`` for a step that isn't in the user role. For one that is: its
    ``author`` (``human``, ``stop_hook``, ``task_notification``, ``harness``,
    ``audit_prompt``, or :data:`PARENT_AUTHOR` in a subagent's stream),
    except that a goal check-in is :data:`GOAL_CHECKIN` and a message queued
    by another agent or session is :data:`QUEUED_COMMAND`, whichever author
    the rules filed them under.
    """
    if step.role != "user":
        return None
    text = step.text.lstrip()
    if text.startswith(_GOAL_CHECKIN_PREFIX) or (
        step.author == _TASK_NOTIFICATION
        and _GOAL_CHECKIN_NOTICE_RE.search(text[:_NOTICE_HEAD_CHARS])
    ):
        return GOAL_CHECKIN
    if text.startswith(_QUEUED_COMMAND_PREFIXES):
        return QUEUED_COMMAND
    return step.author or "harness"


def split_streams(steps: Sequence[StepEvent]) -> dict[str | None, list[StepEvent]]:
    """One session's steps as narration streams: the main chain under ``None``, each subagent under its id.

    A subagent's steps come back as a chain of their own: ``is_sidechain`` is
    cleared, so the renderer and the window marks read them the way they
    read a main chain, and a user-role step no harness wrote is labeled
    :data:`PARENT_AUTHOR`, because in a subagent's transcript the "user" is
    the agent that launched it. Order within a stream is the materialized
    order. The main chain is always present, even when empty.
    """
    streams: dict[str | None, list[StepEvent]] = {None: []}
    for step in steps:
        if not step.is_sidechain:
            streams[None].append(step)
            continue
        author = PARENT_AUTHOR if step.role == "user" and step.author == "human" else step.author
        streams.setdefault(step.agent_id or UNATTRIBUTED_AGENT_ID, []).append(
            replace(step, is_sidechain=False, author=author)
        )
    return streams


def task_notices(text: str, ts: str) -> list[TaskNotice]:
    """The task notifications in ``text``, one per task id, stamped ``ts``.

    ``text`` is anything that may hold ``<task-notification>`` blocks: a
    queued-command record's body as JSON, where one notice can appear twice
    (once as the queued prompt, once as it was shown to the agent). A
    notice whose text was cut short still counts: the task id sits at its
    start.
    """
    notices: dict[str, TaskNotice] = {}
    for block in _NOTICE_BLOCK_RE.findall(text):
        task = _NOTICE_TASK_RE.search(block)
        if task is None or task.group(1) in notices:
            continue
        status = _NOTICE_STATUS_RE.search(block)
        notices[task.group(1)] = TaskNotice(
            ts=ts, task_id=task.group(1), status=status.group(1) if status else None
        )
    return list(notices.values())


def _stamp(ts: str) -> datetime | None:
    """An ISO-8601 stamp as a time, or ``None`` when it's missing or unreadable."""
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _when(step: StepEvent) -> datetime | None:
    """A step's time, or ``None`` when its stamp is missing or unreadable."""
    return _stamp(step.ts)


def _first_step_from(chain: Sequence[StepEvent], start: int, moment: datetime) -> int | None:
    """The first step of ``chain`` from ``start`` on that isn't earlier than ``moment``."""
    for position in range(start, len(chain)):
        when = _when(chain[position])
        if when is not None and when >= moment:
            return position
    return None


def _returned_at(
    chain: Sequence[StepEvent], launch: int, ref: SubagentRef, notices: Sequence[TaskNotice]
) -> int | None:
    """The index in ``chain`` where the subagent launched at ``launch`` came back, or ``None``.

    Three places a report can show, earliest wins: the launching call's own
    result (a foreground subagent), a task notification delivered as a
    user-role step, and a task notification the harness queued beside the
    transcript (``notices``). A queued notice has no step of its own, so
    it's placed at the first step that isn't earlier than it: the first step
    the agent took with the report in hand. One that arrives after the
    chain's last step is placed on that last step.
    """
    step = chain[launch]
    for call_id, content in step.tool_results:
        if call_id == ref.parent_tool_call_id and _ASYNC_LAUNCH_MARKER not in content[:400]:
            return launch
    found: list[int] = []
    # The id as a whole token: a notice names its task as
    # ``<task-id>ID</task-id>``, and a short id must not match inside a word.
    named = re.compile(rf"(?<![\w-]){re.escape(ref.agent_id)}(?![\w-])")
    for position in range(launch + 1, len(chain)):
        later = chain[position]
        if later.author == _TASK_NOTIFICATION and named.search(later.text):
            found.append(position)
            break
    launched = _when(step)
    for notice in notices:
        moment = _stamp(notice.ts)
        if notice.task_id != ref.agent_id or moment is None:
            continue
        if launched is not None and moment < launched:
            continue
        position = _first_step_from(chain, launch, moment)
        found.append(position if position is not None else len(chain) - 1)
    return min(found, default=None)


def _ended_at(chain: Sequence[StepEvent], launch: int, own: Sequence[StepEvent]) -> int | None:
    """The first step of ``chain`` from ``launch`` on that isn't earlier than the subagent's last step."""
    last = next((when for step in reversed(own) if (when := _when(step)) is not None), None)
    return None if last is None else _first_step_from(chain, launch, last)


def link_subagents(
    streams: dict[str | None, list[StepEvent]],
    declared: Sequence[SubagentRef],
    notices: Sequence[TaskNotice] = (),
) -> list[SubagentLink]:
    """Tie every subagent stream to its launching step, children before parents.

    ``notices`` are the task notifications the harness queued beside the
    transcript (:class:`TaskNotice`); they're how most background subagents'
    reports show up.

    One link per subagent stream in ``streams``. A stream the converter
    declared nothing for gets a bare :class:`SubagentRef`, so no steps are
    left unnarrated. The launching call is looked for in the declared parent
    stream first and then in every other stream, which also places a nested
    subagent whose ``parent_agent_id`` wasn't recorded.

    The order is deepest first, so a caller that narrates in order has each
    child's story before it narrates the window that launched the child. A
    launch chain that loops back on itself (which a transcript shouldn't
    hold) is cut at the loop rather than followed.
    """
    refs = {ref.agent_id: ref for ref in declared}
    links: dict[str, SubagentLink] = {}
    for key, own in streams.items():
        if key is None:
            continue
        ref = refs.get(key, SubagentRef(agent_id=key))
        first = ref.parent_agent_id if ref.parent_agent_id in streams else None
        candidates = [first, *(k for k in streams if k not in (first, key))]
        link = SubagentLink(ref=ref, parent=first)
        if ref.parent_tool_call_id:
            for parent in candidates:
                launch = next(
                    (
                        position
                        for position, step in enumerate(streams[parent])
                        if ref.parent_tool_call_id in step.tool_call_ids
                    ),
                    None,
                )
                if launch is not None:
                    link = SubagentLink(
                        ref=ref,
                        parent=parent,
                        launch=launch,
                        returned=_returned_at(streams[parent], launch, ref, notices),
                        ended=_ended_at(streams[parent], launch, own),
                    )
                    break
        links[key] = link

    def ancestors(key: str) -> list[str] | None:
        """The launching streams above ``key``, nearest first; ``None`` when they loop."""
        chain: list[str] = []
        current = links[key].parent
        while current is not None:
            if current == key or current in chain:
                return None
            chain.append(current)
            current = links[current].parent
        return chain

    for key in list(links):
        if ancestors(key) is None:
            links[key] = SubagentLink(ref=links[key].ref)

    def depth(key: str) -> int:
        return len(ancestors(key) or [])

    return sorted(links.values(), key=lambda link: (-depth(link.ref.agent_id), link.ref.agent_id))


def persisted_output(content: str) -> PersistedOutput | None:
    """The harness's "saved to a file" stub in a tool result, or ``None`` for an ordinary result."""
    opening = content[:600]
    match = _PERSISTED_RE.search(opening)
    if match is not None:
        return PersistedOutput(
            path=match.group("path"), size=match.group("size"), preview=content[match.end() :]
        )
    saved = _SAVED_RESULT_RE.search(opening)
    if saved is None:
        return None
    return PersistedOutput(path=saved.group("path"), size=saved.group("size"), preview="")


def result_excerpt(
    content: str,
    *,
    failed: bool,
    head_chars: int,
    failed_tail_chars: int,
    keep_tail: bool = False,
    tail_chars: int = 0,
) -> str:
    """One tool result cut to what a narration call is shown.

    The opening ``head_chars`` characters, closed by :data:`NARRATE_CLIPS`'
    "not shown here" line when more followed. A failed result opens with
    ``[failed]`` and keeps its last ``failed_tail_chars`` characters too,
    with the count of what was left out between them. A result that didn't
    fail keeps its last ``tail_chars`` characters the same way (none by
    default), or ``failed_tail_chars`` of them with ``keep_tail``, which a
    result read back from its saved file asks for.
    """
    mark = "[failed] " if failed else ""
    tail = failed_tail_chars if failed or keep_tail else tail_chars
    if tail > 0 and len(content) > head_chars + tail:
        ending = _from_line_start(content, tail)
        omitted = len(content) - head_chars - len(ending)
        return f"{mark}{content[:head_chars]}\n{NARRATE_CLIPS.dropped.format(n=omitted)}\n{ending}"
    return mark + tool_result_preview(content, head_chars, clips=NARRATE_CLIPS)


def _from_line_start(content: str, tail_chars: int) -> str:
    """The last ``tail_chars`` characters of ``content``, begun at a line start when one is in reach.

    An ending that opens in the middle of a line opens in the middle of a
    value: the last half of a hash reads as a different hash, and a narrator
    reported the two as not matching. When the ending holds a line break
    with text after it, the partial first line is left out. An ending that
    is one long line is kept as it is.
    """
    ending = content[-tail_chars:]
    if content[-tail_chars - 1] == "\n":
        return ending
    _, newline, rest = ending.partition("\n")
    return rest if newline and rest.strip() else ending


def excerpt_results(
    step: StepEvent,
    *,
    head_chars: int,
    failed_tail_chars: int,
    load: Callable[[str], str | None] | None = None,
    tail_chars: int = 0,
) -> StepEvent:
    """``step`` with each tool result replaced by its :func:`result_excerpt`.

    ``tail_chars`` is the ending kept of a result that didn't fail.

    A result the harness saved to a file (:func:`persisted_output`) is
    marked ``[persisted to PATH, SIZE]``, and its excerpt is cut from the
    saved file when ``load`` (given the stub's path) returns its text: the
    opening and the ending, since the end of a long output is where a
    command's last lines are. When the file isn't there, the excerpt is cut
    from the transcript's own preview and says so, or says that there's
    neither when the stub carried no preview.

    A step with no results is returned as it is.
    """
    if not step.tool_results:
        return step
    failed = [*step.tool_result_errors, *([False] * len(step.tool_results))]
    excerpts: list[tuple[str, str]] = []
    for position, (call_id, content) in enumerate(step.tool_results):
        saved = persisted_output(content)
        if saved is None:
            shown = result_excerpt(
                content,
                failed=failed[position],
                head_chars=head_chars,
                failed_tail_chars=failed_tail_chars,
                tail_chars=tail_chars,
            )
        else:
            body = load(saved.path) if load is not None else None
            mark = f"[persisted to {saved.path}, {saved.size}] "
            if body is None and not saved.preview:
                shown = (
                    mark + "(the saved file isn't in the corpus, and the transcript has no preview)"
                )
            else:
                shown = mark + result_excerpt(
                    saved.preview if body is None else body,
                    failed=failed[position],
                    head_chars=head_chars,
                    failed_tail_chars=failed_tail_chars,
                    keep_tail=body is not None,
                    tail_chars=tail_chars,
                )
                if body is None:
                    shown += (
                        "\n(the saved file isn't in the corpus; this is the transcript's preview)"
                    )
        excerpts.append((call_id, shown))
    return replace(step, tool_results=excerpts)


def launches_subagent(step: StepEvent) -> bool:
    """True when one of the step's tool calls launches a subagent."""
    return any(name in SUBAGENT_TOOL_NAMES for name, _ in step.tool_calls)


def step_chars(step: StepEvent) -> int:
    """The rendered size of one step: what it adds to a window's transcript.

    Measured with the renderer the prompt is built from (result bodies
    stubbed), plus the newline that joins it to the next step. A step that
    renders nothing (no text, calls or results) measures zero.
    """
    rendered = render_session_text([step])
    return len(rendered) + 1 if rendered else 0


def clip_step(step: StepEvent, max_text_chars: int) -> StepEvent:
    """``step`` with a text body over ``max_text_chars`` cut to its opening.

    A scheduled job's prompt or a pasted document can be several windows
    long on its own. The renderer's whole-transcript cap drops a line that
    doesn't fit, which for a window made of that one step would leave
    nothing to narrate, so the body is cut here instead and ends with
    :data:`NARRATE_CLIPS`' "not shown here" line. A step within the limit is
    returned as it is.
    """
    if len(step.text) <= max_text_chars:
        return step
    return replace(step, text=tool_result_preview(step.text, max_text_chars, clips=NARRATE_CLIPS))


def _mark_before(step: StepEvent, previous: StepEvent) -> BoundaryKind | None:
    """The mark that sits between ``previous`` and ``step``, or ``None``.

    First match wins, so a human turn right after a subagent returns closes
    the window as ``human_turn``.
    """
    marks: tuple[tuple[bool, BoundaryKind], ...] = (
        (is_human_turn(step), "human_turn"),
        (step.is_compact_summary, "compaction"),
        (launches_subagent(step), "subagent_start"),
        (launches_subagent(previous) or step.author == _TASK_NOTIFICATION, "subagent_end"),
        (step.author == PARENT_AUTHOR, "parent_message"),
    )
    return next((kind for holds, kind in marks if holds), None)


def plan_windows(
    chain: Sequence[StepEvent],
    *,
    max_chars: int = DEFAULT_WINDOW_MAX_CHARS,
    min_chars: int = DEFAULT_WINDOW_MIN_CHARS,
    start: int = 0,
    first_index: int = 0,
    size_of: Callable[[StepEvent], int] = step_chars,
) -> list[Window]:
    """Cut ``chain[start:]`` into narration windows.

    ``chain`` is a session's main-chain steps (:func:`narratable_chain`).
    ``start`` is the read position: the first step not yet narrated.
    ``first_index`` numbers the first window returned, so a resumed session
    continues its count. ``size_of`` measures a step; the default renders it.

    Returns no windows when nothing is left from ``start`` on. A window is
    over ``max_chars`` only when one step is: that step gets a window to
    itself, apart from steps in front of it that render nothing.
    """
    if max_chars < 1:
        msg = f"max_chars must be at least 1, got {max_chars}"
        raise ValueError(msg)
    if not 0 <= min_chars <= max_chars:
        msg = f"min_chars must be between 0 and max_chars ({max_chars}), got {min_chars}"
        raise ValueError(msg)
    if not 0 <= start <= len(chain):
        msg = f"start must be between 0 and {len(chain)}, got {start}"
        raise ValueError(msg)

    windows: list[Window] = []
    opened = start
    chars = 0
    for position in range(start, len(chain)):
        step = chain[position]
        size = size_of(step)
        if position > opened:
            mark = _mark_before(step, chain[position - 1])
            closes: BoundaryKind | None = None
            if mark is not None and chars >= min_chars:
                closes = mark
            elif chars > 0 and chars + size > max_chars:
                closes = "size_cap"
            if closes is not None:
                windows.append(
                    Window(
                        index=first_index + len(windows),
                        start=opened,
                        stop=position,
                        boundary=closes,
                        chars=chars,
                    )
                )
                opened = position
                chars = 0
        chars += size
    if opened < len(chain):
        windows.append(
            Window(
                index=first_index + len(windows),
                start=opened,
                stop=len(chain),
                boundary="session_end",
                chars=chars,
            )
        )
    return windows


def narratable_chain(steps: list[StepEvent]) -> list[StepEvent]:
    """A session's main chain, in materialized order: the ``None`` stream of :func:`split_streams`."""
    return main_chain(steps)


def resume_position(
    chain: Sequence[StepEvent], *, last_uuid: str | None, steps_narrated: int
) -> int:
    """Where narration picks up in ``chain``: the index of the first step not yet narrated.

    ``last_uuid`` is the last narrated step's uuid (the row's
    ``last_step_uuid``) and ``steps_narrated`` the count of main-chain steps
    the session's windows cover. The uuid is the anchor: a step keeps it when
    the transcript is converted again, while a count doesn't survive a
    conversion that adds or drops a step. With no uuid, or one the chain no
    longer holds, the count is the fallback, clamped to the chain.
    """
    if last_uuid:
        for position in range(len(chain) - 1, -1, -1):
            if chain[position].uuid == last_uuid:
                return position + 1
    return max(0, min(steps_narrated, len(chain)))


def render_window(
    steps: Sequence[StepEvent],
    *,
    total_max_chars: int,
    tool_result_max_chars: int = 50_000,
    tool_result_bodies: bool = False,
    tool_input_max_chars: int = TOOL_INPUT_PREVIEW_CHARS,
    machine_text_max_chars: int = DEFAULT_MACHINE_TEXT_CHARS,
) -> str:
    """One window's transcript text, as a narration call reads it.

    The whole-session renderer over the window's steps only, with narrate's
    own labels and wording: every user-role line carries its
    :func:`narration_source` (a person's lines read ``human``), machine
    text is shown to ``machine_text_max_chars``, and each cut is worded
    :data:`NARRATE_CLIPS`. ``total_max_chars`` is a backstop: the renderer
    stops at the first line that would cross it.
    """
    labeled = [
        step
        if (source := narration_source(step)) is None or source == step.author
        else replace(step, author=source)
        for step in steps
    ]
    return render_session_text(
        labeled,
        total_max_chars=total_max_chars,
        tool_result_max_chars=tool_result_max_chars,
        tool_result_bodies=tool_result_bodies,
        tool_input_max_chars=tool_input_max_chars,
        full_text_authors=frozenset({PARENT_AUTHOR}),
        human_label=HUMAN_LABEL,
        non_human_max_chars=machine_text_max_chars,
        clips=NARRATE_CLIPS,
    )


def carry_tail(
    steps: Sequence[StepEvent],
    *,
    max_steps: int,
    max_chars: int,
    render: Callable[[Sequence[StepEvent]], str] | None = None,
) -> str:
    """The end of the previous window, rendered: the raw steps a narration call carries over.

    The last ``max_steps`` steps, and of their rendering the last
    ``max_chars`` characters, cut at a line start so no header is torn. The
    end is what's kept because the new window continues from it. ``render``
    is the window renderer in use, so the carried steps read the way the
    window's do; the default is the plain renderer.
    """
    if max_steps < 1 or max_chars < 1 or not steps:
        return ""
    tail_steps = list(steps[-max_steps:])
    rendered = render(tail_steps) if render is not None else render_session_text(tail_steps)
    if len(rendered) <= max_chars:
        return rendered
    tail = rendered[-max_chars:]
    newline = tail.find("\n")
    return tail[newline + 1 :] if newline != -1 else tail


__all__ = [
    "BOUNDARY_KINDS",
    "DEFAULT_MACHINE_TEXT_CHARS",
    "DEFAULT_RESULT_FAILED_TAIL_CHARS",
    "DEFAULT_RESULT_HEAD_CHARS",
    "DEFAULT_RESULT_TAIL_CHARS",
    "DEFAULT_TOOL_INPUT_CHARS",
    "DEFAULT_WINDOW_MAX_CHARS",
    "DEFAULT_WINDOW_MIN_CHARS",
    "GOAL_CHECKIN",
    "HUMAN_LABEL",
    "NARRATE_CLIPS",
    "PARENT_AUTHOR",
    "QUEUED_COMMAND",
    "SUBAGENT_TOOL_NAMES",
    "UNATTRIBUTED_AGENT_ID",
    "BoundaryKind",
    "PersistedOutput",
    "SubagentLink",
    "SubagentRef",
    "TaskNotice",
    "Window",
    "carry_tail",
    "clip_step",
    "excerpt_results",
    "launches_subagent",
    "link_subagents",
    "narratable_chain",
    "narration_source",
    "persisted_output",
    "plan_windows",
    "render_window",
    "result_excerpt",
    "resume_position",
    "split_streams",
    "step_chars",
    "task_notices",
]
