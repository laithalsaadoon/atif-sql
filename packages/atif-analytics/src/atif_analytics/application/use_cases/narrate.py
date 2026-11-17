# SPDX-License-Identifier: Apache-2.0

"""Narrate pipeline: a plain-language account of each window of a session, and a story per session.

A narration says what the agent was trying to do in one stretch of a
session, what it did, and where things stood at the end. It's the semantic
trace beside the technical one: a person debugging a run reads it first, and
so can a review pipeline, opening raw steps only where the narration points.

Transcript text is sent to a third-party model provider on this path: each
window's rendered steps leave this machine, tool arguments included, and
with them the opening and the ending of every tool result unless
``ATIF_SQL_NARRATE_TOOL_RESULT_HEAD_CHARS`` is 0.

Streams. A session is narrated as several streams
(:func:`~atif_analytics.domain.narration.split_streams`): the main chain,
and one per subagent, keyed by the ``agent_id`` its sidechain steps carry.
Each stream is windowed and narrated on its own, with its own rows and its
own read position. The converter's declared subagent list (the rows of
atif-duck's ``subagents`` view) says which call launched each subagent, and
:func:`~atif_analytics.domain.narration.link_subagents` finds that call's
step and the step where the subagent came back.

How one session is narrated:

1. Subagents first, deepest first, siblings side by side. Each subagent's
   steps are cut into windows by
   :func:`~atif_analytics.domain.narration.plan_windows` and narrated in
   order, then folded into a subagent story with a two-to-four-sentence
   digest.
2. Then the stream that launched them. The window where a subagent's work
   ends is shown the subagent's digest, so the narration can say what the
   subagent was asked, what it did and what it returned. A window that
   launches a subagent still running at its end is told only that, since
   the digest describes work the agent hadn't seen yet. Where no step shows the
   report coming back (a background subagent whose output the agent read
   from a file), the end is placed by the clock, at the launching stream's
   first step after the subagent's last, and said to be that. A subagent
   the launching stream never outlasts is said to be still running.
3. Within a stream each call carries the previous window's narration and
   the last few raw steps of that window, then the new window. That
   carry-over is the sliding part: no call reads older steps again.
4. Once every stream is narrated, the main chain's narrations and the
   subagents' digests are folded into one short session story.

Every session is eligible, one-shot jobs and turn audits included: a
narration describes what happened, so it doesn't need a human in the loop
the way the judgment pipelines do.

Content the harness moved out of the transcript. Two kinds reach a call.
A tool result over the harness's size limit is saved to a file and replaced
by a stub; the window marks it ``[persisted to PATH, SIZE]`` and shows the
opening and ending of the saved file, read from the corpus's archive of the
session's side files (never from the path itself). A background subagent's
report arrives as a queued task notification, not a step; it's read from
the session's events and is what places the subagent's return.

Who wrote a line, and where a window sits. Every user-role line of a
window's transcript carries its source
(:func:`~atif_analytics.domain.narration.narration_source`): ``human`` for a
person, and a machine label (a stop hook, a task notification, a goal
check-in, a queued command, other harness text) for the rest, and the prompt
says that only ``human`` lines are a person. The transcript opens and closes
with a line that says whether the window is the last of its stream and what
comes next (:func:`~atif_analytics.application.prompts.window_frame`), and
the last window is called the last recorded so far: a resumed session keeps
the rows it has, so no narration says the session or the transcript ended.

What a call is shown. A tool call's arguments are previewed at
``narrate_tool_input_chars`` (1,200; the judgment pipelines read 400), and
each tool result as its opening ``narrate_tool_result_head_chars`` (300)
plus its last ``narrate_tool_result_tail_chars`` (200), or the last
``narrate_tool_result_failed_tail_chars`` (300) of a failed one. These are narrate's own bounds; the other pipelines still read a result
as its size. A step whose text is longer than a window is cut to its
opening. Every cut is worded "…(+N chars not shown here)": the text was
whole in the session, and "truncated" is narrated as a fact about the
output.

The output cap. Each call sends the model's own output limit as its cap
(:func:`atif_models.domain.registry.with_full_output`), because reasoning
tokens count toward the cap and a smaller one can end a call with the
answer unwritten. A call that still stops on length is retried once by the
adapter at a lower effort and then fails; a failed call writes no row.

The read position. The caches are append-only, and a stream's position is
its last window row: ``last_step_uuid`` names the last narrated step and the
rows' ``step_count`` values add up to how many steps are covered. A session
that grows after it was narrated (a resume hours later, a background
subagent that finished late) moves its checkpoint bounds, is admitted again,
and every stream continues from the step after its own last row with the
next ``window_index``; earlier rows are never rewritten. A story is told
again when the windows under it grew, and the views keep the newest. The
SQLite checkpoint keeps its usual job (skip a session whose bounds haven't
moved) and holds no position of its own, so the position can't disagree
with the rows.

Money. Sessions are admitted newest-first up to
``llm_max_sessions_per_run``. The dollar ceiling is checked before every
model call inside a session as well as between session batches, so a long
session stops mid-way rather than finishing over budget; sibling subagents
run side by side, so the calls in flight when the ceiling is crossed can be
as many as there are siblings. A session stopped by the ceiling, or by a
failed call, keeps the windows it already wrote, isn't checkpointed, and
continues from them on the next run. A failed session goes on the retry
queue; a refused window is terminal and is written as a row whose three
fields read ``[refused]``, so the chain of windows stays whole.

Output schema, ``analytics/session_narration`` (one row per window)::

    session_id       VARCHAR
    agent_id         VARCHAR    (the subagent whose stream the window
                                 belongs to; NULL on the main chain, as in
                                 ``steps``)
    window_index     INTEGER    (0-based within the stream)
    first_step_uuid  VARCHAR    (the window's first step: source_uuids[0])
    last_step_uuid   VARCHAR    (the window's last step)
    started_at       TIMESTAMP  (first step's time)
    ended_at         TIMESTAMP  (last step's time)
    step_count       INTEGER    (the stream's steps in the window)
    boundary         VARCHAR    (what closed it: human_turn | compaction |
                                 subagent_start | subagent_end |
                                 parent_message | size_cap | session_end)
    intent           VARCHAR
    actions          VARCHAR
    outcome          VARCHAR
    model_id         VARCHAR    (the model that wrote the row)
    prompt_digest    VARCHAR    (the prompt that wrote it)
    narrated_at      TIMESTAMP

``analytics/subagent_stories`` (one row each time a subagent is folded)::

    session_id       VARCHAR
    agent_id         VARCHAR
    story            VARCHAR
    digest           VARCHAR    (two to four sentences; what the launching
                                 window and the session story are shown)
    returned         BOOLEAN    (a step of the launching stream shows its
                                 report coming back)
    window_count     INTEGER
    step_count       INTEGER
    first_step_uuid  VARCHAR
    last_step_uuid   VARCHAR
    started_at       TIMESTAMP
    ended_at         TIMESTAMP
    model_id         VARCHAR
    prompt_digest    VARCHAR
    narrated_at      TIMESTAMP

``analytics/session_stories`` (one row each time a session is folded)::

    session_id             VARCHAR
    story                  VARCHAR
    window_count           INTEGER    (main-chain windows the story covers)
    step_count             INTEGER    (main-chain steps those windows cover)
    subagent_count         INTEGER    (subagent stories folded in)
    subagent_window_count  INTEGER    (windows under those stories)
    first_step_uuid        VARCHAR
    last_step_uuid         VARCHAR
    started_at             TIMESTAMP
    ended_at               TIMESTAMP
    model_id               VARCHAR
    prompt_digest          VARCHAR
    narrated_at            TIMESTAMP
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import polars as pl
from loguru import logger

from atif_analytics.application.prompts import (
    NARRATE_SYSTEM_PROMPT,
    STORY_SYSTEM_PROMPT,
    SUBAGENT_STORY_REQUEST,
    SUBAGENT_STORY_SYSTEM_PROMPT,
    narration_prompt,
    narration_text,
    prompt_digest,
    story_prompt,
    subagent_note,
    subagent_report_note,
    window_end_note,
    window_frame,
)
from atif_analytics.application.use_cases._shared import (
    RunBudget,
    build_cache,
    build_checkpoint,
    build_provider,
    build_retry_queue,
    gather_under_budget,
    pipeline_usage,
)
from atif_analytics.domain.costs import estimate_cost_tokens, tokens_for_chars
from atif_analytics.domain.models import SessionStory, SubagentStory, WindowNarration
from atif_analytics.domain.narration import (
    BOUNDARY_KINDS,
    HUMAN_LABEL,
    NARRATE_CLIPS,
    SubagentLink,
    Window,
    carry_tail,
    clip_step,
    excerpt_results,
    link_subagents,
    plan_windows,
    render_window,
    resume_position,
    split_streams,
)
from atif_analytics.infrastructure.corpus_reader import CorpusReader, parse_ts
from atif_analytics.infrastructure.sqlite_state import checkpointer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from atif_analytics.domain.transcript import StepEvent
    from atif_analytics.infrastructure.parquet_cache import ParquetCache
    from atif_analytics.infrastructure.settings import AnalyticsSettings
    from atif_analytics.infrastructure.sqlite_state.checkpointer import SqliteCheckpoint
    from atif_analytics.infrastructure.sqlite_state.retry_queue import SqliteRetryQueue
    from atif_models.domain.ports import LlmStructuredProvider
    from atif_models.domain.registry import ModelSpec

PIPELINE = "narrate"

NARRATION_SCHEMA: dict[str, Any] = {
    "session_id": pl.Utf8,
    "agent_id": pl.Utf8,
    "window_index": pl.Int32,
    "first_step_uuid": pl.Utf8,
    "last_step_uuid": pl.Utf8,
    "started_at": pl.Datetime("us", "UTC"),
    "ended_at": pl.Datetime("us", "UTC"),
    "step_count": pl.Int32,
    "boundary": pl.Utf8,
    "intent": pl.Utf8,
    "actions": pl.Utf8,
    "outcome": pl.Utf8,
    "model_id": pl.Utf8,
    "prompt_digest": pl.Utf8,
    "narrated_at": pl.Datetime("us", "UTC"),
}

SUBAGENT_STORY_SCHEMA: dict[str, Any] = {
    "session_id": pl.Utf8,
    "agent_id": pl.Utf8,
    "story": pl.Utf8,
    "digest": pl.Utf8,
    "returned": pl.Boolean,
    "window_count": pl.Int32,
    "step_count": pl.Int32,
    "first_step_uuid": pl.Utf8,
    "last_step_uuid": pl.Utf8,
    "started_at": pl.Datetime("us", "UTC"),
    "ended_at": pl.Datetime("us", "UTC"),
    "model_id": pl.Utf8,
    "prompt_digest": pl.Utf8,
    "narrated_at": pl.Datetime("us", "UTC"),
}

STORY_SCHEMA: dict[str, Any] = {
    "session_id": pl.Utf8,
    "story": pl.Utf8,
    "window_count": pl.Int32,
    "step_count": pl.Int32,
    "subagent_count": pl.Int32,
    "subagent_window_count": pl.Int32,
    "first_step_uuid": pl.Utf8,
    "last_step_uuid": pl.Utf8,
    "started_at": pl.Datetime("us", "UTC"),
    "ended_at": pl.Datetime("us", "UTC"),
    "model_id": pl.Utf8,
    "prompt_digest": pl.Utf8,
    "narrated_at": pl.Datetime("us", "UTC"),
}

#: What a refused window's fields (and a refused story) read. In-band, like
#: classify's refusal row, so a stream's windows stay consecutive.
REFUSED = "[refused]"

#: Rows buffered across the run's sessions before a shard is written. A
#: crash loses at most this many paid calls, plus the ones in flight. The
#: buffer is also written at the end of every session batch, so a shard
#: holds many sessions' rows and the cache doesn't grow a file per window.
_FLUSH_ROWS = 64

#: Backstop on one window's rendered size, as a multiple of the window
#: ceiling. Step text is already cut to the ceiling, so only a step with
#: very many tool calls can reach it.
_HARD_CAP_FACTOR = 4

#: Dry-run OUTPUT budgets per call. Input is measured from the planned
#: windows' rendered length. Output is mostly reasoning on the small GPT-6
#: tier: a call's reply is a few hundred tokens, and reasoning tokens bill
#: as output, so the budget covers both.
_AVG_WINDOW_OUT_TOKENS = 650
_AVG_STORY_OUT_TOKENS = 650
#: Dry-run estimate of one stored narration's length in a later prompt.
_AVG_NARRATION_CHARS = 700


def narrate_prompt_digest() -> str:
    """The digest stamped on window rows: system prompt, schema, and the message builder's wording."""
    return prompt_digest(
        NARRATE_SYSTEM_PROMPT,
        json.dumps(WindowNarration.model_json_schema(), sort_keys=True),
        narration_prompt("w", window_index=0),
        narration_prompt(
            "w", window_index=1, previous_narration="n", previous_steps="s", subagents="a"
        ),
        narration_prompt("w", window_index=0, subagent_stream=True),
        *(
            line
            for kind in BOUNDARY_KINDS
            for stream in (False, True)
            for line in window_frame(0, kind, subagent_stream=stream)
        ),
        NARRATE_CLIPS.dropped,
        NARRATE_CLIPS.arguments,
        HUMAN_LABEL,
        subagent_note(agent_type="t", description="d", digest="g", status="s"),
        *(window_end_note(kind) for kind in BOUNDARY_KINDS),
        *(
            window_end_note("session_end", subagent_stream=True, report=state)
            for state in ("received", "none", "running")
        ),
    )


def story_prompt_digest() -> str:
    """The digest stamped on session story rows."""
    return prompt_digest(
        STORY_SYSTEM_PROMPT,
        json.dumps(SessionStory.model_json_schema(), sort_keys=True),
        story_prompt("n", subagents="a"),
    )


def subagent_story_prompt_digest() -> str:
    """The digest stamped on subagent story rows."""
    return prompt_digest(
        SUBAGENT_STORY_SYSTEM_PROMPT,
        json.dumps(SubagentStory.model_json_schema(), sort_keys=True),
        story_prompt("n", subagents="a", request=SUBAGENT_STORY_REQUEST),
        *(subagent_report_note(state) for state in ("received", "none", "running")),
    )


@dataclass(slots=True)
class _SessionResult:
    """What one session's narration did this run."""

    session_id: str
    windows: int = 0
    subagent_stories: int = 0
    stories: int = 0
    #: Every stream is narrated and every story is current.
    complete: bool = False
    #: The call that failed, when one did (the session goes on the retry queue).
    error: BaseException | None = None
    refused: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Session:
    """One session while it's being narrated: its streams, its links, and its rows so far."""

    session_id: str
    #: Stream key (``None`` for the main chain) to the chain as the calls
    #: see it: long text cut, tool results excerpted.
    streams: dict[str | None, list[StepEvent]]
    links: list[SubagentLink]
    #: Stream key to its window rows, stored ones first, in window order.
    rows: dict[str | None, list[dict[str, Any]]]
    #: Agent id to its newest subagent story row (stored, or told this run).
    subagent_stories: dict[str, dict[str, Any]]
    #: Windows the stored session story covers, main chain and subagents together.
    story_coverage: int
    result: _SessionResult

    def children(self, key: str | None) -> list[SubagentLink]:
        """The subagents launched from stream ``key``, in id order."""
        return sorted(
            (link for link in self.links if link.parent == key),
            key=lambda link: link.ref.agent_id,
        )

    def link(self, agent_id: str) -> SubagentLink:
        """The link of one subagent stream."""
        return next(link for link in self.links if link.ref.agent_id == agent_id)


def _read_session(cache: ParquetCache, session_id: str) -> list[dict[str, Any]]:
    """One session's rows from a cache, oldest shard first; empty when it has none.

    A shard written before a column existed reads that column as null.
    """
    parts = cache.iter_part_files()
    if not parts:
        return []
    frame = (
        pl.scan_parquet([str(p) for p in parts], missing_columns="insert")
        .filter(pl.col("session_id") == session_id)
        .collect()
    )
    return frame.to_dicts()


def _edge_uuid(steps: Sequence[StepEvent], *, last: bool) -> str | None:
    """The first (or last) step uuid in ``steps``, skipping steps that carry none."""
    for step in reversed(steps) if last else steps:
        if step.uuid:
            return step.uuid
    return None


def _edge_ts(steps: Sequence[StepEvent], *, last: bool) -> datetime | None:
    """The first (or last) step time in ``steps``."""
    for step in reversed(steps) if last else steps:
        ts = parse_ts(step.ts)
        if ts is not None:
            return ts
    return None


def _row_text(row: dict[str, Any]) -> str:
    return narration_text(intent=row["intent"], actions=row["actions"], outcome=row["outcome"])


def _story_input(rows: Sequence[dict[str, Any]]) -> str:
    """The window narrations as a story prompt reads them, one block per window."""
    blocks = [
        f"window {row['window_index']} ({row['step_count']} steps, closed by {row['boundary']})\n"
        f"{_row_text(row)}"
        for row in rows
    ]
    return "\n\n".join(blocks)


#: Said of a subagent whose end is placed by the clock: the launching agent
#: read its output some other way (a file, usually), so no step is the return.
_NO_RETURN_STEP = "no step shows its report coming back"


def _window_status(link: SubagentLink, window: Window) -> str | None:
    """How a subagent stands to one window of its launching stream, or ``None`` when it doesn't touch it."""
    launched = link.launch is not None and window.start <= link.launch < window.stop
    back = link.back is not None and window.start <= link.back < window.stop
    if not launched and not back:
        return None
    if link.back is None:
        return "launched in this window; no recorded step shows it finishing"
    if launched and not back:
        return "launched in this window, still running when it ended"
    # Its work is over in this window. A step shows the report coming back,
    # or only the clock says so.
    if link.returned is not None:
        return (
            "launched and returned in this window"
            if launched
            else "launched in an earlier window, returned in this one"
        )
    opening = "launched in this window and" if launched else "launched in an earlier window and"
    during = "it" if launched else "this one"
    return f"{opening} took its last step during {during}; {_NO_RETURN_STEP}"


#: What a launch-only window is shown in place of a subagent's digest.
_ACCOUNT_LATER = "its work is described in the window where it ends"


def _still_running_after(link: SubagentLink, window: Window) -> bool:
    """True when the subagent's work is over in a later window of the launching stream."""
    return link.back is not None and link.back >= window.stop


def _report_state(link: SubagentLink) -> str:
    """``received`` | ``none`` | ``running``: what the transcript says of a subagent's report."""
    if link.returned is not None:
        return "received"
    return "none" if link.ended is not None else "running"


def _story_status(link: SubagentLink) -> str:
    """How a subagent stands at the end of the transcript, for a story's ``<subagents>`` section."""
    if link.returned is not None:
        return "returned"
    if link.ended is not None:
        return f"finished; {_NO_RETURN_STEP}"
    return "still running at the last recorded step"


class _Narrator:
    """One run's shared state: the ports, the settings, and the row buffers."""

    def __init__(
        self,
        settings: AnalyticsSettings,
        *,
        reader: CorpusReader,
        provider: LlmStructuredProvider,
        spec: ModelSpec,
        cache: ParquetCache,
        stories: ParquetCache,
        subagent_stories: ParquetCache,
        budget: RunBudget | None,
    ) -> None:
        self.settings = settings
        self.reader = reader
        self.provider = provider
        self.model_id = spec.model_id
        self.cache = cache
        self.stories = stories
        self.subagent_stories = subagent_stories
        self.budget = budget
        self.window_digest = narrate_prompt_digest()
        self.story_digest = story_prompt_digest()
        self.subagent_story_digest = subagent_story_prompt_digest()
        self.hard_cap = settings.narrate_window_max_chars * _HARD_CAP_FACTOR
        self._windows: list[dict[str, Any]] = []
        self._subagent_stories: list[dict[str, Any]] = []
        self._stories: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # What a call is shown
    # ------------------------------------------------------------------

    def _prepare(self, session_id: str, chain: Sequence[StepEvent]) -> list[StepEvent]:
        """One stream's steps as the calls see them: long text cut, tool results excerpted.

        Done once per stream, so the plan, the rendered window and the
        carry-over all measure and show the same step. A result the harness
        saved to a file is excerpted from the corpus's archived copy.
        """
        settings = self.settings
        head = settings.narrate_tool_result_head_chars
        prepared: list[StepEvent] = []
        for step in chain:
            shown = clip_step(step, settings.narrate_window_max_chars)
            if head > 0:
                shown = excerpt_results(
                    shown,
                    head_chars=head,
                    failed_tail_chars=settings.narrate_tool_result_failed_tail_chars,
                    tail_chars=settings.narrate_tool_result_tail_chars,
                    load=lambda path: self.reader.tool_result_file(session_id, path),
                )
            prepared.append(shown)
        return prepared

    def _render(self, steps: Sequence[StepEvent]) -> str:
        head = self.settings.narrate_tool_result_head_chars
        return render_window(
            steps,
            total_max_chars=self.hard_cap,
            # The results are already excerpts; this only has to stay out of
            # their way.
            tool_result_max_chars=self.hard_cap,
            tool_result_bodies=head > 0,
            tool_input_max_chars=self.settings.narrate_tool_input_chars,
        )

    def _size(self, step: StepEvent) -> int:
        rendered = self._render([step])
        return len(rendered) + 1 if rendered else 0

    def _tail(self, steps: Sequence[StepEvent]) -> str:
        return carry_tail(
            steps,
            max_steps=self.settings.narrate_carry_steps,
            max_chars=self.settings.narrate_carry_max_chars,
            render=self._render,
        )

    def _out_of_budget(self) -> bool:
        return self.budget is not None and self.budget.exhausted()

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------

    def flush(self, *, force: bool = True) -> None:
        """Write the buffered rows as shards; with ``force`` off, only a full window buffer."""
        if self._windows and (force or len(self._windows) >= _FLUSH_ROWS):
            self.cache.write_part(pl.DataFrame(self._windows, schema=NARRATION_SCHEMA))
            self._windows.clear()
        if force and self._subagent_stories:
            self.subagent_stories.write_part(
                pl.DataFrame(self._subagent_stories, schema=SUBAGENT_STORY_SCHEMA)
            )
            self._subagent_stories.clear()
        if force and self._stories:
            self.stories.write_part(pl.DataFrame(self._stories, schema=STORY_SCHEMA))
            self._stories.clear()

    def load(self, session_id: str) -> _Session:
        """Read one session: its streams, its subagent links, and the rows it already has."""
        steps = self.reader.load_full_steps(session_id)
        declared = self.reader.subagents(session_id)
        raw = split_streams(steps)
        links = link_subagents(raw, declared, self.reader.task_notices(session_id))
        streams = {key: self._prepare(session_id, chain) for key, chain in raw.items()}

        rows: dict[str | None, list[dict[str, Any]]] = {key: [] for key in streams}
        for row in _read_session(self.cache, session_id):
            rows.setdefault(row.get("agent_id"), []).append(row)
        for key, stream_rows in rows.items():
            # One row per window, in order. The position is derived from the
            # rows, so a run can't write a window twice; a shard copied in
            # twice could, and the count must not double.
            newest = {int(row["window_index"]): row for row in stream_rows}
            rows[key] = [newest[index] for index in sorted(newest)]

        told: dict[str, dict[str, Any]] = {}
        for row in _read_session(self.subagent_stories, session_id):
            current = told.get(row["agent_id"])
            if current is None or int(row["window_count"]) >= int(current["window_count"]):
                told[row["agent_id"]] = row
        coverage = max(
            (
                int(row["window_count"]) + int(row.get("subagent_window_count") or 0)
                for row in _read_session(self.stories, session_id)
            ),
            default=0,
        )
        return _Session(
            session_id=session_id,
            streams=streams,
            links=links,
            rows=rows,
            subagent_stories=told,
            story_coverage=coverage,
            result=_SessionResult(session_id=session_id),
        )

    def plan(self, session: _Session, key: str | None) -> list[Window]:
        """The windows one stream still has to narrate, from its read position."""
        chain = session.streams[key]
        stored = session.rows.get(key, [])
        last_uuid = stored[-1]["last_step_uuid"] if stored else None
        narrated = sum(int(row["step_count"]) for row in stored)
        start = resume_position(chain, last_uuid=last_uuid, steps_narrated=narrated)
        if last_uuid and (start == 0 or chain[start - 1].uuid != last_uuid):
            logger.warning(
                "narrate: {} stream {} no longer holds its last narrated step {}; "
                "continuing from step count {}",
                session.session_id,
                key or "main",
                last_uuid,
                start,
            )
        return plan_windows(
            chain,
            max_chars=self.settings.narrate_window_max_chars,
            min_chars=self.settings.narrate_window_min_chars,
            start=start,
            first_index=int(stored[-1]["window_index"]) + 1 if stored else 0,
            size_of=self._size,
        )

    def _notes(self, session: _Session, key: str | None, window: Window | None) -> str:
        """The ``<subagents>`` section for one window of stream ``key``, or for its story.

        For a window: the subagents it launched or got back. For a story
        (``window`` is ``None``): every subagent the stream launched.
        A subagent with no story (its steps held nothing to narrate) is
        left out.
        """
        notes: list[str] = []
        for link in session.children(key):
            told = session.subagent_stories.get(link.ref.agent_id)
            if told is None:
                continue
            account = told["digest"]
            if window is not None:
                status = _window_status(link, window)
                if status is None:
                    continue
                if _still_running_after(link, window):
                    # The digest is what the subagent went on to do. In the
                    # window that only launched it, that hasn't happened
                    # yet, and a narration given it would report results
                    # the agent didn't have.
                    account = _ACCOUNT_LATER
            else:
                status = _story_status(link)
            notes.append(
                subagent_note(
                    agent_type=link.ref.agent_type,
                    description=link.ref.description,
                    digest=account,
                    status=status,
                )
            )
        return "\n".join(notes)

    # ------------------------------------------------------------------
    # One session
    # ------------------------------------------------------------------

    async def narrate(self, session_id: str) -> _SessionResult:
        """Narrate one session from its read positions; a failure comes back in the result.

        A failed call (or an unreadable session) sets ``error`` and the
        caller queues the session for retry. The windows written before the
        failure are kept, so the retry continues from them.
        """
        result = _SessionResult(session_id=session_id)
        try:
            session = self.load(session_id)
            session.result = result
            if await self._narrate_stream(session, None):
                result.complete = await self._tell_story(session)
        except Exception as exc:  # noqa: BLE001 - any failure sends the session to the retry queue
            result.error = exc
        return result

    async def _narrate_stream(self, session: _Session, key: str | None) -> bool:
        """Narrate stream ``key`` after the subagents it launched; False when the cost ceiling stopped it.

        The children run side by side and finish (or fail) before the
        stream's own windows start, because those windows are shown the
        children's digests.
        """
        children = session.children(key)
        if children:
            outcomes = await asyncio.gather(
                *(self._narrate_stream(session, link.ref.agent_id) for link in children),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            if not all(outcomes):
                return False
        if not await self._narrate_windows(session, key):
            return False
        return key is None or await self._tell_subagent_story(session, key)

    async def _narrate_windows(self, session: _Session, key: str | None) -> bool:
        """Narrate one stream's remaining windows in order; False when the cost ceiling stopped it."""
        from atif_models.domain.ports import RefusalError

        chain = session.streams[key]
        stored = session.rows.setdefault(key, [])
        windows = self.plan(session, key)

        previous: str | None = None
        previous_steps = ""
        if windows and stored:
            previous = _row_text(stored[-1])
            previous_steps = self._tail(chain[: windows[0].start])

        for window in windows:
            steps = chain[window.start : window.stop]
            text = self._render(steps)
            if not text:
                # Steps that render nothing (no text, calls or results) have
                # nothing to narrate; they cost nothing.
                continue
            if self._out_of_budget():
                return False
            try:
                narration = await self.provider.classify_structured(
                    system=NARRATE_SYSTEM_PROMPT,
                    prompt=narration_prompt(
                        text,
                        window_index=window.index,
                        previous_narration=previous,
                        previous_steps=previous_steps,
                        subagents=self._notes(session, key, window),
                        subagent_stream=key is not None,
                        boundary=window.boundary,
                        closes=window_end_note(
                            window.boundary,
                            subagent_stream=key is not None,
                            report=_report_state(session.link(key)) if key is not None else "none",
                        ),
                    ),
                    schema=WindowNarration,
                )
                fields = (narration.intent, narration.actions, narration.outcome)
            except RefusalError as exc:
                logger.info(
                    "narrate: {} stream {} window {} refused (terminal): {}",
                    session.session_id,
                    key or "main",
                    window.index,
                    exc,
                )
                session.result.refused.append(f"{key or 'main'} window {window.index}")
                fields = (REFUSED, REFUSED, REFUSED)
            row = {
                "session_id": session.session_id,
                "agent_id": key,
                "window_index": window.index,
                "first_step_uuid": _edge_uuid(steps, last=False),
                "last_step_uuid": _edge_uuid(steps, last=True),
                "started_at": _edge_ts(steps, last=False),
                "ended_at": _edge_ts(steps, last=True),
                "step_count": window.step_count,
                "boundary": window.boundary,
                "intent": fields[0],
                "actions": fields[1],
                "outcome": fields[2],
                "model_id": self.model_id,
                "prompt_digest": self.window_digest,
                "narrated_at": datetime.now(UTC),
            }
            stored.append(row)
            self._windows.append(row)
            self.flush(force=False)
            session.result.windows += 1
            previous = _row_text(row)
            previous_steps = self._tail(steps)
        return True

    async def _tell_subagent_story(self, session: _Session, agent_id: str) -> bool:
        """Fold one subagent's windows into its story and digest when they grew; False at the ceiling."""
        from atif_models.domain.ports import RefusalError

        rows = session.rows.get(agent_id, [])
        told = session.subagent_stories.get(agent_id)
        if not rows or (told is not None and int(told["window_count"]) >= len(rows)):
            return True
        if self._out_of_budget():
            return False
        try:
            account = await self.provider.classify_structured(
                system=SUBAGENT_STORY_SYSTEM_PROMPT,
                prompt=story_prompt(
                    _story_input(rows),
                    subagents=self._notes(session, agent_id, None),
                    request=(
                        f"{SUBAGENT_STORY_REQUEST} "
                        f"{subagent_report_note(_report_state(session.link(agent_id)))}"
                    ),
                ),
                schema=SubagentStory,
            )
            story, digest = account.story, account.digest
        except RefusalError as exc:
            logger.info(
                "narrate: {} subagent {} story refused (terminal): {}",
                session.session_id,
                agent_id,
                exc,
            )
            session.result.refused.append(f"{agent_id} story")
            story = digest = REFUSED
        row = {
            "session_id": session.session_id,
            "agent_id": agent_id,
            "story": story,
            "digest": digest,
            "returned": session.link(agent_id).returned is not None,
            "window_count": len(rows),
            "step_count": sum(int(r["step_count"]) for r in rows),
            "first_step_uuid": rows[0]["first_step_uuid"],
            "last_step_uuid": rows[-1]["last_step_uuid"],
            "started_at": rows[0]["started_at"],
            "ended_at": rows[-1]["ended_at"],
            "model_id": self.model_id,
            "prompt_digest": self.subagent_story_digest,
            "narrated_at": datetime.now(UTC),
        }
        session.subagent_stories[agent_id] = row
        self._subagent_stories.append(row)
        session.result.subagent_stories += 1
        return True

    async def _tell_story(self, session: _Session) -> bool:
        """Fold the session into a story when the windows under the stored one grew.

        The story covers every main-chain window the session has, earlier
        runs' included, and every subagent's digest. False when the cost
        ceiling stopped it before the call.
        """
        from atif_models.domain.ports import RefusalError

        rows = session.rows.get(None, [])
        folded = [
            session.subagent_stories[link.ref.agent_id]
            for link in session.links
            if link.ref.agent_id in session.subagent_stories
        ]
        subagent_windows = sum(int(row["window_count"]) for row in folded)
        if not rows and not folded:
            return True
        if session.story_coverage >= len(rows) + subagent_windows:
            return True
        if self._out_of_budget():
            return False
        # Every subagent, nested ones included: the story is of the whole
        # session, and a nested subagent's parent isn't the main chain.
        notes = "\n".join(
            subagent_note(
                agent_type=link.ref.agent_type,
                description=link.ref.description,
                digest=session.subagent_stories[link.ref.agent_id]["digest"],
                status=_story_status(link),
            )
            for link in sorted(session.links, key=lambda link: link.ref.agent_id)
            if link.ref.agent_id in session.subagent_stories
        )
        try:
            told = await self.provider.classify_structured(
                system=STORY_SYSTEM_PROMPT,
                prompt=story_prompt(_story_input(rows), subagents=notes),
                schema=SessionStory,
            )
            story = told.story
        except RefusalError as exc:
            logger.info("narrate: {} story refused (terminal): {}", session.session_id, exc)
            session.result.refused.append("story")
            story = REFUSED
        spans = rows or folded
        self._stories.append(
            {
                "session_id": session.session_id,
                "story": story,
                "window_count": len(rows),
                "step_count": sum(int(r["step_count"]) for r in rows),
                "subagent_count": len(folded),
                "subagent_window_count": subagent_windows,
                "first_step_uuid": spans[0]["first_step_uuid"],
                "last_step_uuid": spans[-1]["last_step_uuid"],
                "started_at": spans[0]["started_at"],
                "ended_at": spans[-1]["ended_at"],
                "model_id": self.model_id,
                "prompt_digest": self.story_digest,
                "narrated_at": datetime.now(UTC),
            }
        )
        session.result.stories = 1
        return True


def _pending(
    settings: AnalyticsSettings,
    *,
    reader: CorpusReader,
    since_days: int | None,
    limit: int | None,
    checkpoint: SqliteCheckpoint,
    retry: SqliteRetryQueue | None,
) -> tuple[list[str], dict[str, tuple[datetime | None, datetime | None]], int]:
    """The sessions to narrate this run, newest-first and capped; the bounds; how many were deferred.

    No session is loaded here: the checkpoint and the retry queue decide
    from the cheap bounds, and a session's steps are read only when its turn
    comes, so a long admitted list never holds more than a batch in memory.
    ``retry`` is ``None`` for a dry run, which reads the checkpoint and
    leaves the queue alone.
    """
    bounds = reader.session_bounds(since_days=since_days, limit=limit)
    unchanged_pending, skipped = checkpointer.filter_unchanged(
        ((sid, lt, mt) for sid, (lt, mt) in bounds.items()),
        pipeline=PIPELINE,
        checkpoint_db_path=checkpoint.db_path,
    )
    keep = set(unchanged_pending)
    if retry is not None:
        retry_ids = set(retry.drain(pipeline=PIPELINE))
        if retry_ids:
            logger.info("narrate: draining {} retry-queue entries", len(retry_ids))
            keep |= retry_ids
        blocked = retry.blocked_units(pipeline=PIPELINE)
        if blocked & keep:
            logger.info(
                "narrate: skipping {} sessions with live retry entries (backoff/attempts cap)",
                len(blocked & keep),
            )
            keep -= blocked
        if skipped:
            logger.info("narrate: skipped {} sessions via checkpoint", skipped)
    admitted = [sid for sid in bounds if sid in keep]
    max_sessions = settings.llm_max_sessions_per_run
    deferred = max(0, len(admitted) - max_sessions)
    if deferred and retry is not None:
        logger.warning(
            "narrate: capped at {} sessions (llm_max_sessions_per_run); {} deferred",
            max_sessions,
            deferred,
        )
    return admitted[:max_sessions], bounds, deferred


async def _narrate_async(
    narrator: _Narrator,
    *,
    pending: list[str],
    bounds: dict[str, tuple[datetime | None, datetime | None]],
    checkpoint: SqliteCheckpoint,
    retry: SqliteRetryQueue,
) -> int:
    """Narrate ``pending`` in budget-checked batches; return the sessions completed."""
    completed = 0
    windows = 0
    subagent_stories = 0
    stories = 0
    i = 0
    while i < len(pending):
        try:
            batch, results = await gather_under_budget(
                pending[i:], make_coro=narrator.narrate, budget=narrator.budget
            )
        finally:
            # The rows are written before anything is stamped, so a
            # checkpoint never says more than the caches hold.
            narrator.flush()
        if not batch:
            budget = narrator.budget
            logger.warning(
                "narrate: cost ceiling hit (${:.2f} >= ${:.2f}), "
                "stopping with {} sessions unstarted (nothing stamped)",
                budget.spent_usd() if budget is not None else 0.0,
                budget.max_cost_usd if budget is not None else 0.0,
                len(pending) - i,
            )
            break
        i += len(batch)
        done: list[str] = []
        for sid, res in zip(batch, results, strict=True):
            if isinstance(res, BaseException):
                # ``narrate`` returns its failures; this is a cancellation or
                # the like. Queue the session and keep going.
                logger.warning("narrate: {} failed (queued for retry): {}", sid, res)
                retry.enqueue(pipeline=PIPELINE, unit_id=sid, error=str(res))
                continue
            windows += res.windows
            subagent_stories += res.subagent_stories
            stories += res.stories
            if res.error is not None:
                logger.warning(
                    "narrate: {} failed after {} windows (queued for retry): {}",
                    sid,
                    res.windows,
                    res.error,
                )
                retry.enqueue(pipeline=PIPELINE, unit_id=sid, error=str(res.error))
            elif res.complete:
                done.append(sid)
            else:
                logger.warning(
                    "narrate: {} stopped at the cost ceiling after {} windows; "
                    "it continues from there next run",
                    sid,
                    res.windows,
                )
        if done:
            checkpoint.mark_completed(
                pipeline=PIPELINE,
                rows=[(sid, *bounds.get(sid, (None, None))) for sid in done],
            )
            retry.mark_done(pipeline=PIPELINE, unit_ids=done)
        completed += len(done)
    logger.info(
        "narrate: completed {} sessions, wrote {} window rows, {} subagent stories and {} stories",
        completed,
        windows,
        subagent_stories,
        stories,
    )
    return completed


def _dry_run_plan(
    narrator: _Narrator,
    spec: ModelSpec,
    *,
    pending: list[str],
    deferred: int,
    since_days: int | None,
    limit: int | None,
) -> dict[str, Any]:
    """What a real run would do and cost, measured from the planned windows."""
    settings = narrator.settings
    system_chars = len(NARRATE_SYSTEM_PROMPT)
    carry_chars = _AVG_NARRATION_CHARS + settings.narrate_carry_max_chars
    n_windows = 0
    n_subagents = 0
    n_subagent_stories = 0
    n_stories = 0
    in_chars = 0
    for sid in pending:
        session = narrator.load(sid)
        total = 0
        for key in session.streams:
            windows = narrator.plan(session, key)
            stored = len(session.rows.get(key, []))
            n_windows += len(windows)
            in_chars += sum(system_chars + min(w.chars, narrator.hard_cap) for w in windows)
            in_chars += carry_chars * max(0, len(windows) - (0 if stored else 1))
            total += stored + len(windows)
            if key is None:
                continue
            n_subagents += 1
            told = session.subagent_stories.get(key)
            covered = int(told["window_count"]) if told is not None else 0
            if stored + len(windows) > covered:
                n_subagent_stories += 1
                in_chars += len(SUBAGENT_STORY_SYSTEM_PROMPT) + (
                    (stored + len(windows)) * _AVG_NARRATION_CHARS
                )
        if total > session.story_coverage:
            n_stories += 1
            main = len(session.rows.get(None, [])) + len(narrator.plan(session, None))
            in_chars += len(STORY_SYSTEM_PROMPT) + main * _AVG_NARRATION_CHARS
    calls = n_windows + n_subagent_stories + n_stories
    in_tokens = tokens_for_chars(in_chars, spec.chars_per_token)
    out_tokens = n_windows * _AVG_WINDOW_OUT_TOKENS + (
        (n_subagent_stories + n_stories) * _AVG_STORY_OUT_TOKENS
    )
    cost = estimate_cost_tokens(
        in_tokens, out_tokens, (spec.pricing_in or 0.0, spec.pricing_out or 0.0)
    )
    logger.info(
        "narrate --dry-run: {} sessions would run ({} deferred by the cap), "
        "{} windows over {} subagent streams, {} stories, estimated cost ~${:.2f}",
        len(pending),
        deferred,
        n_windows,
        n_subagents,
        n_subagent_stories + n_stories,
        cost,
    )
    return {
        "pipeline": PIPELINE,
        "candidates": len(pending) + deferred,
        "capped_candidates": len(pending),
        "windows": n_windows,
        "subagent_streams": n_subagents,
        "subagent_stories": n_subagent_stories,
        "stories": n_stories,
        "llm_calls": calls,
        "estimated_input_tokens": in_tokens,
        "estimated_output_tokens": out_tokens,
        "estimated_cost_usd": round(cost, 4),
        "max_sessions_per_run": settings.llm_max_sessions_per_run,
        "max_cost_usd_per_run": settings.llm_max_cost_usd_per_run,
        "max_output_tokens": spec.max_completion_tokens,
        "model": spec.model_id,
        "since_days": since_days,
        "limit": limit,
        "dry_run": True,
    }


def narrate_sessions(
    settings: AnalyticsSettings,
    *,
    since_days: int | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    reader: CorpusReader | None = None,
    provider: LlmStructuredProvider | None = None,
    spec: ModelSpec | None = None,
    cache: ParquetCache | None = None,
    stories: ParquetCache | None = None,
    subagent_stories: ParquetCache | None = None,
    checkpoint: SqliteCheckpoint | None = None,
    retry: SqliteRetryQueue | None = None,
    budget: RunBudget | None = None,
) -> int | dict[str, Any]:
    """Narrate each pending session's streams and fold its stories; return sessions completed.

    A session counts once every window of every stream is narrated and its
    stories cover them, whether that took forty calls or none. ``dry_run``
    (the default) returns a plan dict instead: the windows a real run would
    narrate and their measured cost. The dry run reads the checkpoint and
    writes nothing.
    """
    layout = settings.layout()
    reader = (
        reader
        if reader is not None
        else CorpusReader(settings.corpus_root, caps=settings.transcript_caps())
    )
    cache = cache if cache is not None else build_cache(layout.narration_dir)
    stories = stories if stories is not None else build_cache(layout.stories_dir)
    subagent_stories = (
        subagent_stories
        if subagent_stories is not None
        else build_cache(layout.subagent_stories_dir)
    )
    if provider is None or spec is None:
        provider, spec = build_provider(settings, PIPELINE, full_output=True)
    checkpoint = checkpoint if checkpoint is not None else build_checkpoint(layout.state_db_path)

    narrator = _Narrator(
        settings,
        reader=reader,
        provider=provider,
        spec=spec,
        cache=cache,
        stories=stories,
        subagent_stories=subagent_stories,
        budget=budget,
    )

    if dry_run:
        pending, _, deferred = _pending(
            settings,
            reader=reader,
            since_days=since_days,
            limit=limit,
            checkpoint=checkpoint,
            retry=None,
        )
        return _dry_run_plan(
            narrator,
            spec,
            pending=pending,
            deferred=deferred,
            since_days=since_days,
            limit=limit,
        )

    retry = retry if retry is not None else build_retry_queue(layout.state_db_path)
    pending, bounds, _ = _pending(
        settings,
        reader=reader,
        since_days=since_days,
        limit=limit,
        checkpoint=checkpoint,
        retry=retry,
    )
    if not pending:
        logger.info("narrate: no pending sessions")
        return 0
    logger.info("narrate: {} pending sessions", len(pending))
    if budget is not None:
        budget.watch(provider, spec)
    with pipeline_usage(provider, spec, PIPELINE):
        return asyncio.run(
            _narrate_async(
                narrator,
                pending=pending,
                bounds=bounds,
                checkpoint=checkpoint,
                retry=retry,
            )
        )


__all__ = [
    "NARRATION_SCHEMA",
    "PIPELINE",
    "REFUSED",
    "STORY_SCHEMA",
    "SUBAGENT_STORY_SCHEMA",
    "narrate_prompt_digest",
    "narrate_sessions",
    "story_prompt_digest",
    "subagent_story_prompt_digest",
]
