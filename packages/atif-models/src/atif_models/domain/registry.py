# SPDX-License-Identifier: Apache-2.0

"""The model alias registry (CONTRACT-V2 §Model registry).

Size aliases (small/medium/large) resolve to concrete Bedrock GLOBAL
inference-profile ids per family. This module is the ONLY place in the
atif-sql workspace where a model id is written down — every pipeline
names a (family, size) pair and lets :func:`resolve` pick the id.

Pricing (USD per 1M tokens, on-demand; Bedrock charges OpenAI/Anthropic
list parity, so these track the vendors' published rates):

* openai gpt-5.6, post the 2026-07-30 Bedrock price cut: luna $0.20/$1.20,
  terra $2/$12, sol $5/$30.
* anthropic: haiku-4-5 $1/$5, sonnet-5 $2/$10, opus-5 $5/$25.

Pure domain: frozen value objects, no I/O, no env. Env-driven family /
size selection lives in :mod:`atif_models.infrastructure.settings`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Family = Literal["openai", "anthropic"]
Size = Literal["small", "medium", "large"]

#: GPT-5.6 reasoning levels per the Bedrock GA post; the anthropic entries
#: reuse the same vocabulary.
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]

DEFAULT_FAMILY: Family = "openai"
DEFAULT_REASONING_EFFORT: ReasoningEffort = "high"
DEFAULT_MAX_COMPLETION_TOKENS = 32_000


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One concrete model behind a (family, size) alias.

    ``pricing_in`` / ``pricing_out`` are USD per 1M tokens (``None`` when
    unknown — :func:`estimate_cost` tolerates that by returning ``None``).
    ``supports_strict_json`` marks native OpenAI strict structured outputs
    (``response_format.json_schema.strict``); the anthropic entries are
    ``False`` because Bedrock-Anthropic enforces schemas through
    ``output_config``, a different contract.
    """

    family: Family
    size: Size
    model_id: str
    pricing_in: float | None
    pricing_out: float | None
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS
    reasoning_effort: ReasoningEffort = DEFAULT_REASONING_EFFORT
    supports_strict_json: bool = True


REGISTRY: dict[tuple[Family, Size], ModelSpec] = {
    ("openai", "small"): ModelSpec(
        family="openai",
        size="small",
        model_id="global.openai.gpt-5.6-luna",
        pricing_in=0.20,
        pricing_out=1.20,
    ),
    ("openai", "medium"): ModelSpec(
        family="openai",
        size="medium",
        model_id="global.openai.gpt-5.6-terra",
        pricing_in=2.00,
        pricing_out=12.00,
    ),
    ("openai", "large"): ModelSpec(
        family="openai",
        size="large",
        model_id="global.openai.gpt-5.6-sol",
        pricing_in=5.00,
        pricing_out=30.00,
    ),
    ("anthropic", "small"): ModelSpec(
        family="anthropic",
        size="small",
        model_id="global.anthropic.claude-haiku-4-5",
        pricing_in=1.00,
        pricing_out=5.00,
        supports_strict_json=False,
    ),
    ("anthropic", "medium"): ModelSpec(
        family="anthropic",
        size="medium",
        model_id="global.anthropic.claude-sonnet-5",
        pricing_in=2.00,
        pricing_out=10.00,
        supports_strict_json=False,
    ),
    ("anthropic", "large"): ModelSpec(
        family="anthropic",
        size="large",
        model_id="global.anthropic.claude-opus-5",
        pricing_in=5.00,
        pricing_out=25.00,
        supports_strict_json=False,
    ),
}


def resolve(size: Size, family: Family = DEFAULT_FAMILY) -> ModelSpec:
    """Return the :class:`ModelSpec` behind a (family, size) alias.

    Default family is openai: GPT-5.6 supports native strict structured
    outputs, and it is the only family with a provider adapter. The
    anthropic column records model ids and pricing for a future adapter;
    :data:`atif_models.infrastructure.settings.RUNNABLE_FAMILIES` is the
    gate that keeps it from being selected before one exists.
    """
    try:
        return REGISTRY[(family, size)]
    except KeyError:  # pragma: no cover — Literal types make this unreachable from typed code
        msg = f"no model registered for family={family!r} size={size!r}"
        raise KeyError(msg) from None


def estimate_cost(spec: ModelSpec, *, input_tokens: int, output_tokens: int) -> float | None:
    """USD estimate for one call (or an accumulated pipeline) on ``spec``.

    Returns ``None`` when either price is unknown — callers must render
    that as "pricing unavailable", never as $0. Cache-read / reasoning
    discounts are deliberately NOT modeled here: reasoning tokens bill as
    output (already inside ``output_tokens`` for the OpenAI usage shape)
    and cached input still bills at a nonzero rate, so this is an upper
    bound suitable for the dry-run cost guard.
    """
    if spec.pricing_in is None or spec.pricing_out is None:
        return None
    return (input_tokens * spec.pricing_in + output_tokens * spec.pricing_out) / 1_000_000


__all__ = [
    "DEFAULT_FAMILY",
    "DEFAULT_MAX_COMPLETION_TOKENS",
    "DEFAULT_REASONING_EFFORT",
    "REGISTRY",
    "Family",
    "ModelSpec",
    "ReasoningEffort",
    "Size",
    "estimate_cost",
    "resolve",
]
