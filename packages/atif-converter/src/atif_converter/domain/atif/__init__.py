# SPDX-License-Identifier: Apache-2.0
# Vendored from harbor 0.23.0, src/harbor/models/trajectories/__init__.py
# (Apache-2.0, Copyright the Harbor authors), with the provenance constants
# below added.

"""Pydantic models for Agent Trajectory Interchange Format (ATIF).

The ATIF data classes and the trajectory validator, vendored from harbor
(https://github.com/laude-institute/harbor) at :data:`UPSTREAM_VERSION` under
its Apache-2.0 license, so atif-sql converts and validates trajectories
without installing harbor and the dependency tree harbor's agent runtime
carries. Every sibling module is upstream's file byte for byte apart from its
header and the import path (``harbor.models.trajectories`` reads
``atif_converter.domain.atif`` here); this ``__init__`` adds only the two
constants.

harbor stays a dev dependency, and the converter's tests hold this copy to it:
``tests/test_vendored_atif.py`` compares each file to the installed upstream
source and round-trips the golden trajectories through both sets of models and
both validators. A harbor bump that changes the models fails there, and the fix
is to re-vendor the changed files and move :data:`UPSTREAM_VERSION`.
"""

from atif_converter.domain.atif.agent import Agent
from atif_converter.domain.atif.content import AudioSource, ContentPart, ImageSource
from atif_converter.domain.atif.final_metrics import FinalMetrics
from atif_converter.domain.atif.metrics import Metrics
from atif_converter.domain.atif.observation import Observation
from atif_converter.domain.atif.observation_result import ObservationResult
from atif_converter.domain.atif.step import Step
from atif_converter.domain.atif.subagent_trajectory_ref import SubagentTrajectoryRef
from atif_converter.domain.atif.tool_call import ToolCall
from atif_converter.domain.atif.trajectory import Trajectory

#: The distribution these files were vendored from.
UPSTREAM_DISTRIBUTION = "harbor"
#: The upstream release the files match; ``meta.json`` records it as ``harbor_version``.
UPSTREAM_VERSION = "0.23.0"

__all__ = [
    "UPSTREAM_DISTRIBUTION",
    "UPSTREAM_VERSION",
    "Agent",
    "AudioSource",
    "ContentPart",
    "FinalMetrics",
    "ImageSource",
    "Metrics",
    "Observation",
    "ObservationResult",
    "Step",
    "SubagentTrajectoryRef",
    "ToolCall",
    "Trajectory",
]
