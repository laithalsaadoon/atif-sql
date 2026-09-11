# SPDX-License-Identifier: Apache-2.0

"""The Codex conversion seam: our converter in, a validated ATIF dict out.

The raw rollout -> ``Trajectory`` conversion is ours, in
:mod:`atif_converter.domain.codex_conversion` via
:mod:`atif_converter.infrastructure.codex_converter`, ported from harbor 0.22.0
and held to parity with it by the oracle in this package's tests. harbor itself
supplies only the public data classes and the validator. One rollout in, one
trajectory out, by construction: the converter reads the file it is given, so
the directory-globbing hazard the old wrapper staged around no longer exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from atif_converter.domain.errors import (
    ConversionError,
    EmptySessionError,
    InvalidSessionInput,
)
from atif_converter.infrastructure.harbor_adapter import (
    ConversionResult,
    validate_trajectory,
)

#: A Codex rollout's filename shape. harbor's own
#: ``Codex._ROLLOUT_FILENAME_RE`` requires the same prefix for its upload path,
#: and the session id is the filename's trailing uuid — so a file that does not
#: match this is not a rollout and must not be guessed at.
_ROLLOUT_PREFIX = "rollout-"


def codex_session_id(rollout_jsonl: Path) -> str:
    """The session id a rollout filename carries.

    ``rollout-<ISO-ish timestamp>-<uuid>.jsonl`` — the uuid is the last five
    hyphen-separated groups of the stem, and it equals the ``id`` inside the
    file's ``session_meta`` record (verified 2026-09-11 over the local
    rollouts). Read from the NAME rather than the contents because discovery
    needs a session id before anything parses the file.

    Raises
    ------
        InvalidSessionInput: the name is not a rollout filename.
    """
    stem = rollout_jsonl.stem
    if not stem.startswith(_ROLLOUT_PREFIX):
        msg = f"not a Codex rollout filename (expected rollout-<ts>-<uuid>.jsonl): {rollout_jsonl}"
        raise InvalidSessionInput(msg)
    parts = stem.split("-")
    uuid_groups = 5
    if len(parts) < uuid_groups + 1:
        msg = f"Codex rollout filename carries no session uuid: {rollout_jsonl}"
        raise InvalidSessionInput(msg)
    return "-".join(parts[-uuid_groups:])


def convert_codex_session(rollout_jsonl: Path) -> ConversionResult:
    """Convert one Codex rollout JSONL into a validated ATIF trajectory.

    Raises
    ------
        InvalidSessionInput: not an existing ``.jsonl`` file.
        EmptySessionError: the converter found no convertible events.
        ConversionError: any unexpected failure inside the converter.
    """
    from atif_converter.infrastructure.codex_converter import convert_codex_rollout

    if rollout_jsonl.suffix != ".jsonl" or not rollout_jsonl.is_file():
        msg = f"not a rollout JSONL file: {rollout_jsonl}"
        raise InvalidSessionInput(msg)

    try:
        trajectory = convert_codex_rollout(rollout_jsonl)
    except (
        Exception
    ) as exc:  # the converter is a port of untyped upstream code; classify at the seam
        msg = f"Codex conversion failed for {rollout_jsonl}"
        raise ConversionError(msg) from exc

    if trajectory is None:
        msg = f"no convertible events in {rollout_jsonl}"
        raise EmptySessionError(msg)

    trajectory_dict: dict[str, Any] = trajectory.model_dump(mode="json", exclude_none=True)

    errors = validate_trajectory(trajectory_dict)
    if errors:
        logger.warning("codex trajectory for {} failed validation: {}", rollout_jsonl, errors)

    return ConversionResult(trajectory=trajectory_dict, validation_errors=errors)
