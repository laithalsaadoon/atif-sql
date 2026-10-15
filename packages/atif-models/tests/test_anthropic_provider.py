# SPDX-License-Identifier: Apache-2.0

"""AnthropicBedrockProvider: exact body, stop_reason mapping, enum-case fold, usage, retry."""

from __future__ import annotations

import io
import json
from typing import Any, Literal
from unittest.mock import MagicMock

import anyio
import pytest
from anthropic import transform_schema
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from atif_models.domain.ports import CallUsage, ProviderUnavailable, RefusalError
from atif_models.domain.registry import resolve
from atif_models.infrastructure.anthropic_bedrock import (
    ANTHROPIC_BEDROCK_VERSION,
    AnthropicBedrockProvider,
    extract_usage,
    fold_enum_case,
)
from atif_models.infrastructure.openai_bedrock import OpenAiBedrockProvider
from atif_models.infrastructure.providers import build_structured_provider


class TinyVerdict(BaseModel):
    label: Literal["none", "status_ping"]
    rationale: str = Field(max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)


class Pair(BaseModel):
    kind: Literal["correction", "Open Question"]


class Pairs(BaseModel):
    pairs: list[Pair]


def _response(payload: dict[str, Any]) -> dict[str, Any]:
    return {"body": io.BytesIO(json.dumps(payload).encode())}


def _ok_payload(content: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {
                "type": "text",
                "text": json.dumps(
                    content or {"label": "none", "rationale": "Ordinary.", "confidence": 0.9}
                ),
            },
        ],
        "usage": {
            "input_tokens": 100,
            "cache_read_input_tokens": 60,
            "cache_creation_input_tokens": 40,
            "output_tokens": 50,
            "output_tokens_details": {"thinking_tokens": 30},
        },
    }
    payload.update(overrides)
    return payload


def _provider(
    client: MagicMock, size: Literal["small", "medium", "large"] = "medium"
) -> AnthropicBedrockProvider:
    provider = AnthropicBedrockProvider(
        resolve(size, "anthropic"), region="us-east-1", concurrency=2
    )
    provider._client = client
    return provider


def _run(provider: AnthropicBedrockProvider, schema: type[BaseModel] = TinyVerdict) -> Any:
    return anyio.run(
        lambda: provider.classify_structured(
            system="sys prompt", prompt="user prompt", schema=schema
        )
    )


class TestBodyConstruction:
    def test_exact_body(self):
        provider = AnthropicBedrockProvider(resolve("medium", "anthropic"), region="us-east-1")
        body = provider.build_body(system="sys prompt", prompt="user prompt", schema=TinyVerdict)
        assert body == {
            "anthropic_version": ANTHROPIC_BEDROCK_VERSION,
            "max_tokens": 32_000,
            "system": "sys prompt",
            "messages": [{"role": "user", "content": "user prompt"}],
            "output_config": {
                "effort": "high",
                "format": {"type": "json_schema", "schema": transform_schema(TinyVerdict)},
            },
        }

    def test_body_carries_no_parameter_claude_5_5_rejects(self):
        provider = AnthropicBedrockProvider(resolve("large", "anthropic"), region="us-east-1")
        body = provider.build_body(system="s", prompt="p", schema=TinyVerdict)
        for rejected in ("temperature", "top_p", "thinking"):
            assert rejected not in body
        assert [m["role"] for m in body["messages"]] == ["user"]  # no assistant prefill

    def test_schema_constraints_leave_the_wire_schema(self):
        """Claude 5.5 400s on minimum/maximum/maxLength; the SDK transform moves them to descriptions."""
        schema = AnthropicBedrockProvider(
            resolve("small", "anthropic"), region="us-east-1"
        ).build_body(system="s", prompt="p", schema=TinyVerdict)["output_config"]["format"][
            "schema"
        ]
        wire = json.dumps(schema)
        for keyword in ('"minimum"', '"maximum"', '"maxLength"', '"minLength"'):
            assert keyword not in wire
        assert schema["additionalProperties"] is False

    def test_invoke_model_called_with_exact_kwargs(self):
        client = MagicMock()
        client.invoke_model.return_value = _response(_ok_payload())
        provider = _provider(client)
        _run(provider)
        kwargs = client.invoke_model.call_args.kwargs
        assert kwargs["modelId"] == "global.anthropic.claude-sonnet-5-5"
        assert kwargs["contentType"] == "application/json"
        assert kwargs["accept"] == "application/json"
        assert json.loads(kwargs["body"]) == provider.build_body(
            system="sys prompt", prompt="user prompt", schema=TinyVerdict
        )


class TestStopReasonMapping:
    def test_end_turn_parses_the_text_block_after_a_thinking_block(self):
        client = MagicMock()
        client.invoke_model.return_value = _response(
            _ok_payload(
                {"label": "status_ping", "rationale": "Asks for an update.", "confidence": 0.8}
            )
        )
        assert _run(_provider(client)) == TinyVerdict(
            label="status_ping", rationale="Asks for an update.", confidence=0.8
        )

    def test_refusal_is_terminal_and_names_the_category(self):
        payload = _ok_payload(stop_reason="refusal", stop_details={"category": "cyber"})
        client = MagicMock()
        client.invoke_model.return_value = _response(payload)
        with pytest.raises(RefusalError, match="cyber"):
            _run(_provider(client))
        assert client.invoke_model.call_count == 1

    def test_max_tokens_retries_once_at_one_effort_step_lower(self):
        client = MagicMock()
        client.invoke_model.side_effect = [
            _response(_ok_payload(stop_reason="max_tokens")),
            _response(_ok_payload()),
        ]
        assert _run(_provider(client)).label == "none"
        first = json.loads(client.invoke_model.call_args_list[0].kwargs["body"])
        second = json.loads(client.invoke_model.call_args_list[1].kwargs["body"])
        assert first["output_config"]["effort"] == "high"
        assert second["output_config"]["effort"] == "medium"
        assert second["output_config"]["format"] == first["output_config"]["format"]

    def test_max_tokens_twice_maps_to_provider_unavailable(self):
        client = MagicMock()

        def _truncated(**_: object) -> dict[str, Any]:
            return _response(_ok_payload(stop_reason="max_tokens"))

        client.invoke_model.side_effect = _truncated
        provider = _provider(client)
        with pytest.raises(ProviderUnavailable, match="max_tokens"):
            _run(provider)
        assert client.invoke_model.call_count == 2
        # Both calls billed tokens.
        assert provider.usage.summary()["calls"] == 2

    def test_unexpected_stop_reason_maps_to_provider_unavailable(self):
        client = MagicMock()
        client.invoke_model.return_value = _response(_ok_payload(stop_reason="pause_turn"))
        with pytest.raises(ProviderUnavailable, match="pause_turn"):
            _run(_provider(client))

    def test_no_text_block_maps_to_provider_unavailable(self):
        payload = _ok_payload()
        payload["content"] = [{"type": "thinking", "thinking": "", "signature": "sig"}]
        client = MagicMock()
        client.invoke_model.return_value = _response(payload)
        with pytest.raises(ProviderUnavailable, match="no text content"):
            _run(_provider(client))

    def test_invalid_json_maps_to_provider_unavailable(self):
        payload = _ok_payload()
        payload["content"] = [{"type": "text", "text": "not json {"}]
        client = MagicMock()
        client.invoke_model.return_value = _response(payload)
        with pytest.raises(ProviderUnavailable, match="not valid JSON"):
            _run(_provider(client))

    def test_a_dropped_constraint_is_still_enforced_by_pydantic(self):
        """The wire schema has no maximum, so pydantic is the gate for confidence <= 1."""
        client = MagicMock()
        client.invoke_model.return_value = _response(
            _ok_payload({"label": "none", "rationale": "x", "confidence": 1.5})
        )
        with pytest.raises(ProviderUnavailable, match="pydantic validation"):
            _run(_provider(client))


class TestEnumCaseFold:
    def test_a_case_variant_folds_onto_the_declared_member(self):
        client = MagicMock()
        client.invoke_model.return_value = _response(
            _ok_payload({"label": "Status_Ping", "rationale": "x", "confidence": 0.5})
        )
        assert _run(_provider(client)).label == "status_ping"

    def test_fold_reaches_enums_behind_defs_inside_lists(self):
        schema = Pairs.model_json_schema()
        data = {"pairs": [{"kind": "CORRECTION"}, {"kind": "open question"}]}
        assert fold_enum_case(data, schema, schema["$defs"]) == {
            "pairs": [{"kind": "correction"}, {"kind": "Open Question"}]
        }

    def test_a_value_matching_no_member_passes_through_for_pydantic(self):
        schema = TinyVerdict.model_json_schema()
        data = {"label": "frustration", "rationale": "x", "confidence": 0.5}
        assert fold_enum_case(data, schema, {}) == data

    def test_an_ambiguous_fold_is_left_alone(self):
        node = {"enum": ["Topic", "topic"], "type": "string"}
        assert fold_enum_case("TOPIC", node, {}) == "TOPIC"


class TestUsageAccounting:
    def test_input_tokens_include_cache_reads_and_writes(self):
        assert extract_usage(_ok_payload()) == CallUsage(
            input_tokens=200, output_tokens=50, reasoning_tokens=30, cached_tokens=60
        )

    def test_missing_fields_default_to_zero(self):
        assert extract_usage({}) == CallUsage()

    def test_haiku_marks_a_call_over_100k_input_tokens_long(self):
        payload = _ok_payload()
        payload["usage"]["input_tokens"] = 100_001
        payload["usage"]["cache_read_input_tokens"] = 0
        payload["usage"]["cache_creation_input_tokens"] = 0
        client = MagicMock()
        client.invoke_model.return_value = _response(payload)
        provider = _provider(client, "small")
        _run(provider)
        summary = provider.usage.summary()
        assert summary["long_input_tokens"] == 100_001
        assert summary["long_output_tokens"] == 50

    def test_sonnet_never_marks_a_call_long(self):
        payload = _ok_payload()
        payload["usage"]["input_tokens"] = 900_000
        client = MagicMock()
        client.invoke_model.return_value = _response(payload)
        provider = _provider(client, "medium")
        _run(provider)
        assert provider.usage.summary()["long_input_tokens"] == 0


class TestRetry:
    def test_throttle_is_retried_then_succeeds(self, monkeypatch: pytest.MonkeyPatch):
        wait = AnthropicBedrockProvider._invoke_sync.retry.wait
        monkeypatch.setattr(wait, "multiplier", 0)
        monkeypatch.setattr(wait, "min", 0)
        throttle = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "InvokeModel"
        )
        client = MagicMock()
        client.invoke_model.side_effect = [throttle, _response(_ok_payload())]
        assert _run(_provider(client)).label == "none"
        assert client.invoke_model.call_count == 2

    def test_validation_exception_raises_immediately(self):
        client = MagicMock()
        client.invoke_model.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "bad schema"}}, "InvokeModel"
        )
        with pytest.raises(ClientError):
            _run(_provider(client))
        assert client.invoke_model.call_count == 1


class TestFactory:
    def test_each_family_gets_its_adapter(self):
        anthropic = build_structured_provider(resolve("small", "anthropic"), region="us-east-1")
        openai = build_structured_provider(resolve("small", "openai"), region="us-east-1")
        assert isinstance(anthropic, AnthropicBedrockProvider)
        assert isinstance(openai, OpenAiBedrockProvider)
        assert anthropic.spec.model_id == "global.anthropic.claude-haiku-5-5"
        assert anthropic.provider == "anthropic-bedrock"
