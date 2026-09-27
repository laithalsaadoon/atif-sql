# SPDX-License-Identifier: Apache-2.0

"""Synthetic contract-shaped corpus + provider fakes for atif-analytics tests.

Handwritten ATIF-v1.7 trajectory dicts + edges lines per docs/CONTRACT.md —
NO dependency on atif-corpus/atif-converter (independence). Two sessions:

* ``SESSION_IDS[0]`` — rich and ``interactive``: main-chain text steps
  (uuids via ``extra.source_uuids``), a tool call + result on one assistant
  step, an ERROR tool result step followed by a trailing-? user step
  (friction rule 3), a repeated user message (rule 1), a short imperative
  (rule 2), one sidechain step and one compact-summary step, and the
  machine-written user-role text the live corpora carry: a Stop hook block,
  a retry nudge twice and a screenshot's ``[Image: original ...]`` line
  twice (each pair a rule-1 "repeat" if it were read as the user), plus a
  continuation marker.
* ``SESSION_IDS[1]`` — small and ``interactive``: two human turns, one
  reply (one human-AI pair, below perceived's floor).

``FakeProvider`` implements the LlmStructuredProvider protocol with
deterministic canned outputs per schema and scriptable failures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atif_analytics.domain.models import (
    ConflictPair,
    ConflictsResult,
    PerceivedError,
    PerceivedErrorsResult,
    SessionClassification,
    UserFrictionSignal,
)
from atif_models.domain.ports import ProviderUnavailable, RefusalError, UsageAccumulator

SESSION_IDS = [
    "aaaaaaaa-1111-1111-1111-111111111111",
    "bbbbbbbb-2222-2222-2222-222222222222",
]

#: Session one's HUMAN turns, in order: the only user steps a reader treats
#: as the user speaking.
S1_HUMAN_UUIDS = ["u-01", "u-04", "u-05", "u-06"]

#: Session one's machine-written user-role steps (never friction candidates,
#: never perceived anchors, never rendered as ``[user ...]``).
S1_MACHINE_UUIDS = ["u-hook", "u-retry-1", "u-img-1", "u-retry-2", "u-img-2", "u-marker"]

RETRY_NUDGE = "Your previous attempt hit a transient error. Try that again."
IMAGE_META = "[Image: original 1000x500, displayed at 800x400.]"


def _step(
    step_id: int,
    ts: str,
    source: str,
    message: Any,
    *,
    uuid: str | None = None,
    sidechain: bool = False,
    compact: bool = False,
    tool_calls: list[dict[str, Any]] | None = None,
    observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extra: dict[str, Any] = {"is_sidechain": sidechain}
    if compact:
        extra["is_compact_summary"] = True
    if uuid is not None:
        extra["source_uuids"] = [uuid, f"{uuid}-extra"]
    step: dict[str, Any] = {
        "step_id": step_id,
        "timestamp": ts,
        "source": source,
        "message": message,
        "extra": extra,
    }
    if tool_calls is not None:
        step["tool_calls"] = tool_calls
    if observation is not None:
        step["observation"] = observation
    return step


def _session_one() -> dict[str, Any]:
    sid = SESSION_IDS[0]
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": sid,
        "agent": {"name": "claude-code", "version": "2.1.218", "model_name": "claude-opus-4-6"},
        "steps": [
            _step(
                1,
                "2026-08-20T10:00:00.000Z",
                "user",
                "please fix the flaky auth test",
                uuid="u-01",
            ),
            _step(
                2,
                "2026-08-20T10:00:10.000Z",
                "agent",
                "Reading the test file now.",
                uuid="u-02",
                tool_calls=[
                    {
                        "tool_call_id": "toolu_01",
                        "function_name": "Read",
                        "arguments": {"file_path": "/proj/tests/test_auth.py"},
                    }
                ],
                observation={"results": [{"source_call_id": "toolu_01", "content": "X" * 60_000}]},
            ),
            # Sidechain step — excluded from transcripts and windows.
            _step(
                3,
                "2026-08-20T10:00:20.000Z",
                "agent",
                "subagent chatter that must never appear",
                uuid="u-sc",
                sidechain=True,
            ),
            # ERROR tool result step (assistant), then a trailing-? user step.
            _step(
                4,
                "2026-08-20T10:00:30.000Z",
                "agent",
                "Running the suite.",
                uuid="u-03",
                tool_calls=[
                    {
                        "tool_call_id": "toolu_02",
                        "function_name": "Bash",
                        "arguments": {"command": "pytest -q"},
                    }
                ],
                observation={
                    "results": [
                        {
                            "source_call_id": "toolu_02",
                            "content": "Error: 3 failed",
                            "extra": {"tool_result_metadata": {"is_error": True}},
                        }
                    ]
                },
            ),
            _step(5, "2026-08-20T10:00:40.000Z", "user", "why is it failing?", uuid="u-04"),
            # Repeated user message (rule 1) — same body as u-01.
            _step(
                6,
                "2026-08-20T10:00:50.000Z",
                "user",
                "please fix the flaky auth test",
                uuid="u-05",
            ),
            # Short imperative (rule 2).
            _step(7, "2026-08-20T10:01:00.000Z", "user", "undo that", uuid="u-06"),
            # Machine-written user-role text. Each of the two pairs repeats
            # within ten user turns, which rule 1 stamped as unmet_expectation
            # before readers saw only human turns.
            _step(
                11,
                "2026-08-20T10:01:01.000Z",
                "user",
                "Stop hook feedback: the claim has no evidence. undo that?",
                uuid="u-hook",
            ),
            _step(12, "2026-08-20T10:01:02.000Z", "user", RETRY_NUDGE, uuid="u-retry-1"),
            _step(13, "2026-08-20T10:01:02.500Z", "user", IMAGE_META, uuid="u-img-1"),
            _step(14, "2026-08-20T10:01:03.000Z", "user", RETRY_NUDGE, uuid="u-retry-2"),
            _step(15, "2026-08-20T10:01:03.500Z", "user", IMAGE_META, uuid="u-img-2"),
            # Friction system marker — excluded from candidates.
            _step(
                8,
                "2026-08-20T10:01:05.000Z",
                "user",
                "Continue from where you left off.",
                uuid="u-marker",
            ),
            # Compact-summary step — excluded from windows.
            _step(
                9,
                "2026-08-20T10:01:07.000Z",
                "user",
                "compact summary body",
                uuid="u-cs",
                compact=True,
            ),
            _step(
                10,
                "2026-08-20T10:01:10.000Z",
                "agent",
                [
                    {"type": "text", "text": "fixed the race"},
                    {"type": "text", "text": "re-ran twice, green"},
                ],
                uuid="u-07",
            ),
        ],
        "final_metrics": None,
        "extra": None,
    }


def _session_two() -> dict[str, Any]:
    sid = SESSION_IDS[1]
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": sid,
        "agent": {"name": "claude-code", "version": "2.1.218", "model_name": "claude-opus-4-6"},
        "steps": [
            _step(1, "2026-08-21T09:00:00.000Z", "user", "draft the launch memo", uuid="v-01"),
            _step(2, "2026-08-21T09:00:30.000Z", "agent", "Here is a first draft.", uuid="v-02"),
            # A second human turn the session ended on, unanswered: two human
            # turns make the session interactive (classify and conflicts read
            # it), one completed human-AI pair keeps it below perceived's
            # two-pair floor.
            _step(3, "2026-08-21T09:01:00.000Z", "user", "tighten the intro", uuid="v-03"),
        ],
        "final_metrics": None,
        "extra": None,
    }


def _edges_for(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """One edges line per source uuid of every step (both entries)."""
    return [
        {
            "uuid": uuid,
            "parent_uuid": None,
            "message_id": None,
            "type": "user" if step["source"] == "user" else "assistant",
            "ts": step["timestamp"],
            "is_sidechain": step.get("extra", {}).get("is_sidechain", False),
            "is_compact_summary": step.get("extra", {}).get("is_compact_summary", False),
            "source_file": "transcript.jsonl",
            "tool_use_ids": [],
        }
        for step in doc["steps"]
        for uuid in step.get("extra", {}).get("source_uuids", [])
    ]


def write_session(root: Path, session_id: str, turns: list[tuple[str, str]], day: int) -> None:
    """Write one extra ``(source, message)`` session under ``root/sessions/``.

    Steps get uuids ``<session_id[:4]>-<n>`` and timestamps on 2026-08-``day``,
    so a caller controls where the session sorts among the fixture ones.
    """
    doc = {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id,
        "agent": {"name": "claude-code", "version": "2.1.218", "model_name": "claude-opus-4-6"},
        "steps": [
            _step(
                i + 1,
                f"2026-08-{day:02d}T08:{i:02d}:00.000Z",
                source,
                message,
                uuid=f"{session_id[:4]}-{i + 1}",
            )
            for i, (source, message) in enumerate(turns)
        ],
        "final_metrics": None,
        "extra": None,
    }
    session_dir = root / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "trajectory.json").write_text(json.dumps(doc))
    (session_dir / "edges.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in _edges_for(doc))
    )
    (session_dir / "loss_report.json").write_text("{}")
    (session_dir / "meta.json").write_text(
        json.dumps({"session_id": session_id, "source_mtime_ns": 1, "materialized_at": "t"})
    )


def build_fixture_corpus(root: Path) -> Path:
    """Write the two-session contract-shaped corpus under ``root``; return it."""
    for doc in (_session_one(), _session_two()):
        sid = doc["session_id"]
        session_dir = root / "sessions" / sid
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "trajectory.json").write_text(json.dumps(doc))
        (session_dir / "edges.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in _edges_for(doc))
        )
        (session_dir / "loss_report.json").write_text("{}")
        # meta.json LAST (torn-set guard contract).
        (session_dir / "meta.json").write_text(
            json.dumps({"session_id": sid, "source_mtime_ns": 1, "materialized_at": "t"})
        )
    return root


class FakeProvider:
    """Deterministic LlmStructuredProvider double.

    ``fail_prompts_containing`` raises :class:`ProviderUnavailable` when the
    marker appears in the prompt; ``refuse_prompts_containing`` raises
    :class:`RefusalError`. Otherwise a canned instance of the requested
    schema comes back and the call is recorded.
    """

    provider = "fake"

    def __init__(
        self,
        *,
        fail_prompts_containing: str | None = None,
        refuse_prompts_containing: str | None = None,
    ) -> None:
        self.usage = UsageAccumulator()
        self.calls: list[tuple[str, str]] = []  # (schema name, prompt)
        self._fail_marker = fail_prompts_containing
        self._refuse_marker = refuse_prompts_containing

    async def classify_structured(self, *, system: str, prompt: str, schema: type) -> Any:
        del system
        self.calls.append((schema.__name__, prompt))
        if self._fail_marker is not None and self._fail_marker in prompt:
            msg = "scripted transport failure"
            raise ProviderUnavailable(msg)
        if self._refuse_marker is not None and self._refuse_marker in prompt:
            msg = "scripted refusal"
            raise RefusalError(msg)
        if schema is SessionClassification:
            return SessionClassification(
                work_category="sde",
                goal="Fix the flaky auth test.",
                confidence=0.9,
            )
        if schema is ConflictsResult:
            return ConflictsResult(
                conflicts=[
                    ConflictPair(
                        turn_a_uuid="u-01",
                        turn_b_uuid="u-04",
                        conflict_kind="correction",
                        severity="low",
                        agent_position="Tests are green.",
                        user_position="They are still failing.",
                        confidence=0.8,
                    ),
                    # Invalid pair — must be dropped by the edges-uuid guard.
                    ConflictPair(
                        turn_a_uuid="not-a-real-uuid",
                        turn_b_uuid="u-04",
                        conflict_kind="disagreement",
                        severity="low",
                        agent_position="A.",
                        user_position="B.",
                        confidence=0.5,
                    ),
                ]
            )
        if schema is UserFrictionSignal:
            return UserFrictionSignal(
                label="none", rationale="ordinary instruction", confidence=0.9
            )
        if schema is PerceivedErrorsResult:
            return PerceivedErrorsResult(
                errors=[
                    PerceivedError(
                        turn_uuid="u-04",
                        signal="correction",
                        severity="minor",
                        evidence="why is it failing?",
                        agent_error_summary="The agent claimed success while tests failed.",
                        confidence=0.8,
                    ),
                    # Hallucinated uuid — must be dropped by the edges guard.
                    PerceivedError(
                        turn_uuid="not-a-real-uuid",
                        signal="unresolved_outcome",
                        severity="major",
                        evidence="fabricated",
                        agent_error_summary="Fabricated row.",
                        confidence=0.6,
                    ),
                ]
            )
        msg = f"unexpected schema {schema!r}"
        raise AssertionError(msg)
