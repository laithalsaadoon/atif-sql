# SPDX-License-Identifier: Apache-2.0

"""The port the pipelines read session data through.

Two adapters satisfy it. :class:`~atif_analytics.infrastructure.corpus_reader.TrajectoryFileSource`
parses each session directory's ``trajectory.json`` and ``edges.jsonl``, and is
the default. The lake adapter lives in atif-cli, because atif-analytics may not
import atif-duck: it reads the same sessions from the DuckLake every corpus is
loaded into, and falls back to the file adapter per session when the lake does
not hold a session's current artifacts.

Both adapters hand back :class:`~atif_analytics.domain.transcript.StepEvent`
rows built by :func:`~atif_analytics.domain.transcript.step_event`, so the
projection rules (role names, the first source uuid, the error flag, the
timestamp spelling, and the author label from
:mod:`atif_analytics.domain.authorship`) are written once. What a source may
choose is how it batches: reading one session from the lake costs about as
much as reading sixty-four, while parsing a trajectory costs per session.

:class:`~atif_analytics.infrastructure.corpus_reader.CorpusReader` wraps a
source with the window filter, the newest-first order, the bounded memos and
the transcript renderer; the pipelines only ever see the reader.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from atif_analytics.domain.transcript import StepEvent

#: ``{session_id: (last_record_ts, trajectory_mtime)}``: the two bounds the
#: checkpoint compares (either one advancing re-admits a session).
SessionBounds = dict[str, tuple["datetime | None", "datetime | None"]]


class SessionSource(Protocol):
    """Where one corpus's session data comes from."""

    #: A short name for logs and the analyze summary (``files`` or ``lake``).
    name: str
    #: How many sessions one :meth:`load_turns` call should carry. The reader
    #: reads ahead in the newest-first order by this many; 1 turns that off.
    turns_batch_size: int
    #: The same for :meth:`load_steps`, which carries the tool payloads.
    steps_batch_size: int
    #: True when :meth:`load_turns` already returns everything
    #: :meth:`load_steps` does (a trajectory parse yields the payloads anyway),
    #: so the reader keeps one memo and loads each session once.
    turns_are_complete: bool

    def session_bounds(self) -> SessionBounds:
        """Every complete session's bounds, in no particular order.

        Must not read a session's steps: this runs over the whole corpus once
        per stage, including every session the checkpoint then skips.
        """
        ...

    def load_turns(self, session_ids: Sequence[str]) -> Mapping[str, list[StepEvent]]:
        """Each session's steps, in order, with the tool payloads optional.

        Every step is present with its text, flags, uuid, author and
        ``has_error_result``; ``tool_calls`` and ``tool_results`` may be left
        empty. The gates (session kind, human-AI pairs, friction candidates
        and stamps) read nothing else. Every requested id is a key of the
        result; an unreadable session maps to an empty list.
        """
        ...

    def load_steps(self, session_ids: Sequence[str]) -> Mapping[str, list[StepEvent]]:
        """Each session's steps, in order, with every tool call and result.

        What the transcript renderer reads. Same key rule as :meth:`load_turns`.
        """
        ...

    def edges_uuids(self, session_id: str) -> set[str] | None:
        """The session's non-null raw-record uuids, or ``None`` when unknown.

        ``None`` and an empty set are different answers (see
        :meth:`~atif_analytics.infrastructure.corpus_reader.CorpusReader.edges_uuids`).
        """
        ...


__all__ = ["SessionBounds", "SessionSource"]
