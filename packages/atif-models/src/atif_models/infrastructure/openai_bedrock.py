# SPDX-License-Identifier: Apache-2.0

"""OpenAI GPT-5.6 structured-output adapter on bedrock-runtime (the default).

Implements :class:`atif_models.domain.ports.LlmStructuredProvider` with the
OpenAI chat-completions body shape on ``invoke_model``:

.. code-block:: json

    {"messages": [{"role": "system", ...}, {"role": "user", ...}],
     "max_completion_tokens": 32000,
     "reasoning_effort": "high",
     "response_format": {"type": "json_schema",
                         "json_schema": {"name": ..., "strict": true, "schema": ...}}}

Retry policy: tenacity owns the semantic loop (10 attempts, exponential
2..60s) over :data:`RETRY_CODES` + network errors; botocore's own retries
are OFF (``max_attempts=0``) so tenacity sees every error immediately. The
blocking ``invoke_model`` call is dispatched via ``anyio.to_thread`` under
an ``anyio.CapacityLimiter`` so cancellation scopes propagate.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import anyio
import anyio.to_thread
import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    ClientError,
    ConnectionError as BotoConnectionError,
    EndpointConnectionError,
    ReadTimeoutError,
    SSLError,
)
from loguru import logger
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from atif_models.domain.ports import (
    CallUsage,
    ProviderUnavailable,
    RefusalError,
    SchemaT,
    UsageAccumulator,
)
from atif_models.domain.registry import ModelSpec
from atif_models.domain.schema import to_openai_strict

#: Bedrock error codes worth retrying. TWIN of
#: ``atif_embed.infrastructure.cohere_bedrock.RETRY_CODES`` — the two
#: packages may not import each other (independence contract), so each pins
#: this set against the same literal in its own test suite.
RETRY_CODES: frozenset[str] = frozenset(
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

#: finish_reason values that mean "the model declined" (terminal).
_REFUSAL_FINISH_REASONS = {"content_filter", "refusal"}

#: One-step reasoning_effort degradation ladder for the finish_reason=length
#: retry. Truncation is DETERMINISTIC for a fixed (prompt, parameters) pair —
#: resending the same request would truncate identically and just double the
#: bill — so the single retry changes a parameter: less reasoning leaves more
#: of the completion budget for the answer. Efforts at "low" or below don't
#: retry (nothing left to degrade to).
_EFFORT_DEGRADE: dict[str, str] = {
    "max": "xhigh",
    "xhigh": "high",
    "high": "medium",
    "medium": "low",
}


class _LengthTruncation(ProviderUnavailable):
    """Internal marker for ``finish_reason=length``.

    Callers only ever see :class:`ProviderUnavailable`;
    :meth:`OpenAiBedrockProvider.classify_structured` catches this one to run
    the one-shot effort-degrade retry.
    """


def _is_retryable(exc: BaseException) -> bool:
    """True for throttle/service ``ClientError`` codes + network errors."""
    if isinstance(exc, SSLError | BotoConnectionError | EndpointConnectionError | ReadTimeoutError):
        return True
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code")
    return code in RETRY_CODES


def _log_before_sleep(retry_state: Any) -> None:
    """Log one WARNING per backoff (loguru — stdlib logging is banned)."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(
        "bedrock invoke retry {}/10 in {:.1f}s after {}: {}",
        retry_state.attempt_number,
        getattr(retry_state.next_action, "sleep", 0.0),
        type(exc).__name__ if exc else "?",
        exc,
    )


def extract_usage(payload: dict[str, Any]) -> CallUsage:
    """Pull :class:`CallUsage` out of one chat-completions response.

    GPT-5.6 on Bedrock reports ``usage.prompt_tokens`` /
    ``usage.completion_tokens`` plus ``completion_tokens_details.reasoning_tokens``
    and ``prompt_tokens_details.cached_tokens``. Missing fields default to 0.
    """
    usage = payload.get("usage") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    return CallUsage(
        input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
        cached_tokens=int(prompt_details.get("cached_tokens") or 0),
    )


class OpenAiBedrockProvider:
    """The default :class:`~atif_models.domain.ports.LlmStructuredProvider`.

    Everything provider-specific is fixed at construction: the resolved
    :class:`~atif_models.domain.registry.ModelSpec`, the AWS region, the
    concurrency ceiling, and (optionally) a shared per-pipeline
    :class:`~atif_models.domain.ports.UsageAccumulator`.
    """

    def __init__(
        self,
        spec: ModelSpec,
        *,
        region: str,
        concurrency: int = 16,
        usage: UsageAccumulator | None = None,
    ) -> None:
        self._spec = spec
        self._region = region
        self._limiter = anyio.CapacityLimiter(concurrency)
        self.usage = usage if usage is not None else UsageAccumulator()
        self._client_lock = threading.Lock()
        self._client: Any = None
        self._pool_size = max(32, concurrency * 2)

    @property
    def provider(self) -> str:
        """Stable provider tag for logs and store stamps."""
        return "openai-bedrock"

    @property
    def spec(self) -> ModelSpec:
        """The resolved model spec this adapter is bound to."""
        return self._spec

    def _get_client(self) -> Any:
        """Lazily build one shared thread-safe ``bedrock-runtime`` client.

        botocore retries are OFF (``max_attempts=0``) — the tenacity
        decorator on :meth:`_invoke_sync` owns the retry policy. Pool
        sized to 2× concurrency with a floor of 32; ``read_timeout=600``
        because high reasoning effort can hold the connection well past
        botocore's 60s default.
        """
        with self._client_lock:
            if self._client is None:
                boto_cfg = BotoConfig(
                    region_name=self._region,
                    retries={"max_attempts": 0, "mode": "standard"},
                    max_pool_connections=self._pool_size,
                    connect_timeout=10,
                    read_timeout=600,
                )
                self._client = boto3.client("bedrock-runtime", config=boto_cfg)
            return self._client

    def build_body(self, *, system: str, prompt: str, schema: type[SchemaT]) -> dict[str, Any]:
        """Return the exact ``invoke_model`` body for one call (pure; unit-testable)."""
        return {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_completion_tokens": self._spec.max_completion_tokens,
            "reasoning_effort": self._spec.reasoning_effort,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": to_openai_strict(schema),
                },
            },
        }

    async def classify_structured(
        self, *, system: str, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        """One structured-output call under the concurrency limiter.

        ``finish_reason=length`` retries ONCE with ``reasoning_effort``
        degraded one step (high→medium→low): truncation is deterministic
        for a fixed (prompt, parameters) pair, so the second chance must
        change a parameter, not resend. Still-truncated (or already at the
        floor) raises :class:`ProviderUnavailable` for the retry queue.
        """
        body = self.build_body(system=system, prompt=prompt, schema=schema)
        async with self._limiter:
            payload = await anyio.to_thread.run_sync(lambda: self._invoke_sync(body))
            try:
                return self._parse_payload(payload, schema)
            except _LengthTruncation as first:
                degraded = _EFFORT_DEGRADE.get(str(body.get("reasoning_effort")))
                if degraded is None:
                    raise ProviderUnavailable(str(first)) from first
                logger.warning(
                    "bedrock invoke: finish_reason=length at effort={} — "
                    "retrying once at effort={} (deterministic truncation "
                    "needs a parameter change, not a resend)",
                    body.get("reasoning_effort"),
                    degraded,
                )
                retry_body = {**body, "reasoning_effort": degraded}
                payload = await anyio.to_thread.run_sync(lambda: self._invoke_sync(retry_body))
                try:
                    return self._parse_payload(payload, schema)
                except _LengthTruncation as second:
                    raise ProviderUnavailable(str(second)) from second

    @retry(
        stop=stop_after_attempt(10),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        retry=retry_if_exception(_is_retryable),
        before_sleep=_log_before_sleep,
        reraise=True,
    )
    def _invoke_sync(self, body: dict[str, Any]) -> dict[str, Any]:
        """One blocking ``invoke_model`` call; returns the parsed JSON payload."""
        client = self._get_client()
        resp = client.invoke_model(
            modelId=self._spec.model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        return json.loads(resp["body"].read())

    def _parse_payload(self, payload: dict[str, Any], schema: type[SchemaT]) -> SchemaT:
        """choices[0].message.content → json → ``schema.model_validate``.

        Usage is accumulated FIRST (even a length-truncated call billed
        tokens), then finish_reason gates: ``length`` →
        :class:`ProviderUnavailable` with a hint, ``content_filter`` /
        ``refusal`` → :class:`RefusalError`.
        """
        self.usage.add(extract_usage(payload))
        choices = payload.get("choices") or []
        if not choices:
            msg = f"no choices in response (keys: {sorted(payload.keys())})"
            raise ProviderUnavailable(msg)
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason in _REFUSAL_FINISH_REASONS:
            msg = f"model declined the input (finish_reason={finish_reason})"
            raise RefusalError(msg)
        message = choice.get("message") or {}
        if message.get("refusal"):
            msg = f"model refused: {message['refusal']}"
            raise RefusalError(msg)
        if finish_reason == "length":
            msg = (
                "response truncated (finish_reason=length) — raise "
                f"max_completion_tokens (currently {self._spec.max_completion_tokens}), "
                "lower reasoning_effort, or shrink the prompt"
            )
            raise _LengthTruncation(msg)
        content = message.get("content")
        if not isinstance(content, str) or not content:
            msg = f"no message content (finish_reason={finish_reason!r})"
            raise ProviderUnavailable(msg)
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            msg = f"content is not valid JSON: {exc}"
            raise ProviderUnavailable(msg) from exc
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            msg = f"strict output failed pydantic validation: {exc}"
            raise ProviderUnavailable(msg) from exc


__all__ = ["RETRY_CODES", "OpenAiBedrockProvider", "extract_usage"]
