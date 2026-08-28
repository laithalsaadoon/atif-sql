# SPDX-License-Identifier: Apache-2.0

"""LIVE smoke: one strict structured-output call per openai registry entry.

Calls luna, terra, and sol once each through
:class:`atif_models.infrastructure.openai_bedrock.OpenAiBedrockProvider`
with a tiny schema at the registry defaults (reasoning_effort=high,
max_completion_tokens=32000), verifies the strict output parses through
pydantic, and prints per-model latency + usage + estimated cost.

Costs real money (fractions of a cent) and needs ambient AWS creds with
bedrock-runtime access in us-east-1. Run from the workspace root:

    uv run python scripts/dev/smoke_llm_models.py

Deliberately NOT a pytest test — it must never run inside the gate suite.
"""

from __future__ import annotations

import time
from enum import StrEnum

import anyio
from pydantic import BaseModel, Field

from atif_models.domain.registry import Size, estimate_cost, resolve
from atif_models.infrastructure.openai_bedrock import OpenAiBedrockProvider

REGION = "us-east-1"

SYSTEM = (
    "You classify one software-engineering work session. "
    "Answer strictly in the requested JSON schema."
)
PROMPT = (
    "Session summary: the agent renamed a flaky integration test, fixed the "
    "underlying race by adding a lock around a shared counter, and re-ran the "
    "suite twice until green. Classify this session."
)


class WorkCategory(StrEnum):
    """The enum the smoke schema nests, to exercise ``$defs`` generation."""

    BUGFIX = "bugfix"
    FEATURE = "feature"
    REFACTOR = "refactor"
    OTHER = "other"


class SmokeVerdict(BaseModel):
    """Tiny schema exercising enum + $defs + optional->nullable + bounds."""

    category: WorkCategory
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    caveat: str | None = None


async def _smoke_one(size: Size) -> None:
    spec = resolve(size)
    provider = OpenAiBedrockProvider(spec, region=REGION, concurrency=1)
    t0 = time.monotonic()
    verdict = await provider.classify_structured(system=SYSTEM, prompt=PROMPT, schema=SmokeVerdict)
    elapsed = time.monotonic() - t0
    usage = provider.usage.summary()
    cost = estimate_cost(
        spec, input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]
    )
    print(f"\n=== {size} -> {spec.model_id} ===")  # noqa: T201
    print(f"  parse ok    : {verdict!r}")  # noqa: T201
    print(f"  latency     : {elapsed:.2f}s")  # noqa: T201
    print(f"  usage       : {usage}")  # noqa: T201
    print(f"  est. cost   : ${cost:.6f}" if cost is not None else "  est. cost   : n/a")  # noqa: T201


async def _main() -> None:
    for size in ("small", "medium", "large"):
        await _smoke_one(size)


if __name__ == "__main__":
    anyio.run(_main)
