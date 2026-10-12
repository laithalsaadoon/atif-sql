# SPDX-License-Identifier: Apache-2.0

"""Anthropic Claude structured-output adapter on bedrock-runtime.

Implements :class:`atif_models.domain.ports.LlmStructuredProvider` with the
Anthropic Messages body shape on ``invoke_model``:

.. code-block:: json

    {"anthropic_version": "bedrock-2023-05-31",
     "max_tokens": 32000,
     "system": "...",
     "messages": [{"role": "user", "content": "..."}],
     "output_config": {"effort": "high",
                       "format": {"type": "json_schema", "schema": ...}}}

Verified live on 2026-10-12 against global.anthropic.claude-haiku-5-5,
-sonnet-5-5 and -opus-5-5 in us-east-1: all three honor ``output_config.format``
and reject a schema that carries ``minimum`` / ``maximum`` / ``minLength`` /
``maxLength``. The schema goes through the Anthropic SDK's own
``transform_schema``, which moves those constraints into the field
descriptions; pydantic validation of the response still enforces them.

Thinking stays at each model's default (adaptive; Opus 5.5 cannot turn it
off), because Anthropic's guidance for JSON answers to judgment tasks is to
let the model think first. Thinking counts toward ``max_tokens``, and a
response may open with a ``thinking`` block, so the parser joins the
``text`` blocks only. No ``temperature``, ``top_p`` or assistant prefill is
sent: the Claude 5.5 models reject all three.

Claude may return a string enum value that differs from the schema only in
letter case (documented under "Invalid outputs" in Anthropic's structured
outputs guide), so the parser folds such a value onto the declared one
before validation.

Retry and truncation follow the OpenAI adapter: tenacity over
:data:`~atif_models.infrastructure.bedrock_common.RETRY_CODES`, and a
``max_tokens`` stop is retried once at one effort step lower.
``stop_reason: refusal`` is terminal.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from typing import Any

import anyio
import anyio.to_thread
from anthropic import transform_schema
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
from atif_models.domain.registry import ModelSpec, is_long_context
from atif_models.infrastructure.bedrock_common import (
    EFFORT_DEGRADE,
    LengthTruncation,
    build_runtime_client,
    is_retryable,
    log_before_sleep,
)

#: The Bedrock Messages API version string every Anthropic body carries.
ANTHROPIC_BEDROCK_VERSION = "bedrock-2023-05-31"


def extract_usage(payload: dict[str, Any]) -> CallUsage:
    """Pull :class:`CallUsage` out of one Messages response.

    Anthropic reports ``input_tokens`` WITHOUT the cache reads and writes, so
    the total input is the sum of the three; ``cached_tokens`` is the cache
    reads. ``output_tokens`` includes thinking, which
    ``output_tokens_details.thinking_tokens`` breaks out. Missing fields
    default to 0.
    """
    usage = payload.get("usage") or {}
    details = usage.get("output_tokens_details") or {}
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    return CallUsage(
        input_tokens=int(usage.get("input_tokens") or 0) + cache_read + cache_write,
        output_tokens=int(usage.get("output_tokens") or 0),
        reasoning_tokens=int(details.get("thinking_tokens") or 0),
        cached_tokens=cache_read,
    )


def fold_enum_case(value: Any, node: dict[str, Any], defs: dict[str, Any]) -> Any:
    """Return ``value`` with each string enum member spelled as ``node`` declares it.

    ``node`` is the pydantic JSON schema for ``value`` (``defs`` its
    ``$defs``). A string that matches exactly one declared member when case
    is ignored is replaced by that member; anything else passes through for
    pydantic to judge.
    """
    ref = node.get("$ref")
    if isinstance(ref, str):
        node = defs.get(ref.rsplit("/", 1)[-1], {})
    enum, props, items = node.get("enum"), node.get("properties"), node.get("items")
    if isinstance(value, str) and isinstance(enum, list):
        matches = [m for m in enum if isinstance(m, str) and m.casefold() == value.casefold()]
        folded = matches[0] if value not in enum and len(matches) == 1 else value
    elif isinstance(value, dict) and isinstance(props, dict):
        folded = {k: fold_enum_case(v, props.get(k, {}), defs) for k, v in value.items()}
    elif isinstance(value, list) and isinstance(items, dict):
        folded = [fold_enum_case(v, items, defs) for v in value]
    else:
        # An optional field's schema is an anyOf of the real shape and null;
        # the first branch that changes the value wins.
        branches = [b for b in node.get("anyOf") or () if isinstance(b, dict)]
        candidates = (fold_enum_case(value, b, defs) for b in branches)
        folded = next((c for c in candidates if c != value), value)
    return folded


class AnthropicBedrockProvider:
    """The anthropic-family :class:`~atif_models.domain.ports.LlmStructuredProvider`.

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
        return "anthropic-bedrock"

    @property
    def spec(self) -> ModelSpec:
        """The resolved model spec this adapter is bound to."""
        return self._spec

    def _get_client(self) -> Any:
        """Lazily build one shared thread-safe ``bedrock-runtime`` client."""
        with self._client_lock:
            if self._client is None:
                self._client = build_runtime_client(region=self._region, pool_size=self._pool_size)
            return self._client

    def build_body(self, *, system: str, prompt: str, schema: type[SchemaT]) -> dict[str, Any]:
        """Return the exact ``invoke_model`` body for one call (pure; unit-testable)."""
        return {
            "anthropic_version": ANTHROPIC_BEDROCK_VERSION,
            "max_tokens": self._spec.max_completion_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {
                "effort": self._spec.reasoning_effort,
                "format": {"type": "json_schema", "schema": transform_schema(schema)},
            },
        }

    async def classify_structured(
        self, *, system: str, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        """One structured-output call under the concurrency limiter.

        ``stop_reason=max_tokens`` retries ONCE with the effort degraded one
        step (high→medium→low), since resending the same request would stop
        in the same place. Still truncated, or already at the floor, raises
        :class:`ProviderUnavailable` for the retry queue.
        """
        body = self.build_body(system=system, prompt=prompt, schema=schema)
        async with self._limiter:
            payload = await anyio.to_thread.run_sync(lambda: self._invoke_sync(body))
            try:
                return self._parse_payload(payload, schema)
            except LengthTruncation as first:
                effort = str(body["output_config"]["effort"])
                degraded = EFFORT_DEGRADE.get(effort)
                if degraded is None:
                    raise ProviderUnavailable(str(first)) from first
                logger.warning(
                    "bedrock invoke: stop_reason=max_tokens at effort={} — retrying once at "
                    "effort={} (deterministic truncation needs a parameter change, not a resend)",
                    effort,
                    degraded,
                )
                retry_body = {
                    **body,
                    "output_config": {**body["output_config"], "effort": degraded},
                }
                payload = await anyio.to_thread.run_sync(lambda: self._invoke_sync(retry_body))
                try:
                    return self._parse_payload(payload, schema)
                except LengthTruncation as second:
                    raise ProviderUnavailable(str(second)) from second

    @retry(
        stop=stop_after_attempt(10),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        retry=retry_if_exception(is_retryable),
        before_sleep=log_before_sleep,
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
        """Text blocks → json → enum-case fold → ``schema.model_validate``.

        Usage is accumulated FIRST (a truncated or refused call still billed
        tokens), then ``stop_reason`` gates: ``refusal`` →
        :class:`RefusalError`, ``max_tokens`` → truncation (one retry, see
        :meth:`classify_structured`), anything but ``end_turn`` →
        :class:`ProviderUnavailable`.
        """
        usage = extract_usage(payload)
        if is_long_context(self._spec, usage.input_tokens):
            usage = dataclasses.replace(usage, long_context=True)
        self.usage.add(usage)
        stop_reason = payload.get("stop_reason")
        if stop_reason == "refusal":
            details = payload.get("stop_details") or {}
            category = details.get("category") if isinstance(details, dict) else None
            msg = f"model declined the input (stop_reason=refusal, category={category!r})"
            raise RefusalError(msg)
        if stop_reason == "max_tokens":
            msg = (
                "response truncated (stop_reason=max_tokens) — raise "
                f"max_completion_tokens (currently {self._spec.max_completion_tokens}), "
                "lower the effort, or shrink the prompt"
            )
            raise LengthTruncation(msg)
        if stop_reason != "end_turn":
            msg = f"unexpected stop_reason={stop_reason!r}"
            raise ProviderUnavailable(msg)
        blocks = payload.get("content") or []
        text = "".join(
            str(b.get("text") or "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )
        if not text:
            msg = f"no text content (block types: {[b.get('type') for b in blocks if isinstance(b, dict)]})"
            raise ProviderUnavailable(msg)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            msg = f"content is not valid JSON: {exc}"
            raise ProviderUnavailable(msg) from exc
        json_schema = schema.model_json_schema()
        data = fold_enum_case(data, json_schema, json_schema.get("$defs") or {})
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            msg = f"structured output failed pydantic validation: {exc}"
            raise ProviderUnavailable(msg) from exc


__all__ = [
    "ANTHROPIC_BEDROCK_VERSION",
    "AnthropicBedrockProvider",
    "extract_usage",
    "fold_enum_case",
]
