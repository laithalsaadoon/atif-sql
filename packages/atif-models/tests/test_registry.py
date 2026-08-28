# SPDX-License-Identifier: Apache-2.0

"""Registry resolution, defaults, and cost estimation (CONTRACT-V2 §Model registry)."""

from __future__ import annotations

import pytest

from atif_models.domain.registry import (
    DEFAULT_FAMILY,
    DEFAULT_MAX_COMPLETION_TOKENS,
    DEFAULT_REASONING_EFFORT,
    REGISTRY,
    ModelSpec,
    Size,
    estimate_cost,
    resolve,
)


class TestResolve:
    def test_default_family_is_openai(self):
        assert DEFAULT_FAMILY == "openai"
        assert resolve("medium").model_id == "global.openai.gpt-5.6-terra"

    @pytest.mark.parametrize(
        ("size", "model_id"),
        [
            ("small", "global.openai.gpt-5.6-luna"),
            ("medium", "global.openai.gpt-5.6-terra"),
            ("large", "global.openai.gpt-5.6-sol"),
        ],
    )
    def test_openai_sizes(self, size: Size, model_id: str):
        spec = resolve(size, "openai")
        assert spec.model_id == model_id
        assert spec.family == "openai"
        assert spec.size == size
        assert spec.supports_strict_json is True

    @pytest.mark.parametrize(
        ("size", "model_id"),
        [
            ("small", "global.anthropic.claude-haiku-4-5"),
            ("medium", "global.anthropic.claude-sonnet-5"),
            ("large", "global.anthropic.claude-opus-5"),
        ],
    )
    def test_anthropic_sizes(self, size: Size, model_id: str):
        spec = resolve(size, "anthropic")
        assert spec.model_id == model_id
        assert spec.supports_strict_json is False

    def test_defaults(self):
        spec = resolve("large")
        assert spec.reasoning_effort == "high"
        assert spec.reasoning_effort == DEFAULT_REASONING_EFFORT
        assert spec.max_completion_tokens == 32_000
        assert spec.max_completion_tokens == DEFAULT_MAX_COMPLETION_TOKENS

    def test_registry_is_total_over_family_x_size(self):
        assert len(REGISTRY) == 6
        assert {k[0] for k in REGISTRY} == {"openai", "anthropic"}
        assert {k[1] for k in REGISTRY} == {"small", "medium", "large"}

    def test_specs_are_frozen(self):
        spec = resolve("small")
        with pytest.raises(AttributeError):
            # The refused assignment IS the assertion: ``ModelSpec`` is a frozen
            # slots dataclass, so a checker rejecting the write statically and the
            # runtime raising ``AttributeError`` are the same invariant.
            spec.model_id = "something-else"  # ty: ignore[invalid-assignment]  # pyright: ignore[reportAttributeAccessIssue]


class TestEstimateCost:
    def test_known_pricing(self):
        # terra: $2/M in, $12/M out.
        spec = resolve("medium")
        cost = estimate_cost(spec, input_tokens=1_000_000, output_tokens=500_000)
        assert cost == pytest.approx(2.0 + 6.0)

    def test_zero_tokens_is_zero_dollars(self):
        assert estimate_cost(resolve("large"), input_tokens=0, output_tokens=0) == 0.0

    def test_none_pricing_yields_none_not_zero(self):
        spec = ModelSpec(
            family="openai",
            size="small",
            model_id="example.unpriced",
            pricing_in=None,
            pricing_out=None,
        )
        assert estimate_cost(spec, input_tokens=1000, output_tokens=1000) is None

    def test_partial_none_pricing_yields_none(self):
        spec = ModelSpec(
            family="openai",
            size="small",
            model_id="example.half-priced",
            pricing_in=1.0,
            pricing_out=None,
        )
        assert estimate_cost(spec, input_tokens=1000, output_tokens=0) is None
