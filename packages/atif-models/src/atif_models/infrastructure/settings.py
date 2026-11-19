# SPDX-License-Identifier: Apache-2.0

"""Env-driven LLM settings for atif-models (CONTRACT-V2 §Model registry).

Pydantic v2 ``BaseSettings`` under the workspace-wide ``ATIF_SQL_`` prefix.
Carries the family/region/concurrency knobs plus the per-pipeline size
overrides — ``ATIF_SQL_LLM_SIZE_CLASSIFY`` and friends. Reading process env
is I/O, so this lives in ``infrastructure``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from atif_models.domain.registry import (
    DEFAULT_FAMILY,
    Family,
    ModelSpec,
    ReasoningEffort,
    Size,
    resolve,
    resolve_model,
)

#: Families with a wired provider adapter
#: (:func:`atif_models.infrastructure.providers.build_structured_provider`).
#: A family the registry knows but no adapter serves would send another
#: family's body shape to its model ids, a Bedrock 400 on every call, so
#: settings load refuses it up front.
RUNNABLE_FAMILIES: frozenset[Family] = frozenset({"openai", "anthropic"})


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
    # conflicts is the hardest judgment task (large), friction is a
    # per-message enum (small). classify moved to small on 2026-09-27 when
    # its schema shrank to work category + goal; the trajectory pipeline (and
    # its size) left the same day.
    llm_size_classify: Size = "small"
    llm_size_conflicts: Size = "large"
    llm_size_friction: Size = "small"
    # perceived judges user-visible evidence over full transcripts — medium.
    # Env: ATIF_SQL_LLM_SIZE_PERCEIVED.
    llm_size_perceived: Size = "medium"
    # narrate describes one window per call and makes many calls per session,
    # so it runs on the high-volume tier. Env: ATIF_SQL_LLM_SIZE_NARRATE.
    llm_size_narrate: Size = "small"
    # narrate can also be pointed at one model by name, and at a reasoning
    # effort of its own, without moving any other pipeline: the model wins
    # over the size alias (and brings its own family), and the effort
    # replaces the registry's default for narrate's calls only. Names are
    # the ones :func:`atif_models.domain.registry.resolve_model` accepts
    # (``gpt-6.1-terra``, ``gpt-6-luna``, ...). Env:
    # ATIF_SQL_LLM_MODEL_NARRATE / ATIF_SQL_LLM_EFFORT_NARRATE.
    llm_model_narrate: str | None = None
    llm_effort_narrate: ReasoningEffort | None = None
    # review judges whether the agent fell short, from narrations another
    # model wrote. It names its model so that it defaults to a different
    # vendor's than narrate's default: a reviewer from the narrator's own
    # family tends to share its blind spots. An empty value falls back to the
    # size alias in the configured family. Env: ATIF_SQL_LLM_MODEL_REVIEW /
    # ATIF_SQL_LLM_EFFORT_REVIEW / ATIF_SQL_LLM_SIZE_REVIEW.
    llm_size_review: Size = "medium"
    llm_model_review: str | None = "claude-sonnet-5-5"
    llm_effort_review: ReasoningEffort | None = None

    @field_validator("llm_model_narrate", "llm_model_review")
    @classmethod
    def _model_must_be_known(cls, value: str | None) -> str | None:
        """Refuse a model name the registry can't resolve, with the names it can."""
        if not value:
            return None
        try:
            resolve_model(value)
        except KeyError as exc:
            raise ValueError(str(exc.args[0])) from None
        return value

    def size_for(self, pipeline: str) -> Size:
        """The configured size alias for ``pipeline``.

        Pipelines: classify / conflicts / friction / perceived / narrate / review.
        """
        try:
            size = getattr(self, f"llm_size_{pipeline}")
        except AttributeError:
            msg = f"unknown pipeline {pipeline!r} — no llm_size_{pipeline} setting"
            raise KeyError(msg) from None
        return cast("Size", size)

    def spec_for(self, pipeline: str) -> ModelSpec:
        """Resolve ``pipeline`` to its :class:`~atif_models.domain.registry.ModelSpec`.

        The pipeline's size alias in the configured family, unless the
        pipeline has a ``llm_model_<pipeline>`` setting that names a model;
        a ``llm_effort_<pipeline>`` setting then replaces the spec's
        reasoning effort. Narrate and review have both.
        """
        name = getattr(self, f"llm_model_{pipeline}", None)
        spec = resolve_model(name) if name else resolve(self.size_for(pipeline), self.llm_family)
        effort = getattr(self, f"llm_effort_{pipeline}", None)
        return replace(spec, reasoning_effort=effort) if effort else spec


__all__ = ["LlmSettings"]
