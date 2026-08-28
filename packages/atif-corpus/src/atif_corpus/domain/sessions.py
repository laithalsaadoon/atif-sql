# SPDX-License-Identifier: Apache-2.0

"""Value objects describing what a materialization pass sees and decides.

The decision pipeline is three pure pieces:

* :class:`SessionSource` — one session's source files and their mtimes, as
  scanned (infrastructure feeds this in; the domain never stats a file).
* :class:`QuiescencePolicy` — the "is this session done being written?"
  rule. A transcript is appended to many times per turn; converting a
  session mid-write wastes a conversion and materializes a half-turn. A
  session qualifies only once its newest source mtime is at least
  ``quiesce_seconds`` old.
* :class:`MaterializationPlan` / :func:`build_plan` — the deterministic
  partition of scanned sessions into to-materialize / up-to-date /
  skipped-live, given the recorded watermark and "now".

Clock discipline: all ``*_ns`` values are **epoch nanoseconds** — mtimes from
``os.stat().st_mtime_ns``, "now" from ``time.time_ns()`` passed in by the
caller. The domain never reads a clock, so the same inputs always yield the
same plan (pinned by the determinism tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from atif_corpus.domain.watermark import diff_source_mtimes

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

#: Nanoseconds per second; policies are configured in seconds and convert
#: once at the boundary because ``st_mtime_ns`` is the native unit.
NANOS_PER_SECOND: int = 1_000_000_000


@dataclass(frozen=True, slots=True)
class SessionSource:
    """One discovered session: its id and every source file with its mtime.

    ``source_mtimes`` covers the main ``<session_id>.jsonl`` PLUS every
    side-file under the session dir (``subagents/agent-*.jsonl``,
    ``subagents/workflows/wf_*/agent-*.jsonl``, and any deeper future
    nesting) — the contract requires the watermark to notice a workflow
    side-file changing even when the main transcript did not.
    """

    #: The session UUID (the main JSONL's stem).
    session_id: str
    #: Absolute path of the main ``<session_id>.jsonl`` transcript.
    session_jsonl: str
    #: ``{absolute path: epoch-ns mtime}`` for the main file + all side-files.
    source_mtimes: Mapping[str, int]

    @property
    def newest_mtime_ns(self) -> int:
        """The most recent write across all of this session's source files."""
        return max(self.source_mtimes.values())

    @property
    def source_files(self) -> tuple[str, ...]:
        """Sorted source paths — deterministic, for meta.json and diffing."""
        return tuple(sorted(self.source_mtimes))


@dataclass(frozen=True, slots=True)
class QuiescencePolicy:
    """The rule for when a session is settled enough to convert.

    A session is *quiescent* when its newest source mtime is at least
    ``quiesce_seconds`` in the past. Nothing announces that a writer finished,
    so silence is the only available signal.

    SCOPE: the quiet period is per-SESSION, not per-file. One late write to
    any of a session's source files holds the whole session back, which is
    what keeps a multi-file session from being materialized half-written.
    """

    #: Seconds of source silence required before a session may materialize.
    quiesce_seconds: int = 300

    def is_quiescent(self, newest_mtime_ns: int, now_ns: int) -> bool:
        """True when the newest source write is at least ``quiesce_seconds`` old.

        The boundary is inclusive (``>=``): a session exactly at the
        threshold counts as quiescent, so the edge is testable without
        racing a clock.

        A source mtime in the FUTURE (a clock step on the writing host, a
        copied file carrying a bogus stamp) never satisfies the threshold,
        so the session would starve in ``skipped_live`` silently. It stays
        skipped — converting is still the wrong call — but it warns every
        pass so the starvation is visible instead of mute.
        """
        age_ns = now_ns - newest_mtime_ns
        if age_ns < 0:
            logger.warning(
                "quiescence: newest source mtime is {:.1f}s in the FUTURE; "
                "session stays skipped until the clock catches up "
                "(check the writing host's clock)",
                -age_ns / NANOS_PER_SECOND,
            )
        return age_ns >= self.quiesce_seconds * NANOS_PER_SECOND


@dataclass(frozen=True, slots=True)
class MaterializationPlan:
    """The deterministic work order for one materialization pass.

    All three partitions are sorted by session id so the same scan always
    yields the same plan (and the same log output, and the same failure
    ordering).
    """

    #: Sessions to (re)convert: quiescent AND stale relative to the watermark.
    to_materialize: tuple[SessionSource, ...]
    #: Sessions whose artifacts already reflect their sources; nothing to do.
    up_to_date: tuple[SessionSource, ...]
    #: Sessions still being written (not quiescent); revisit next pass.
    skipped_live: tuple[SessionSource, ...]

    @property
    def is_noop(self) -> bool:
        """True when the pass has nothing to convert."""
        return not self.to_materialize


def owns_path(session_jsonl: str, path: str) -> bool:
    """True when ``path`` is ``session_jsonl`` itself or one of its side-files.

    The single definition of a session's watermark scope: the main JSONL
    plus everything under ``<main-jsonl-stem>/``. Staleness and watermark
    retention both key off this, so the two can never disagree about which
    entries belong to a session. Keyed on the main path rather than a whole
    :class:`SessionSource` so a session the scanner could NOT read — it has
    a path but no mtimes — gets scoped by the same rule.
    """
    stem = session_jsonl.rsplit(".", 1)[0]
    return path == session_jsonl or path.startswith(f"{stem}/")


def _is_stale(session: SessionSource, watermark: Mapping[str, int]) -> bool:
    """True when the recorded watermark fails to describe this session's sources.

    Delegates the comparison to
    :func:`atif_corpus.domain.watermark.diff_source_mtimes` over this
    session's scope: a source file added, changed in either mtime direction,
    or *removed* since the last pass all make the session stale. Removal
    counts because the materialized trajectory still embeds the vanished
    file's records.
    """
    recorded = {
        path: mtime for path, mtime in watermark.items() if owns_path(session.session_jsonl, path)
    }
    return not diff_source_mtimes(recorded, session.source_mtimes).is_empty


def build_plan(
    sessions: Sequence[SessionSource],
    *,
    watermark: Mapping[str, int],
    policy: QuiescencePolicy,
    now_ns: int,
    force: bool = False,
    unmaterialized_session_ids: Collection[str] = (),
) -> MaterializationPlan:
    """Partition scanned sessions into the three plan buckets.

    Decision order per the contract: a session is (re)materialized when its
    newest source mtime is quiescent AND the watermark says its sources
    moved. ``force`` overrides ONLY the staleness check — a live session is
    never converted even under force, because converting a half-written
    transcript produces a wrong artifact rather than a stale one.

    Parameters
    ----------
    sessions
        Every session the scanner discovered under the source root.
    watermark
        The recorded ``{path: mtime_ns}`` map from the previous pass
        (``watermark.json``); empty on first run.
    policy
        The quiescence rule.
    now_ns
        Epoch nanoseconds "now", read once by the caller — never here.
    force
        Re-materialize every quiescent session regardless of the watermark.
    unmaterialized_session_ids
        Sessions the watermark claims are current but whose artifact dir is
        absent on disk (a pass killed mid-swap). Treated as stale so the
        artifacts come back; the watermark alone cannot express this, since
        it records SOURCE mtimes and knows nothing about the corpus dir.
    """
    ordered = sorted(sessions, key=lambda s: s.session_id)
    missing = set(unmaterialized_session_ids)
    to_materialize: list[SessionSource] = []
    up_to_date: list[SessionSource] = []
    skipped_live: list[SessionSource] = []
    for session in ordered:
        if not policy.is_quiescent(session.newest_mtime_ns, now_ns):
            skipped_live.append(session)
        elif force or session.session_id in missing or _is_stale(session, watermark):
            to_materialize.append(session)
        else:
            up_to_date.append(session)
    return MaterializationPlan(
        to_materialize=tuple(to_materialize),
        up_to_date=tuple(up_to_date),
        skipped_live=tuple(skipped_live),
    )
