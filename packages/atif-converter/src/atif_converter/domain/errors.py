# SPDX-License-Identifier: Apache-2.0

"""The domain error hierarchy for atif-converter.

Pure, dependency-free exception types. Callers map these to exit codes
themselves (atif-cli owns that mapping in ``atif_cli.errors``).
"""

from __future__ import annotations

from pathlib import Path


class DomainError(Exception):
    """Base for every atif-converter domain error.

    Anything that crosses a layer boundary as an error value is a subclass of
    this. Adapters may raise transport-specific exceptions internally, but the
    application layer surfaces only ``DomainError`` subtypes.
    """


class InvalidSessionInput(DomainError):  # noqa: N818 — named as a terminal input verdict, not "*Error"
    """The supplied path is not a Claude Code session JSONL file."""


class EmptySessionError(DomainError):
    """Harbor produced no trajectory (no convertible events in the session)."""


class TrajectoryValidationError(DomainError):
    """The converted trajectory failed harbor's ``TrajectoryValidator``.

    Carries the validator's error list so callers can report the exact
    schema violations.
    """

    def __init__(self, errors: list[str]) -> None:
        super().__init__(f"trajectory failed validation with {len(errors)} error(s)")
        self.errors = errors


class ConversionError(DomainError):
    """Unexpected failure inside the harbor adapter during conversion."""


class SourceMutatedDuringConversion(DomainError):  # noqa: N818 — names a race verdict, not a runtime fault
    """A source file changed while the session was being converted.

    The artifacts derived from the pre-change snapshot would disagree with
    each other, so conversion refuses instead of publishing an inconsistent
    generation. Retrying once the session goes quiet succeeds.
    """

    def __init__(self, session_jsonl: Path, mutated: tuple[Path, ...]) -> None:
        super().__init__(
            f"{len(mutated)} source file(s) changed while converting {session_jsonl}: "
            f"{', '.join(str(path) for path in mutated)}"
        )
        self.mutated = mutated
