# SPDX-License-Identifier: Apache-2.0

"""Images by reference, typed tool outcomes, and subagent linkage, end to end.

Every test here runs the real use case (``convert_and_audit`` /
``convert_codex_and_audit``) over a fixture on disk, because the three
features are only right when the pre-pass, the port, the enrichment pass and
the result-signals pass agree about the same records.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from codex_fixtures import codex_rollout_records, write_codex_rollout
from harbor_oracle import (
    diff_paths,
    harbor_claude_code_trajectory,
    parity_diffs,
    require_harbor_private_api,
)
from subagent_fixtures import PASTED_PNG, READ_PNG

from atif_converter.application.convert_and_audit import convert_and_audit
from atif_converter.application.convert_codex import convert_codex_and_audit
from atif_converter.domain.blobs import BlobCollector, extract_claude_code_blobs
from atif_converter.domain.claude_code_conversion import convert_claude_code_records
from atif_converter.domain.errors import SourceMutatedDuringConversion
from atif_converter.infrastructure import raw_records
from atif_converter.infrastructure.claude_code_converter import convert_claude_code_session


def _steps(trajectory: dict[str, Any]) -> list[dict[str, Any]]:
    return trajectory["steps"]


def _result(trajectory: dict[str, Any], call_id: str) -> dict[str, Any]:
    for step in _steps(trajectory):
        for result in (step.get("observation") or {}).get("results") or []:
            if result.get("source_call_id") == call_id:
                return result
    raise AssertionError(call_id)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestSubagentAgentId:
    def test_every_sidechain_step_names_its_own_subagent(
        self, parallel_subagent_session: Path
    ) -> None:
        """Two subagents interleaved in time: each step carries the RIGHT id."""
        result, _ = convert_and_audit(parallel_subagent_session)
        sidechain = [s for s in _steps(result.trajectory) if s["extra"]["is_sidechain"]]
        # Per subagent: its prompt, its tool turn (result attached), its answer.
        assert len(sidechain) == 6
        by_text = {s["message"]: s["extra"].get("agent_id") for s in sidechain if s["message"]}
        assert by_text["look at alpha"] == "aaa"
        assert by_text["alpha answer"] == "aaa"
        assert by_text["look at beta"] == "bbb"
        assert by_text["beta answer"] == "bbb"
        assert all(s["extra"].get("agent_id") in {"aaa", "bbb"} for s in sidechain)
        main = [s for s in _steps(result.trajectory) if not s["extra"]["is_sidechain"]]
        assert not any("agent_id" in s["extra"] for s in main)

    def test_the_port_reads_the_camel_case_key(self) -> None:
        """The harbor port itself, with no enrichment: ``agentId`` lands on the step."""
        records = [
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "2026-09-27T00:00:01Z",
                "isSidechain": True,
                "agentId": "ag-7",
                "message": {"id": "m1", "role": "assistant", "content": "hi"},
            }
        ]
        trajectory = convert_claude_code_records(records)
        assert trajectory is not None
        extra = trajectory.steps[0].extra
        assert extra is not None
        assert extra["agent_id"] == "ag-7"

    def test_harbor_differs_only_by_the_named_divergence(
        self, parallel_subagent_session: Path
    ) -> None:
        """The oracle still holds everywhere else, and the divergence is real."""
        require_harbor_private_api("claude-code")
        theirs = harbor_claude_code_trajectory(parallel_subagent_session)
        ours = convert_claude_code_session(parallel_subagent_session)
        assert theirs is not None
        assert ours is not None
        raw = diff_paths(theirs, ours.to_json_dict())
        assert raw, "the fixture must exercise the divergence the filter names"
        assert all(".extra.agent_id: only in ours" in line for line in raw)
        assert parity_diffs(theirs, ours.to_json_dict()) == []


class TestSubagentLinks:
    def test_sidecar_and_result_links(self, parallel_subagent_session: Path) -> None:
        result, _ = convert_and_audit(parallel_subagent_session)
        entries = {e["agent_id"]: e for e in result.trajectory["extra"]["subagents"]}
        assert entries["aaa"] == {
            "agent_id": "aaa",
            "agent_type": "Explore",
            "description": "Alpha scout",
            "parent_tool_call_id": "toolu_A",
            "spawn_depth": 1,
            "link_source": "meta",
        }
        # bbb's sidecar has no toolUseId: the spawning call's result links it,
        # and supplies the description the sidecar lacks.
        assert entries["bbb"] == {
            "agent_id": "bbb",
            "agent_type": "Explore",
            "spawn_depth": 1,
            "parent_tool_call_id": "toolu_B",
            "link_source": "tool_result",
            "description": "Beta scout",
        }

    def test_sidecars_come_from_the_single_read(
        self, parallel_subagent_session: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each sidecar is read once by the load and once by the re-check, like a transcript."""
        opened: list[Path] = []
        real_open = Path.open

        def counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            opened.append(self)
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", counting_open)
        convert_and_audit(parallel_subagent_session)
        sidecars = raw_records.discover_sidecar_files(parallel_subagent_session)
        assert len(sidecars) == 2
        assert {path: opened.count(path) for path in sidecars} == dict.fromkeys(sidecars, 2)

    def test_a_sidecar_rewritten_mid_conversion_refuses_the_session(
        self, parallel_subagent_session: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sidecar = raw_records.discover_sidecar_files(parallel_subagent_session)[0]
        real_load = raw_records.load_session

        def load_then_touch(session: Path) -> raw_records.LoadedSession:
            loaded = real_load(session)
            sidecar.write_text(json.dumps({"agentType": "rewritten"}), encoding="utf-8")
            return loaded

        monkeypatch.setattr(
            "atif_converter.infrastructure.harbor_adapter.load_session", load_then_touch
        )
        with pytest.raises(SourceMutatedDuringConversion):
            convert_and_audit(parallel_subagent_session)

    def test_sidecars_stay_out_of_the_record_census(self, parallel_subagent_session: Path) -> None:
        loaded = raw_records.load_session(parallel_subagent_session)
        assert all(not path.name.endswith(".meta.json") for path in loaded.records_by_file)
        assert sorted(p.name for p in loaded.sidecars) == [
            "agent-aaa.meta.json",
            "agent-bbb.meta.json",
        ]
        assert loaded.record_pairs() == raw_records.read_snapshot_records(loaded.snapshot)


class TestImagesByReference:
    def test_no_base64_survives_anywhere_in_the_trajectory(
        self, parallel_subagent_session: Path
    ) -> None:
        result, _ = convert_and_audit(parallel_subagent_session)
        text = json.dumps(result.trajectory)
        for data in (READ_PNG, PASTED_PNG):
            assert base64.b64encode(data).decode() not in text
        assert f"[image sha256:{_sha(READ_PNG)} image/png {len(READ_PNG)} bytes]" in text
        assert result.validation_errors == ()

    def test_blobs_are_deduplicated_by_content(self, parallel_subagent_session: Path) -> None:
        """The Read result's block and its toolUseResult copy are one blob."""
        result, _ = convert_and_audit(parallel_subagent_session)
        assert sorted(blob.ref.sha256 for blob in result.blobs) == sorted(
            [_sha(READ_PNG), _sha(PASTED_PNG)]
        )
        by_hash = {blob.ref.sha256: blob for blob in result.blobs}
        assert by_hash[_sha(READ_PNG)].data == READ_PNG

    def test_tool_result_images_are_typed(self, parallel_subagent_session: Path) -> None:
        result, _ = convert_and_audit(parallel_subagent_session)
        images = _result(result.trajectory, "toolu_RA")["extra"]["images"]
        assert images == [
            {
                "sha256": _sha(READ_PNG),
                "media_type": "image/png",
                "bytes": len(READ_PNG),
                "width": 3,
                "height": 2,
                "extension": "png",
            }
        ]

    def test_pasted_user_images_are_typed_on_the_user_step(
        self, parallel_subagent_session: Path
    ) -> None:
        result, _ = convert_and_audit(parallel_subagent_session)
        pasted = [
            s for s in _steps(result.trajectory) if s["source"] == "user" and "images" in s["extra"]
        ]
        assert len(pasted) == 1
        step = pasted[0]
        assert step["message"].startswith("see this screenshot\n\n[image sha256:")
        assert [(i["sha256"], i["width"], i["height"]) for i in step["extra"]["images"]] == [
            (_sha(PASTED_PNG), 5, 4)
        ]

    def test_malformed_base64_is_left_in_place(self) -> None:
        block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "not base64!"},
        }
        record: dict[str, Any] = {
            "type": "user",
            "uuid": "u",
            "message": {"role": "user", "content": [block]},
        }
        collector = BlobCollector()
        extract_claude_code_blobs([record], collector)
        assert record["message"]["content"] == [block]
        assert collector.blobs == ()

    def test_read_tool_use_result_copy_is_replaced(self) -> None:
        data = base64.b64encode(READ_PNG).decode()
        record: dict[str, Any] = {
            "type": "user",
            "uuid": "u",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t", "content": []}],
            },
            "toolUseResult": {"type": "image", "file": {"base64": data, "type": "image/png"}},
        }
        index = extract_claude_code_blobs([record], BlobCollector())
        file_value = record["toolUseResult"]["file"]
        assert "base64" not in file_value
        assert (
            file_value["blob"] == f"[image sha256:{_sha(READ_PNG)} image/png {len(READ_PNG)} bytes]"
        )
        assert [ref.sha256 for ref in index.by_tool_call_id["t"]] == [_sha(READ_PNG)]


class TestToolOutcomes:
    def test_failed_bash_carries_is_error_and_exit_code(
        self, parallel_subagent_session: Path
    ) -> None:
        result, _ = convert_and_audit(parallel_subagent_session)
        extra = _result(result.trajectory, "toolu_RB")["extra"]
        assert extra["is_error"] is True
        assert extra["exit_code"] == 2

    def test_clean_bash_is_not_an_error_and_states_no_exit_code(
        self, parallel_subagent_session: Path
    ) -> None:
        result, _ = convert_and_audit(parallel_subagent_session)
        extra = _result(result.trajectory, "toolu_C")["extra"]
        assert extra["is_error"] is False
        assert extra["interrupted"] is False
        assert "exit_code" not in extra

    def test_a_result_without_the_flag_reads_as_not_an_error(
        self, parallel_subagent_session: Path
    ) -> None:
        """The Messages API defaults ``is_error`` to false; the Agent results omit it."""
        result, _ = convert_and_audit(parallel_subagent_session)
        assert _result(result.trajectory, "toolu_A")["extra"]["is_error"] is False

    def test_exit_code_text_on_a_non_bash_tool_is_not_an_exit_code(self, tmp_path: Path) -> None:
        records = [
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "2026-09-27T00:00:01Z",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "name": "Grep", "input": {}}],
                },
            },
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-09-27T00:00:02Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "Exit code 1",
                            "is_error": True,
                        }
                    ],
                },
            },
        ]
        session = tmp_path / "p" / "s.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        result, _ = convert_and_audit(session)
        extra = _result(result.trajectory, "t1")["extra"]
        assert extra["is_error"] is True
        assert "exit_code" not in extra


def _codex_records_with_images_and_failures() -> list[dict[str, Any]]:
    records = codex_rollout_records()
    png = base64.b64encode(READ_PNG).decode()
    pasted = base64.b64encode(PASTED_PNG).decode()
    records[5]["payload"]["content"].append(
        {"type": "input_image", "image_url": f"data:image/png;base64,{pasted}"}
    )
    extra: list[dict[str, Any]] = [
        {
            "timestamp": "2026-09-11T17:27:02.310Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call_view",
                "name": "view_image",
                "arguments": '{"path":"/w/shot.png"}',
            },
        },
        {
            "timestamp": "2026-09-11T17:27:02.320Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_view",
                "output": [{"type": "input_image", "image_url": f"data:image/png;base64,{png}"}],
            },
        },
        {
            "timestamp": "2026-09-11T17:27:02.330Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call_fail",
                "name": "exec_command",
                "arguments": '{"cmd":"false"}',
            },
        },
        {
            "timestamp": "2026-09-11T17:27:02.340Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_fail",
                "output": "Chunk ID: 1\nWall time: 0.0 seconds\nProcess exited with code 1\nOutput:\n",
            },
        },
        {
            "timestamp": "2026-09-11T17:27:02.350Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "call_fail",
                    "status": "failed",
                    "exit_code": 1,
                },
            },
        },
    ]
    return records[:10] + extra + records[10:]


class TestCodex:
    def test_legacy_metadata_exit_code(self, codex_rollout: Path) -> None:
        result, _ = convert_codex_and_audit(codex_rollout)
        extra = _result(result.trajectory, "call_1")["extra"]
        assert extra == {"is_error": False, "exit_code": 0}

    def test_images_and_command_failures(self, tmp_path: Path) -> None:
        rollout = write_codex_rollout(
            tmp_path / "sessions", _codex_records_with_images_and_failures()
        )
        result, _ = convert_codex_and_audit(rollout)
        text = json.dumps(result.trajectory)
        for data in (READ_PNG, PASTED_PNG):
            assert base64.b64encode(data).decode() not in text
        assert sorted(blob.ref.sha256 for blob in result.blobs) == sorted(
            [_sha(READ_PNG), _sha(PASTED_PNG)]
        )
        view = _result(result.trajectory, "call_view")["extra"]
        assert [(i["sha256"], i["width"], i["height"]) for i in view["images"]] == [
            (_sha(READ_PNG), 3, 2)
        ]
        failed = _result(result.trajectory, "call_fail")["extra"]
        assert failed == {"is_error": True, "exit_code": 1}
        user_images = [
            s["extra"]["images"]
            for s in _steps(result.trajectory)
            if s["source"] == "user" and "images" in (s.get("extra") or {})
        ]
        assert [[i["sha256"] for i in images] for images in user_images] == [[_sha(PASTED_PNG)]]
        assert result.validation_errors == ()
