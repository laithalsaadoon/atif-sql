# SPDX-License-Identifier: Apache-2.0

"""Prompt-port guards: the documented adaptations and nothing else."""

from __future__ import annotations

from atif_analytics.application.prompts import (
    CLASSIFY_SYSTEM_PROMPT,
    CONFLICTS_SYSTEM_PROMPT,
    PERCEIVED_SYSTEM_PROMPT,
    USER_FRICTION_SYSTEM_PROMPT,
)
from atif_analytics.domain.authorship import AUTHOR_VALUES
from atif_analytics.domain.models import SessionClassification

ALL_PROMPTS = (
    CLASSIFY_SYSTEM_PROMPT,
    CONFLICTS_SYSTEM_PROMPT,
    USER_FRICTION_SYSTEM_PROMPT,
    PERCEIVED_SYSTEM_PROMPT,
)


def test_no_bedrock_output_config_phrasing_survives() -> None:
    """The two documented adaptations: output_config never reaches a GPT model."""
    for prompt in ALL_PROMPTS:
        assert "output_config" not in prompt
        assert "Bedrock's" not in prompt


def test_adapted_strict_mode_phrases_present() -> None:
    assert "The response_format json_schema strict mode" in CLASSIFY_SYSTEM_PROMPT


def test_appendix_on_all_four_prompts() -> None:
    """The classifier appendix attaches to classify/conflicts/friction/perceived."""
    for prompt in ALL_PROMPTS:
        assert "<quality_bar>" in prompt
        assert "<output_rules>" in prompt


def test_transcript_prompts_name_every_machine_author() -> None:
    """The three transcript readers are told which labels are not the user.

    The renderer puts a machine-written user-role step under its author
    label; a prompt that did not name a label would leave the model to guess
    whether ``[stop_hook ...]`` is the user speaking.
    """
    machine = [a for a in AUTHOR_VALUES if a != "human"]
    for prompt in (CLASSIFY_SYSTEM_PROMPT, CONFLICTS_SYSTEM_PROMPT, PERCEIVED_SYSTEM_PROMPT):
        assert "<transcript_roles>" in prompt
        for author in machine:
            assert f"[{author} ...]" in prompt
    # Friction reads one human message at a time and never sees the labels.
    assert "<transcript_roles>" not in USER_FRICTION_SYSTEM_PROMPT


def test_classify_prompt_asks_only_for_schema_fields() -> None:
    """The dropped labels are gone from the prompt as well as the schema."""
    assert set(SessionClassification.model_fields) == {"work_category", "goal", "confidence"}
    for dropped in ("autonomy_tier", "success=", "Success semantics", "Pacing patterns"):
        assert dropped not in CLASSIFY_SYSTEM_PROMPT


def test_claude_code_data_descriptions_kept() -> None:
    """The corpus IS Claude Code transcripts — that phrasing must survive."""
    for prompt in ALL_PROMPTS:
        assert "Claude Code" in prompt


def test_prompt_substance_anchors() -> None:
    """Spot-check the prompt lines that steer each classifier's output."""
    assert "Don't infer goals from agent actions." in CLASSIFY_SYSTEM_PROMPT
    assert "Never invent turn UUIDs." in CONFLICTS_SYSTEM_PROMPT
    assert "USE THIS FOR" not in USER_FRICTION_SYSTEM_PROMPT  # schema-side text, not prompt
    assert "THIS IS THE MAJORITY CLASS" in USER_FRICTION_SYSTEM_PROMPT
    assert "[Request interrupted by user for tool use]" in USER_FRICTION_SYSTEM_PROMPT


def test_perceived_prompt_structure_and_substance() -> None:
    """House-style sections + the lines carrying the LangSmith definition."""
    for tag in ("<instructions>", "<context>", "<calibration>", "<examples>", "<anti_patterns>"):
        assert tag in PERCEIVED_SYSTEM_PROMPT
    # All seven signals appear as enum values in the instructions.
    for signal in (
        "correction",
        "repeated_request",
        "rejected_action",
        "contradictory_response",
        "acknowledged_mistake",
        "persistent_misunderstanding",
        "unresolved_outcome",
    ):
        assert signal in PERCEIVED_SYSTEM_PROMPT
    for severity in ("minor", "moderate", "major"):
        assert severity in PERCEIVED_SYSTEM_PROMPT
    # Perception, not objective correctness — the definitional line.
    assert "You judge PERCEPTION, not objective correctness." in PERCEIVED_SYSTEM_PROMPT
    # Anti-patterns the task pinned.
    assert "Don't count the user CHANGING THEIR MIND." in PERCEIVED_SYSTEM_PROMPT
    assert "Don't count exploratory iteration as repeated_request." in PERCEIVED_SYSTEM_PROMPT
    assert "Don't count system or hook output as user perception." in PERCEIVED_SYSTEM_PROMPT
    # Quoted-content additions (documented in the port-adaptation ledger):
    # pasted reviews/transcripts/logs are not THIS session's user perceiving,
    # and an orchestrator re-issue / infra retry is not repeated_request.
    assert "Don't count QUOTED or PASTED text inside a turn" in PERCEIVED_SYSTEM_PROMPT
    assert "judge only perceptions directed at THIS session's" in PERCEIVED_SYSTEM_PROMPT
    assert "Don't count an orchestrator re-issuing a task packet" in PERCEIVED_SYSTEM_PROMPT
    assert "infrastructure failure" in PERCEIVED_SYSTEM_PROMPT
    assert "Mild steering is the texture of coding" in PERCEIVED_SYSTEM_PROMPT
    # uuid discipline + the clean-session empty-array example.
    assert "Never invent turn UUIDs" in PERCEIVED_SYSTEM_PROMPT
    assert "errors=[]" in PERCEIVED_SYSTEM_PROMPT
    assert "An empty errors array is valid and common" in PERCEIVED_SYSTEM_PROMPT
