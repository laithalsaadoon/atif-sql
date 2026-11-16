# SPDX-License-Identifier: Apache-2.0

"""Narrate pipeline: window rows with provenance, the carry-over, the story, and resuming.

FakeProvider-driven (no live LLM calls). The fake numbers its narrations by
call (``intent 1``, ``intent 2``, ...), so a test can say which call wrote
which row and what the next call was shown.

The sessions here are written with ``write_session``: step ``n`` of session
``abcd...`` has uuid ``abcd-n``, so the expected first and last step uuids
of a window can be read off the turn list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

import pytest
from analytics_fixtures import SESSION_IDS, FakeProvider, write_session

from atif_analytics.application.use_cases._shared import RunBudget
from atif_analytics.application.use_cases.narrate import (
    NARRATION_SCHEMA,
    REFUSED,
    STORY_SCHEMA,
    narrate_prompt_digest,
    narrate_sessions,
    story_prompt_digest,
)
from atif_analytics.domain.narration import UNATTRIBUTED_AGENT_ID
from atif_analytics.infrastructure.corpus_reader import CorpusReader
from atif_analytics.infrastructure.parquet_cache import ParquetCache
from atif_analytics.infrastructure.settings import AnalyticsSettings
from atif_analytics.infrastructure.sqlite_state.checkpointer import SqliteCheckpoint, load_as_map
from atif_analytics.infrastructure.sqlite_state.retry_queue import SqliteRetryQueue
from atif_models.domain.ports import CallUsage, ProviderUnavailable
from atif_models.domain.registry import resolve

if TYPE_CHECKING:
    from pathlib import Path

SPEC = resolve("small")

JOB = "dddddddd-4444-4444-4444-444444444444"
#: Three human turns, each followed by agent work: three windows when the
#: floor is 0 (a window closes at every human turn).
JOB_TURNS = [
    ("user", "fix the export job"),
    ("agent", "Reading jobs/export.py."),
    ("agent", "Found a missing region key."),
    ("user", "add the key and rerun"),
    ("agent", "Added region to config/defaults.toml."),
    ("user", "now write the test"),
    ("agent", "Wrote tests/test_export.py, 3 passed."),
]
#: What the same session holds after it resumes: one more exchange.
JOB_TURNS_RESUMED = [
    *JOB_TURNS,
    ("user", "also update the changelog"),
    ("agent", "Added a CHANGELOG.md entry."),
]


@pytest.fixture
def job_root(tmp_path: Path) -> Path:
    """A corpus holding the one three-window session."""
    root = tmp_path / "job-corpus"
    write_session(root, JOB, JOB_TURNS, day=22)
    return root


def _settings(root: Path, **overrides: Any) -> AnalyticsSettings:
    """Settings whose windows close at every mark (floor 0), unless overridden."""
    return AnalyticsSettings(corpus_root=root, narrate_window_min_chars=0, **overrides)


def _run(settings: AnalyticsSettings, provider: FakeProvider, **kwargs: Any) -> int:
    out = narrate_sessions(settings, dry_run=False, provider=provider, spec=SPEC, **kwargs)
    assert isinstance(out, int)
    return out


def _windows(
    settings: AnalyticsSettings, session_id: str = JOB, agent: str | None = None
) -> list[dict[str, Any]]:
    """One stream's window rows in order: the main chain's, or subagent ``agent``'s."""
    df = ParquetCache(settings.layout().narration_dir).read_all()
    if df is None:
        return []
    rows = df.filter(df["session_id"] == session_id).sort("window_index").to_dicts()
    return [row for row in rows if row["agent_id"] == agent]


def _subagent_stories(settings: AnalyticsSettings, session_id: str) -> list[dict[str, Any]]:
    df = ParquetCache(settings.layout().subagent_stories_dir).read_all()
    if df is None:
        return []
    return df.filter(df["session_id"] == session_id).sort(["agent_id", "window_count"]).to_dicts()


def _stories(settings: AnalyticsSettings, session_id: str = JOB) -> list[dict[str, Any]]:
    df = ParquetCache(settings.layout().stories_dir).read_all()
    if df is None:
        return []
    return df.filter(df["session_id"] == session_id).sort("window_count").to_dicts()


def _narration_prompts(provider: FakeProvider) -> list[str]:
    return [prompt for name, prompt in provider.calls if name == "WindowNarration"]


# ---------------------------------------------------------------------------
# rows and provenance
# ---------------------------------------------------------------------------


def test_one_row_per_window_with_provenance(job_root: Path) -> None:
    settings = _settings(job_root)
    provider = FakeProvider()
    assert _run(settings, provider) == 1

    rows = _windows(settings)
    assert [r["window_index"] for r in rows] == [0, 1, 2]
    # Each window points at the steps it covers: source_uuids[0] of its
    # first and last step, and how many steps lie between.
    assert [(r["first_step_uuid"], r["last_step_uuid"], r["step_count"]) for r in rows] == [
        ("dddd-1", "dddd-3", 3),
        ("dddd-4", "dddd-5", 2),
        ("dddd-6", "dddd-7", 2),
    ]
    assert [r["boundary"] for r in rows] == ["human_turn", "human_turn", "session_end"]
    assert rows[0]["started_at"].isoformat() == "2026-08-22T08:00:00+00:00"
    assert rows[0]["ended_at"].isoformat() == "2026-08-22T08:02:00+00:00"
    assert [(r["intent"], r["actions"], r["outcome"]) for r in rows] == [
        (f"intent {n}", f"actions {n}", f"outcome {n}") for n in (1, 2, 3)
    ]
    # The model and the prompt that wrote each row travel with it.
    assert {r["model_id"] for r in rows} == {SPEC.model_id}
    assert {r["prompt_digest"] for r in rows} == {narrate_prompt_digest()}
    assert list(rows[0]) == list(NARRATION_SCHEMA)


def test_the_story_folds_every_window(job_root: Path) -> None:
    settings = _settings(job_root)
    provider = FakeProvider()
    _run(settings, provider)

    (story,) = _stories(settings)
    assert story["story"] == "story 1"
    assert story["window_count"] == 3
    assert story["step_count"] == 7
    assert (story["first_step_uuid"], story["last_step_uuid"]) == ("dddd-1", "dddd-7")
    assert story["model_id"] == SPEC.model_id
    assert story["prompt_digest"] == story_prompt_digest()
    assert story_prompt_digest() != narrate_prompt_digest()
    assert list(story) == list(STORY_SCHEMA)
    # The story call read the three narrations, not the transcript.
    (story_call,) = [prompt for name, prompt in provider.calls if name == "SessionStory"]
    assert "<narrations id=" in story_call
    for n in (1, 2, 3):
        assert f"intent: intent {n}" in story_call
    assert "closed by human_turn" in story_call
    assert "Reading jobs/export.py." not in story_call


def test_each_call_carries_the_previous_window_and_nothing_older(job_root: Path) -> None:
    settings = _settings(job_root)
    provider = FakeProvider()
    _run(settings, provider)
    first, second, third = _narration_prompts(provider)

    # The first window has nothing before it.
    assert "<previous_narration" not in first
    assert "<previous_steps" not in first
    assert "[user 2026-08-22T08:00:00.000Z] fix the export job" in first
    assert "first window of the session" in first

    # The second carries the first's narration and the end of its steps.
    assert "intent: intent 1\nactions: actions 1\noutcome: outcome 1" in second
    carried, _, window = second.partition("<transcript id=")
    assert "Found a missing region key." in carried
    assert "add the key and rerun" in window
    assert "add the key and rerun" not in carried

    # The third carries the second's, and no raw step of the first window.
    assert "intent: intent 2" in third
    assert "intent: intent 1" not in third
    assert "fix the export job" not in third
    assert "Reading jobs/export.py." not in third
    assert "Added region to config/defaults.toml." in third.partition("<transcript id=")[0]


def test_carry_steps_zero_sends_the_narration_alone(job_root: Path) -> None:
    settings = _settings(job_root, narrate_carry_steps=0)
    provider = FakeProvider()
    _run(settings, provider)
    second = _narration_prompts(provider)[1]
    assert "<previous_narration" in second
    assert "<previous_steps" not in second


def test_a_sidechain_step_with_no_agent_id_is_narrated_as_its_own_stream(
    settings: AnalyticsSettings, reader: CorpusReader
) -> None:
    """The fixture's one subagent step predates agent ids: it's narrated, apart from the main chain."""
    provider = FakeProvider()
    _run(settings, provider, reader=reader)
    main = _windows(settings, SESSION_IDS[0])
    # 15 steps: 14 on the main chain, one sidechain step.
    assert sum(r["step_count"] for r in main) == 14
    assert "u-sc" not in {r["first_step_uuid"] for r in main} | {r["last_step_uuid"] for r in main}
    (side,) = _windows(settings, SESSION_IDS[0], agent=UNATTRIBUTED_AGENT_ID)
    assert (side["first_step_uuid"], side["last_step_uuid"], side["step_count"]) == (
        "u-sc",
        "u-sc",
        1,
    )
    # Its text reaches its own window's call and no main-chain one.
    holders = [prompt for _, prompt in provider.calls if "subagent chatter" in prompt]
    assert len(holders) == 1
    assert "a subagent's own steps" in holders[0]
    (story,) = _subagent_stories(settings, SESSION_IDS[0])
    assert (story["agent_id"], story["returned"]) == (UNATTRIBUTED_AGENT_ID, False)


def test_every_session_kind_is_narrated(tmp_path: Path) -> None:
    """One-shot jobs and turn audits are eligible; the judgment pipelines skip both."""
    root = tmp_path / "kinds"
    one_shot = "eeeeeeee-5555-5555-5555-555555555555"
    audit = "ffffffff-6666-6666-6666-666666666666"
    write_session(root, one_shot, [("user", "run the nightly report"), ("agent", "Done.")], 20)
    write_session(root, audit, [("user", "You are auditing an agent turn."), ("agent", "PASS")], 21)
    reader = CorpusReader(root)
    assert reader.session_kind(one_shot) == "one_shot_job"
    assert reader.session_kind(audit) == "turn_audit"

    settings = _settings(root)
    assert _run(settings, FakeProvider(), reader=reader) == 2
    assert len(_windows(settings, one_shot)) == 1
    assert len(_windows(settings, audit)) == 1
    assert len(_stories(settings, one_shot)) == 1
    assert len(_stories(settings, audit)) == 1


def test_thin_windows_merge_under_the_default_floor(job_root: Path) -> None:
    """With the default 2,000-character floor this short session is one window."""
    settings = AnalyticsSettings(corpus_root=job_root)
    _run(settings, FakeProvider())
    (row,) = _windows(settings)
    assert (row["first_step_uuid"], row["last_step_uuid"], row["step_count"]) == (
        "dddd-1",
        "dddd-7",
        7,
    )
    assert row["boundary"] == "session_end"


def test_the_size_cap_splits_a_run_with_no_marks(tmp_path: Path) -> None:
    root = tmp_path / "long"
    sid = "abababab-7777-7777-7777-777777777777"
    turns = [
        ("user", "refactor everything"),
        *[("agent", f"edit {i} " + "x" * 80) for i in range(12)],
    ]
    write_session(root, sid, turns, day=23)
    settings = _settings(root, narrate_window_max_chars=500)
    _run(settings, FakeProvider())
    rows = _windows(settings, sid)
    assert len(rows) > 2
    assert {r["boundary"] for r in rows[:-1]} == {"size_cap"}
    assert sum(r["step_count"] for r in rows) == len(turns)


def test_an_oversized_step_is_narrated_from_its_opening(tmp_path: Path) -> None:
    """A job prompt longer than a window reaches the model cut short, not dropped.

    The renderer's size cap drops a line that doesn't fit. Left to it, a
    window made of one oversized step would render as the truncation notice
    alone and be narrated from nothing.
    """
    root = tmp_path / "big"
    sid = "cdcdcdcd-8888-8888-8888-888888888888"
    prompt = "OPENING run the nightly bench. " + "detail " * 400 + "CLOSING"
    write_session(root, sid, [("user", prompt), ("agent", "Bench finished.")], day=24)
    settings = _settings(root, narrate_window_max_chars=500)
    provider = FakeProvider()
    _run(settings, provider)

    first = _narration_prompts(provider)[0]
    assert "OPENING run the nightly bench." in first
    assert "CLOSING" not in first
    assert "chars dropped)" in first
    assert "session truncated" not in first
    rows = _windows(settings, sid)
    assert (rows[0]["first_step_uuid"], rows[0]["step_count"]) == ("cdcd-1", 1)
    assert sum(r["step_count"] for r in rows) == 2


# ---------------------------------------------------------------------------
# checkpoint and resume
# ---------------------------------------------------------------------------


def test_a_rerun_makes_no_call(job_root: Path) -> None:
    settings = _settings(job_root)
    _run(settings, FakeProvider())
    assert JOB in load_as_map(settings.layout().state_db_path, "narrate")

    again = FakeProvider()
    assert _run(settings, again) == 0
    assert again.calls == []
    assert len(_windows(settings)) == 3


def test_a_resumed_session_continues_from_its_last_narrated_step(job_root: Path) -> None:
    settings = _settings(job_root)
    _run(settings, FakeProvider())
    before = _windows(settings)

    # The session resumes: two more steps land in its transcript.
    write_session(job_root, JOB, JOB_TURNS_RESUMED, day=22)
    provider = FakeProvider()
    assert _run(settings, provider) == 1

    rows = _windows(settings)
    # The three narrated windows are untouched; one new window follows them.
    assert rows[:3] == before
    assert len(rows) == 4
    new = rows[3]
    assert new["window_index"] == 3
    assert (new["first_step_uuid"], new["last_step_uuid"], new["step_count"]) == (
        "dddd-8",
        "dddd-9",
        2,
    )
    # One narration call, for the new steps only, carrying the stored
    # narration of the window before them and the end of its raw steps.
    (prompt,) = _narration_prompts(provider)
    assert "intent: intent 3\nactions: actions 3\noutcome: outcome 3" in prompt
    carried, _, window = prompt.partition("<transcript id=")
    assert "Wrote tests/test_export.py, 3 passed." in carried
    assert "also update the changelog" in window
    assert "fix the export job" not in prompt
    assert "Narrate window 3 " in prompt

    # The story is told again over all four windows; the old one stays.
    stories = _stories(settings)
    assert [s["window_count"] for s in stories] == [3, 4]
    assert stories[1]["step_count"] == 9
    assert stories[1]["last_step_uuid"] == "dddd-9"


def test_the_read_position_lives_in_the_rows_not_the_checkpoint(
    job_root: Path, tmp_path: Path
) -> None:
    """With an empty checkpoint, a narrated session is re-admitted and costs nothing."""
    settings = _settings(job_root)
    _run(settings, FakeProvider())

    # A fresh state db stands in for a lost one: no checkpoint row says the
    # session was narrated, so only the window rows can.
    fresh = tmp_path / "fresh-state.db"
    provider = FakeProvider()
    done = _run(
        settings,
        provider,
        checkpoint=SqliteCheckpoint(fresh),
        retry=SqliteRetryQueue(fresh),
    )
    assert done == 1
    assert provider.calls == []
    assert len(_windows(settings)) == 3
    assert len(_stories(settings)) == 1
    assert JOB in load_as_map(fresh, "narrate")


def test_a_reconverted_session_falls_back_to_the_step_count(
    job_root: Path, captured_logs: list[str]
) -> None:
    """When the last narrated uuid is gone, the covered step count is the position."""
    settings = _settings(job_root)
    _run(settings, FakeProvider())

    # Same steps under new uuids (a different session id prefix would do it
    # in a real re-conversion), plus the resumed exchange.
    import json

    session_dir = job_root / "sessions" / JOB
    write_session(job_root, JOB, JOB_TURNS_RESUMED, day=22)
    doc = json.loads((session_dir / "trajectory.json").read_text())
    for step in doc["steps"]:
        step["extra"]["source_uuids"] = ["new-" + step["extra"]["source_uuids"][0]]
    (session_dir / "trajectory.json").write_text(json.dumps(doc))

    provider = FakeProvider()
    _run(settings, provider)
    new = _windows(settings)[3]
    assert (new["first_step_uuid"], new["step_count"]) == ("new-dddd-8", 2)
    assert any("no longer holds its last narrated step" in line for line in captured_logs)


# ---------------------------------------------------------------------------
# budget, failure, refusal
# ---------------------------------------------------------------------------


class _BillingProvider(FakeProvider):
    """Bills 10M input tokens per call: $1 on the small tier."""

    @override
    async def classify_structured(self, *, system: str, prompt: str, schema: type) -> Any:
        self.usage.add(CallUsage(input_tokens=10_000_000, output_tokens=0))
        return await super().classify_structured(system=system, prompt=prompt, schema=schema)


def test_the_cost_ceiling_stops_a_session_between_windows(job_root: Path) -> None:
    settings = _settings(job_root)
    provider = _BillingProvider()
    # $1.50 ceiling against $1 a call: the second call crosses it, and the
    # third window is never sent.
    assert _run(settings, provider, budget=RunBudget(1.5)) == 0
    assert len(provider.calls) == 2
    assert [r["window_index"] for r in _windows(settings)] == [0, 1]
    assert _stories(settings) == []
    # Not checkpointed and not queued: it's unfinished, not failed.
    state_db = settings.layout().state_db_path
    assert load_as_map(state_db, "narrate") == {}
    assert SqliteRetryQueue(state_db).pending_count(pipeline="narrate") == 0

    # The next run picks up at window 2 and tells the story.
    resumed = FakeProvider()
    assert _run(settings, resumed) == 1
    assert [name for name, _ in resumed.calls] == ["WindowNarration", "SessionStory"]
    assert [r["window_index"] for r in _windows(settings)] == [0, 1, 2]
    assert "intent: intent 2" in _narration_prompts(resumed)[0]
    assert [s["window_count"] for s in _stories(settings)] == [3]


def test_an_exhausted_budget_starts_nothing(job_root: Path) -> None:
    settings = _settings(job_root)
    provider = FakeProvider()
    assert _run(settings, provider, budget=RunBudget(0.0)) == 0
    assert provider.calls == []
    assert _windows(settings) == []


def test_a_failed_call_keeps_the_windows_before_it_and_queues_the_session(job_root: Path) -> None:
    settings = _settings(job_root)
    # The third window's transcript is the only one holding this text.
    provider = FakeProvider(fail_prompts_containing="[user 2026-08-22T08:05:00.000Z]")
    assert _run(settings, provider) == 0
    assert [r["window_index"] for r in _windows(settings)] == [0, 1]
    state_db = settings.layout().state_db_path
    assert load_as_map(state_db, "narrate") == {}
    queue = SqliteRetryQueue(state_db)
    assert queue.pending_count(pipeline="narrate") == 1
    assert queue.blocked_units(pipeline="narrate") == {JOB}

    # While the retry entry backs off, the session is not re-sent.
    waiting = FakeProvider()
    assert _run(settings, waiting) == 0
    assert waiting.calls == []


def test_a_failed_story_keeps_the_windows_and_retries_only_the_story(job_root: Path) -> None:
    settings = _settings(job_root)
    provider = FakeProvider(fail_prompts_containing="<narrations id=")
    assert _run(settings, provider) == 0
    assert len(_windows(settings)) == 3
    assert _stories(settings) == []
    state_db = settings.layout().state_db_path
    assert load_as_map(state_db, "narrate") == {}

    # Clear the backoff by hand, as a later tick would find it.
    SqliteRetryQueue(state_db).mark_done(pipeline="narrate", unit_ids=[JOB])
    retry = FakeProvider()
    assert _run(settings, retry) == 1
    assert [name for name, _ in retry.calls] == ["SessionStory"]
    assert len(_windows(settings)) == 3
    assert len(_stories(settings)) == 1


def test_a_refused_window_is_written_in_band_and_the_session_completes(job_root: Path) -> None:
    # One carried step, so the refused window's human turn (the marker) isn't
    # also in the next call's carry-over.
    settings = _settings(job_root, narrate_carry_steps=1)
    provider = FakeProvider(refuse_prompts_containing="[user 2026-08-22T08:03:00.000Z] add the key")
    assert _run(settings, provider) == 1
    rows = _windows(settings)
    assert [r["window_index"] for r in rows] == [0, 1, 2]
    assert (rows[1]["intent"], rows[1]["actions"], rows[1]["outcome"]) == (REFUSED,) * 3
    assert rows[2]["intent"] != REFUSED
    assert JOB in load_as_map(settings.layout().state_db_path, "narrate")


def test_the_session_cap_admits_the_newest_sessions(tmp_path: Path) -> None:
    root = tmp_path / "cap"
    older = "11111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    newer = "22222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    write_session(root, older, [("user", "old work"), ("agent", "ok")], day=10)
    write_session(root, newer, [("user", "new work"), ("agent", "ok")], day=20)
    settings = _settings(root, llm_max_sessions_per_run=1)
    assert _run(settings, FakeProvider()) == 1
    assert len(_windows(settings, newer)) == 1
    assert _windows(settings, older) == []


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def test_dry_run_plans_the_windows_and_spends_nothing(job_root: Path) -> None:
    settings = _settings(job_root)
    provider = FakeProvider()
    plan = narrate_sessions(settings, provider=provider, spec=SPEC)
    assert isinstance(plan, dict)
    assert provider.calls == []
    assert plan["pipeline"] == "narrate"
    assert plan["dry_run"] is True
    assert plan["candidates"] == 1
    assert plan["capped_candidates"] == 1
    assert plan["windows"] == 3
    assert plan["stories"] == 1
    assert plan["llm_calls"] == 4
    assert plan["model"] == SPEC.model_id
    assert plan["estimated_input_tokens"] > 0
    assert plan["estimated_cost_usd"] >= 0
    assert plan["max_sessions_per_run"] == 50
    # Nothing was written.
    assert not settings.layout().narration_dir.exists()
    assert not settings.layout().stories_dir.exists()

    # After a real run the plan is empty.
    _run(settings, FakeProvider())
    after = narrate_sessions(settings, provider=provider, spec=SPEC)
    assert isinstance(after, dict)
    assert (after["candidates"], after["windows"], after["llm_calls"]) == (0, 0, 0)


# ---------------------------------------------------------------------------
# a call that stops on length, and the output cap
# ---------------------------------------------------------------------------


class _LengthStopProvider(FakeProvider):
    """Stops on length for one window, the way an adapter reports it after its own retry.

    Both adapters turn a ``length`` / ``max_tokens`` finish into
    :class:`ProviderUnavailable` once the one lower-effort retry has also
    been cut off, and never hand back the partial reply.
    """

    def __init__(self, marker: str) -> None:
        super().__init__()
        self._marker = marker

    @override
    async def classify_structured(self, *, system: str, prompt: str, schema: type) -> Any:
        if self._marker in prompt.rpartition("<transcript id=")[2]:
            self.calls.append((schema.__name__, prompt))
            msg = "response truncated (finish_reason=length) — raise max_completion_tokens"
            raise ProviderUnavailable(msg)
        return await super().classify_structured(system=system, prompt=prompt, schema=schema)


def test_a_length_stop_writes_no_row_and_queues_the_session(job_root: Path) -> None:
    """A cut-off reply is never stored: the window has no row until a call completes."""
    settings = _settings(job_root)
    provider = _LengthStopProvider("add the key and rerun")
    assert _run(settings, provider) == 0

    # Window 0 completed and is kept. Window 1 stopped on length: no row for
    # it, and none for window 2, which would have carried its narration.
    assert [r["window_index"] for r in _windows(settings)] == [0]
    assert _stories(settings) == []
    state_db = settings.layout().state_db_path
    assert load_as_map(state_db, "narrate") == {}
    queue = SqliteRetryQueue(state_db)
    assert queue.blocked_units(pipeline="narrate") == {JOB}

    # Once the retry comes due, the session continues from window 1.
    queue.mark_done(pipeline="narrate", unit_ids=[JOB])
    retry = FakeProvider()
    assert _run(settings, retry) == 1
    assert [r["window_index"] for r in _windows(settings)] == [0, 1, 2]
    assert REFUSED not in {r["intent"] for r in _windows(settings)}


def test_narrate_asks_for_the_models_own_output_limit(
    job_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider narrate builds caps output at the model's limit, not the shared default."""
    from atif_analytics.application.use_cases import _shared, narrate as narrate_module

    built: list[tuple[str, int]] = []
    real = _shared.build_provider

    def _recording(settings: AnalyticsSettings, pipeline: str, **kwargs: Any) -> tuple[Any, Any]:
        provider, spec = real(settings, pipeline, **kwargs)
        built.append((pipeline, spec.max_completion_tokens))
        return provider, spec

    monkeypatch.setattr(narrate_module, "build_provider", _recording)
    plan = narrate_sessions(_settings(job_root))
    assert isinstance(plan, dict)
    assert built == [("narrate", 128_000)]
    assert plan["max_output_tokens"] == 128_000
    # The judgment pipelines keep the registry default.
    _, classify_spec = real(_settings(job_root), "classify")
    assert classify_spec.max_completion_tokens == 32_000
