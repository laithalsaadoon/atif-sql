# SPDX-License-Identifier: Apache-2.0

"""LlmSettings: env prefix, defaults, per-pipeline size overrides."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atif_models.domain.registry import REGISTRY, resolve
from atif_models.infrastructure.settings import RUNNABLE_FAMILIES, LlmSettings


def _settings() -> LlmSettings:
    """Construct settings with dotenv loading off, so no test reads a real ``.env``.

    ``_env_file`` is declared on ``pydantic_settings.BaseSettings.__init__``, but
    pydantic's ``@dataclass_transform`` makes a type checker synthesize a fresh
    ``__init__`` from the model fields for every subclass, which shadows it. One
    helper carries the one suppression that costs.
    """
    return LlmSettings(_env_file=None)  # pyright: ignore[reportCallIssue]


class TestDefaults:
    def test_contract_defaults(self, monkeypatch: pytest.MonkeyPatch):
        for var in list(__import__("os").environ):
            if var.startswith("ATIF_SQL_"):
                monkeypatch.delenv(var)
        settings = _settings()
        assert settings.llm_family == "openai"
        assert settings.llm_region == "us-east-1"
        assert settings.llm_concurrency == 16
        assert settings.llm_size_classify == "small"
        assert not hasattr(settings, "llm_size_trajectory")
        assert settings.llm_size_conflicts == "large"
        assert settings.llm_size_friction == "small"
        assert settings.llm_size_perceived == "medium"
        assert settings.llm_size_narrate == "small"
        assert settings.llm_size_review == "medium"
        assert settings.llm_model_review == "claude-sonnet-5-5"


class TestEnvOverrides:
    def test_env_prefix_overrides(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATIF_SQL_LLM_FAMILY", "openai")
        monkeypatch.setenv("ATIF_SQL_LLM_SIZE_CONFLICTS", "medium")
        monkeypatch.setenv("ATIF_SQL_LLM_CONCURRENCY", "4")
        settings = _settings()
        assert settings.llm_family == "openai"
        assert settings.llm_size_conflicts == "medium"
        assert settings.llm_concurrency == 4


class TestRunnableFamilyGate:
    """A family with no provider adapter must be refused at settings load.

    Otherwise it resolves a real model id and then gets sent another family's
    body shape, which 400s on every call at runtime.
    """

    def test_both_families_are_runnable(self):
        assert {"openai", "anthropic"} == RUNNABLE_FAMILIES

    def test_anthropic_family_loads(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATIF_SQL_LLM_FAMILY", "anthropic")
        settings = _settings()
        assert settings.llm_family == "anthropic"
        assert settings.spec_for("conflicts").model_id == "global.anthropic.claude-opus-5-5"

    def test_unwired_family_is_rejected_at_load(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "atif_models.infrastructure.settings.RUNNABLE_FAMILIES", frozenset({"openai"})
        )
        monkeypatch.setenv("ATIF_SQL_LLM_FAMILY", "anthropic")
        with pytest.raises(ValidationError, match="Runnable families: openai"):
            _settings()

    def test_runnable_families_all_exist_in_the_registry(self):
        assert {family for family, _ in REGISTRY} >= RUNNABLE_FAMILIES

    def test_default_family_is_runnable(self):
        assert _settings().llm_family in RUNNABLE_FAMILIES


class TestSpecFor:
    def test_spec_for_resolves_pipeline_to_model(self):
        settings = _settings()
        assert settings.spec_for("conflicts").model_id == "global.openai.gpt-6-astra"
        assert settings.spec_for("friction").model_id == "global.openai.gpt-6-luna"
        assert settings.spec_for("perceived").model_id == "global.openai.gpt-6.1-sol"

    def test_narrate_runs_on_the_small_tier_and_takes_an_override(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        assert _settings().spec_for("narrate").model_id == "global.openai.gpt-6-luna"
        monkeypatch.setenv("ATIF_SQL_LLM_SIZE_NARRATE", "medium")
        assert _settings().spec_for("narrate").model_id == "global.openai.gpt-6.1-sol"

    def test_perceived_size_env_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATIF_SQL_LLM_SIZE_PERCEIVED", "large")
        settings = _settings()
        assert settings.spec_for("perceived").model_id == "global.openai.gpt-6-astra"

    def test_unknown_pipeline_raises_keyerror(self):
        settings = _settings()
        with pytest.raises(KeyError, match="unknown pipeline"):
            settings.size_for("embed")


class TestNarrateModelAndEffort:
    """narrate can name its model and its effort; no other pipeline moves."""

    def test_defaults_are_the_size_alias_and_the_registry_effort(self):
        spec = _settings().spec_for("narrate")
        assert (spec.model_id, spec.reasoning_effort) == ("global.openai.gpt-6-luna", "high")

    def test_a_named_model_wins_over_the_size_alias(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATIF_SQL_LLM_MODEL_NARRATE", "gpt-6.1-terra")
        monkeypatch.setenv("ATIF_SQL_LLM_SIZE_NARRATE", "large")
        settings = _settings()
        spec = settings.spec_for("narrate")
        assert spec.model_id == "global.openai.gpt-6.1-terra"
        assert (spec.pricing_in, spec.pricing_out, spec.max_output_tokens) == (1.0, 5.0, 128_000)
        # Every other pipeline still resolves through its size alias.
        assert settings.spec_for("classify").model_id == "global.openai.gpt-6-luna"
        assert settings.spec_for("perceived").model_id == "global.openai.gpt-6.1-sol"

    def test_a_named_model_brings_its_own_family(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATIF_SQL_LLM_MODEL_NARRATE", "claude-haiku-5-5")
        settings = _settings()
        assert settings.llm_family == "openai"
        assert settings.spec_for("narrate").family == "anthropic"

    def test_the_effort_setting_moves_narrate_only(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATIF_SQL_LLM_EFFORT_NARRATE", "medium")
        settings = _settings()
        assert settings.spec_for("narrate").reasoning_effort == "medium"
        assert settings.spec_for("narrate").model_id == "global.openai.gpt-6-luna"
        assert settings.spec_for("classify").reasoning_effort == "high"
        # The registry entry itself is untouched.
        assert resolve("small").reasoning_effort == "high"

    def test_an_unknown_model_name_fails_at_load_with_the_choices(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ATIF_SQL_LLM_MODEL_NARRATE", "gpt-6.1-tera")
        with pytest.raises(ValueError, match=r"no model named 'gpt-6.1-tera'.*gpt-6.1-terra"):
            _settings()

    def test_an_unknown_effort_fails_at_load(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATIF_SQL_LLM_EFFORT_NARRATE", "extreme")
        with pytest.raises(ValueError, match="llm_effort_narrate"):
            _settings()


class TestReviewModel:
    """review names its model, so it defaults to another vendor's than the narrator's."""

    def test_the_default_reviewer_is_not_from_the_narrators_family(self):
        settings = _settings()
        review, narrate = settings.spec_for("review"), settings.spec_for("narrate")
        assert review.model_id == "global.anthropic.claude-sonnet-5-5"
        assert review.family != narrate.family
        # Never the high-volume tier: a review is a judgment.
        assert review.size != "small"
        # The run's family setting is untouched, and so is every other pipeline.
        assert settings.llm_family == "openai"
        assert settings.spec_for("perceived").model_id == "global.openai.gpt-6.1-sol"

    def test_the_model_and_the_effort_can_be_set_for_review_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ATIF_SQL_LLM_MODEL_REVIEW", "claude-opus-5-5")
        monkeypatch.setenv("ATIF_SQL_LLM_EFFORT_REVIEW", "medium")
        settings = _settings()
        spec = settings.spec_for("review")
        assert (spec.model_id, spec.reasoning_effort) == (
            "global.anthropic.claude-opus-5-5",
            "medium",
        )
        assert settings.spec_for("narrate").reasoning_effort == "high"
        assert settings.spec_for("narrate").model_id == "global.openai.gpt-6-luna"

    def test_an_empty_model_falls_back_to_the_size_alias_in_the_configured_family(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ATIF_SQL_LLM_MODEL_REVIEW", "")
        assert _settings().spec_for("review").model_id == "global.openai.gpt-6.1-sol"
        monkeypatch.setenv("ATIF_SQL_LLM_SIZE_REVIEW", "large")
        assert _settings().spec_for("review").model_id == "global.openai.gpt-6-astra"

    def test_an_unknown_review_model_fails_at_load(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATIF_SQL_LLM_MODEL_REVIEW", "claude-sonet-5-5")
        with pytest.raises(ValueError, match="no model named 'claude-sonet-5-5'"):
            _settings()
