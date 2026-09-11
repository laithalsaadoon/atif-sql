# SPDX-License-Identifier: Apache-2.0

"""The Claude Code conversion seam: our converter in, a validated ATIF dict out.

harbor is used here for its PUBLIC surface only — ``harbor.utils.trajectory_validator``
(and, in the domain, the ``harbor.models.trajectories`` data classes the
converter builds). The raw-JSONL -> ``Trajectory`` conversion is ours, in
:mod:`atif_converter.domain.claude_code_conversion` via
:mod:`atif_converter.infrastructure.claude_code_converter`, ported from harbor
0.22.0 and held to parity with it by the oracle in this package's tests.

harbor ships no ``py.typed`` marker, so every import from it is untyped; the
import sites carry the ``import-untyped`` ignore for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from atif_converter.domain.errors import (
    ConversionError,
    EmptySessionError,
    InvalidSessionInput,
)


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """Output of one conversion: trajectory dict + validation verdict.

    ``convert_session`` returns the raw harbor output (``edges_lines`` empty);
    ``convert_and_audit`` returns the ENRICHED trajectory plus the ready-to-
    write edges.jsonl lines derived from the raw records.
    """

    trajectory: dict[str, Any]
    validation_errors: tuple[str, ...]
    edges_lines: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        """True when harbor's ``TrajectoryValidator`` accepted the trajectory."""
        return not self.validation_errors


def require_transcript_file(path: Path) -> None:
    """Refuse anything that is not an existing ``.jsonl`` transcript.

    Called at the TOP of each use case, before the fingerprint snapshot: the
    snapshot stats every discovered file, so a path that does not exist reached
    ``OSError`` from inside the snapshot and surfaced as an uncaught
    ``FileNotFoundError`` — exit 1 from the CLI, indistinguishable from a crash
    — instead of the terminal input verdict the caller can act on.

    Raises
    ------
        InvalidSessionInput: the path is absent, or not a ``.jsonl`` file.
    """
    if path.suffix != ".jsonl" or not path.is_file():
        msg = f"not a session JSONL file: {path}"
        raise InvalidSessionInput(msg)


def validate_trajectory(trajectory: dict[str, Any]) -> tuple[str, ...]:
    """Run harbor's ``TrajectoryValidator``; return its errors (empty = valid).

    Used to re-validate after the enrichment pass mutates ``extra`` fields
    (contract: TrajectoryValidator MUST pass post-enrichment).
    """
    from harbor.utils.trajectory_validator import (  # type: ignore[import-untyped]
        TrajectoryValidator,
    )

    validator = TrajectoryValidator()
    ok = validator.validate(trajectory)
    return () if ok else tuple(str(e) for e in validator.errors)


def convert_session(
    session_jsonl: Path,
    *,
    include_subagents: bool = True,
) -> ConversionResult:
    """Convert one Claude Code session JSONL into a validated ATIF trajectory.

    Reads the session and its side files through our converter and validates
    the result with harbor's ``TrajectoryValidator``.

    Raises
    ------
        InvalidSessionInput: ``session_jsonl`` is not an existing ``.jsonl`` file.
        EmptySessionError: the converter found no convertible events.
        ConversionError: any unexpected failure inside the converter.
    """
    from atif_converter.infrastructure.claude_code_converter import convert_claude_code_session

    require_transcript_file(session_jsonl)

    try:
        trajectory = convert_claude_code_session(session_jsonl, include_subagents=include_subagents)
    except (
        Exception
    ) as exc:  # the converter is a port of untyped upstream code; classify at the seam
        msg = f"conversion failed for {session_jsonl}"
        raise ConversionError(msg) from exc

    if trajectory is None:
        msg = f"no convertible events in {session_jsonl}"
        raise EmptySessionError(msg)

    trajectory_dict: dict[str, Any] = trajectory.model_dump(mode="json", exclude_none=True)

    errors = validate_trajectory(trajectory_dict)
    if errors:
        logger.warning("trajectory for {} failed validation: {}", session_jsonl, errors)

    return ConversionResult(trajectory=trajectory_dict, validation_errors=errors)
