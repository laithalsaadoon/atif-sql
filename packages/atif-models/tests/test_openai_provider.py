# SPDX-License-Identifier: Apache-2.0

"""OpenAiBedrockProvider: exact body, finish_reason mapping, usage, retry."""

from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import MagicMock

import anyio
import pytest
from botocore.exceptions import ClientError
from pydantic import BaseModel

from atif_models.domain.ports import CallUsage, ProviderUnavailable, RefusalError, UsageAccumulator
from atif_models.domain.registry import resolve
from atif_models.domain.schema import to_openai_strict
from atif_models.infrastructure.openai_bedrock import (
    RETRY_CODES,
    OpenAiBedrockProvider,
    extract_usage,
)

#: The retryable Bedrock code set, spelled out independently of the module
#: under test. atif-embed's cohere adapter carries a TWIN of this set and
#: pins it against the SAME literal in its own suite; the two packages may
#: not import each other, so a shared literal on both sides is what catches
#: drift between the copies.
BEDROCK_RETRY_CODES = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "ModelTimeoutException",
        "ModelErrorException",
        "ProvisionedThroughputExceededException",
        "TooManyRequestsException",
        "InternalServerException",
        "InternalFailure",
    }
)


class TinyVerdict(BaseModel):
    label: str
    confidence: float


def _response(payload: dict[str, Any]) -> dict[str, Any]:
    return {"body": io.BytesIO(json.dumps(payload).encode())}


def _ok_payload(content: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": json.dumps(content or {"label": "ok", "confidence": 0.9})},
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 40,
            "completion_tokens_details": {"reasoning_tokens": 25},
            "prompt_tokens_details": {"cached_tokens": 60},
        },
    }
    payload.update(overrides)
    return payload


def _provider(client: MagicMock) -> OpenAiBedrockProvider:
    provider = OpenAiBedrockProvider(resolve("medium"), region="us-east-1", concurrency=2)
    provider._client = client
    return provider


def _run(provider: OpenAiBedrockProvider) -> TinyVerdict:
    return anyio.run(
        lambda: provider.classify_structured(
            system="sys prompt", prompt="user prompt", schema=TinyVerdict
        )
    )


class TestBodyConstruction:
    def test_exact_body(self):
        provider = OpenAiBedrockProvider(resolve("medium"), region="us-east-1")
        body = provider.build_body(system="sys prompt", prompt="user prompt", schema=TinyVerdict)
        assert body == {
            "messages": [
                {"role": "system", "content": "sys prompt"},
                {"role": "user", "content": "user prompt"},
            ],
            "max_completion_tokens": 32_000,
            "reasoning_effort": "high",
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "TinyVerdict",
                    "strict": True,
                    "schema": to_openai_strict(TinyVerdict),
                },
            },
        }

    def test_invoke_model_called_with_exact_kwargs(self):
        client = MagicMock()
        client.invoke_model.return_value = _response(_ok_payload())
        provider = _provider(client)
        _run(provider)
        kwargs = client.invoke_model.call_args.kwargs
        assert kwargs["modelId"] == "global.openai.gpt-5.6-terra"
        assert kwargs["contentType"] == "application/json"
        assert kwargs["accept"] == "application/json"
        assert json.loads(kwargs["body"]) == provider.build_body(
            system="sys prompt", prompt="user prompt", schema=TinyVerdict
        )


class TestFinishReasonMapping:
    def test_stop_parses_and_validates(self):
        client = MagicMock()
        client.invoke_model.return_value = _response(_ok_payload({"label": "x", "confidence": 0.5}))
        result = _run(_provider(client))
        assert result == TinyVerdict(label="x", confidence=0.5)

    def test_length_twice_maps_to_provider_unavailable_with_hint(self):
        payload = _ok_payload()
        payload["choices"][0]["finish_reason"] = "length"
        client = MagicMock()

        def _truncated(**_: object) -> dict[str, Any]:
            return _response(payload)

        client.invoke_model.side_effect = _truncated
        with pytest.raises(ProviderUnavailable, match="max_completion_tokens"):
            _run(_provider(client))
        # Exactly one degraded retry (deterministic truncation — a resend
        # would truncate identically), then the error surfaces.
        assert client.invoke_model.call_count == 2

    def test_length_retries_once_with_degraded_reasoning_effort(self):
        length_payload = _ok_payload()
        length_payload["choices"][0]["finish_reason"] = "length"
        client = MagicMock()
        client.invoke_model.side_effect = [
            _response(length_payload),
            _response(_ok_payload({"label": "recovered", "confidence": 0.8})),
        ]
        provider = _provider(client)
        result = _run(provider)
        assert result.label == "recovered"
        first_body = json.loads(client.invoke_model.call_args_list[0].kwargs["body"])
        second_body = json.loads(client.invoke_model.call_args_list[1].kwargs["body"])
        assert first_body["reasoning_effort"] == "high"
        assert second_body["reasoning_effort"] == "medium"  # high -> medium
        assert {k: v for k, v in second_body.items() if k != "reasoning_effort"} == {
            k: v for k, v in first_body.items() if k != "reasoning_effort"
        }

    def test_length_at_low_effort_does_not_retry(self):
        import dataclasses

        length_payload = _ok_payload()
        length_payload["choices"][0]["finish_reason"] = "length"
        client = MagicMock()

        def _truncated(**_: object) -> dict[str, Any]:
            return _response(length_payload)

        client.invoke_model.side_effect = _truncated
        spec = dataclasses.replace(resolve("medium"), reasoning_effort="low")
        provider = OpenAiBedrockProvider(spec, region="us-east-1", concurrency=2)
        provider._client = client
        with pytest.raises(ProviderUnavailable, match="length"):
            _run(provider)
        assert client.invoke_model.call_count == 1  # nothing left to degrade to

    @pytest.mark.parametrize("reason", ["content_filter", "refusal"])
    def test_refusal_finish_reasons_map_to_refusal_error(self, reason: str):
        payload = _ok_payload()
        payload["choices"][0]["finish_reason"] = reason
        client = MagicMock()
        client.invoke_model.return_value = _response(payload)
        with pytest.raises(RefusalError):
            _run(_provider(client))

    def test_refusal_message_field_maps_to_refusal_error(self):
        payload = _ok_payload()
        payload["choices"][0]["message"] = {"refusal": "I can't help with that."}
        client = MagicMock()
        client.invoke_model.return_value = _response(payload)
        with pytest.raises(RefusalError, match="can't help"):
            _run(_provider(client))

    def test_invalid_json_content_maps_to_provider_unavailable(self):
        payload = _ok_payload()
        payload["choices"][0]["message"]["content"] = "not json {"
        client = MagicMock()
        client.invoke_model.return_value = _response(payload)
        with pytest.raises(ProviderUnavailable, match="not valid JSON"):
            _run(_provider(client))

    def test_schema_mismatch_maps_to_provider_unavailable(self):
        client = MagicMock()
        client.invoke_model.return_value = _response(_ok_payload({"wrong": "shape"}))
        with pytest.raises(ProviderUnavailable, match="pydantic validation"):
            _run(_provider(client))

    def test_empty_choices_maps_to_provider_unavailable(self):
        client = MagicMock()
        client.invoke_model.return_value = _response({"choices": [], "usage": {}})
        with pytest.raises(ProviderUnavailable, match="no choices"):
            _run(_provider(client))


class TestUsageAccounting:
    def test_extract_usage_reads_all_four_counters(self):
        usage = extract_usage(_ok_payload())
        assert usage == CallUsage(
            input_tokens=100, output_tokens=40, reasoning_tokens=25, cached_tokens=60
        )

    def test_extract_usage_defaults_missing_fields_to_zero(self):
        assert extract_usage({}) == CallUsage()

    def test_usage_accumulates_across_calls(self):
        client = MagicMock()

        # Fresh BytesIO per call — a shared return_value is consumed by the
        # first body.read().
        def _fresh(**_: object) -> dict[str, Any]:
            return _response(_ok_payload())

        client.invoke_model.side_effect = _fresh
        provider = _provider(client)
        _run(provider)
        _run(provider)
        assert provider.usage.summary() == {
            "calls": 2,
            "input_tokens": 200,
            "output_tokens": 80,
            "reasoning_tokens": 50,
            "cached_tokens": 120,
        }

    def test_usage_accumulated_even_on_length_failure(self):
        payload = _ok_payload()
        payload["choices"][0]["finish_reason"] = "length"
        client = MagicMock()

        def _truncated(**_: object) -> dict[str, Any]:
            return _response(payload)

        client.invoke_model.side_effect = _truncated
        provider = _provider(client)
        with pytest.raises(ProviderUnavailable):
            _run(provider)
        # Both the truncated call AND its degraded retry billed tokens.
        assert provider.usage.summary()["input_tokens"] == 200

    def test_shared_accumulator_is_threadsafe_shape(self):
        acc = UsageAccumulator()
        acc.add(CallUsage(input_tokens=1, output_tokens=2, reasoning_tokens=3, cached_tokens=4))
        acc.add(CallUsage(input_tokens=10))
        assert acc.summary() == {
            "calls": 2,
            "input_tokens": 11,
            "output_tokens": 2,
            "reasoning_tokens": 3,
            "cached_tokens": 4,
        }


class TestRetry:
    def _throttle(self) -> ClientError:
        return ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "InvokeModel",
        )

    def test_retries_throttle_twice_then_succeeds(self, monkeypatch: pytest.MonkeyPatch):
        # Collapse tenacity's exponential waits so the test is instant
        # (wait_exponential clamps to `min`, so both knobs must drop).
        # tenacity's `@retry` attaches `.retry` to the wrapped function at run
        # time and neither checker's view of a `FunctionType` describes it.
        wait = OpenAiBedrockProvider._invoke_sync.retry.wait  # ty: ignore[unresolved-attribute] # pyright: ignore[reportFunctionMemberAccess]
        monkeypatch.setattr(wait, "multiplier", 0)
        monkeypatch.setattr(wait, "min", 0)
        client = MagicMock()
        client.invoke_model.side_effect = [
            self._throttle(),
            self._throttle(),
            _response(_ok_payload()),
        ]
        provider = _provider(client)
        result = _run(provider)
        assert result.label == "ok"
        assert client.invoke_model.call_count == 3

    def test_non_retryable_client_error_raises_immediately(self):
        client = MagicMock()
        client.invoke_model.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "bad schema"}},
            "InvokeModel",
        )
        provider = _provider(client)
        with pytest.raises(ClientError):
            _run(provider)
        assert client.invoke_model.call_count == 1


class TestRetryCodePin:
    """Pin the retryable set so it cannot drift from atif-embed's twin copy."""

    def test_retry_codes_match_the_shared_set(self):
        assert RETRY_CODES == BEDROCK_RETRY_CODES

    @pytest.mark.parametrize("code", sorted(BEDROCK_RETRY_CODES))
    def test_every_shared_code_is_retryable_here(self, code: str):
        from atif_models.infrastructure.openai_bedrock import _is_retryable

        assert _is_retryable(ClientError({"Error": {"Code": code, "Message": "x"}}, "InvokeModel"))
