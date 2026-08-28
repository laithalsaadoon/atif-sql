# SPDX-License-Identifier: Apache-2.0

"""Schema/enum pins — expected values HARDCODED, never read from the models.

CONTRACT-V2 freezes these enum sets: autonomy tiers, work categories, 6
transition_kinds, 4 conflict kinds, 7 friction labels. A hardcoded expectation
is the point — reading the values off the model under test would pass no
matter how far the enums drifted, and a drifted enum silently unbinds the
analytics views keyed on it.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import BaseModel, ValidationError

from atif_analytics.domain.models import (
    ConflictPair,
    ConflictsResult,
    PerceivedError,
    PerceivedErrorsResult,
    SessionClassification,
    TrajectoryWindow,
    UserFrictionSignal,
)
from atif_analytics.domain.trajectory import TRANSITION_KINDS


def _literal_values(model: type[BaseModel], field: str) -> set[str]:
    annotation = model.model_fields[field].annotation
    values: set[str] = set()
    for arg in get_args(annotation):
        if isinstance(arg, str):
            values.add(arg)
        else:  # Literal nested in Optional
            values.update(a for a in get_args(arg) if isinstance(a, str))
    return values


def test_autonomy_tier_values_exact() -> None:
    assert _literal_values(SessionClassification, "autonomy_tier") == {
        "manual",
        "assisted",
        "autonomous",
    }


def test_work_category_values_exact() -> None:
    assert _literal_values(SessionClassification, "work_category") == {
        "sde",
        "admin",
        "strategy_business",
        "events",
        "thought_leadership",
        "other",
    }


def test_success_values_exact() -> None:
    assert _literal_values(SessionClassification, "success") == {
        "success",
        "partial",
        "failure",
        "unknown",
    }


def test_transition_kinds_are_exactly_six() -> None:
    assert TRANSITION_KINDS == (
        "frustration_spike",
        "resolution",
        "reset",
        "drift",
        "clarification",
        "none",
    )
    assert _literal_values(TrajectoryWindow, "transition_kind") == set(TRANSITION_KINDS)


def test_sentiment_values_exact() -> None:
    assert _literal_values(TrajectoryWindow, "curr_sentiment") == {
        "negative",
        "neutral",
        "positive",
    }
    assert _literal_values(TrajectoryWindow, "prev_sentiment") == {
        "negative",
        "neutral",
        "positive",
    }


def test_conflict_kinds_exactly_four() -> None:
    assert _literal_values(ConflictPair, "conflict_kind") == {
        "disagreement",
        "correction",
        "reversal",
        "impasse",
    }


def test_severities_exactly_three() -> None:
    assert _literal_values(ConflictPair, "severity") == {"low", "medium", "high"}


def test_friction_labels_exactly_seven() -> None:
    assert _literal_values(UserFrictionSignal, "label") == {
        "status_ping",
        "unmet_expectation",
        "confusion",
        "interruption",
        "correction",
        "frustration",
        "none",
    }


def test_goal_length_bounds() -> None:
    with pytest.raises(ValidationError):
        SessionClassification(
            autonomy_tier="manual",
            work_category="sde",
            success="success",
            goal="x" * 281,
            confidence=0.5,
        )


def test_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        UserFrictionSignal(label="none", rationale="r", confidence=1.5)


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        SessionClassification.model_validate(
            {
                "autonomy_tier": "manual",
                "work_category": "sde",
                "success": "success",
                "goal": "g",
                "confidence": 0.5,
                "bonus": True,
            }
        )


def test_conflicts_result_defaults_empty() -> None:
    assert ConflictsResult().conflicts == []


# ---------------------------------------------------------------------------
# perceived (pinned against the LangSmith Perceived Error definition)
# ---------------------------------------------------------------------------


def _perceived(**overrides: object) -> PerceivedError:
    base: dict[str, object] = {
        "turn_uuid": "u-1",
        "signal": "correction",
        "severity": "minor",
        "evidence": "no, I meant X",
        "agent_error_summary": "Agent edited the wrong file.",
        "confidence": 0.8,
    }
    base.update(overrides)
    return PerceivedError.model_validate(base)


def test_perceived_signals_exactly_seven() -> None:
    """3 explicit + 4 inferred, LangSmith's published evidence categories."""
    assert _literal_values(PerceivedError, "signal") == {
        "correction",
        "repeated_request",
        "rejected_action",
        "contradictory_response",
        "acknowledged_mistake",
        "persistent_misunderstanding",
        "unresolved_outcome",
    }


def test_perceived_severities_exactly_three() -> None:
    assert _literal_values(PerceivedError, "severity") == {"minor", "moderate", "major"}


def test_perceived_text_bounds() -> None:
    with pytest.raises(ValidationError):
        _perceived(evidence="")
    with pytest.raises(ValidationError):
        _perceived(evidence="x" * 281)
    with pytest.raises(ValidationError):
        _perceived(agent_error_summary="")
    with pytest.raises(ValidationError):
        _perceived(agent_error_summary="x" * 281)
    assert _perceived(evidence="x" * 280).evidence == "x" * 280


def test_perceived_confidence_and_uuid_bounds() -> None:
    with pytest.raises(ValidationError):
        _perceived(confidence=1.5)
    with pytest.raises(ValidationError):
        _perceived(turn_uuid="")
    with pytest.raises(ValidationError):
        _perceived(turn_uuid="x" * 65)


def test_perceived_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        PerceivedError.model_validate(
            {
                "turn_uuid": "u-1",
                "signal": "correction",
                "severity": "minor",
                "evidence": "e",
                "agent_error_summary": "s",
                "confidence": 0.5,
                "bonus": True,
            }
        )


def test_perceived_result_defaults_empty() -> None:
    """Empty errors list = clean session (writes zero rows downstream)."""
    assert PerceivedErrorsResult().errors == []
