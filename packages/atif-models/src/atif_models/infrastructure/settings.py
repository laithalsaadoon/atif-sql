# SPDX-License-Identifier: Apache-2.0

"""Env-driven LLM settings for atif-models (CONTRACT-V2 §Model registry).

Pydantic v2 ``BaseSettings`` under the workspace-wide ``ATIF_SQL_`` prefix.
Carries the family/region/concurrency knobs plus the per-pipeline size
overrides — ``ATIF_SQL_LLM_SIZE_CLASSIFY`` and friends. Reading process env
is I/O, so this lives in ``infrastructure``.
"""

from __future__ import annotations

from typing import cast

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from atif_models.domain.registry import DEFAULT_FAMILY, Family, ModelSpec, Size, resolve

#: Families with a wired provider adapter. The registry knows every family's
#: model ids, but only these can actually be called: an unwired family
#: resolves a model id and then sends it the OpenAI chat-completions body
#: (``response_format`` / ``reasoning_effort``), which is a Bedrock 400 on
#: every call. Rejecting at settings load turns that into a startup refusal.
RUNNABLE_FAMILIES: frozenset[Family] = frozenset({"openai"})


class LlmSettings(BaseSettings):
    """Env-driven model selection for the LLM analytics pipelines."""

    model_config = SettingsConfigDict(
        env_prefix="ATIF_SQL_",
        env_file=".env",
        extra="ignore",
    )

    llm_family: Family = DEFAULT_FAMILY
    llm_region: str = "us-east-1"
    llm_concurrency: int = 16

    @field_validator("llm_family")
    @classmethod
    def _family_must_be_runnable(cls, value: Family) -> Family:
        """Refuse a family that has no provider adapter behind it."""
        if value in RUNNABLE_FAMILIES:
            return value
        runnable = ", ".join(sorted(RUNNABLE_FAMILIES))
        msg = (
            f"llm_family={value!r} has no provider adapter, so every pipeline call "
            f"would send an OpenAI chat-completions body to a {value} model id and "
            f"get a Bedrock 400. Runnable families: {runnable}. "
            f"Unset ATIF_SQL_LLM_FAMILY or set it to one of those."
        )
        raise ValueError(msg)

    # Per-pipeline size assignments (CONTRACT-V2 §Pipeline size assignments):
    # conflicts is the hardest judgment task (sol), friction is a
    # per-message enum (luna).
    llm_size_classify: Size = "medium"
    llm_size_trajectory: Size = "medium"
    llm_size_conflicts: Size = "large"
    llm_size_friction: Size = "small"
    # perceived judges user-visible evidence over full transcripts — medium
    # (terra), like classify/trajectory. Env: ATIF_SQL_LLM_SIZE_PERCEIVED.
    llm_size_perceived: Size = "medium"

    def size_for(self, pipeline: str) -> Size:
        """The configured size alias for ``pipeline``.

        Pipelines: classify / trajectory / conflicts / friction / perceived.
        """
        try:
            size = getattr(self, f"llm_size_{pipeline}")
        except AttributeError:
            msg = f"unknown pipeline {pipeline!r} — no llm_size_{pipeline} setting"
            raise KeyError(msg) from None
        return cast("Size", size)

    def spec_for(self, pipeline: str) -> ModelSpec:
        """Resolve ``pipeline`` to its :class:`~atif_models.domain.registry.ModelSpec`."""
        return resolve(self.size_for(pipeline), self.llm_family)


__all__ = ["LlmSettings"]
