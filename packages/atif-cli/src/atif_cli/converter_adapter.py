# SPDX-License-Identifier: Apache-2.0

"""The port-adapter seam: atif-converter's real converter behind atif-corpus's port.

atif-corpus may never import atif-converter (import-linter independence
contract), so its materialize use case is written against
:class:`atif_corpus.domain.ports.ConverterPort`. This module — living in the
one package allowed to import both — adapts the real
:func:`atif_converter.application.convert_and_audit` to that port.

Mapping decisions (ConversionResult -> ConversionOutput):

* ``trajectory_dict``  <- ``ConversionResult.trajectory`` (the ENRICHED
  trajectory; the corpus writer owns compact serialization).
* ``loss_report_dict`` <- ``LossReport.to_json()`` (the contract's
  loss_report.json shape, enum members flattened to strings).
* ``edges_lines``      <- ``ConversionResult.edges_lines`` (already-serialized
  JSON lines derived from the RAW records, per the contract; the writer owns
  line termination).

Validation posture: a trajectory that fails post-enrichment validation
raises rather than materializing — the materialize use case records the
failure against the session and continues (its documented port contract),
so one invalid trajectory degrades to a per-session failure line instead of
a silently-wrong artifact.

NOTE: importing this module drags harbor (via atif_converter.application),
so it must only be imported inside command bodies — never at
``atif_cli.app`` module top (pinned by the lean-import test).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atif_converter.application.convert_and_audit import convert_and_audit
from atif_converter.domain.errors import TrajectoryValidationError
from atif_corpus.domain.ports import ConversionOutput

if TYPE_CHECKING:
    from pathlib import Path


class RealConverter:
    """The production :class:`~atif_corpus.domain.ports.ConverterPort` adapter."""

    def __init__(self, *, include_subagents: bool = True) -> None:
        #: Stage subagent side-files alongside the main chain (contract default).
        self.include_subagents = include_subagents

    def convert(self, session_jsonl: Path) -> ConversionOutput:
        """Convert one session through convert_and_audit; raise on invalid output.

        Raises
        ------
        TrajectoryValidationError
            When the enriched trajectory fails harbor's validator — the
            materialize use case records this against the session.
        atif_converter.domain.errors.DomainError
            For empty/invalid sessions or adapter failures (same handling).
        """
        result, report = convert_and_audit(session_jsonl, include_subagents=self.include_subagents)
        if result.validation_errors:
            raise TrajectoryValidationError(list(result.validation_errors))
        return ConversionOutput(
            trajectory_dict=result.trajectory,
            loss_report_dict=report.to_json(),
            edges_lines=list(result.edges_lines),
        )


__all__ = ["RealConverter"]
