# SPDX-License-Identifier: Apache-2.0

"""LlmSettings: env prefix, defaults, per-pipeline size overrides."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atif_models.domain.registry import REGISTRY
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

    def test_perceived_size_env_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATIF_SQL_LLM_SIZE_PERCEIVED", "large")
        settings = _settings()
        assert settings.spec_for("perceived").model_id == "global.openai.gpt-6-astra"

    def test_unknown_pipeline_raises_keyerror(self):
        settings = _settings()
        with pytest.raises(KeyError, match="unknown pipeline"):
            settings.size_for("embed")
