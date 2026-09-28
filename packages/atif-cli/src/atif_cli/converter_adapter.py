# SPDX-License-Identifier: Apache-2.0

"""The port-adapter seam: atif-converter's real converters behind atif-corpus's port.

atif-corpus may never import atif-converter (import-linter independence
contract), so its materialize use case is written against
:class:`atif_corpus.domain.ports.ConverterPort`. This module — living in the
one package allowed to import both — adapts the real
:func:`atif_converter.application.convert_and_audit` (Claude Code) and
:func:`atif_converter.application.convert_codex.convert_codex_and_audit`
(Codex CLI) to that port.

ONE PORT, TWO AGENTS. The port takes a path and returns contract-shaped
artifacts, and that is all materialization needs to know: which agent wrote the
transcript changes which use case runs, not what comes back. So the agent is
chosen once, here, when the adapter is constructed — a corpus root holds one
agent's sessions by construction, so no per-session dispatch is needed and a
mixed pass is not a state this seam can reach.

THE AGENT ARGUMENT IS NORMALIZED BY VALUE, and that is load-bearing rather than
defensive. ``AgentSource`` is a deliberate TWIN: atif-converter has one copy and
atif-corpus has another, because the independence contract forbids either from
importing the other. Two enum classes with equal values are still two classes,
so ``settings.agent is AgentSource.CODEX`` is FALSE when ``settings`` came from
atif-corpus — which silently routed a Codex pass into the Claude Code converter
until this seam started converting the value instead of comparing the object.

Mapping decisions (ConversionResult -> ConversionOutput):

* ``trajectory_dict``  <- ``ConversionResult.trajectory`` (the ENRICHED
  trajectory; the corpus writer owns compact serialization).
* ``loss_report_dict`` <- ``LossReport.to_json()`` (the contract's
  loss_report.json shape, enum members flattened to strings).
* ``edges_lines``      <- ``ConversionResult.edges_lines`` (already-serialized
  JSON lines derived from the RAW records, per the contract; the writer owns
  line termination).
* ``events_lines``     <- ``ConversionResult.events_lines`` (session_events.jsonl,
  same serialization and termination rules as the edges).
* ``blobs``            <- ``ConversionResult.blobs`` (the inline attachments the
  converter replaced with placeholders), one ``BlobOutput`` per content hash.
* ``source_archive``   <- the files a
  :class:`~atif_converter.infrastructure.source_archive.SourceArchiveWriter`
  wrote into ``archive_dir`` while the use case re-verified its input, so the
  archive holds the parsed bytes and costs no extra open of a transcript.
* ``EmptySessionError`` -> :class:`atif_corpus.domain.ports.EmptySourceError`:
  the port's "nothing to convert" verdict, which materialize records instead
  of retrying every pass.

Validation posture: a trajectory that fails post-enrichment validation
raises rather than materializing — the materialize use case records the
failure against the session and continues (its documented port contract),
so one invalid trajectory degrades to a per-session failure line instead of
a silently-wrong artifact.

NOTE: importing this module builds the ATIF pydantic models (via
atif_converter.application), so it must only be imported inside command bodies — never at
``atif_cli.app`` module top (pinned by the lean-import test).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atif_converter.application.convert_and_audit import convert_and_audit
from atif_converter.application.convert_codex import convert_codex_and_audit
from atif_converter.domain.agents import DEFAULT_AGENT, AgentSource
from atif_converter.domain.errors import EmptySessionError, TrajectoryValidationError
from atif_converter.infrastructure.source_archive import SourceArchiveWriter
from atif_corpus.domain.ports import (
    ArchivedSource,
    BlobOutput,
    ConversionOutput,
    EmptySourceError,
)

if TYPE_CHECKING:
    from pathlib import Path


class RealConverter:
    """The production :class:`~atif_corpus.domain.ports.ConverterPort` adapter."""

    def __init__(
        self,
        *,
        include_subagents: bool = True,
        agent: AgentSource | str = DEFAULT_AGENT,
    ) -> None:
        #: Stage subagent side-files alongside the main chain (contract default).
        #: Claude Code only — a Codex rollout has no side-files to stage.
        self.include_subagents = include_subagents
        #: Which agent wrote the transcripts this adapter will be handed. Coerced
        #: through the enum by VALUE so atif-corpus's twin member, or a plain
        #: string from a caller, resolves to THIS package's member — see the
        #: module docstring.
        self.agent = AgentSource(str(agent))

    def convert(self, session_jsonl: Path, *, archive_dir: Path | None = None) -> ConversionOutput:
        """Convert one session through its agent's use case; raise on invalid output.

        With ``archive_dir``, the session's source files are archived there as
        a by-product of the use case's verifying re-read.

        Raises
        ------
        EmptySourceError
            The session holds no convertible record (the converter's
            ``EmptySessionError``, translated to the port's type).
        TrajectoryValidationError
            When the enriched trajectory fails the ATIF validator — the
            materialize use case records this against the session.
        atif_converter.domain.errors.DomainError
            For invalid sessions or adapter failures (same handling).
        """
        archive = (
            None
            if archive_dir is None
            else SourceArchiveWriter(archive_dir, base=session_jsonl.parent)
        )
        try:
            if self.agent is AgentSource.CODEX:
                result, report = convert_codex_and_audit(session_jsonl, archive=archive)
            else:
                result, report = convert_and_audit(
                    session_jsonl, include_subagents=self.include_subagents, archive=archive
                )
        except EmptySessionError as error:
            raise EmptySourceError(str(error)) from error
        if result.validation_errors:
            raise TrajectoryValidationError(list(result.validation_errors))
        return ConversionOutput(
            trajectory_dict=result.trajectory,
            loss_report_dict=report.to_json(),
            edges_lines=list(result.edges_lines),
            events_lines=list(result.events_lines),
            blobs=tuple(
                BlobOutput(sha256=blob.ref.sha256, extension=blob.ref.extension, data=blob.data)
                for blob in result.blobs
            ),
            source_archive=(
                ()
                if archive is None
                else tuple(
                    ArchivedSource(relative_path=f.relative_path, size=f.size, sha256=f.sha256)
                    for f in archive.files
                )
            ),
        )


__all__ = ["RealConverter"]
