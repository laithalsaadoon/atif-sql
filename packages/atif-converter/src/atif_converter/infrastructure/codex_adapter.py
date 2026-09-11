# SPDX-License-Identifier: Apache-2.0

"""Thin wrapper around harbor's ``Codex`` rollout -> ATIF converter.

PRIVATE-METHOD DEPENDENCY, the same one the Claude Code adapter carries: this
module calls ``Codex._convert_events_to_trajectory``, which is not public API.
The workspace pins ``harbor>=0.22.0,<0.23`` and the atif-converter test suite
pins the observed behavior; treat any test failure after a harbor bump as
upstream drift, not a local bug.

harbor ships no ``py.typed`` marker, so every import from it is untyped —
keep the harbor surface confined to this module and its Claude Code sibling.

STAGING, and why it is one file per directory. harbor's Codex converter takes
a DIRECTORY, globs ``*.jsonl`` inside it and converts ``max(...)`` of the
matches — one rollout, chosen by filename order, with the rest dropped
silently
(:attr:`~atif_converter.domain.codex_fidelity.CodexFidelityGap.SINGLE_ROLLOUT_PER_DIRECTORY`).
So each conversion stages exactly ONE rollout into an otherwise empty
directory: with a single candidate, ``max`` is that candidate and the gap
cannot bite through this wrapper.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from atif_converter.domain.errors import (
    ConversionError,
    EmptySessionError,
    HarborPrivateApiMissing,
    InvalidSessionInput,
)
from atif_converter.infrastructure.harbor_adapter import (
    ConversionResult,
    validate_trajectory,
)

#: The private harbor entry point the Codex path is built on.
_CONVERT_METHOD = "_convert_events_to_trajectory"

#: A Codex rollout's filename shape. harbor's own
#: ``Codex._ROLLOUT_FILENAME_RE`` requires the same prefix for its upload path,
#: and the session id is the filename's trailing uuid — so a file that does not
#: match this is not a rollout and must not be guessed at.
_ROLLOUT_PREFIX = "rollout-"


def assert_harbor_codex_private_api() -> None:
    """Fail loudly and specifically if harbor's private Codex converter is gone.

    Separate from the Claude Code probe on purpose: the two adapters can rot
    independently, and a message naming the wrong class sends the reader to
    the wrong pin.

    Raises
    ------
        HarborPrivateApiMissing: the pinned private method no longer exists.
    """
    from harbor.agents.installed.codex import Codex  # type: ignore[import-untyped]

    if not callable(getattr(Codex, _CONVERT_METHOD, None)):
        try:
            from importlib.metadata import version

            installed = version("harbor")
        except Exception:  # noqa: BLE001 — the version is diagnostic only
            installed = "unknown"
        msg = (
            f"harbor {installed} has no Codex.{_CONVERT_METHOD}: the pinned "
            "private conversion API the Codex path is built on was renamed or "
            "removed upstream. Every Codex session will fail until the adapter "
            "is re-targeted (see the harbor pin in atif-converter's pyproject.toml)."
        )
        raise HarborPrivateApiMissing(msg)


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

    Stages the rollout alone in a temp directory (see the module docstring),
    instantiates ``Codex``, calls the private converter, and validates the
    result with harbor's ``TrajectoryValidator``.

    Raises
    ------
        InvalidSessionInput: not an existing ``.jsonl`` file.
        HarborPrivateApiMissing: harbor no longer exposes the pinned private method.
        EmptySessionError: harbor returned ``None`` (no convertible events).
        ConversionError: any unexpected failure inside harbor.
    """
    # harbor has no py.typed, so this import is untyped by construction.
    from harbor.agents.installed.codex import Codex  # type: ignore[import-untyped]

    if rollout_jsonl.suffix != ".jsonl" or not rollout_jsonl.is_file():
        msg = f"not a rollout JSONL file: {rollout_jsonl}"
        raise InvalidSessionInput(msg)

    assert_harbor_codex_private_api()

    with tempfile.TemporaryDirectory(prefix="atif-codex-") as scratch:
        staging = Path(scratch)
        session_dir = staging / "sessions"
        session_dir.mkdir(parents=True)
        # A per-FILE symlink under a REAL directory: harbor globs this
        # directory, and a symlinked DIRECTORY would not be walked by
        # Path.glob on Python 3.13.
        (session_dir / rollout_jsonl.name).symlink_to(rollout_jsonl.resolve())

        logs_dir = staging / "logs"
        logs_dir.mkdir(parents=True)
        try:
            agent = Codex(logs_dir=logs_dir)
            # Pinned private dependency — see module docstring.
            trajectory = getattr(agent, _CONVERT_METHOD)(session_dir)
        except (
            Exception
        ) as exc:  # harbor raises untyped errors; re-raise as our terminal domain error
            msg = f"harbor Codex conversion failed for {rollout_jsonl}"
            raise ConversionError(msg) from exc

    if trajectory is None:
        msg = f"no convertible events in {rollout_jsonl}"
        raise EmptySessionError(msg)

    trajectory_dict: dict[str, Any] = trajectory.model_dump(mode="json", exclude_none=True)

    errors = validate_trajectory(trajectory_dict)
    if errors:
        logger.warning("codex trajectory for {} failed validation: {}", rollout_jsonl, errors)

    return ConversionResult(trajectory=trajectory_dict, validation_errors=errors)
