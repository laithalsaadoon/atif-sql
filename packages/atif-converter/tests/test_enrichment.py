# SPDX-License-Identifier: Apache-2.0

"""Enrichment pass: uuid restoration, compact-summary flag, cache split.

The gap-7 canary lives here now in its repaired form: harbor ALONE still
drops uuids (pinned in test_convert_and_audit), and this pass restores them.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from atif_converter.domain.enrichment import enrich_trajectory
from atif_converter.infrastructure.harbor_adapter import convert_session
from atif_converter.infrastructure.raw_records import (
    read_snapshot_records,
    take_session_snapshot,
)

#: Typed stand-in for an absent ``extra`` mapping. A bare ``{}`` literal infers
#: as ``dict[Unknown, Unknown]``, which leaks Unknown into every ``.get``
#: chained onto the ``or`` result.
_NO_EXTRA: Mapping[str, Any] = {}


@pytest.fixture
def enriched(synthetic_session: Path) -> dict[str, Any]:
    result = convert_session(synthetic_session)
    records = [
        record for record, _src in read_snapshot_records(take_session_snapshot(synthetic_session))
    ]
    return enrich_trajectory(result.trajectory, records)


def _write_session(tmp_path: Path, events: list[dict[str, Any]]) -> Path:
    project_dir = tmp_path / "projects" / "-tmp-orphan"
    project_dir.mkdir(parents=True, exist_ok=True)
    main = project_dir / "bbbbbbbb-1111-2222-3333-444444444444.jsonl"
    main.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return main


def _assistant(
    uuid: str,
    parent: str,
    ts: str,
    msg_id: str,
    content: list[dict[str, Any]],
    *,
    compact: bool = False,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "uuid": uuid,
        "parentUuid": parent,
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-x",
            "content": content,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }
    if compact:
        event["isCompactSummary"] = True
    return event


def orphan_repro_events() -> list[dict[str, Any]]:
    """The critic's repro shape (/tmp/atif-critic/proj2/bbbbbbbb-...jsonl):

    an ORPHAN tool_result (replayed after compaction, no pending call)
    followed by a compact-summary assistant turn and a normal
    tool_use/tool_result/text tail. Pre-fix, the positional zip desynced
    from the orphan step onward, cascading wrong source_uuids and the
    compact flag onto the wrong step.
    """
    return [
        {
            "uuid": "u1",
            "parentUuid": None,
            "type": "user",
            "timestamp": "2026-08-20T10:00:00Z",
            "message": {"role": "user", "content": "hello"},
        },
        {
            "uuid": "u2",
            "parentUuid": "u1",
            "type": "user",
            "timestamp": "2026-08-20T10:00:01Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_orphan",
                        "name": "Bash",
                        "content": "old",
                    }
                ],
            },
        },
        _assistant(
            "u3",
            "u2",
            "2026-08-20T10:00:02Z",
            "m1",
            [{"type": "text", "text": "summary of prior work"}],
            compact=True,
        ),
        _assistant(
            "u4",
            "u3",
            "2026-08-20T10:00:03Z",
            "m2",
            [{"type": "tool_use", "id": "tu_real", "name": "Bash", "input": {"command": "ls"}}],
        ),
        {
            "uuid": "u5",
            "parentUuid": "u4",
            "type": "user",
            "timestamp": "2026-08-20T10:00:04Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_real", "content": "file.txt"}
                ],
            },
        },
        _assistant(
            "u6", "u5", "2026-08-20T10:00:05Z", "m3", [{"type": "text", "text": "all done"}]
        ),
    ]


@pytest.fixture
def orphan_session(tmp_path: Path) -> Path:
    """The critic's orphan repro session on disk."""
    return _write_session(tmp_path, orphan_repro_events())


@pytest.fixture
def orphan_compact_session(tmp_path: Path) -> Path:
    """Orphan + compact-summary combination: the ORPHAN tool_result record
    itself carries ``isCompactSummary`` (the replayed-after-compaction
    shape), so the compact flag must land on the orphan step — not on
    whatever step the pre-fix desync happened to shift it onto."""
    events = orphan_repro_events()
    events[1]["isCompactSummary"] = True  # the orphan tool_result record
    del events[2]["isCompactSummary"]  # keep exactly one compact source
    return _write_session(tmp_path, events)


def _enrich_file(session: Path) -> dict[str, Any]:
    result = convert_session(session)
    records = [record for record, _src in read_snapshot_records(take_session_snapshot(session))]
    return enrich_trajectory(result.trajectory, records)


def _steps_by_uuid(trajectory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for step in trajectory["steps"]:
        for uuid in (step.get("extra") or _NO_EXTRA).get("source_uuids", []):
            index[uuid] = step
    return index


class TestSourceUuids:
    def test_gap7_repaired_all_contributing_uuids_restored(self, enriched: dict[str, Any]) -> None:
        """The repaired half of the gap-7 canary: enrichment restores the
        step-to-raw-record identity harbor drops."""
        restored = set(_steps_by_uuid(enriched))
        # every convertible record: 3 user text + 2 assistant + 1 tool_result
        # + subagent pair (su/sa) — attachment & queue-operation never convert
        assert restored == {"u-1", "a-1", "u-2", "a-2", "su-1", "sa-1", "wu-1"}

    def test_assistant_uuid_on_its_agent_step(self, enriched: dict[str, Any]) -> None:
        index = _steps_by_uuid(enriched)
        step = index["a-1"]
        assert step["source"] == "agent"
        assert step["tool_calls"][0]["tool_call_id"] == "toolu_01"

    def test_tool_result_uuid_joins_owning_agent_step(self, enriched: dict[str, Any]) -> None:
        index = _steps_by_uuid(enriched)
        assert index["u-2"] is index["a-1"]  # result attaches to its call's turn

    def test_user_text_uuids_walk_in_ts_order(self, enriched: dict[str, Any]) -> None:
        index = _steps_by_uuid(enriched)
        assert index["u-1"]["source"] == "user"
        assert "hello please run a tool" in index["u-1"]["message"]
        assert "workflow subagent prompt" in index["wu-1"]["message"]
        assert "subagent prompt" in index["su-1"]["message"]

    def test_input_trajectory_untouched(self, synthetic_session: Path) -> None:
        result = convert_session(synthetic_session)
        records = [r for r, _ in read_snapshot_records(take_session_snapshot(synthetic_session))]
        before = copy.deepcopy(result.trajectory)
        enrich_trajectory(result.trajectory, records)
        assert result.trajectory == before


class TestOrphanToolResults:
    """The critic's BLOCKER: orphan tool_result steps must not desync the
    positional zip (they have no assistant group behind them)."""

    def test_orphan_repro_every_step_correctly_attributed(self, orphan_session: Path) -> None:
        enriched = _enrich_file(orphan_session)
        by_step = {
            step["step_id"]: (step.get("extra") or _NO_EXTRA).get("source_uuids")
            for step in enriched["steps"]
        }
        # step 1: the user text; step 2: the ORPHAN tool_result (u2 via
        # tool_use_id); step 3: the compact assistant turn (u3); step 4:
        # the real tool turn (u4 + its result u5); step 5: the final text.
        assert by_step == {
            1: ["u1"],
            2: ["u2"],
            3: ["u3"],
            4: ["u4", "u5"],
            5: ["u6"],
        }
        assert "enrichment_unattributed_steps" not in (enriched.get("extra") or _NO_EXTRA)
        assert "enrichment_leftover_groups" not in (enriched.get("extra") or _NO_EXTRA)

    def test_orphan_repro_compact_flag_lands_on_summary_step(self, orphan_session: Path) -> None:
        enriched = _enrich_file(orphan_session)
        flags = {
            step["step_id"]: (step.get("extra") or _NO_EXTRA).get("is_compact_summary", False)
            for step in enriched["steps"]
        }
        assert flags == {1: False, 2: False, 3: True, 4: False, 5: False}

    def test_orphan_plus_compact_summary_combination(self, orphan_compact_session: Path) -> None:
        enriched = _enrich_file(orphan_compact_session)
        by_step = {
            step["step_id"]: (
                (step.get("extra") or _NO_EXTRA).get("source_uuids"),
                (step.get("extra") or _NO_EXTRA).get("is_compact_summary", False),
            )
            for step in enriched["steps"]
        }
        # the compact flag rides the ORPHAN record here, so it must land on
        # the orphan step (2) and nowhere else.
        assert by_step == {
            1: (["u1"], False),
            2: (["u2"], True),
            3: (["u3"], False),
            4: (["u4", "u5"], False),
            5: (["u6"], False),
        }

    def test_enriched_orphan_trajectory_still_validates(self, orphan_session: Path) -> None:
        from harbor.utils.trajectory_validator import (  # type: ignore[import-untyped]
            TrajectoryValidator,
        )

        payload = json.loads(json.dumps(_enrich_file(orphan_session)))
        validator = TrajectoryValidator()
        assert validator.validate(payload), validator.errors


class TestDesyncAccounting:
    """Leftovers on either side of the zips must be counted and surfaced."""

    def test_missing_assistant_record_surfaces_unattributed_step(
        self, orphan_session: Path
    ) -> None:
        result = convert_session(orphan_session)
        records = [
            r
            for r, _ in read_snapshot_records(take_session_snapshot(orphan_session))
            if r.get("uuid") != "u6"
        ]
        enriched = enrich_trajectory(result.trajectory, records)
        # step 5 (u6's turn) has no group left to claim it -> unattributed.
        assert enriched["extra"]["enrichment_unattributed_steps"] == 1
        last = enriched["steps"][-1]
        assert "source_uuids" not in (last.get("extra") or _NO_EXTRA)

    def test_extra_assistant_group_surfaces_leftover(self, orphan_session: Path) -> None:
        result = convert_session(orphan_session)
        records = [r for r, _ in read_snapshot_records(take_session_snapshot(orphan_session))]
        records.append(
            _assistant(
                "ghost-1",
                "u6",
                "2026-08-20T10:00:06Z",
                "m_ghost",
                [{"type": "text", "text": "never converted"}],
            )
        )
        enriched = enrich_trajectory(result.trajectory, records)
        assert enriched["extra"]["enrichment_leftover_groups"] == 1

    def test_extra_user_text_record_counts_as_leftover_too(self, orphan_session: Path) -> None:
        """FIX 4: the user-step strict=False zip is covered by the same
        leftover accounting."""
        result = convert_session(orphan_session)
        records = [r for r, _ in read_snapshot_records(take_session_snapshot(orphan_session))]
        records.append(
            {
                "uuid": "ghost-u",
                "parentUuid": "u6",
                "type": "user",
                "timestamp": "2026-08-20T10:00:07Z",
                "message": {"role": "user", "content": "never converted"},
            }
        )
        enriched = enrich_trajectory(result.trajectory, records)
        assert enriched["extra"]["enrichment_leftover_groups"] == 1

    def test_clean_session_surfaces_nothing(self, enriched: dict[str, Any]) -> None:
        extra = enriched.get("extra") or _NO_EXTRA
        assert "enrichment_unattributed_steps" not in extra
        assert "enrichment_leftover_groups" not in extra
        assert "enrichment_truncated_at_step" not in extra


class TestRefuseAndLog:
    """The guard-first alignment: a mismatch STOPS attribution from that
    step onward and records the truncation marker — later steps get NO
    source_uuids rather than wrong ones."""

    def test_agent_mismatch_truncates_later_steps_and_sets_marker(
        self, orphan_session: Path
    ) -> None:
        result = convert_session(orphan_session)
        records = [r for r, _ in read_snapshot_records(take_session_snapshot(orphan_session))]
        # Sabotage the m2 group's tool_use id so its cross-check MUST fail:
        # the step still owns tu_real but the group now claims tu_wrong.
        for record in records:
            if record.get("uuid") == "u4":
                record["message"]["content"][0]["id"] = "tu_wrong"
        enriched = enrich_trajectory(result.trajectory, records)

        by_step = {
            step["step_id"]: (step.get("extra") or _NO_EXTRA).get("source_uuids")
            for step in enriched["steps"]
        }
        # steps 1-3 attributed normally. Step 4's tool id (tu_real) now
        # exists in NO group, so it degrades to the orphan path and keeps
        # only the tool-id-VERIFIED result join (u5) — the assistant uuid
        # u4 is refused, never guessed. The dangling tu_wrong group then
        # fails the cross-check against text-only step 5, which stops the
        # walk: step 5 gets NOTHING and the truncation marker is set.
        assert by_step[1] == ["u1"]
        assert by_step[3] == ["u3"]
        assert by_step[4] == ["u5"]
        assert "u4" not in (by_step[4] or [])
        assert by_step[5] is None
        assert enriched["extra"]["enrichment_truncated_at_step"] == 5
        assert enriched["extra"]["enrichment_unattributed_steps"] == 1

    def test_leftover_agent_steps_set_truncation_marker(self, orphan_session: Path) -> None:
        """Length mismatch (steps beyond the groups) is the same desync."""
        result = convert_session(orphan_session)
        records = [
            r
            for r, _ in read_snapshot_records(take_session_snapshot(orphan_session))
            if r.get("uuid") != "u6"
        ]
        enriched = enrich_trajectory(result.trajectory, records)
        assert enriched["extra"]["enrichment_truncated_at_step"] == 5

    def test_user_walk_mismatch_truncates_instead_of_misattributing(
        self, orphan_session: Path
    ) -> None:
        result = convert_session(orphan_session)
        records = [r for r, _ in read_snapshot_records(take_session_snapshot(orphan_session))]
        # Remove the FIRST user text record: pre-guard, the 1:1 walk would
        # shift every later user record one step early (silent lie).
        records = [r for r in records if r.get("uuid") != "u1"]
        enriched = enrich_trajectory(result.trajectory, records)
        first_user = next(s for s in enriched["steps"] if s.get("source") == "user")
        assert "source_uuids" not in (first_user.get("extra") or _NO_EXTRA)
        assert enriched["extra"]["enrichment_truncated_at_step"] == first_user["step_id"]


class TestMalformedTextBlock:
    """Finding 5: harbor stringifies a {"type": "text", "text": <non-str>}
    block and still emits a user step; _visible_user_text must agree, and
    the user walk must survive the record (verified against harbor 0.22.0:
    the step message is the json-encoded block)."""

    @pytest.fixture
    def malformed_text_session(self, tmp_path: Path) -> Path:
        sid = "cccccccc-1111-2222-3333-444444444444"
        events: list[dict[str, Any]] = [
            {
                "uuid": "u1",
                "parentUuid": None,
                "type": "user",
                "timestamp": "2026-08-22T00:00:01Z",
                "message": {"role": "user", "content": "first"},
            },
            _assistant("a1", "u1", "2026-08-22T00:00:02Z", "m1", [{"type": "text", "text": "ok"}]),
            # malformed: text block whose text is an int, not a str
            {
                "uuid": "u2",
                "parentUuid": "a1",
                "type": "user",
                "timestamp": "2026-08-22T00:00:03Z",
                "message": {"role": "user", "content": [{"type": "text", "text": 123}]},
            },
            _assistant(
                "a2", "u2", "2026-08-22T00:00:04Z", "m2", [{"type": "text", "text": "done"}]
            ),
            {
                "uuid": "u3",
                "parentUuid": "a2",
                "type": "user",
                "timestamp": "2026-08-22T00:00:05Z",
                "message": {"role": "user", "content": "third"},
            },
        ]
        project_dir = tmp_path / "projects" / "-tmp-malformed"
        project_dir.mkdir(parents=True)
        main = project_dir / f"{sid}.jsonl"
        main.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        return main

    def test_alignment_survives_non_str_text_block(self, malformed_text_session: Path) -> None:
        enriched = _enrich_file(malformed_text_session)
        by_step = {
            step["step_id"]: (step.get("extra") or _NO_EXTRA).get("source_uuids")
            for step in enriched["steps"]
        }
        # harbor emits the malformed record as step 3 (json-encoded block);
        # every user step keeps its own uuid — no shift, no truncation.
        assert by_step == {1: ["u1"], 2: ["a1"], 3: ["u2"], 4: ["a2"], 5: ["u3"]}
        extra = enriched.get("extra") or _NO_EXTRA
        assert "enrichment_truncated_at_step" not in extra
        assert "enrichment_unattributed_steps" not in extra


class TestCompactSummary:
    def test_flag_set_when_record_carries_it(self, synthetic_session: Path) -> None:
        result = convert_session(synthetic_session)
        records = [r for r, _ in read_snapshot_records(take_session_snapshot(synthetic_session))]
        for record in records:
            if record.get("uuid") == "u-1":
                record["isCompactSummary"] = True
        enriched = enrich_trajectory(result.trajectory, records)
        step = _steps_by_uuid(enriched)["u-1"]
        assert step["extra"]["is_compact_summary"] is True

    def test_flag_absent_otherwise(self, enriched: dict[str, Any]) -> None:
        for step in enriched["steps"]:
            assert "is_compact_summary" not in (step.get("extra") or _NO_EXTRA)


class TestCacheCreationTotal:
    def test_surfaced_from_final_metrics_extra(self, enriched: dict[str, Any]) -> None:
        assert enriched["extra"]["cache_creation_total"] == 50

    def test_absent_when_source_missing(self, enriched: dict[str, Any]) -> None:
        stripped = copy.deepcopy(enriched)
        stripped["final_metrics"].pop("extra", None)
        stripped.pop("extra", None)
        out = enrich_trajectory(stripped, [])
        assert "cache_creation_total" not in (out.get("extra") or _NO_EXTRA)


class TestValidation:
    def test_enriched_trajectory_still_validates(self, enriched: dict[str, Any]) -> None:
        from harbor.utils.trajectory_validator import (  # type: ignore[import-untyped]
            TrajectoryValidator,
        )

        # round-trip through JSON like the corpus writer will
        payload = json.loads(json.dumps(enriched))
        validator = TrajectoryValidator()
        assert validator.validate(payload), validator.errors
