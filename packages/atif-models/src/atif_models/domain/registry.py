# SPDX-License-Identifier: Apache-2.0

"""The model alias registry (CONTRACT-V2 §Model registry).

Size aliases (small/medium/large) resolve to concrete Bedrock GLOBAL
inference-profile ids per family. This module is the ONLY place in the
atif-sql workspace where a model id is written down — every pipeline
names a (family, size) pair and lets :func:`resolve` pick the id.

The families follow each vendor's own tiers: OpenAI's GPT-6 family is Luna
(fast, high volume), 6.1 Sol (balanced) and Astra (highest intelligence);
Anthropic's Claude 5.5 family is Haiku, Sonnet and Opus. Every id below
answered an ``invoke_model`` structured-output call in us-east-1 on
2026-10-12.

Pricing (USD per 1M tokens, standard tier). These are the vendors' list
prices, read 2026-10-12 at developers.openai.com/api/docs/pricing and
platform.claude.com/docs/en/about-claude/pricing. Bedrock's GLOBAL profiles
bill list price (litellm's Bedrock entry for global.openai.gpt-6-astra agrees
at $10/$50) and its US geo profiles add 10%:

* openai gpt-6: luna $0.10/$0.50, 6.1 sol $2/$10, astra $10/$50. A request
  over 272K input tokens bills 2x input and 1.5x output for the whole request.
* anthropic claude 5.5: haiku $0.10/$0.50, sonnet $2/$10, opus $4/$20. A
  Haiku 5.5 request over 100K input tokens bills $0.50/$2.50 for the whole
  request; Sonnet and Opus price the full 1M window at the standard rate.

Pure domain: frozen value objects, no I/O, no env. Env-driven family /
size selection lives in :mod:`atif_models.infrastructure.settings`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Family = Literal["openai", "anthropic"]
Size = Literal["small", "medium", "large"]

#: Reasoning levels. GPT-6 Luna accepts all six; GPT-6.1 Sol, GPT-6 Astra and
#: the Claude 5.5 models accept ``low`` through ``max`` (no ``none``).
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
    ``output_config.format``, a different contract with its own adapter.

    ``long_context_threshold`` is the input-token count above which the
    vendor bills the WHOLE request at ``long_pricing_in`` /
    ``long_pricing_out``; ``None`` means one rate across the context window.
    The adapters mark each call that crosses it, so the totals can be priced
    at the right rate.

    ``chars_per_token`` is the family's tokenizer density on rendered
    coding-agent transcripts, which the ``--dry-run`` plans use to turn a
    measured prompt length into tokens. Measured 2026-10-12 over 19 sessions
    and 63 messages (about 5.3M characters): GPT-6 averaged 3.3 characters
    per token and Claude 5.5 averaged 2.2, so the same transcript costs a
    Claude model about 1.5x the input tokens.
    """

    family: Family
    size: Size
    model_id: str
    pricing_in: float | None
    pricing_out: float | None
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS
    reasoning_effort: ReasoningEffort = DEFAULT_REASONING_EFFORT
    supports_strict_json: bool = True
    long_context_threshold: int | None = None
    long_pricing_in: float | None = None
    long_pricing_out: float | None = None
    chars_per_token: float = 4.0


#: GPT-6's long-context step: over 272K input tokens, 2x input and 1.5x output.
_GPT6_LONG_CONTEXT = 272_000

#: Measured tokenizer densities (see :class:`ModelSpec`).
_GPT6_CHARS_PER_TOKEN = 3.3
_CLAUDE55_CHARS_PER_TOKEN = 2.2

REGISTRY: dict[tuple[Family, Size], ModelSpec] = {
    ("openai", "small"): ModelSpec(
        family="openai",
        size="small",
        model_id="global.openai.gpt-6-luna",
        pricing_in=0.10,
        pricing_out=0.50,
        long_context_threshold=_GPT6_LONG_CONTEXT,
        long_pricing_in=0.20,
        long_pricing_out=0.75,
        chars_per_token=_GPT6_CHARS_PER_TOKEN,
    ),
    ("openai", "medium"): ModelSpec(
        family="openai",
        size="medium",
        model_id="global.openai.gpt-6.1-sol",
        pricing_in=2.00,
        pricing_out=10.00,
        long_context_threshold=_GPT6_LONG_CONTEXT,
        long_pricing_in=4.00,
        long_pricing_out=15.00,
        chars_per_token=_GPT6_CHARS_PER_TOKEN,
    ),
    ("openai", "large"): ModelSpec(
        family="openai",
        size="large",
        model_id="global.openai.gpt-6-astra",
        pricing_in=10.00,
        pricing_out=50.00,
        long_context_threshold=_GPT6_LONG_CONTEXT,
        long_pricing_in=20.00,
        long_pricing_out=75.00,
        chars_per_token=_GPT6_CHARS_PER_TOKEN,
    ),
    ("anthropic", "small"): ModelSpec(
        family="anthropic",
        size="small",
        model_id="global.anthropic.claude-haiku-5-5",
        pricing_in=0.10,
        pricing_out=0.50,
        supports_strict_json=False,
        chars_per_token=_CLAUDE55_CHARS_PER_TOKEN,
        long_context_threshold=100_000,
        long_pricing_in=0.50,
        long_pricing_out=2.50,
    ),
    ("anthropic", "medium"): ModelSpec(
        family="anthropic",
        size="medium",
        model_id="global.anthropic.claude-sonnet-5-5",
        pricing_in=2.00,
        pricing_out=10.00,
        supports_strict_json=False,
        chars_per_token=_CLAUDE55_CHARS_PER_TOKEN,
    ),
    ("anthropic", "large"): ModelSpec(
        family="anthropic",
        size="large",
        model_id="global.anthropic.claude-opus-5-5",
        pricing_in=4.00,
        pricing_out=20.00,
        supports_strict_json=False,
        chars_per_token=_CLAUDE55_CHARS_PER_TOKEN,
    ),
}


def resolve(size: Size, family: Family = DEFAULT_FAMILY) -> ModelSpec:
    """Return the :class:`ModelSpec` behind a (family, size) alias.

    Default family is openai (the operator's choice, CONTRACT-V2); anthropic
    runs through its own adapter when ``ATIF_SQL_LLM_FAMILY=anthropic``.
    """
    try:
        return REGISTRY[(family, size)]
    except KeyError:  # pragma: no cover — Literal types make this unreachable from typed code
        msg = f"no model registered for family={family!r} size={size!r}"
        raise KeyError(msg) from None


def is_long_context(spec: ModelSpec, input_tokens: int) -> bool:
    """True when one request of ``input_tokens`` bills at the long-context rate."""
    return spec.long_context_threshold is not None and input_tokens > spec.long_context_threshold


def estimate_cost(
    spec: ModelSpec,
    *,
    input_tokens: int,
    output_tokens: int,
    long_input_tokens: int = 0,
    long_output_tokens: int = 0,
) -> float | None:
    """USD estimate for one call (or an accumulated pipeline) on ``spec``.

    ``long_input_tokens`` / ``long_output_tokens`` are the part of the totals
    that came from calls over the spec's long-context threshold
    (:func:`is_long_context`); they bill at the long rates and the rest at
    the standard rates. A spec without long rates bills them at the standard
    rates.

    Returns ``None`` when either price is unknown — callers must render
    that as "pricing unavailable", never as $0. Cache-read discounts are
    deliberately NOT modeled here: reasoning tokens bill as output (already
    inside ``output_tokens``) and cached input still bills at a nonzero rate,
    so this is an upper bound suitable for the dry-run cost guard.
    """
    if spec.pricing_in is None or spec.pricing_out is None:
        return None
    long_in_rate = spec.long_pricing_in if spec.long_pricing_in is not None else spec.pricing_in
    long_out_rate = spec.long_pricing_out if spec.long_pricing_out is not None else spec.pricing_out
    standard = (input_tokens - long_input_tokens) * spec.pricing_in + (
        output_tokens - long_output_tokens
    ) * spec.pricing_out
    long = long_input_tokens * long_in_rate + long_output_tokens * long_out_rate
    return (standard + long) / 1_000_000


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
    "is_long_context",
    "resolve",
]
