# SPDX-License-Identifier: Apache-2.0
# Vendored from harbor 0.24.0, src/harbor/models/trajectories/observation.py
# (Apache-2.0, Copyright the Harbor authors). Byte for byte upstream apart
# from these header lines and the import path, which reads
# atif_converter.domain.atif where upstream reads harbor.models.trajectories.
"""Observation model for ATIF trajectories."""

from pydantic import BaseModel, Field

from atif_converter.domain.atif.observation_result import ObservationResult


class Observation(BaseModel):
    """Environment feedback/result after actions or system events."""

    results: list[ObservationResult] = Field(
        default=...,
        description="Array of result objects from tool calls or actions",
    )

    model_config = {"extra": "forbid"}
