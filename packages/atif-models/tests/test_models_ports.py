# SPDX-License-Identifier: Apache-2.0

"""`LlmStructuredProvider` has a proven implementation.

A declared `Protocol` with nothing asserted against it is worse than no port at
all: it reads as a contract, the type checker never sees a candidate to compare
it to, and an adapter can lose a method without a single gate going red. This
port is satisfied structurally, with no import in either direction, so nothing
else in the workspace connects it to its adapter.

Three assertions, because they fail on different mistakes: the annotated
binding, which pyright and ty check statically; the `runtime_checkable`
``isinstance``, which checks member presence at run time; and the member sweep,
which reports WHICH member went missing rather than just that the check failed.

Constructing the provider builds no client and reaches no network — the boto3
handle is created on first call.
"""

from __future__ import annotations

from typing import get_protocol_members, is_protocol

from atif_models.domain.ports import LlmStructuredProvider
from atif_models.domain.registry import resolve
from atif_models.infrastructure.openai_bedrock import OpenAiBedrockProvider


def test_openai_bedrock_satisfies_llm_structured_provider() -> None:
    provider: LlmStructuredProvider = OpenAiBedrockProvider(
        resolve("medium"), region="us-east-1", concurrency=1
    )
    assert isinstance(provider, LlmStructuredProvider)
    assert is_protocol(LlmStructuredProvider)
    members = get_protocol_members(LlmStructuredProvider)
    assert members, "LlmStructuredProvider declares no members"
    for member in members:
        assert hasattr(provider, member), f"OpenAiBedrockProvider is missing {member}"
