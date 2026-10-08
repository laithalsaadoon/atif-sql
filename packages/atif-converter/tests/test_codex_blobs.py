# SPDX-License-Identifier: Apache-2.0

"""Every Codex tool output image the converter names by placeholder is a stored blob.

The converter's harbor 0.24.0 port turns an inline image in a tool output list
into ``[image sha256:<hex> <media> <n> bytes]``, and the pre-pass
(:func:`~atif_converter.domain.blobs.extract_codex_blobs`) is what hands those
bytes to the corpus blob store. Both read the image by
:func:`~atif_converter.domain.blobs.codex_tool_image_ref`, so these tests hold
the two to one rule: whatever placeholder the port would write, the collector
holds the bytes it names, the index maps them to the output's ``call_id``, and
whatever the port would call ``[image omitted]`` stays in the record untouched.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from codex_fixtures import codex_rollout_records, write_codex_rollout
from subagent_fixtures import PASTED_PNG, READ_PNG

from atif_converter.application.convert_codex import convert_codex_and_audit
from atif_converter.domain.blobs import BlobCollector, BlobIndex, extract_codex_blobs
from atif_converter.domain.codex_conversion import convert_codex_records

_TS = "2026-09-11T17:27:02.{:03d}Z"
_PLACEHOLDER = re.compile(r"\[image sha256:(?P<sha>[0-9a-f]{64}) (?P<media>\S+) (?P<n>\d+) bytes\]")

_READ_B64 = base64.b64encode(READ_PNG).decode()
_PASTED_B64 = base64.b64encode(PASTED_PNG).decode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _placeholder(data: bytes, media_type: str = "image/png") -> str:
    return f"[image sha256:{_sha(data)} {media_type} {len(data)} bytes]"


def _item(payload: dict[str, Any], n: int) -> dict[str, Any]:
    return {"timestamp": _TS.format(n), "type": "response_item", "payload": payload}


def _output(kind: str, call_id: str, output: Any) -> dict[str, Any]:
    return _item({"type": kind, "call_id": call_id, "output": output}, 2)


def _mcp(data: str, media_type: str | None = "image/png", key: str = "mimeType") -> dict[str, Any]:
    block: dict[str, Any] = {"type": "image", "data": data}
    if media_type is not None:
        block[key] = media_type
    return block


def _extract(records: list[dict[str, Any]]) -> tuple[BlobCollector, BlobIndex]:
    collector = BlobCollector()
    return collector, extract_codex_blobs(records, collector)


class TestMcpImageInAToolOutput:
    @pytest.mark.parametrize("kind", ["function_call_output", "custom_tool_call_output"])
    def test_is_lifted_stored_and_indexed_by_call_id(self, kind: str) -> None:
        records = [_output(kind, "c1", [{"type": "text", "text": "shot:"}, _mcp(_READ_B64)])]
        collector, index = _extract(records)
        output = records[0]["payload"]["output"]
        assert output == [
            {"type": "text", "text": "shot:"},
            {"type": "input_text", "text": _placeholder(READ_PNG)},
        ]
        assert [(blob.ref.sha256, blob.data) for blob in collector.blobs] == [
            (_sha(READ_PNG), READ_PNG)
        ]
        ((ref,),) = index.by_tool_call_id.values()
        assert list(index.by_tool_call_id) == ["c1"]
        assert (ref.sha256, ref.media_type, ref.width, ref.height) == (
            _sha(READ_PNG),
            "image/png",
            3,
            2,
        )
        assert index.by_record_key == {}

    @pytest.mark.parametrize(
        ("block", "media_type"),
        [
            (_mcp(_READ_B64, "image/png", key="mime_type"), "image/png"),
            (_mcp(_READ_B64, " IMAGE/JPG "), "image/jpeg"),
            (_mcp(f"data:image/webp;base64,{_READ_B64}", None), "image/webp"),
            (_mcp(f"data:image/gif;name=x.gif;base64,{_READ_B64}", "image/bmp"), "image/gif"),
            (_mcp(_READ_B64[:20] + "\n" + _READ_B64[20:]), "image/png"),
        ],
        ids=["mime_type-key", "jpg-alias", "data-url", "data-url-params-win", "wrapped-base64"],
    )
    def test_reads_the_media_type_and_payload_the_way_the_port_does(
        self, block: dict[str, Any], media_type: str
    ) -> None:
        records = [_output("function_call_output", "c1", [block])]
        collector, index = _extract(records)
        assert records[0]["payload"]["output"] == [
            {"type": "input_text", "text": _placeholder(READ_PNG, media_type)}
        ]
        assert [blob.ref.media_type for blob in collector.blobs] == [media_type]
        assert [ref.sha256 for ref in index.by_tool_call_id["c1"]] == [_sha(READ_PNG)]

    @pytest.mark.parametrize(
        "block",
        [
            _mcp(_READ_B64, "image/bmp"),
            _mcp(_READ_B64, "text/plain"),
            _mcp("not base64!"),
            _mcp(""),
            _mcp(_READ_B64, None),
            _mcp("https://example.com/shot.png"),
            {"type": "image", "data": 7, "mimeType": "image/png"},
            {"type": "image", "mimeType": "image/png"},
        ],
        ids=[
            "unsupported-bmp",
            "not-an-image",
            "bad-base64",
            "empty",
            "no-media-type",
            "remote-url",
            "data-not-a-string",
            "no-data",
        ],
    )
    def test_an_image_the_port_omits_stays_unlifted(self, block: dict[str, Any]) -> None:
        records = [_output("function_call_output", "c1", [{"type": "text", "text": "a"}, block])]
        before = copy.deepcopy(records)
        collector, index = _extract(records)
        assert records == before
        assert collector.blobs == ()
        assert collector.references == 0
        assert index.by_tool_call_id == {}

    def test_an_image_block_in_message_content_stays(self) -> None:
        """Message content is not a tool output: the port reads only its ``text`` fields."""
        records = [
            _item(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "q"}, _mcp(_READ_B64)],
                },
                1,
            )
        ]
        before = copy.deepcopy(records)
        collector, index = _extract(records)
        assert records == before
        assert collector.blobs == ()
        assert index.by_record_key == {}


class TestInputImageInAToolOutput:
    @pytest.mark.parametrize(
        "url",
        [
            f"data:image/png;charset=binary;base64,{_READ_B64}",
            f"data:image/png;base64,{_READ_B64[:20]}\n{_READ_B64[20:]}",
        ],
        ids=["data-url-params", "wrapped-base64"],
    )
    def test_the_ports_wider_data_urls_are_lifted_too(self, url: str) -> None:
        records = [
            _output("function_call_output", "c1", [{"type": "input_image", "image_url": url}])
        ]
        collector, index = _extract(records)
        assert records[0]["payload"]["output"] == [
            {"type": "input_text", "text": _placeholder(READ_PNG)}
        ]
        assert [blob.data for blob in collector.blobs] == [READ_PNG]
        assert [ref.sha256 for ref in index.by_tool_call_id["c1"]] == [_sha(READ_PNG)]

    def test_in_message_content_only_the_strict_data_url_is_lifted(self) -> None:
        url = f"data:image/png;charset=binary;base64,{_READ_B64}"
        records = [
            _item(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": url}],
                },
                1,
            )
        ]
        before = copy.deepcopy(records)
        collector, _ = _extract(records)
        assert records == before
        assert collector.blobs == ()


def _tool_call_records(output: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = codex_rollout_records()
    extra = [
        _item(
            {
                "type": "function_call",
                "call_id": "call_mcp",
                "name": "mcp__browser__screenshot",
                "arguments": "{}",
            },
            310,
        ),
        _item({"type": "function_call_output", "call_id": "call_mcp", "output": output}, 320),
    ]
    return records[:10] + extra + records[10:]


def _result(trajectory: dict[str, Any], call_id: str) -> dict[str, Any]:
    for step in trajectory["steps"]:
        for result in (step.get("observation") or {}).get("results") or []:
            if result.get("source_call_id") == call_id:
                return result
    raise AssertionError(call_id)


_MIXED_OUTPUT: list[dict[str, Any]] = [
    {"type": "text", "text": "two shots:"},
    _mcp(_READ_B64),
    _mcp(_PASTED_B64, "image/jpg"),
    _mcp(_READ_B64, "image/bmp"),
]


class TestEndToEnd:
    def test_every_placeholder_names_a_stored_blob(self, tmp_path: Path) -> None:
        """The production use case: each placeholder's sha256 is a blob the result carries."""
        rollout = write_codex_rollout(tmp_path / "sessions", _tool_call_records(_MIXED_OUTPUT))
        result, _ = convert_codex_and_audit(rollout)
        content = _result(result.trajectory, "call_mcp")["content"]
        assert content == "\n".join(
            [
                "two shots:",
                _placeholder(READ_PNG),
                _placeholder(PASTED_PNG, "image/jpeg"),
                "[image omitted]",
            ]
        )
        stored = {blob.ref.sha256: blob for blob in result.blobs}
        named = [match.group("sha") for match in _PLACEHOLDER.finditer(content)]
        assert named == [_sha(READ_PNG), _sha(PASTED_PNG)]
        for sha in named:
            assert hashlib.sha256(stored[sha].data).hexdigest() == sha
        assert stored[_sha(PASTED_PNG)].data == PASTED_PNG
        images = _result(result.trajectory, "call_mcp")["extra"]["images"]
        assert [(i["sha256"], i["media_type"], i["width"], i["height"]) for i in images] == [
            (_sha(READ_PNG), "image/png", 3, 2),
            (_sha(PASTED_PNG), "image/jpeg", 5, 4),
        ]
        text = json.dumps(result.trajectory)
        assert _PASTED_B64 not in text
        assert result.validation_errors == ()

    def test_the_pre_pass_changes_no_converted_byte(self) -> None:
        """Raw and pre-passed records convert to the same trajectory: placeholders are identical."""
        records = _tool_call_records(copy.deepcopy(_MIXED_OUTPUT))
        raw = convert_codex_records(copy.deepcopy(records), fallback_session_id="s")
        lifted = copy.deepcopy(records)
        _, index = _extract(lifted)
        assert list(index.by_tool_call_id) == ["call_mcp"]
        pre_passed = convert_codex_records(lifted, fallback_session_id="s")
        assert raw is not None
        assert pre_passed is not None
        assert raw.to_json_dict() == pre_passed.to_json_dict()

    def test_a_list_the_port_dumps_as_json_keeps_no_base64(self, tmp_path: Path) -> None:
        """One unknown block makes the port dump the list: the dump holds the placeholder item."""
        output = [_mcp(_PASTED_B64), {"type": "mystery"}]
        rollout = write_codex_rollout(tmp_path / "sessions", _tool_call_records(output))
        result, _ = convert_codex_and_audit(rollout)
        content = _result(result.trajectory, "call_mcp")["content"]
        assert json.loads(content) == [
            {"type": "input_text", "text": _placeholder(PASTED_PNG)},
            {"type": "mystery"},
        ]
        assert [blob.data for blob in result.blobs] == [PASTED_PNG]
