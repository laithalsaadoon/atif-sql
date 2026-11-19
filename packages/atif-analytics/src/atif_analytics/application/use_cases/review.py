# SPDX-License-Identifier: Apache-2.0

"""Review pipeline: where the agent fell short in a session, as expected-versus-actual findings.

A finding says what the agent should have done, what it did instead, and
quotes the steps that show it. It's the first stage that reads narrations,
and it treats them as an index: a narration decides which steps get read,
and only the steps decide what gets written.

Transcript text is sent to a third-party model provider on this path: the
narrations of a session, and the rendered steps of the windows a review
opens, tool arguments and the opening and ending of tool results included.

Two routes (``review_route``):

``narration`` (the default)
    1. Triage. One call reads the session's story and every window's
       narration and names the windows worth opening
       (:class:`~atif_analytics.domain.models.ReviewTriage`), at most
       ``review_max_windows``. The session's last main-chain window is
       opened as well unless ``review_always_last_window`` is off, because
       it holds the agent's closing report.
    2. Review. The steps of the opened windows are rendered, each under a
       ``[step N]`` line, and read against the checklist in calls of at
       most ``review_call_max_chars``, about one window each. Each call is
       shown the story, its windows' narrations and the triage's leads as
       context it's told not to trust.

    The design is a triage call and a fixed set of review calls with no
    tool loop: the model can't ask for more steps than the triage named, so
    a session's cost is bounded before it starts and a call is a pure
    function of its input.

``steps``
    No triage and no narration. Every step of every stream is rendered and
    read, in as many calls as ``review_call_max_chars`` needs. It needs no
    narrated session and costs more; it's also the yardstick the narration
    route's misses are measured against.

The pass bar is the citation check
(:func:`atif_analytics.domain.review.check_citations`), the same on both
routes. Every quote of every finding is searched for, verbatim, in the step
it names. A finding with a quote that isn't there, a step number the call
wasn't shown, or no quote at all is dropped and counted on the session's
review row; it's never stored. So a wrong narration can cost a missed
problem or a wasted read. It can't put text into a finding's evidence that
the recorded steps don't hold.

Sampling. Sessions are admitted newest-first up to
``llm_max_sessions_per_run``, every kind except those in
``review_skip_kinds`` (turn audits by default: they're a reviewer's own
sessions). One-shot jobs and sessions with no human turn are read like any
other, which the judgment pipelines don't do. On the narration route a
session with no narration, or one that grew past its last narrated step, is
left for a later run and counted as ``unnarrated``. Steps after the last
narrated window that render nothing don't count as growth: narrate writes no
window for them.

Money. The dollar ceiling is checked before every call. A session stopped by
the ceiling or by a failed call writes nothing and isn't checkpointed; a
failed session goes on the retry queue. A refused call is terminal: the
session gets a review row whose status is ``refused``.

Output schema, ``analytics/session_findings`` (one row per finding)::

    session_id           VARCHAR
    agent_id             VARCHAR    (the stream of the anchor step; NULL on
                                     the main chain)
    window_index         INTEGER    (the narration window holding the anchor
                                     step; NULL on the steps route)
    first_step_uuid      VARCHAR    (the anchor: the earliest step quoted)
    last_step_uuid       VARCHAR    (the latest step quoted)
    heading              VARCHAR    (the checklist heading)
    expected             VARCHAR
    actual               VARCHAR
    evidence_step_uuids  VARCHAR[]  (one per quote, in step order)
    evidence_quotes      VARCHAR[]  (the verified text, parallel to it)
    finding_key          VARCHAR    (session, heading, anchor step)
    route                VARCHAR    (narration | steps)
    model_id             VARCHAR
    prompt_digest        VARCHAR
    rubric_digest        VARCHAR    (the checklist and the goal file)
    reviewed_at          TIMESTAMP

``analytics/session_reviews`` (one row each time a session is reviewed)::

    session_id        VARCHAR
    route             VARCHAR
    status            VARCHAR    (reviewed | refused)
    windows_offered   INTEGER    (windows the session has)
    windows_opened    INTEGER    (windows whose steps were read)
    steps_read        INTEGER
    calls             INTEGER    (model calls made)
    findings          INTEGER    (findings stored)
    dropped           INTEGER    (findings the citation check dropped)
    dropped_reasons   VARCHAR    (JSON object, reason to count)
    first_step_uuid   VARCHAR    (the session's first and last main-chain
    last_step_uuid    VARCHAR     step when it was reviewed)
    model_id          VARCHAR
    prompt_digest     VARCHAR
    rubric_digest     VARCHAR
    reviewed_at       TIMESTAMP

A review row with no finding rows says the session was read and nothing was
found, which is what lets a later stage tell "resolved" from "not looked
at".
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import polars as pl
from loguru import logger

from atif_analytics.application.prompts import (
    GOAL_MAX_CHARS,
    REVIEW_SYSTEM_PROMPT,
    REVIEW_TRIAGE_SYSTEM_PROMPT,
    narration_text,
    prompt_digest,
    review_prompt,
    review_triage_prompt,
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
from atif_analytics.domain.models import (
    MAX_FINDINGS_PER_CALL,
    MAX_QUOTES_PER_FINDING,
    ReviewFindings,
    ReviewTriage,
)
from atif_analytics.domain.narration import (
    clip_step,
    excerpt_results,
    plan_windows,
    render_window,
    split_streams,
)
from atif_analytics.domain.review import (
    LabeledStep,
    check_citations,
    checklist_text,
    finding_key,
    pack_groups,
    render_labeled,
)
from atif_analytics.infrastructure.corpus_reader import CorpusReader
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

PIPELINE = "review"

FINDING_SCHEMA: dict[str, Any] = {
    "session_id": pl.Utf8,
    "agent_id": pl.Utf8,
    "window_index": pl.Int32,
    "first_step_uuid": pl.Utf8,
    "last_step_uuid": pl.Utf8,
    "heading": pl.Utf8,
    "expected": pl.Utf8,
    "actual": pl.Utf8,
    "evidence_step_uuids": pl.List(pl.Utf8),
    "evidence_quotes": pl.List(pl.Utf8),
    "finding_key": pl.Utf8,
    "route": pl.Utf8,
    "model_id": pl.Utf8,
    "prompt_digest": pl.Utf8,
    "rubric_digest": pl.Utf8,
    "reviewed_at": pl.Datetime("us", "UTC"),
}

REVIEW_SCHEMA: dict[str, Any] = {
    "session_id": pl.Utf8,
    "route": pl.Utf8,
    "status": pl.Utf8,
    "windows_offered": pl.Int32,
    "windows_opened": pl.Int32,
    "steps_read": pl.Int32,
    "calls": pl.Int32,
    "findings": pl.Int32,
    "dropped": pl.Int32,
    "dropped_reasons": pl.Utf8,
    "first_step_uuid": pl.Utf8,
    "last_step_uuid": pl.Utf8,
    "model_id": pl.Utf8,
    "prompt_digest": pl.Utf8,
    "rubric_digest": pl.Utf8,
    "reviewed_at": pl.Datetime("us", "UTC"),
}

#: A review row's ``status`` values.
REVIEWED = "reviewed"
REFUSED = "refused"

#: How much of the session's first user-role step a review call is shown
#: when its steps don't start at the session's first step.
_OPENING_CHARS = 4_000

#: Backstop on one step's rendered size, as a multiple of the window ceiling.
_HARD_CAP_FACTOR = 4

#: Dry-run OUTPUT budgets per call (reasoning tokens bill as output).
_AVG_TRIAGE_OUT_TOKENS = 1_500
_AVG_REVIEW_OUT_TOKENS = 3_000


def read_goal(settings: AnalyticsSettings) -> str:
    """The goal file's text, at most :data:`GOAL_MAX_CHARS` of it; empty when no file is set.

    A path that's set and can't be read raises: a review that silently ran
    without the goal it was given would stamp rows with the wrong rubric.
    """
    path = settings.review_goal_file
    if path is None:
        return ""
    return path.expanduser().read_text(encoding="utf-8").strip()[:GOAL_MAX_CHARS]


def review_prompt_digest() -> str:
    """The digest stamped on finding and review rows: both system prompts, both schemas, both builders."""
    return prompt_digest(
        REVIEW_TRIAGE_SYSTEM_PROMPT,
        json.dumps(ReviewTriage.model_json_schema(), sort_keys=True),
        REVIEW_SYSTEM_PROMPT,
        json.dumps(ReviewFindings.model_json_schema(), sort_keys=True),
        review_triage_prompt("n", story="s", goal="g", max_leads=1),
        review_prompt("t", story="s", narrations="n", opening="o", goal="g"),
        review_prompt("t", whole_session=True),
    )


def rubric_digest(goal: str) -> str:
    """The digest of what a session was judged against: the checklist and the goal text."""
    return prompt_digest(checklist_text(), "", goal)


@dataclass(frozen=True, slots=True)
class _Window:
    """One window of one stream as the review addresses it."""

    label: str
    agent_id: str | None
    index: int
    start: int
    stop: int
    narration: str = ""
    boundary: str = ""


@dataclass(slots=True)
class _SessionResult:
    """What one session's review did this run."""

    session_id: str
    findings: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    calls: int = 0
    #: A review row was written: checkpoint the session.
    complete: bool = False
    #: Left for a later run: no narration, or narration behind the steps.
    unnarrated: bool = False
    refused: bool = False
    error: BaseException | None = None


def _read_session(cache: ParquetCache, session_id: str) -> list[dict[str, Any]]:
    """One session's rows from a cache, oldest shard first; empty when it has none."""
    parts = cache.iter_part_files()
    if not parts:
        return []
    frame = (
        pl.scan_parquet([str(p) for p in parts], missing_columns="insert")
        .filter(pl.col("session_id") == session_id)
        .collect()
    )
    return frame.to_dicts()


def _narrated_sessions(cache: ParquetCache) -> set[str]:
    """The ids of every session with at least one narration row."""
    parts = cache.iter_part_files()
    if not parts:
        return set()
    frame = pl.scan_parquet([str(p) for p in parts]).select("session_id").unique().collect()
    return set(frame["session_id"].to_list())


def _label(agent_id: str | None, index: int) -> str:
    return f"main/{index}" if agent_id is None else f"sub/{agent_id}/{index}"


def _edge_uuid(steps: Sequence[StepEvent], *, last: bool) -> str | None:
    for step in reversed(steps) if last else steps:
        if step.uuid:
            return step.uuid
    return None


class _Reviewer:
    """One run's shared state: the ports, the settings, the goal, and the row buffers."""

    def __init__(
        self,
        settings: AnalyticsSettings,
        *,
        reader: CorpusReader,
        provider: LlmStructuredProvider,
        spec: ModelSpec,
        findings: ParquetCache,
        reviews: ParquetCache,
        narration: ParquetCache,
        stories: ParquetCache,
        budget: RunBudget | None,
    ) -> None:
        self.settings = settings
        self.reader = reader
        self.provider = provider
        self.model_id = spec.model_id
        self.findings = findings
        self.reviews = reviews
        self.narration = narration
        self.stories = stories
        self.budget = budget
        self.route = settings.review_route
        self.goal = read_goal(settings)
        self.prompt_digest = review_prompt_digest()
        self.rubric_digest = rubric_digest(self.goal)
        self._finding_rows: list[dict[str, Any]] = []
        self._review_rows: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # What a call is shown
    # ------------------------------------------------------------------

    def prepare(self, session_id: str, step: StepEvent) -> StepEvent:
        """One step as a review call sees it: long text cut, tool results excerpted."""
        settings = self.settings
        shown = clip_step(step, settings.narrate_window_max_chars)
        return excerpt_results(
            shown,
            head_chars=settings.review_tool_result_head_chars,
            failed_tail_chars=settings.review_tool_result_tail_chars,
            tail_chars=settings.review_tool_result_tail_chars,
            load=lambda path: self.reader.tool_result_file(session_id, path),
        )

    def _render(self, steps: Sequence[StepEvent]) -> str:
        # A backstop on one step's rendered size, as in narrate: step text is
        # already cut to a window, so only a step with very many tool calls
        # reaches it. It's not the call ceiling, which packs whole windows.
        cap = self.settings.narrate_window_max_chars * _HARD_CAP_FACTOR
        return render_window(
            steps,
            total_max_chars=cap,
            tool_result_max_chars=cap,
            tool_result_bodies=True,
            tool_input_max_chars=self.settings.review_tool_input_chars,
        )

    def size(self, step: StepEvent) -> int:
        rendered = self._render([step])
        return len(rendered) + 1 if rendered else 0

    def _out_of_budget(self) -> bool:
        return self.budget is not None and self.budget.exhausted()

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Write the buffered rows as shards: findings first, so a review row never counts unwritten ones."""
        if self._finding_rows:
            self.findings.write_part(pl.DataFrame(self._finding_rows, schema=FINDING_SCHEMA))
            self._finding_rows.clear()
        if self._review_rows:
            self.reviews.write_part(pl.DataFrame(self._review_rows, schema=REVIEW_SCHEMA))
            self._review_rows.clear()

    # ------------------------------------------------------------------
    # Windows
    # ------------------------------------------------------------------

    def narrated_windows(
        self, session_id: str, streams: dict[str | None, list[StepEvent]]
    ) -> list[_Window] | None:
        """The session's narrated windows, located in its streams; ``None`` when narration is missing or behind.

        A window row names its first and last step by uuid. Those place it
        in the stream; where a uuid is gone (the session was converted
        again), the rows' step counts do.
        """
        by_stream: dict[str | None, dict[int, dict[str, Any]]] = {}
        for row in _read_session(self.narration, session_id):
            by_stream.setdefault(row.get("agent_id"), {})[int(row["window_index"])] = row
        if not by_stream.get(None):
            return None
        windows: list[_Window] = []
        for key in sorted(by_stream, key=lambda k: (k is not None, k or "")):
            chain = streams.get(key)
            if chain is None:
                continue
            positions = {step.uuid: i for i, step in enumerate(chain) if step.uuid}
            running = 0
            for index in sorted(by_stream[key]):
                row = by_stream[key][index]
                start = positions.get(row["first_step_uuid"], running)
                last = positions.get(row["last_step_uuid"])
                stop = last + 1 if last is not None else start + int(row["step_count"])
                start, stop = min(start, len(chain)), min(max(stop, start), len(chain))
                running = stop
                windows.append(
                    _Window(
                        label=_label(key, index),
                        agent_id=key,
                        index=index,
                        start=start,
                        stop=stop,
                        narration=narration_text(
                            intent=row["intent"], actions=row["actions"], outcome=row["outcome"]
                        ),
                        boundary=str(row["boundary"]),
                    )
                )
            if key is None and any(step.uuid and self._render([step]) for step in chain[running:]):
                # The main chain has steps after its last narrated window:
                # the session grew, and narrate hasn't caught up. Steps that
                # render nothing don't count: narrate writes no window for
                # them, so waiting for one would never end.
                return None
        return windows

    def planned_windows(self, streams: dict[str | None, list[StepEvent]]) -> list[_Window]:
        """Every stream cut into windows by the narrate planner: what the steps route reads."""
        windows: list[_Window] = []
        for key in sorted(streams, key=lambda k: (k is not None, k or "")):
            planned = plan_windows(
                streams[key],
                max_chars=self.settings.narrate_window_max_chars,
                min_chars=self.settings.narrate_window_min_chars,
                size_of=self.size,
            )
            windows.extend(
                _Window(
                    label=_label(key, w.index),
                    agent_id=key,
                    index=w.index,
                    start=w.start,
                    stop=w.stop,
                    boundary=w.boundary,
                )
                for w in planned
            )
        return windows

    def _story(self, session_id: str) -> str:
        """The newest session story: the one covering the most windows."""
        rows = _read_session(self.stories, session_id)
        if not rows:
            return ""
        best = max(
            rows,
            key=lambda r: (
                int(r["window_count"]) + int(r.get("subagent_window_count") or 0),
                r["narrated_at"],
            ),
        )
        return str(best["story"])

    @staticmethod
    def narrations_block(windows: Sequence[_Window], leads: dict[str, str] | None = None) -> str:
        blocks = []
        for window in windows:
            head = (
                f"window {window.label} ({window.stop - window.start} steps, "
                f"closed by {window.boundary})"
            )
            block = f"{head}\n{window.narration}"
            if leads and window.label in leads:
                block += f"\nlead: {leads[window.label]}"
            blocks.append(block)
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # One session
    # ------------------------------------------------------------------

    async def review(self, session_id: str) -> _SessionResult:
        """Review one session; a failure comes back in the result and the caller queues a retry."""
        result = _SessionResult(session_id=session_id)
        try:
            await self._review(session_id, result)
        except Exception as exc:  # noqa: BLE001 - any failure sends the session to the retry queue
            result.error = exc
        return result

    async def _review(self, session_id: str, result: _SessionResult) -> None:
        from atif_models.domain.ports import RefusalError

        raw = split_streams(self.reader.load_full_steps(session_id))
        shown = {
            key: [self.prepare(session_id, step) for step in chain] for key, chain in raw.items()
        }

        story = ""
        leads: dict[str, str] = {}
        if self.route == "narration":
            windows = self.narrated_windows(session_id, raw)
            if windows is None:
                result.unnarrated = True
                return
            story = self._story(session_id)
            if self._out_of_budget():
                return
            try:
                triage = await self.provider.classify_structured(
                    system=REVIEW_TRIAGE_SYSTEM_PROMPT,
                    prompt=review_triage_prompt(
                        self.narrations_block(windows),
                        story=story,
                        goal=self.goal,
                        max_leads=self.settings.review_max_windows,
                    ),
                    schema=ReviewTriage,
                )
            except RefusalError as exc:
                logger.info("review: {} triage refused (terminal): {}", session_id, exc)
                result.calls += 1
                self._record(session_id, raw, result, windows, opened=[], status=REFUSED)
                return
            result.calls += 1
            opened = self._select(windows, triage, leads)
        else:
            windows = self.planned_windows(shown)
            opened = windows

        rows: list[dict[str, Any]] = []
        opened = [w for w in opened if w.stop > w.start]
        whole = len(opened) == len([w for w in windows if w.stop > w.start])
        groups = [self._labeled(raw, shown, window) for window in opened]
        numbered = self._number(groups)
        texts = [
            f"[window {window.label}]\n{render_labeled(steps, self._render)}"
            for window, steps in zip(opened, numbered, strict=True)
        ]
        calls = pack_groups([len(text) for text in texts], self.settings.review_call_max_chars)
        opening = self._opening(shown)
        for start, stop in calls:
            if self._out_of_budget():
                return
            in_call = {step.number: step for steps in numbered[start:stop] for step in steps}
            holds_first = any(
                step.agent_id is None and step.position == 0 for step in in_call.values()
            )
            try:
                reply = await self.provider.classify_structured(
                    system=REVIEW_SYSTEM_PROMPT,
                    prompt=review_prompt(
                        "\n".join(texts[start:stop]),
                        story=story,
                        narrations=(
                            self.narrations_block(opened[start:stop], leads)
                            if self.route == "narration"
                            else ""
                        ),
                        opening="" if holds_first else opening,
                        goal=self.goal,
                        whole_session=whole and len(calls) == 1,
                    ),
                    schema=ReviewFindings,
                )
            except RefusalError as exc:
                logger.info("review: {} refused (terminal): {}", session_id, exc)
                result.calls += 1
                self._record(session_id, raw, result, windows, opened=opened, status=REFUSED)
                return
            result.calls += 1
            rows.extend(self._verified(session_id, reply, in_call, opened, result))

        # One row per key: two calls over neighboring windows can report the
        # same fault at the same step.
        unique = {row["finding_key"]: row for row in reversed(rows)}
        self._finding_rows.extend(reversed(unique.values()))
        result.findings = len(unique)
        self._record(session_id, raw, result, windows, opened=opened, status=REVIEWED)

    def _select(
        self, windows: Sequence[_Window], triage: ReviewTriage, leads: dict[str, str]
    ) -> list[_Window]:
        """The windows to open: the triage's leads in its order, capped, plus the last main window.

        A lead naming a label the session doesn't have is ignored. The
        result is in stream order, which is the order the review call reads.
        """
        by_label = {window.label: window for window in windows}
        chosen: dict[str, _Window] = {}
        for lead in triage.leads:
            window = by_label.get(lead.window.strip())
            if window is None or window.label in chosen:
                continue
            if len(chosen) >= self.settings.review_max_windows:
                break
            chosen[window.label] = window
            leads[window.label] = f"({lead.heading}) {lead.why}"
        if self.settings.review_always_last_window:
            main = [w for w in windows if w.agent_id is None and w.stop > w.start]
            if main:
                chosen.setdefault(main[-1].label, main[-1])
        return [window for window in windows if window.label in chosen]

    @staticmethod
    def _labeled(
        raw: dict[str | None, list[StepEvent]],
        shown: dict[str | None, list[StepEvent]],
        window: _Window,
    ) -> list[tuple[StepEvent, StepEvent, str | None, int]]:
        return [
            (raw[window.agent_id][i], shown[window.agent_id][i], window.agent_id, i)
            for i in range(window.start, window.stop)
        ]

    @staticmethod
    def _number(
        groups: Sequence[Sequence[tuple[StepEvent, StepEvent, str | None, int]]],
    ) -> list[list[LabeledStep]]:
        """Number every step across the session's opened windows, from 1."""
        numbered: list[list[LabeledStep]] = []
        number = 0
        for group in groups:
            steps = []
            for raw, shown, agent_id, position in group:
                number += 1
                steps.append(
                    LabeledStep(
                        number=number, raw=raw, shown=shown, agent_id=agent_id, position=position
                    )
                )
            numbered.append(steps)
        return numbered

    def _opening(self, shown: dict[str | None, list[StepEvent]]) -> str:
        """The start of the session's request: its first user-role step, rendered and cut."""
        for step in shown.get(None, []):
            if step.role == "user" and step.text:
                return self._render([step])[:_OPENING_CHARS]
        return ""

    def _verified(
        self,
        session_id: str,
        reply: ReviewFindings,
        in_call: dict[int, LabeledStep],
        opened: Sequence[_Window],
        result: _SessionResult,
    ) -> list[dict[str, Any]]:
        """The reply's findings that pass the citation check, as rows; the rest are counted."""
        rows: list[dict[str, Any]] = []
        if len(reply.findings) > MAX_FINDINGS_PER_CALL:
            result.dropped["over_limit"] += len(reply.findings) - MAX_FINDINGS_PER_CALL
        for finding in reply.findings[:MAX_FINDINGS_PER_CALL]:
            checked = check_citations(
                [(q.step, q.text) for q in finding.quotes[:MAX_QUOTES_PER_FINDING]], in_call
            )
            if checked.reason is not None:
                result.dropped[checked.reason] += 1
                logger.info(
                    "review: {} dropped a finding under {} ({})",
                    session_id,
                    finding.heading,
                    checked.reason,
                )
                continue
            anchor = checked.citations[0]
            window_index = None
            if self.route == "narration":
                window_index = next(
                    (
                        w.index
                        for w in opened
                        if w.agent_id == anchor.agent_id and w.start <= anchor.position < w.stop
                    ),
                    None,
                )
            rows.append(
                {
                    "session_id": session_id,
                    "agent_id": anchor.agent_id,
                    "window_index": window_index,
                    "first_step_uuid": anchor.step_uuid,
                    "last_step_uuid": checked.citations[-1].step_uuid,
                    "heading": finding.heading,
                    "expected": finding.expected,
                    "actual": finding.actual,
                    "evidence_step_uuids": [c.step_uuid for c in checked.citations],
                    "evidence_quotes": [c.quote for c in checked.citations],
                    "finding_key": finding_key(session_id, finding.heading, anchor.step_uuid),
                    "route": self.route,
                    "model_id": self.model_id,
                    "prompt_digest": self.prompt_digest,
                    "rubric_digest": self.rubric_digest,
                    "reviewed_at": datetime.now(UTC),
                }
            )
        return rows

    def _record(
        self,
        session_id: str,
        raw: dict[str | None, list[StepEvent]],
        result: _SessionResult,
        windows: Sequence[_Window],
        *,
        opened: Sequence[_Window],
        status: str,
    ) -> None:
        """Buffer the session's review row and mark the session complete."""
        main = raw.get(None, [])
        result.refused = status == REFUSED
        self._review_rows.append(
            {
                "session_id": session_id,
                "route": self.route,
                "status": status,
                "windows_offered": len(windows),
                "windows_opened": len(opened),
                "steps_read": sum(w.stop - w.start for w in opened),
                "calls": result.calls,
                "findings": result.findings,
                "dropped": sum(result.dropped.values()),
                "dropped_reasons": json.dumps(dict(result.dropped), sort_keys=True),
                "first_step_uuid": _edge_uuid(main, last=False),
                "last_step_uuid": _edge_uuid(main, last=True),
                "model_id": self.model_id,
                "prompt_digest": self.prompt_digest,
                "rubric_digest": self.rubric_digest,
                "reviewed_at": datetime.now(UTC),
            }
        )
        result.complete = True


@dataclass(frozen=True, slots=True)
class _Admission:
    """Which sessions a run reviews, and why the others wait."""

    pending: list[str]
    bounds: dict[str, tuple[datetime | None, datetime | None]]
    #: Admitted sessions past the session cap.
    deferred: int
    #: Sessions with no narration row (narration route only).
    unnarrated: int
    #: Sessions of a kind ``review_skip_kinds`` leaves out.
    skipped_kind: list[str]


def _pending(
    settings: AnalyticsSettings,
    *,
    reader: CorpusReader,
    narration: ParquetCache,
    since_days: int | None,
    limit: int | None,
    checkpoint: SqliteCheckpoint,
    retry: SqliteRetryQueue | None,
) -> _Admission:
    """The sessions to review this run, newest-first and capped.

    On the narration route a session with no narration row is left out
    before the cap, and so is a session whose kind ``review_skip_kinds``
    names, so the cap is spent on sessions a review reads. Telling a
    session's kind loads its steps; the walk stops at the cap, so it loads
    no more sessions than it admits or skips. ``retry`` is ``None`` for a
    dry run, which reads the checkpoint and leaves the queue alone.
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
            logger.info("review: draining {} retry-queue entries", len(retry_ids))
            keep |= retry_ids
        blocked = retry.blocked_units(pipeline=PIPELINE)
        if blocked & keep:
            logger.info(
                "review: skipping {} sessions with live retry entries (backoff/attempts cap)",
                len(blocked & keep),
            )
            keep -= blocked
        if skipped:
            logger.info("review: skipped {} sessions via checkpoint", skipped)
    admitted = [sid for sid in bounds if sid in keep]
    unnarrated = 0
    if settings.review_route == "narration":
        narrated = _narrated_sessions(narration)
        unnarrated = sum(1 for sid in admitted if sid not in narrated)
        admitted = [sid for sid in admitted if sid in narrated]
        if unnarrated and retry is not None:
            logger.info(
                "review: {} sessions have no narration yet; left for a later run", unnarrated
            )
    skip_kinds = frozenset(
        kind.strip() for kind in settings.review_skip_kinds.split(",") if kind.strip()
    )
    max_sessions = settings.llm_max_sessions_per_run
    pending: list[str] = []
    skipped_kind: list[str] = []
    deferred = 0
    for position, sid in enumerate(admitted):
        if len(pending) >= max_sessions:
            deferred = len(admitted) - position
            break
        if skip_kinds and reader.session_kind(sid) in skip_kinds:
            skipped_kind.append(sid)
            continue
        pending.append(sid)
    if deferred and retry is not None:
        logger.warning(
            "review: capped at {} sessions (llm_max_sessions_per_run); {} deferred",
            max_sessions,
            deferred,
        )
    return _Admission(
        pending=pending,
        bounds=bounds,
        deferred=deferred,
        unnarrated=unnarrated,
        skipped_kind=skipped_kind,
    )


async def _review_async(
    reviewer: _Reviewer,
    *,
    pending: list[str],
    bounds: dict[str, tuple[datetime | None, datetime | None]],
    checkpoint: SqliteCheckpoint,
    retry: SqliteRetryQueue,
) -> int:
    """Review ``pending`` in budget-checked batches; return the sessions completed."""
    completed = 0
    findings = 0
    dropped: Counter[str] = Counter()
    unnarrated = 0
    i = 0
    while i < len(pending):
        try:
            batch, results = await gather_under_budget(
                pending[i:], make_coro=reviewer.review, budget=reviewer.budget
            )
        finally:
            # Rows are written before anything is stamped, so a checkpoint
            # never says more than the caches hold.
            reviewer.flush()
        if not batch:
            budget = reviewer.budget
            logger.warning(
                "review: cost ceiling hit (${:.2f} >= ${:.2f}), "
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
                logger.warning("review: {} failed (queued for retry): {}", sid, res)
                retry.enqueue(pipeline=PIPELINE, unit_id=sid, error=str(res))
                continue
            if res.error is not None:
                logger.warning("review: {} failed (queued for retry): {}", sid, res.error)
                retry.enqueue(pipeline=PIPELINE, unit_id=sid, error=str(res.error))
            elif res.complete:
                done.append(sid)
                findings += res.findings
                dropped.update(res.dropped)
            elif res.unnarrated:
                unnarrated += 1
            else:
                logger.warning(
                    "review: {} stopped at the cost ceiling; it's reviewed whole next run", sid
                )
        if done:
            checkpoint.mark_completed(
                pipeline=PIPELINE,
                rows=[(sid, *bounds.get(sid, (None, None))) for sid in done],
            )
            retry.mark_done(pipeline=PIPELINE, unit_ids=done)
        completed += len(done)
    logger.info(
        "review: completed {} sessions, stored {} findings, dropped {} on citations ({}), "
        "{} left unnarrated",
        completed,
        findings,
        sum(dropped.values()),
        dict(dropped),
        unnarrated,
    )
    return completed


def _dry_run_plan(
    reviewer: _Reviewer,
    spec: ModelSpec,
    *,
    admission: _Admission,
    since_days: int | None,
    limit: int | None,
) -> dict[str, Any]:
    """What a real run would do and cost at most, measured from the sessions' rendered steps.

    The narration route's estimate assumes every session opens its
    ``review_max_windows`` largest windows, which is the most a triage can
    ask for, so it's an upper bound on input.
    """
    settings = reviewer.settings
    pending, deferred, unnarrated = admission.pending, admission.deferred, admission.unnarrated
    calls = 0
    in_chars = 0
    out_tokens = 0
    behind = 0
    for sid in pending:
        raw = split_streams(reviewer.reader.load_full_steps(sid))
        shown = {key: [reviewer.prepare(sid, s) for s in chain] for key, chain in raw.items()}
        if reviewer.route == "narration":
            windows = reviewer.narrated_windows(sid, raw)
            if windows is None:
                behind += 1
                continue
            calls += 1
            in_chars += len(REVIEW_TRIAGE_SYSTEM_PROMPT) + len(reviewer.narrations_block(windows))
            out_tokens += _AVG_TRIAGE_OUT_TOKENS
        else:
            windows = reviewer.planned_windows(shown)
        sizes = sorted(
            (
                sum(reviewer.size(shown[w.agent_id][i]) for i in range(w.start, w.stop))
                for w in windows
            ),
            reverse=True,
        )
        if reviewer.route == "narration":
            sizes = sizes[: settings.review_max_windows]
        n_calls = len(pack_groups(sizes, settings.review_call_max_chars))
        calls += n_calls
        in_chars += sum(sizes) + n_calls * len(REVIEW_SYSTEM_PROMPT)
        out_tokens += n_calls * _AVG_REVIEW_OUT_TOKENS
    in_tokens = tokens_for_chars(in_chars, spec.chars_per_token)
    cost = estimate_cost_tokens(
        in_tokens, out_tokens, (spec.pricing_in or 0.0, spec.pricing_out or 0.0)
    )
    logger.info(
        "review --dry-run: {} sessions would run ({} deferred by the cap, {} not narrated), "
        "{} calls, estimated cost at most ~${:.2f}",
        len(pending) - behind,
        deferred,
        unnarrated + behind,
        calls,
        cost,
    )
    return {
        "pipeline": PIPELINE,
        "route": reviewer.route,
        "candidates": len(pending) + deferred,
        "capped_candidates": len(pending) - behind,
        "unnarrated": unnarrated + behind,
        "skipped_kind": len(admission.skipped_kind),
        "llm_calls": calls,
        "estimated_input_tokens": in_tokens,
        "estimated_output_tokens": out_tokens,
        "estimated_cost_usd": round(cost, 4),
        "max_sessions_per_run": settings.llm_max_sessions_per_run,
        "max_cost_usd_per_run": settings.llm_max_cost_usd_per_run,
        "max_output_tokens": spec.max_completion_tokens,
        "model": spec.model_id,
        "prompt_digest": reviewer.prompt_digest,
        "rubric_digest": reviewer.rubric_digest,
        "since_days": since_days,
        "limit": limit,
        "dry_run": True,
    }


def review_sessions(
    settings: AnalyticsSettings,
    *,
    since_days: int | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    reader: CorpusReader | None = None,
    provider: LlmStructuredProvider | None = None,
    spec: ModelSpec | None = None,
    findings: ParquetCache | None = None,
    reviews: ParquetCache | None = None,
    checkpoint: SqliteCheckpoint | None = None,
    retry: SqliteRetryQueue | None = None,
    budget: RunBudget | None = None,
) -> int | dict[str, Any]:
    """Review each pending session and store its verified findings; return sessions completed.

    A session counts once it has a review row, whether it produced findings
    or none. ``dry_run`` (the default) returns a plan dict instead and
    writes nothing.
    """
    layout = settings.layout()
    reader = (
        reader
        if reader is not None
        else CorpusReader(settings.corpus_root, caps=settings.transcript_caps())
    )
    findings = findings if findings is not None else build_cache(layout.findings_dir)
    reviews = reviews if reviews is not None else build_cache(layout.reviews_dir)
    narration = build_cache(layout.narration_dir)
    if provider is None or spec is None:
        provider, spec = build_provider(settings, PIPELINE, full_output=True)
    checkpoint = checkpoint if checkpoint is not None else build_checkpoint(layout.state_db_path)

    reviewer = _Reviewer(
        settings,
        reader=reader,
        provider=provider,
        spec=spec,
        findings=findings,
        reviews=reviews,
        narration=narration,
        stories=build_cache(layout.stories_dir),
        budget=budget,
    )

    if dry_run:
        admission = _pending(
            settings,
            reader=reader,
            narration=narration,
            since_days=since_days,
            limit=limit,
            checkpoint=checkpoint,
            retry=None,
        )
        return _dry_run_plan(
            reviewer,
            spec,
            admission=admission,
            since_days=since_days,
            limit=limit,
        )

    retry = retry if retry is not None else build_retry_queue(layout.state_db_path)
    admission = _pending(
        settings,
        reader=reader,
        narration=narration,
        since_days=since_days,
        limit=limit,
        checkpoint=checkpoint,
        retry=retry,
    )
    pending, bounds = admission.pending, admission.bounds
    if admission.skipped_kind:
        # Stamped so the next run doesn't load them again to learn their kind.
        logger.info(
            "review: {} sessions are of a kind review leaves out ({})",
            len(admission.skipped_kind),
            settings.review_skip_kinds,
        )
        checkpoint.mark_completed(
            pipeline=PIPELINE,
            rows=[(sid, *bounds.get(sid, (None, None))) for sid in admission.skipped_kind],
        )
    if not pending:
        logger.info("review: no pending sessions")
        return 0
    logger.info("review: {} pending sessions ({} route)", len(pending), reviewer.route)
    if budget is not None:
        budget.watch(provider, spec)
    with pipeline_usage(provider, spec, PIPELINE):
        return asyncio.run(
            _review_async(
                reviewer,
                pending=pending,
                bounds=bounds,
                checkpoint=checkpoint,
                retry=retry,
            )
        )


__all__ = [
    "FINDING_SCHEMA",
    "PIPELINE",
    "REFUSED",
    "REVIEWED",
    "REVIEW_SCHEMA",
    "read_goal",
    "review_prompt_digest",
    "review_sessions",
    "rubric_digest",
]
