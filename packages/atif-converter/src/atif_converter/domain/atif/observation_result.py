# SPDX-License-Identifier: Apache-2.0
# Vendored from harbor 0.23.0, src/harbor/models/trajectories/observation_result.py
# (Apache-2.0, Copyright the Harbor authors). Byte for byte upstream apart
# from these header lines and the import path, which reads
# atif_converter.domain.atif where upstream reads harbor.models.trajectories.
"""Observation result model for ATIF trajectories."""

from typing import Any

from pydantic import BaseModel, Field

from atif_converter.domain.atif.content import ContentPart
from atif_converter.domain.atif.subagent_trajectory_ref import SubagentTrajectoryRef


class ObservationResult(BaseModel):
    """A single result within an observation."""

    source_call_id: str | None = Field(
        default=None,
        description=(
            "The `tool_call_id` from the _tool_calls_ array in _StepObject_ that this "
            "result corresponds to. If null or omitted, the result comes from an "
            "action that doesn't use the standard tool calling format (e.g., agent "
            "actions without tool calls or system-initiated operations)."
        ),
    )
    content: str | list[ContentPart] | None = Field(
        default=None,
        description=(
            "The output or result from the tool execution. String for text-only "
            "content, or array of ContentPart for multimodal content (added in ATIF-v1.6)."
        ),
    )
    subagent_trajectory_ref: list[SubagentTrajectoryRef] | None = Field(
        default=None,
        description="Array of references to delegated subagent trajectories",
    )
    extra: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Custom observation-result-level metadata (e.g., confidence score, "
            "retrieval score, source document ID). Added in ATIF-v1.7."
        ),
    )

    model_config = {"extra": "forbid"}
