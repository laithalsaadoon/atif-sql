# SPDX-License-Identifier: Apache-2.0

"""Registry resolution, defaults, and cost estimation (CONTRACT-V2 §Model registry)."""

from __future__ import annotations

import pytest

from atif_models.domain.registry import (
    DEFAULT_FAMILY,
    DEFAULT_MAX_COMPLETION_TOKENS,
    DEFAULT_REASONING_EFFORT,
    NAMED_MODELS,
    REGISTRY,
    Family,
    ModelSpec,
    Size,
    estimate_cost,
    is_long_context,
    model_names,
    resolve,
    resolve_model,
    with_full_output,
)


class TestResolve:
    def test_default_family_is_openai(self):
        assert DEFAULT_FAMILY == "openai"
        assert resolve("medium").model_id == "global.openai.gpt-6.1-sol"

    @pytest.mark.parametrize(
        ("size", "model_id"),
        [
            ("small", "global.openai.gpt-6-luna"),
            ("medium", "global.openai.gpt-6.1-sol"),
            ("large", "global.openai.gpt-6-astra"),
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
            ("small", "global.anthropic.claude-haiku-5-5"),
            ("medium", "global.anthropic.claude-sonnet-5-5"),
            ("large", "global.anthropic.claude-opus-5-5"),
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
        # gpt-6.1-sol: $2/M in, $10/M out.
        spec = resolve("medium")
        cost = estimate_cost(spec, input_tokens=1_000_000, output_tokens=500_000)
        assert cost == pytest.approx(2.0 + 5.0)

    def test_long_context_share_bills_at_the_long_rates(self):
        # haiku-5-5: $0.10/$0.50 standard, $0.50/$2.50 for a request over 100K input tokens.
        spec = resolve("small", "anthropic")
        cost = estimate_cost(
            spec,
            input_tokens=1_200_000,
            output_tokens=20_000,
            long_input_tokens=1_000_000,
            long_output_tokens=10_000,
        )
        standard = 200_000 * 0.10 + 10_000 * 0.50
        long = 1_000_000 * 0.50 + 10_000 * 2.50
        assert cost == pytest.approx((standard + long) / 1_000_000)

    def test_a_spec_without_long_rates_bills_the_long_share_at_standard(self):
        spec = resolve("large", "anthropic")  # opus 5.5 prices the full window flat
        assert spec.long_context_threshold is None
        flat = estimate_cost(spec, input_tokens=500_000, output_tokens=1_000)
        split = estimate_cost(
            spec, input_tokens=500_000, output_tokens=1_000, long_input_tokens=400_000
        )
        assert flat == split

    @pytest.mark.parametrize(
        ("family", "size", "threshold"),
        [
            ("openai", "small", 272_000),
            ("openai", "medium", 272_000),
            ("openai", "large", 272_000),
            ("anthropic", "small", 100_000),
            ("anthropic", "medium", None),
            ("anthropic", "large", None),
        ],
    )
    def test_long_context_thresholds(self, family: Family, size: Size, threshold: int | None):
        spec = resolve(size, family)
        assert spec.long_context_threshold == threshold
        if threshold is not None:
            assert is_long_context(spec, threshold + 1)
            assert not is_long_context(spec, threshold)

    @pytest.mark.parametrize("size", ["small", "medium", "large"])
    def test_claude_tokenizer_is_denser_than_gpt(self, size: Size):
        """Claude 5.5 spends about 1.5x GPT-6's tokens on the same transcript."""
        gpt, claude = resolve(size, "openai"), resolve(size, "anthropic")
        assert gpt.chars_per_token == 3.3
        assert claude.chars_per_token == 2.2

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


class TestOutputLimit:
    """Each model's own output limit, and the spec that sends it as the cap."""

    @pytest.mark.parametrize(
        ("family", "size", "limit"),
        [
            ("openai", "small", 128_000),
            ("openai", "medium", 131_072),
            ("openai", "large", 128_000),
            ("anthropic", "small", 128_000),
            ("anthropic", "medium", 128_000),
            ("anthropic", "large", 128_000),
        ],
    )
    def test_every_registry_model_records_its_limit(self, family: Family, size: Size, limit: int):
        assert resolve(size, family).max_output_tokens == limit

    def test_the_default_cap_stays_below_the_limit(self):
        """The judgment pipelines keep the shared default; the limit is opt-in."""
        for spec in REGISTRY.values():
            assert spec.max_completion_tokens == DEFAULT_MAX_COMPLETION_TOKENS
            assert spec.max_output_tokens is not None
            assert spec.max_output_tokens > spec.max_completion_tokens

    def test_with_full_output_raises_the_cap_to_the_models_limit(self):
        for spec in REGISTRY.values():
            full = with_full_output(spec)
            assert full.max_completion_tokens == spec.max_output_tokens
            # Nothing else about the model moves, and the registry entry is untouched.
            assert (full.model_id, full.reasoning_effort, full.pricing_in) == (
                spec.model_id,
                spec.reasoning_effort,
                spec.pricing_in,
            )
            assert spec.max_completion_tokens == DEFAULT_MAX_COMPLETION_TOKENS

    def test_a_spec_with_no_recorded_limit_is_left_alone(self):
        spec = ModelSpec(
            family="openai", size="small", model_id="x", pricing_in=None, pricing_out=None
        )
        assert with_full_output(spec) is spec


class TestNamedModels:
    def test_terra_is_named_and_priced(self):
        spec = resolve_model("gpt-6.1-terra")
        assert spec is NAMED_MODELS["gpt-6.1-terra"]
        assert spec.model_id == "global.openai.gpt-6.1-terra"
        assert (spec.family, spec.pricing_in, spec.pricing_out) == ("openai", 1.0, 5.0)
        assert (spec.long_context_threshold, spec.long_pricing_in, spec.long_pricing_out) == (
            272_000,
            2.0,
            7.5,
        )
        assert spec.max_output_tokens == 128_000
        assert with_full_output(spec).max_completion_tokens == 128_000

    def test_a_size_aliased_model_resolves_by_its_own_name_too(self):
        assert resolve_model("gpt-6-luna") is resolve("small")
        assert resolve_model("claude-opus-5-5") is resolve("large", "anthropic")

    def test_no_size_alias_points_at_a_named_model(self):
        """Naming a model is opt-in: the aliases every pipeline defaults to don't reach it."""
        aliased = {spec.model_id for spec in REGISTRY.values()}
        assert not aliased & {spec.model_id for spec in NAMED_MODELS.values()}

    def test_an_unknown_name_lists_the_known_ones(self):
        with pytest.raises(KeyError, match=r"gpt-6\.1-terra"):
            resolve_model("gpt-7")
        assert "gpt-6.1-terra" in model_names()
        assert "gpt-6-luna" in model_names()
