# SPDX-License-Identifier: Apache-2.0

"""Pick the structured-output adapter for a resolved :class:`ModelSpec`.

One adapter per family: openai → :class:`OpenAiBedrockProvider`,
anthropic → :class:`AnthropicBedrockProvider`. Each adapter module is
imported only when its family is asked for, so building one provider never
loads the other SDK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atif_models.domain.ports import LlmStructuredProvider, UsageAccumulator
    from atif_models.domain.registry import ModelSpec


def build_structured_provider(
    spec: ModelSpec,
    *,
    region: str,
    concurrency: int = 16,
    usage: UsageAccumulator | None = None,
) -> LlmStructuredProvider:
    """The adapter for ``spec.family``, bound to ``spec`` at construction."""
    if spec.family == "anthropic":
        from atif_models.infrastructure.anthropic_bedrock import AnthropicBedrockProvider

        return AnthropicBedrockProvider(spec, region=region, concurrency=concurrency, usage=usage)
    from atif_models.infrastructure.openai_bedrock import OpenAiBedrockProvider

    return OpenAiBedrockProvider(spec, region=region, concurrency=concurrency, usage=usage)


__all__ = ["build_structured_provider"]
