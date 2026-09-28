# SPDX-License-Identifier: Apache-2.0
# Vendored from harbor 0.23.0, src/harbor/models/trajectories/tool_call.py
# (Apache-2.0, Copyright the Harbor authors). Byte for byte upstream apart
# from these header lines and the import path, which reads
# atif_converter.domain.atif where upstream reads harbor.models.trajectories.
"""Tool call model for ATIF trajectories."""

from typing import Any

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """A tool call within a step."""

    tool_call_id: str = Field(
        default=...,
        description="Unique identifier for this specific tool call",
    )
    function_name: str = Field(
        default=...,
        description="The name of the function or tool being invoked",
    )
    arguments: dict[str, Any] = Field(
        default=...,
        description="Arguments passed to the function (can be empty dict)",
    )
    extra: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Custom tool-call-level metadata (e.g., timeout, retry count, tool version). "
            "Added in ATIF-v1.7."
        ),
    )

    model_config = {"extra": "forbid"}
