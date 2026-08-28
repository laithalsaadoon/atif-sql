# SPDX-License-Identifier: Apache-2.0

"""Thin wrapper around harbor's ``ClaudeCode`` Claude Code -> ATIF converter.

PRIVATE-METHOD DEPENDENCY: this module calls
``ClaudeCode._convert_events_to_trajectory``, which is not public API. The
workspace pins ``harbor>=0.22.0,<0.23`` and the atif-converter test suite pins
the observed behavior; treat any test failure after a harbor bump as upstream
drift, not a local bug.

harbor ships no ``py.typed`` marker, so every import from it is untyped —
keep the harbor surface confined to this module.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from atif_converter.domain.errors import (
    ConversionError,
    EmptySessionError,
    HarborPrivateApiMissing,
    InvalidSessionInput,
)

#: The private harbor entry point this whole package is built on.
_CONVERT_METHOD = "_convert_events_to_trajectory"


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


def _stage_session(session_jsonl: Path, staging: Path, *, include_subagents: bool) -> Path:
    """Symlink the session into the layout harbor's converter expects.

    ``_convert_events_to_trajectory`` takes a directory containing
    ``<session>.jsonl`` plus optional ``subagents/*.jsonl`` side-files under a
    ``<session-stem>/`` subdirectory. We build
    ``<staging>/sessions/projects/<slug>/`` (the shape ``ClaudeCode`` uses for
    its own logs) and symlink the inputs in.
    """
    slug = session_jsonl.parent.name or "-unknown"
    session_dir = staging / "sessions" / "projects" / slug
    session_dir.mkdir(parents=True, exist_ok=True)

    link = session_dir / session_jsonl.name
    if not link.exists():
        link.symlink_to(session_jsonl.resolve())

    # Stage EVERY side-file (including workflow-nested ones harbor's own
    # ``rglob("subagents/*.jsonl")`` discovery cannot see — fidelity gap 1)
    # FLAT into the harbor-visible ``subagents/`` dir. Two rules:
    #   - per-FILE symlinks under REAL directories only: Python 3.13's
    #     ``Path.rglob``/``glob`` do not descend symlinked directories, so a
    #     single directory symlink silently hides every subagent transcript.
    #   - collision-safe flat names: nested path parts joined with ``__`` so
    #     ``subagents/workflows/wf_x/agent-a.jsonl`` stages as
    #     ``subagents/workflows__wf_x__agent-a.jsonl``.
    # ``rglob("*.jsonl")`` never matches ``*.meta.json`` sidecars, so they are
    # excluded by construction.
    side_dir = session_jsonl.parent / session_jsonl.stem
    if include_subagents and side_dir.is_dir():
        staged_subagents = session_dir / side_dir.name / "subagents"
        for side_file in sorted(side_dir.rglob("*.jsonl")):
            rel_parts = side_file.relative_to(side_dir).parts
            if rel_parts and rel_parts[0] == "subagents":
                rel_parts = rel_parts[1:]
            staged = staged_subagents / "__".join(rel_parts)
            staged.parent.mkdir(parents=True, exist_ok=True)
            if not staged.exists():
                staged.symlink_to(side_file.resolve())

    return session_dir


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


def assert_harbor_private_api() -> None:
    """Fail loudly and specifically if harbor's private converter is gone.

    Without this probe an upstream rename surfaces as an ``AttributeError``
    swallowed into :class:`ConversionError` — one indistinguishable
    per-session failure line among many, when in fact EVERY session is about
    to fail for a reason unrelated to any transcript.

    Raises
    ------
        HarborPrivateApiMissing: the pinned private method no longer exists.
    """
    from harbor.agents.installed.claude_code import ClaudeCode  # type: ignore[import-untyped]

    if not callable(getattr(ClaudeCode, _CONVERT_METHOD, None)):
        try:
            from importlib.metadata import version

            installed = version("harbor")
        except Exception:  # noqa: BLE001 — the version is diagnostic only
            installed = "unknown"
        msg = (
            f"harbor {installed} has no ClaudeCode.{_CONVERT_METHOD}: the pinned "
            "private conversion API this package is built on was renamed or "
            "removed upstream. Every session will fail until the adapter is "
            "re-targeted (see the harbor pin in atif-converter's pyproject.toml)."
        )
        raise HarborPrivateApiMissing(msg)


def convert_session(
    session_jsonl: Path,
    *,
    include_subagents: bool = True,
) -> ConversionResult:
    """Convert one Claude Code session JSONL into a validated ATIF trajectory.

    Stages the session (symlinks) into harbor's expected directory shape,
    instantiates ``ClaudeCode``, calls the private converter, and validates the
    result with ``TrajectoryValidator``.

    Raises
    ------
        InvalidSessionInput: ``session_jsonl`` is not an existing ``.jsonl`` file.
        HarborPrivateApiMissing: harbor no longer exposes the pinned private method.
        EmptySessionError: harbor returned ``None`` (no convertible events).
        ConversionError: any unexpected failure inside harbor.
    """
    # harbor has no py.typed, so these imports are untyped by construction.
    from harbor.agents.installed.claude_code import ClaudeCode  # type: ignore[import-untyped]

    if session_jsonl.suffix != ".jsonl" or not session_jsonl.is_file():
        msg = f"not a session JSONL file: {session_jsonl}"
        raise InvalidSessionInput(msg)

    assert_harbor_private_api()

    with tempfile.TemporaryDirectory(prefix="atif-convert-") as scratch:
        staging = Path(scratch)
        session_dir = _stage_session(session_jsonl, staging, include_subagents=include_subagents)

        logs_dir = staging / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        try:
            agent = ClaudeCode(logs_dir=logs_dir)
            # Pinned private dependency — see module docstring.
            trajectory = getattr(agent, _CONVERT_METHOD)(session_dir)
        except (
            Exception
        ) as exc:  # harbor raises untyped errors; re-raise as our terminal domain error
            msg = f"harbor conversion failed for {session_jsonl}"
            raise ConversionError(msg) from exc

    if trajectory is None:
        msg = f"no convertible events in {session_jsonl}"
        raise EmptySessionError(msg)

    trajectory_dict: dict[str, Any] = trajectory.model_dump(mode="json", exclude_none=True)

    errors = validate_trajectory(trajectory_dict)
    if errors:
        logger.warning("trajectory for {} failed validation: {}", session_jsonl, errors)

    return ConversionResult(trajectory=trajectory_dict, validation_errors=errors)
