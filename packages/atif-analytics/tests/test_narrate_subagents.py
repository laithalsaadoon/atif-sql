# SPDX-License-Identifier: Apache-2.0

"""Narrate over subagents: one stream per subagent, its story, and its digest in the launching window.

FakeProvider-driven. One hand-built session holds every shape the pipeline
has to place:

* ``agent-a``: a foreground subagent (its report is the launching call's
  result), which itself launches ``agent-a1`` (nested).
* ``agent-b``: a background subagent whose report arrives later as a task
  notification.
* ``agent-c``: a background subagent that never reports back.

``agent-b`` and ``agent-c`` run in parallel, so their steps interleave in the
flat step list the way a real transcript's do.

The fake numbers its answers by call. Sibling streams start in id order, and
a stream with children of its own waits for them, so the call order is: b's
window and story, c's window and story, a1's window and story, a's three
windows and story, then the five main-chain windows and the session story.
That makes the digests: b 1, c 2, a1 3, a 4.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from analytics_fixtures import FakeProvider

from atif_analytics.application.use_cases.narrate import (
    SUBAGENT_STORY_SCHEMA,
    narrate_prompt_digest,
    narrate_sessions,
    story_prompt_digest,
    subagent_story_prompt_digest,
)
from atif_analytics.domain.narration import SubagentRef, TaskNotice
from atif_analytics.infrastructure.corpus_reader import CorpusReader
from atif_analytics.infrastructure.parquet_cache import ParquetCache
from atif_analytics.infrastructure.settings import AnalyticsSettings
from atif_models.domain.registry import resolve

if TYPE_CHECKING:
    from pathlib import Path

SPEC = resolve("small")
SID = "5ba9e275-0000-4000-8000-000000000001"

#: The task agent-a is given: longer than the 300 characters a harness line
#: is clipped to, so a clipped prompt would lose its end.
A_PROMPT = "Find vendor A's list price. " + "Check the pricing page and the FAQ. " * 12 + "END-A"
SEARCH_RESULT = "RESULT-HEAD " + "r" * 900 + " RESULT-TAIL"
FAILED_RESULT = "ERR-HEAD " + "e" * 900 + " ERR-TAIL"
LONG_COMMAND = "curl https://vendor-c.example/pricing " + "--header x " * 60 + "CMD-END"


def _step(
    n: int,
    source: str,
    message: str,
    *,
    agent: str | None = None,
    calls: list[tuple[str, str, dict[str, Any]]] | None = None,
    results: list[tuple[str, str, bool]] | None = None,
) -> dict[str, Any]:
    """One trajectory step at minute ``n``; ``agent`` makes it a sidechain step."""
    extra: dict[str, Any] = {"is_sidechain": agent is not None, "source_uuids": [f"u-{n:02d}"]}
    if agent is not None:
        extra["agent_id"] = agent
    step: dict[str, Any] = {
        "step_id": n,
        "timestamp": f"2026-11-01T10:{n:02d}:00.000Z",
        "source": source,
        "message": message,
        "extra": extra,
    }
    if calls:
        step["tool_calls"] = [
            {"tool_call_id": call_id, "function_name": name, "arguments": args}
            for call_id, name, args in calls
        ]
    if results:
        step["observation"] = {
            "results": [
                {"source_call_id": call_id, "content": content, "extra": {"is_error": failed}}
                for call_id, content, failed in results
            ]
        }
    return step


def _steps() -> list[dict[str, Any]]:
    launch = {"subagent_type": "researcher", "prompt": "see the subagent"}
    return [
        _step(1, "user", "research vendor pricing and write the report"),
        _step(
            2,
            "agent",
            "Launching a researcher for vendor A.",
            calls=[("tc-a", "Agent", launch)],
            results=[("tc-a", "[Subagent hand-back] Vendor A costs $5.", False)],
        ),
        _step(3, "user", A_PROMPT, agent="agent-a"),
        _step(
            4,
            "agent",
            "Searching.",
            agent="agent-a",
            calls=[("tc-s", "WebSearch", {"query": "vendor A pricing"})],
            results=[("tc-s", SEARCH_RESULT, False)],
        ),
        _step(
            5,
            "agent",
            "Asking a helper to read the pricing page.",
            agent="agent-a",
            calls=[("tc-a1", "Agent", launch)],
            results=[("tc-a1", "[Subagent hand-back] The page says $5.", False)],
        ),
        _step(6, "user", "Read the pricing page and report the price.", agent="agent-a1"),
        _step(7, "agent", "The page says $5.", agent="agent-a1"),
        _step(8, "agent", "Vendor A costs $5.", agent="agent-a"),
        _step(
            9,
            "agent",
            "Launching two background researchers.",
            calls=[("tc-b", "Agent", launch), ("tc-c", "Agent", launch)],
            results=[
                ("tc-b", "Async agent launched successfully. agentId: agent-b", False),
                ("tc-c", "Async agent launched successfully. agentId: agent-c", False),
            ],
        ),
        # agent-b and agent-c run side by side: their steps interleave.
        _step(10, "user", "Find vendor B's list price.", agent="agent-b"),
        _step(11, "user", "Find vendor C's list price.", agent="agent-c"),
        _step(12, "agent", "Reading vendor B's site.", agent="agent-b"),
        _step(
            13,
            "agent",
            "Fetching vendor C's site.",
            agent="agent-c",
            calls=[("tc-f", "Bash", {"command": LONG_COMMAND})],
            results=[("tc-f", FAILED_RESULT, True)],
        ),
        _step(14, "agent", "Vendor B costs $7.", agent="agent-b"),
        _step(15, "user", "any news?"),
        _step(16, "agent", "Still waiting on vendors B and C."),
        _step(
            17,
            "user",
            "<task-notification>\n<task-id>agent-b</task-id>\n<status>completed</status>\n"
            '<summary>Agent "Vendor B pricing" finished</summary>\n</task-notification>',
        ),
        _step(18, "agent", "Vendor B costs $7. Writing the report."),
    ]


SUBAGENTS = [
    {
        "agent_id": "agent-a",
        "agent_type": "researcher",
        "description": "Vendor A pricing",
        "parent_tool_call_id": "tc-a",
        "spawn_depth": 1,
        "link_source": "meta",
    },
    {
        "agent_id": "agent-a1",
        "agent_type": "reader",
        "description": "Read the pricing page",
        "parent_tool_call_id": "tc-a1",
        "parent_agent_id": "agent-a",
        "spawn_depth": 2,
        "link_source": "meta",
    },
    {
        "agent_id": "agent-b",
        "agent_type": "researcher",
        "description": "Vendor B pricing",
        "parent_tool_call_id": "tc-b",
        "spawn_depth": 1,
        "link_source": "meta",
    },
    {
        "agent_id": "agent-c",
        "agent_type": "researcher",
        "description": "Vendor C pricing",
        "parent_tool_call_id": "tc-c",
        "spawn_depth": 1,
        "link_source": "meta",
    },
]


def _write(root: Path, steps: list[dict[str, Any]]) -> None:
    """Write the session under ``root/sessions`` the way the corpus holds one."""
    doc = {
        "schema_version": "ATIF-v1.7",
        "session_id": SID,
        "agent": {"name": "claude-code", "version": "2.1.218", "model_name": "claude-opus-4-6"},
        "steps": steps,
        "final_metrics": None,
        "extra": {"subagents": SUBAGENTS},
    }
    session_dir = root / "sessions" / SID
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "trajectory.json").write_text(json.dumps(doc))
    edges = [
        {"uuid": step["extra"]["source_uuids"][0], "ts": step["timestamp"], "type": step["source"]}
        for step in steps
    ]
    (session_dir / "edges.jsonl").write_text("".join(json.dumps(e) + "\n" for e in edges))
    (session_dir / "loss_report.json").write_text("{}")
    (session_dir / "meta.json").write_text(
        json.dumps({"session_id": SID, "source_mtime_ns": 1, "materialized_at": "t"})
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    corpus = tmp_path / "subagent-corpus"
    _write(corpus, _steps())
    return corpus


def _settings(root: Path, **overrides: Any) -> AnalyticsSettings:
    """Windows close at every mark (floor 0), unless overridden."""
    return AnalyticsSettings(corpus_root=root, narrate_window_min_chars=0, **overrides)


def _run(settings: AnalyticsSettings, provider: FakeProvider) -> int:
    out = narrate_sessions(settings, dry_run=False, provider=provider, spec=SPEC)
    assert isinstance(out, int)
    return out


def _rows(settings: AnalyticsSettings, agent: str | None) -> list[dict[str, Any]]:
    df = ParquetCache(settings.layout().narration_dir).read_all()
    assert df is not None
    return [r for r in df.sort("window_index").to_dicts() if r["agent_id"] == agent]


def _subagent_stories(settings: AnalyticsSettings) -> list[dict[str, Any]]:
    df = ParquetCache(settings.layout().subagent_stories_dir).read_all()
    assert df is not None
    return df.sort(["agent_id", "window_count"]).to_dicts()


def _session_stories(settings: AnalyticsSettings) -> list[dict[str, Any]]:
    df = ParquetCache(settings.layout().stories_dir).read_all()
    assert df is not None
    return df.sort("narrated_at").to_dicts()


def _prompt(provider: FakeProvider, containing: str, schema: str = "WindowNarration") -> str:
    """The one prompt of ``schema`` whose transcript section holds ``containing``."""
    hits = [
        prompt
        for name, prompt in provider.calls
        if name == schema and containing in prompt.rpartition("<transcript id=")[2]
    ]
    assert len(hits) == 1, f"{len(hits)} prompts hold {containing!r}"
    return hits[0]


# ---------------------------------------------------------------------------
# the declared list reaches the pipeline
# ---------------------------------------------------------------------------


def test_the_reader_hands_over_the_declared_subagents(root: Path) -> None:
    reader = CorpusReader(root)
    steps = reader.load_full_steps(SID)
    refs = reader.subagents(SID)
    assert [r.agent_id for r in refs] == ["agent-a", "agent-a1", "agent-b", "agent-c"]
    assert refs[1] == SubagentRef(
        agent_id="agent-a1",
        agent_type="reader",
        description="Read the pricing page",
        parent_tool_call_id="tc-a1",
        parent_agent_id="agent-a",
        spawn_depth=2,
    )
    # Each step says whose it is, and each call carries its id.
    assert [s.agent_id for s in steps][:8] == [
        None,
        None,
        "agent-a",
        "agent-a",
        "agent-a",
        "agent-a1",
        "agent-a1",
        "agent-a",
    ]
    assert steps[8].tool_call_ids == ["tc-b", "tc-c"]
    assert steps[12].tool_result_errors == [True]
    # A second reader, which never loaded the steps, parses for the list.
    assert CorpusReader(root).subagents(SID) == refs


# ---------------------------------------------------------------------------
# one stream per subagent
# ---------------------------------------------------------------------------


def test_every_subagent_step_is_narrated_in_its_own_stream(root: Path) -> None:
    settings = _settings(root)
    assert _run(settings, FakeProvider()) == 1

    spans = {
        agent: [
            (r["window_index"], r["first_step_uuid"], r["last_step_uuid"], r["step_count"])
            for r in _rows(settings, agent)
        ]
        for agent in (None, "agent-a", "agent-a1", "agent-b", "agent-c")
    }
    assert spans == {
        None: [
            (0, "u-01", "u-01", 1),
            (1, "u-02", "u-02", 1),
            (2, "u-09", "u-09", 1),
            (3, "u-15", "u-16", 2),
            (4, "u-17", "u-18", 2),
        ],
        "agent-a": [(0, "u-03", "u-04", 2), (1, "u-05", "u-05", 1), (2, "u-08", "u-08", 1)],
        "agent-a1": [(0, "u-06", "u-07", 2)],
        # Interleaved in the transcript, separate in the rows.
        "agent-b": [(0, "u-10", "u-14", 3)],
        "agent-c": [(0, "u-11", "u-13", 2)],
    }
    # Nothing is left out: 18 steps in, 18 steps covered.
    df = ParquetCache(settings.layout().narration_dir).read_all()
    assert df is not None
    assert int(df["step_count"].sum()) == 18
    assert set(df["prompt_digest"].to_list()) == {narrate_prompt_digest()}


def test_window_boundaries_inside_a_subagent(root: Path) -> None:
    settings = _settings(root)
    _run(settings, FakeProvider())
    # No human is in a subagent: its instructions never close a window as a
    # human turn. The nested launch and its return do.
    assert [r["boundary"] for r in _rows(settings, "agent-a")] == [
        "subagent_start",
        "subagent_end",
        "session_end",
    ]
    assert [r["boundary"] for r in _rows(settings, None)] == [
        "subagent_start",
        "subagent_start",
        "human_turn",
        "subagent_end",
        "session_end",
    ]


def test_a_subagent_window_reads_as_a_subagents_own_steps(root: Path) -> None:
    settings = _settings(root)
    provider = FakeProvider()
    _run(settings, provider)
    prompt = _prompt(provider, "Searching.")
    # Its instructions are labeled as the launching agent's, and shown whole.
    assert f"[parent_agent 2026-11-01T10:03:00.000Z] {A_PROMPT}" in prompt
    assert "[user " not in prompt
    assert "Narrate window 0 of a subagent's own steps" in prompt
    assert "instructions from the agent that launched it, not from a human" in prompt
    # A main-chain window says neither.
    main = _prompt(provider, "research vendor pricing and write the report")
    assert "Narrate window 0 of the session" in main
    assert "subagent's own steps" not in main


def test_children_are_narrated_before_the_stream_that_launched_them(root: Path) -> None:
    provider = FakeProvider()
    _run(_settings(root), provider)
    position = {
        stream: next(
            i
            for i, (name, prompt) in enumerate(provider.calls)
            if name == "WindowNarration" and text in prompt
        )
        for stream, text in (
            ("a1", "Read the pricing page and report the price."),
            ("a", "Searching."),
            ("b", "Reading vendor B's site."),
            ("c", "Fetching vendor C's site."),
            ("main", "research vendor pricing and write the report"),
        )
    }
    assert position["a1"] < position["a"] < position["main"]
    assert position["b"] < position["main"]
    assert position["c"] < position["main"]
    assert [name for name, _ in provider.calls][-1] == "SessionStory"


# ---------------------------------------------------------------------------
# subagent stories and their digests
# ---------------------------------------------------------------------------


def test_each_subagent_gets_a_story_and_says_whether_it_returned(root: Path) -> None:
    settings = _settings(root)
    _run(settings, FakeProvider())
    stories = _subagent_stories(settings)
    assert [(s["agent_id"], s["story"], s["digest"], s["returned"]) for s in stories] == [
        ("agent-a", "subagent story 4", "digest 4", True),  # report on the launching call
        ("agent-a1", "subagent story 3", "digest 3", True),
        ("agent-b", "subagent story 1", "digest 1", True),  # report by task notification
        ("agent-c", "subagent story 2", "digest 2", False),  # never came back
    ]
    a = stories[0]
    assert (a["window_count"], a["step_count"]) == (3, 4)
    assert (a["first_step_uuid"], a["last_step_uuid"]) == ("u-03", "u-08")
    assert a["prompt_digest"] == subagent_story_prompt_digest()
    assert (
        len({narrate_prompt_digest(), story_prompt_digest(), subagent_story_prompt_digest()}) == 3
    )
    assert list(a) == list(SUBAGENT_STORY_SCHEMA)


def test_the_launching_window_is_shown_the_subagents_digest(root: Path) -> None:
    provider = FakeProvider()
    _run(_settings(root), provider)

    # Foreground: launched and back within one window.
    window = _prompt(provider, "Launching a researcher for vendor A.")
    section = window.partition("<subagents id=")[2].partition("</subagents id=")[0]
    assert (
        '- researcher, task "Vendor A pricing" (launched and returned in this window): digest 4'
        in section
    )
    # Only the subagents this window touches.
    assert "Vendor B pricing" not in section

    # Background: the launch window says both are still running, and isn't
    # told what they went on to find. The agent didn't know it yet.
    launch = _prompt(provider, "Launching two background researchers.")
    assert (
        '- researcher, task "Vendor B pricing" (launched in this window, still running when it '
        "ended): its work is described in the window where it ends" in launch
    )
    assert (
        '- researcher, task "Vendor C pricing" (launched in this window, still running when it '
        "ended): its work is described in the window where it ends" in launch
    )
    assert "digest" not in launch.partition("<subagents id=")[2].partition("</subagents id=")[0]

    # The window where the notification lands gets the digest again.
    back = _prompt(provider, "Writing the report.")
    assert (
        '- researcher, task "Vendor B pricing" (launched in an earlier window, returned in this '
        "one): digest 1" in back
    )
    assert "Vendor C pricing" not in back

    # A window no subagent touches has no section.
    # agent-c never reports: no step is its return. Its end is placed by the
    # clock, in the window holding the main chain's first step after its
    # last one, and said to be that.
    waiting = _prompt(provider, "Still waiting on vendors B and C.")
    assert (
        '- researcher, task "Vendor C pricing" (launched in an earlier window and took its last '
        "step during this one; no step shows its report coming back): digest 2" in waiting
    )
    assert "Vendor B pricing" not in waiting.partition("<transcript id=")[0]

    # A window no subagent touches has no section.
    assert "<subagents id=" not in _prompt(provider, "research vendor pricing and write the report")


def test_a_nested_subagents_digest_goes_to_the_subagent_that_launched_it(root: Path) -> None:
    provider = FakeProvider()
    _run(_settings(root), provider)
    nested_launch = _prompt(provider, "Asking a helper to read the pricing page.")
    assert (
        '- reader, task "Read the pricing page" (launched and returned in this window): digest 3'
        in nested_launch
    )
    # And to that subagent's story, but not to a main-chain window.
    (a_story,) = [p for n, p in provider.calls if n == "SubagentStory" and "intent: intent 4" in p]
    assert 'task "Read the pricing page" (returned): digest 3' in a_story
    assert "Read the pricing page" not in _prompt(provider, "Launching a researcher for vendor A.")


def test_the_session_story_is_told_what_the_subagents_contributed(root: Path) -> None:
    settings = _settings(root)
    provider = FakeProvider()
    _run(settings, provider)
    (prompt,) = [p for n, p in provider.calls if n == "SessionStory"]
    section = prompt.partition("<subagents id=")[2].partition("</subagents id=")[0]
    for line in (
        'task "Vendor A pricing" (returned): digest 4',
        'task "Read the pricing page" (returned): digest 3',
        'task "Vendor B pricing" (returned): digest 1',
        'task "Vendor C pricing" (finished; no step shows its report coming back): digest 2',
    ):
        assert line in section
    # The narrations it folds are the main chain's five, not the subagents'.
    narrations = prompt.partition("</narrations id=")[0]
    assert narrations.count("\nintent: ") == 5

    (story,) = _session_stories(settings)
    assert (story["window_count"], story["step_count"]) == (5, 7)
    assert (story["subagent_count"], story["subagent_window_count"]) == (4, 6)


# ---------------------------------------------------------------------------
# resume, per stream
# ---------------------------------------------------------------------------


def test_each_stream_resumes_from_its_own_last_row(root: Path) -> None:
    settings = _settings(root)
    _run(settings, FakeProvider())
    before = {agent: _rows(settings, agent) for agent in (None, "agent-a", "agent-b", "agent-c")}

    # The session goes on: agent-b corrects itself, and the main chain has
    # one more exchange. agent-a, agent-a1 and agent-c don't move.
    grown = [
        *_steps(),
        _step(19, "agent", "Correction: vendor B costs $8.", agent="agent-b"),
        _step(20, "user", "use the corrected price"),
        _step(21, "agent", "Report updated to $8."),
    ]
    _write(root, grown)
    provider = FakeProvider()
    assert _run(settings, provider) == 1

    # One new window for agent-b, numbered after its own last one.
    b_rows = _rows(settings, "agent-b")
    assert b_rows[:1] == before["agent-b"]
    assert [(r["window_index"], r["first_step_uuid"], r["step_count"]) for r in b_rows[1:]] == [
        (1, "u-19", 1)
    ]
    # One new window on the main chain, numbered after ITS last one.
    main_rows = _rows(settings, None)
    assert main_rows[:5] == before[None]
    assert [(r["window_index"], r["first_step_uuid"], r["step_count"]) for r in main_rows[5:]] == [
        (5, "u-20", 2)
    ]
    # The streams that didn't move made no call and kept their rows.
    assert _rows(settings, "agent-a") == before["agent-a"]
    assert _rows(settings, "agent-c") == before["agent-c"]
    assert [name for name, _ in provider.calls] == [
        "WindowNarration",  # agent-b window 1
        "SubagentStory",  # agent-b, told again over two windows
        "WindowNarration",  # main window 5
        "SessionStory",
    ]
    # agent-b's new window carries its own previous narration, not the main chain's.
    b_prompt = provider.calls[0][1]
    assert "intent: intent 1\nactions: actions 1\noutcome: outcome 1" in b_prompt
    assert "Narrate window 1 of a subagent's own steps" in b_prompt

    stories = [s for s in _subagent_stories(settings) if s["agent_id"] == "agent-b"]
    assert [(s["window_count"], s["step_count"]) for s in stories] == [(1, 3), (2, 4)]
    told = _session_stories(settings)
    assert [(s["window_count"], s["subagent_window_count"]) for s in told] == [(5, 6), (6, 7)]


def test_a_rerun_with_nothing_new_makes_no_call(root: Path) -> None:
    settings = _settings(root)
    _run(settings, FakeProvider())
    again = FakeProvider()
    assert _run(settings, again) == 0
    assert again.calls == []


def test_a_failed_subagent_call_stops_the_session_and_keeps_what_was_written(root: Path) -> None:
    settings = _settings(root)
    provider = FakeProvider(fail_prompts_containing="Reading vendor B's site.")
    assert _run(settings, provider) == 0
    # agent-a and its nested helper finished before agent-b failed; the
    # main chain, which needs agent-b's digest, never started.
    assert len(_rows(settings, "agent-a")) == 3
    assert len(_rows(settings, "agent-a1")) == 1
    assert _rows(settings, "agent-b") == []
    assert _rows(settings, None) == []
    assert [s["agent_id"] for s in _subagent_stories(settings)] == [
        "agent-a",
        "agent-a1",
        "agent-c",
    ]


# ---------------------------------------------------------------------------
# what a call is shown: tool results and tool arguments
# ---------------------------------------------------------------------------


def test_a_call_sees_the_opening_and_the_ending_of_each_tool_result(root: Path) -> None:
    provider = FakeProvider()
    _run(_settings(root), provider)
    search = _prompt(provider, "Searching.")
    assert "RESULT-HEAD " + "r" * 288 in search
    # The ending is where a command's summary line is.
    assert "r" * 188 + " RESULT-TAIL" in search
    assert "…(+424 chars not shown here)" in search
    # The cut is this view's, so it isn't worded as a truncation.
    assert "truncated" not in search
    # A short result is shown whole.
    assert "[Subagent hand-back] Vendor A costs $5." in _prompt(
        provider, "Launching a researcher for vendor A."
    )


def test_a_failed_result_keeps_its_ending_too(root: Path) -> None:
    provider = FakeProvider()
    _run(_settings(root), provider)
    fetch = _prompt(provider, "Fetching vendor C's site.")
    assert "[failed] ERR-HEAD " in fetch
    assert "ERR-TAIL" in fetch
    assert "…(+318 chars not shown here)" in fetch


def test_tail_zero_shows_only_the_opening_of_a_result_that_did_not_fail(root: Path) -> None:
    provider = FakeProvider()
    _run(_settings(root, narrate_tool_result_tail_chars=0), provider)
    search = _prompt(provider, "Searching.")
    assert "RESULT-TAIL" not in search
    assert "…(+624 chars not shown here)" in search
    # A failed result's ending has its own setting.
    assert "ERR-TAIL" in _prompt(provider, "Fetching vendor C's site.")


def test_head_zero_shows_results_as_sizes_like_the_other_pipelines(root: Path) -> None:
    provider = FakeProvider()
    _run(_settings(root, narrate_tool_result_head_chars=0), provider)
    search = _prompt(provider, "Searching.")
    assert f"({len(SEARCH_RESULT)} chars omitted)" in search
    assert "RESULT-HEAD" not in search
    fetch = _prompt(provider, "Fetching vendor C's site.")
    assert f"({len(FAILED_RESULT)} chars omitted, the step has a failed result)" in fetch


def test_the_other_pipelines_still_read_results_as_sizes(root: Path) -> None:
    """Narrate's excerpts are its own: the shared reader's transcript is unchanged."""
    text = CorpusReader(root).session_text(SID)
    assert "RESULT-HEAD" not in text
    assert "(39 chars omitted)" in text  # the hand-back on the main chain, as a size
    assert "[failed]" not in text


def test_tool_arguments_are_shown_at_narrates_own_length(root: Path) -> None:
    assert len(LONG_COMMAND) > 400
    provider = FakeProvider()
    _run(_settings(root), provider)
    assert "CMD-END" in _prompt(provider, "Fetching vendor C's site.")

    short = FakeProvider()
    clipped_root = root.parent / "clipped"
    _write(clipped_root, _steps())
    _run(_settings(clipped_root, narrate_tool_input_chars=400), short)
    fetch = _prompt(short, "Fetching vendor C's site.")
    assert "CMD-END" not in fetch
    assert "chars not shown here)" in fetch
    assert "…(truncated)" not in fetch


# ---------------------------------------------------------------------------
# reports that arrive beside the transcript, and what each window is told of its end
# ---------------------------------------------------------------------------


def _queue_notice(root: Path, agent: str, minute: int) -> None:
    """Add a queued task notification for ``agent`` to the session's events, as the converter keeps one."""
    block = (
        f"<task-notification>\n<task-id>{agent}</task-id>\n<status>completed</status>\n"
        "<result>Vendor C costs $9.</result>\n</task-notification>"
    )
    rows = [
        # What surrounds a notice in a real session: hook output and a
        # message queued TO a subagent, neither of which is a report.
        {
            "seq": 1,
            "ts": "2026-11-01T10:01:00.000Z",
            "event_type": "attachment",
            "subtype": "hook_additional_context",
            "payload": {"content": f"<task-id>{agent}</task-id>"},
        },
        {
            "seq": 2,
            "ts": "2026-11-01T10:12:00.000Z",
            "event_type": "attachment",
            "subtype": "queued_command",
            "payload": {"agentId": agent, "prompt": "Priority lead"},
        },
        {
            "seq": 3,
            "ts": f"2026-11-01T10:{minute:02d}:30.000Z",
            "event_type": "attachment",
            "subtype": "queued_command",
            "payload": {"renderedInHumanTurn": [{"content": block}], "prompt": block},
        },
    ]
    (root / "sessions" / SID / "session_events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )


def test_the_reader_finds_the_queued_notice_and_nothing_else(root: Path) -> None:
    assert CorpusReader(root).task_notices(SID) == []
    _queue_notice(root, "agent-c", 16)
    assert CorpusReader(root).task_notices(SID) == [
        TaskNotice(ts="2026-11-01T10:16:30.000Z", task_id="agent-c", status="completed")
    ]


def test_a_queued_report_is_the_subagents_return(root: Path) -> None:
    """agent-c's report arrives as a queued notice at 10:16:30: no step holds it.

    Without it, agent-c's end is where the clock puts it (the window holding
    minute 15) and it reads as never having reported. With it, the return is
    the agent's first step after the notice, minute 17, which is in the last
    window.
    """
    _queue_notice(root, "agent-c", 16)
    settings = _settings(root)
    provider = FakeProvider()
    _run(settings, provider)

    (c_story,) = [s for s in _subagent_stories(settings) if s["agent_id"] == "agent-c"]
    assert c_story["returned"] is True
    # The digest lands in the window the report reached the agent in, said
    # as a return, and no longer in the one the clock pointed at.
    last = _prompt(provider, "Writing the report.")
    assert (
        '- researcher, task "Vendor C pricing" (launched in an earlier window, returned in this '
        "one): digest 2" in last
    )
    waiting = _prompt(provider, "Still waiting on vendors B and C.")
    assert "<subagents id=" not in waiting
    # The subagent's own last window and its story are told the report got back.
    c_window = _prompt(provider, "Fetching vendor C's site.")
    assert c_window.rstrip().endswith(
        "This is the last window of the subagent's steps recorded so far. The launching agent "
        "received the subagent's report: its last message is what it handed back."
    )
    (c_request,) = [
        p for n, p in provider.calls if n == "SubagentStory" and "intent: intent 2" in p
    ]
    assert c_request.rstrip().endswith(
        "The launching agent received the subagent's report: its last message is what it "
        "handed back."
    )
    (session_story,) = [p for n, p in provider.calls if n == "SessionStory"]
    assert 'task "Vendor C pricing" (returned): digest 2' in session_story


def test_with_no_report_on_record_a_subagent_is_told_so(root: Path) -> None:
    provider = FakeProvider()
    _run(_settings(root), provider)
    c_window = _prompt(provider, "Fetching vendor C's site.")
    assert c_window.rstrip().endswith(
        "This is the last window of the subagent's steps recorded so far. No record shows the "
        "launching agent receiving a report from it."
    )


def test_each_window_is_told_what_closed_it(root: Path) -> None:
    """A window's end isn't the session's: only the last window of a stream is told it's the last."""
    provider = FakeProvider()
    _run(_settings(root), provider)
    endings = {
        "research vendor pricing and write the report": (
            "This isn't the last window: it closes because the agent launches a subagent next, "
            "and the session goes on after it. Say where things stood when the window ended."
        ),
        "Launching two background researchers.": (
            "This isn't the last window: it closes because the human speaks next, and the "
            "session goes on after it. Say where things stood when the window ended."
        ),
        "Still waiting on vendors B and C.": (
            "This isn't the last window: it closes because a subagent's work comes back at this "
            "point, and the session goes on after it. Say where things stood when the window "
            "ended."
        ),
        "Writing the report.": (
            "This is the last window recorded so far: no later steps are in the record. Say "
            "where things stood at its end. A session can be resumed, so don't say that the "
            "session or the transcript ended."
        ),
        # Inside agent-a: its first window closes at the nested launch.
        "Searching.": (
            "This isn't the last window: it closes because the agent launches a subagent next, "
            "and the subagent's steps go on after it. Say where things stood when the window "
            "ended."
        ),
    }
    for text, ending in endings.items():
        assert _prompt(provider, text).rstrip().endswith(ending), text
    # Exactly one window of each stream is told it's the last.
    lasts = [
        p for n, p in provider.calls if n == "WindowNarration" and "This is the last window" in p
    ]
    assert len(lasts) == 5  # the main chain and the four subagents
    # The transcript says it too, at both ends of the steps.
    for prompt in lasts:
        assert "; the last window recorded so far]\n" in prompt
        assert "; no later steps are recorded]\n</transcript id=" in prompt
    middle = _prompt(provider, "Launching two background researchers.")
    assert " of the session; not the last window: more steps follow it]\n" in middle
    assert "; next, not shown: the human speaks]\n</transcript id=" in middle


# ---------------------------------------------------------------------------
# a tool result the harness saved to a file
# ---------------------------------------------------------------------------

SAVED = f"/home/someone/.claude/projects/-work/{SID}/tool-results/out-1.txt"
PERSISTED = (
    f"<persisted-output>\nOutput too large (67.8KB). Full output saved to: {SAVED}\n\n"
    "Preview (first 2KB):\nPREVIEW-ONLY " + "p" * 500
)
SAVED_BODY = "ARCHIVE-HEAD " + "b" * 3_000 + " ARCHIVE-TAIL"


def _with_persisted_result(root: Path, *, archived: bool) -> None:
    """Make agent-c's fetch return a persisted stub, and put the saved file in the corpus archive."""
    steps = _steps()
    steps[12]["observation"]["results"][0] = {
        "source_call_id": "tc-f",
        "content": PERSISTED,
        "extra": {"is_error": False},
    }
    _write(root, steps)
    if archived:
        archive = root / "sessions" / SID / "source" / SID / "tool-results"
        archive.mkdir(parents=True)
        (archive / "out-1.txt").write_text(SAVED_BODY)


def test_a_persisted_result_is_marked_and_read_from_the_corpus_archive(root: Path) -> None:
    _with_persisted_result(root, archived=True)
    provider = FakeProvider()
    _run(_settings(root), provider)
    fetch = _prompt(provider, "Fetching vendor C's site.")
    assert f"[persisted to {SAVED}, 67.8KB] ARCHIVE-HEAD " in fetch
    assert "ARCHIVE-TAIL" in fetch
    # The preview the transcript carried isn't what the call is shown.
    assert "PREVIEW-ONLY" not in fetch


def test_a_persisted_result_without_its_file_shows_the_preview(root: Path) -> None:
    _with_persisted_result(root, archived=False)
    provider = FakeProvider()
    _run(_settings(root), provider)
    fetch = _prompt(provider, "Fetching vendor C's site.")
    assert f"[persisted to {SAVED}, 67.8KB] PREVIEW-ONLY " in fetch
    assert "(the saved file isn't in the corpus; this is the transcript's preview)" in fetch


def test_the_saved_file_is_found_by_name_in_the_archive_and_the_path_is_never_opened(
    root: Path, tmp_path: Path
) -> None:
    """The stub's path is transcript text. Only its file name is used, under the session's archive."""
    _with_persisted_result(root, archived=True)
    reader = CorpusReader(root)
    assert reader.tool_result_file(SID, SAVED) == SAVED_BODY
    # A compressed archive copy reads the same.
    import zstandard

    archive = root / "sessions" / SID / "source" / SID / "tool-results"
    (archive / "big.json.zst").write_bytes(zstandard.ZstdCompressor().compress(b"[1, 2, 3]"))
    assert reader.tool_result_file(SID, "/anywhere/else/big.json") == "[1, 2, 3]"

    # A real file at the named path, outside the archive, is not read.
    outside = tmp_path / "outside" / SID / "tool-results"
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_text("not for the model")
    assert reader.tool_result_file(SID, str(outside / "secret.txt")) is None
    # Nor is anything the name could be made to point at.
    (root / "sessions" / SID / "source" / SID / "notes.txt").write_text("a sibling of tool-results")
    (archive / ".hidden").write_text("a dotfile inside the archive")
    for path in (
        "../notes.txt",
        "..",
        "",
        ".hidden",
        "a/../../meta.json",
        "out-*.txt",
        "/etc/passwd",
    ):
        assert reader.tool_result_file(SID, path) is None, path
    # A symlink planted in the archive isn't followed.
    (archive / "link.txt").symlink_to(outside / "secret.txt")
    assert reader.tool_result_file(SID, "link.txt") is None
