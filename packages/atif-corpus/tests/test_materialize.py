# SPDX-License-Identifier: Apache-2.0

"""End-to-end materialize tests over a synthetic corpus with a FakeConverter."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import TypedDict, Unpack, override

import pytest
from corpus_fixtures import LIVE_NS, NOW_NS, SESSION_A, SESSION_B, STALE_NS, write_session

from atif_corpus.application.materialize import (
    MaterializationReport,
    SuspiciousEmptyScanError,
    _unmaterialized_session_ids,
    materialize,
    read_watermark,
)
from atif_corpus.domain.layout import CorpusLayout
from atif_corpus.infrastructure.fake_converter import FakeConverter
from atif_corpus.infrastructure.scanner import scan_source_root


class _Provenance(TypedDict):
    """The three provenance keys every pass stamps, keyed as `materialize` names them.

    Typed rather than a bare `dict[str, str]` so `**VERSIONS` binds to exactly
    these three parameters instead of to every remaining `str`-typed keyword.
    """

    materialized_at: str
    harbor_version: str
    converter_version: str


VERSIONS: _Provenance = {
    "materialized_at": "2026-01-02T00:00:00+00:00",
    "harbor_version": "0.22.0",
    "converter_version": "0.1.0",
}


class _RunOverrides(TypedDict, total=False):
    """The `materialize` keywords these tests vary, with `materialize`'s types."""

    force: bool
    session_ids: Collection[str]


def run(
    source_root: Path,
    corpus_root: Path,
    converter: FakeConverter,
    **kwargs: Unpack[_RunOverrides],
) -> MaterializationReport:
    return materialize(
        source_root=source_root,
        corpus_root=corpus_root,
        converter=converter,
        now_ns=NOW_NS,
        **VERSIONS,
        **kwargs,
    )


class TestFirstPass:
    def test_materializes_quiescent_sessions_with_full_layout(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        converter = FakeConverter()

        report = run(source_root, corpus_root, converter)

        assert report.materialized_count == 1
        assert report.failed_count == 0
        layout = CorpusLayout(corpus_root=corpus_root)
        trajectory = json.loads(layout.trajectory_path(SESSION_A).read_text())
        assert trajectory["session_id"] == SESSION_A
        # Compact separators per contract: no spaces after , or :
        raw = layout.trajectory_path(SESSION_A).read_text()
        assert '", "' not in raw
        assert json.loads(layout.loss_report_path(SESSION_A).read_text())
        edges = layout.edges_path(SESSION_A).read_text().splitlines()
        assert len(edges) == 1
        assert json.loads(edges[0])["uuid"] == f"{SESSION_A}-u1"

    def test_meta_json_carries_provenance_passed_in(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        run(source_root, corpus_root, FakeConverter())

        meta = json.loads(CorpusLayout(corpus_root=corpus_root).meta_path(SESSION_A).read_text())
        assert meta["session_id"] == SESSION_A
        assert meta["materialized_at"] == VERSIONS["materialized_at"]
        assert meta["harbor_version"] == VERSIONS["harbor_version"]
        assert meta["converter_version"] == VERSIONS["converter_version"]
        assert meta["source_mtime_ns"] == STALE_NS
        # main + flat subagent + workflow-nested subagent; NOT the .meta.json decoy
        assert len(meta["source_files"]) == 3
        assert not any(f.endswith(".meta.json") for f in meta["source_files"])
        assert any("workflows/wf_001" in f for f in meta["source_files"])

    def test_watermark_written_across_all_source_files(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        run(source_root, corpus_root, FakeConverter())

        watermark = json.loads(CorpusLayout(corpus_root=corpus_root).watermark_path.read_text())
        assert len(watermark) == 3
        assert all(mtime == STALE_NS for mtime in watermark.values())


class TestQuiescenceAndWatermark:
    def test_live_session_skipped_then_materialized_once_quiescent(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        main = write_session(source_root, SESSION_A, mtime_ns=LIVE_NS)
        converter = FakeConverter()

        first = run(source_root, corpus_root, converter)
        assert first.skipped_live_count == 1
        assert first.materialized_count == 0
        assert not converter.converted

        os.utime(main, ns=(STALE_NS, STALE_NS))
        second = run(source_root, corpus_root, converter)
        assert second.materialized_count == 1

    def test_second_pass_is_noop_when_nothing_moved(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        converter = FakeConverter()
        run(source_root, corpus_root, converter)
        report = run(source_root, corpus_root, converter)
        assert report.materialized_count == 0
        assert report.up_to_date_count == 1
        assert len(converter.converted) == 1

    def test_force_rematerializes_up_to_date_session(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        converter = FakeConverter()
        run(source_root, corpus_root, converter)
        report = run(source_root, corpus_root, converter, force=True)
        assert report.materialized_count == 1
        assert len(converter.converted) == 2

    def test_side_file_touch_triggers_rematerialization(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        converter = FakeConverter()
        run(source_root, corpus_root, converter)

        nested = (
            source_root
            / "-proj-a"
            / SESSION_A
            / "subagents"
            / "workflows"
            / "wf_001"
            / "agent-bbbb.jsonl"
        )
        os.utime(nested, ns=(STALE_NS + 1, STALE_NS + 1))
        report = run(source_root, corpus_root, converter)
        assert report.materialized_count == 1

    def test_removed_session_paths_drop_from_watermark(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        main = write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())

        main.unlink()
        run(source_root, corpus_root, FakeConverter())
        watermark = json.loads(CorpusLayout(corpus_root=corpus_root).watermark_path.read_text())
        assert str(main) not in watermark
        assert len(watermark) == 1

    def test_failed_session_retains_watermark_of_deleted_side_file(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """An UNFILTERED pass must not drop watermark entries for vanished
        files of a session that FAILED. The vanished entry IS the staleness
        signal (recorded set != scanned set); dropping it makes the next
        pass see recorded == scanned and report 'up_to_date' forever, while
        the published trajectory still embeds the deleted file's records."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)

        # Pass 1: materializes cleanly, watermark covers all three sources.
        run(source_root, corpus_root, FakeConverter())
        watermark_path = CorpusLayout(corpus_root=corpus_root).watermark_path
        assert len(json.loads(watermark_path.read_text())) == 3

        # A side-file is deleted: the published trajectory is now wrong.
        deleted = source_root / "-proj-a" / SESSION_A / "subagents" / "agent-aaaa.jsonl"
        deleted.unlink()

        # Pass 2: the session is stale and gets re-attempted, but conversion fails.
        failing = FakeConverter(fail_sessions=frozenset({SESSION_A}))
        second = run(source_root, corpus_root, failing)
        assert second.failed_count == 1
        assert [p.stem for p in failing.converted] == [SESSION_A]
        assert str(deleted) in json.loads(watermark_path.read_text())

        # Pass 3: the staleness signal survived, so the session is RETRIED
        # rather than classified up_to_date.
        retry = FakeConverter()
        third = run(source_root, corpus_root, retry)
        assert third.up_to_date_count == 0
        assert third.materialized_count == 1
        assert [p.stem for p in retry.converted] == [SESSION_A]
        # Only now, after success, does the vanished entry drop.
        assert str(deleted) not in json.loads(watermark_path.read_text())

    def test_filtered_run_retains_watermark_of_unplanned_sessions_deleted_files(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """A --sessions-filtered pass must not drop watermark entries for
        vanished files of UNPLANNED sessions — dropping them erases the
        staleness signal, so the next full pass would never re-materialize
        the session whose side-file was deleted."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())

        # Delete an unplanned session's side-file, then run filtered to B only.
        deleted = source_root / "-proj-a" / SESSION_A / "subagents" / "agent-aaaa.jsonl"
        deleted.unlink()
        run(source_root, corpus_root, FakeConverter(), session_ids={SESSION_B}, force=True)

        watermark = json.loads(CorpusLayout(corpus_root=corpus_root).watermark_path.read_text())
        assert str(deleted) in watermark  # retained verbatim, not dropped

        # Next FULL pass sees recorded != scanned for A -> re-materializes it.
        full = FakeConverter()
        report = run(source_root, corpus_root, full)
        assert report.materialized_count == 1
        assert [p.stem for p in full.converted] == [SESSION_A]
        watermark = json.loads(CorpusLayout(corpus_root=corpus_root).watermark_path.read_text())
        assert str(deleted) not in watermark  # full pass drops it properly


class TestGhostRemoval:
    def test_deleted_source_removes_corpus_session_dir(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        main = write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        layout = CorpusLayout(corpus_root=corpus_root)
        assert layout.session_dir(SESSION_A).is_dir()

        main.unlink()
        report = run(source_root, corpus_root, FakeConverter())

        assert report.sessions_removed == 1
        assert report.removed_session_ids == (SESSION_A,)
        assert not layout.session_dir(SESSION_A).exists()
        assert layout.session_dir(SESSION_B).is_dir()

    def test_no_removal_when_nothing_vanished(self, source_root: Path, corpus_root: Path) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        report = run(source_root, corpus_root, FakeConverter())
        assert report.sessions_removed == 0
        assert report.removed_session_ids == ()

    def test_session_filter_does_not_ghost_unplanned_sessions(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """Ghost detection keys off the FULL scan, not the --sessions filter."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())

        report = run(source_root, corpus_root, FakeConverter(), session_ids={SESSION_B})
        assert report.sessions_removed == 0
        assert CorpusLayout(corpus_root=corpus_root).session_dir(SESSION_A).is_dir()

    def test_empty_scan_over_nonempty_corpus_fails_loud(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """Wrong-source-root guard: refuse to GC the whole corpus."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())

        shutil.rmtree(source_root)
        source_root.mkdir()
        with pytest.raises(SuspiciousEmptyScanError, match="refusing to remove"):
            run(source_root, corpus_root, FakeConverter())
        # nothing was removed
        assert CorpusLayout(corpus_root=corpus_root).session_dir(SESSION_A).is_dir()

    def test_empty_scan_over_empty_corpus_is_fine(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """A fresh machine (nothing anywhere) is not an error condition."""
        report = run(source_root, corpus_root, FakeConverter())
        assert report.materialized_count == 0
        assert report.sessions_removed == 0


class TestFailureIsolation:
    def test_failing_session_recorded_and_skipped_never_aborts(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        converter = FakeConverter(fail_sessions=frozenset({SESSION_A}))

        report = run(source_root, corpus_root, converter)

        assert report.materialized_count == 1
        assert report.failed_count == 1
        assert report.failures[0].session_id == SESSION_A
        assert "scripted failure" in report.failures[0].error
        layout = CorpusLayout(corpus_root=corpus_root)
        assert layout.trajectory_path(SESSION_B).exists()
        assert not layout.trajectory_path(SESSION_A).exists()

    def test_failed_session_watermark_not_advanced_so_it_retries(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter(fail_sessions=frozenset({SESSION_A})))

        retry = FakeConverter()
        report = run(source_root, corpus_root, retry)
        assert report.materialized_count == 1
        assert len(retry.converted) == 1


class TestTornArtifactSets:
    def test_crash_between_trajectory_and_edges_leaves_old_generation(
        self,
        source_root: Path,
        corpus_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Directory-swap discipline: a crash mid-artifact-set never tears
        the published session dir — readers keep the complete OLD generation."""
        main = write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        layout = CorpusLayout(corpus_root=corpus_root)
        old_trajectory = layout.trajectory_path(SESSION_A).read_text()
        old_edges = layout.edges_path(SESSION_A).read_text()
        old_meta = layout.meta_path(SESSION_A).read_text()

        # Touch the source so the session replans, then crash the edges
        # write (i.e. AFTER trajectory, BEFORE edges) on the next pass.
        os.utime(main, ns=(STALE_NS + 1, STALE_NS + 1))
        import atif_corpus.application.materialize as mat_mod

        def _boom(path: Path, text: str) -> None:
            msg = "simulated crash between trajectory and edges"
            raise OSError(msg)

        monkeypatch.setattr(mat_mod, "write_text_atomic", _boom)
        report = run(source_root, corpus_root, FakeConverter())

        assert report.failed_count == 1
        # The published dir is byte-identical to the old generation —
        # complete and internally consistent, never a new-trajectory/
        # old-edges mix.
        assert layout.trajectory_path(SESSION_A).read_text() == old_trajectory
        assert layout.edges_path(SESSION_A).read_text() == old_edges
        assert layout.meta_path(SESSION_A).read_text() == old_meta
        # No staging litter under sessions/ where readers glob.
        assert sorted(p.name for p in layout.sessions_dir.iterdir()) == [SESSION_A]

    def test_watermark_not_advanced_after_crash_so_session_retries(
        self,
        source_root: Path,
        corpus_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        main = write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        os.utime(main, ns=(STALE_NS + 1, STALE_NS + 1))
        import atif_corpus.application.materialize as mat_mod

        def _boom(path: Path, text: str) -> None:
            msg = "simulated crash"
            raise OSError(msg)

        monkeypatch.setattr(mat_mod, "write_text_atomic", _boom)
        run(source_root, corpus_root, FakeConverter())
        monkeypatch.undo()

        retry = FakeConverter()
        report = run(source_root, corpus_root, retry)
        assert report.materialized_count == 1
        assert len(retry.converted) == 1


class TestCrashResidueRecovery:
    def test_staging_residue_from_a_dead_pass_is_swept(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """.staging/ holds nothing durable, so residue from a process that
        died mid-swap must be swept at pass start — the per-session cleanup
        only matches the CURRENT pid, so *.tmp-<oldpid> and *.old-<pid>
        would otherwise accumulate on the corpus filesystem forever."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        staging = CorpusLayout(corpus_root=corpus_root).staging_dir
        staging.mkdir(parents=True, exist_ok=True)
        for residue in (f"{SESSION_A}.tmp-999999", f"{SESSION_A}.tmp-999999.old-888888"):
            (staging / residue).mkdir()
            (staging / residue / "meta.json").write_text("{}")
        (staging / "stray.json").write_text("{}")

        run(source_root, corpus_root, FakeConverter())

        assert list(staging.iterdir()) == []

    def test_sweep_spares_a_live_pids_in_flight_staging_dir(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """The refresh script's flock is PER LANE and a manual `atif-sql
        materialize` takes no lock at all, so a second pass can be mid-swap
        when this one starts. Its *.tmp-<pid> holds the only copy of artifacts
        already written but not yet swapped in; deleting it destroys live
        work. Only debris whose owning pid is GONE may be swept."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        staging = CorpusLayout(corpus_root=corpus_root).staging_dir
        staging.mkdir(parents=True, exist_ok=True)

        # os.getpid() is alive by definition — the weakest possible liveness
        # claim, and the one the sweep must already honor.
        live = staging / f"{SESSION_A}.tmp-{os.getpid()}"
        live.mkdir()
        (live / "trajectory.json").write_text('{"in":"flight"}')
        dead = staging / f"{SESSION_A}.tmp-999999"
        dead.mkdir()
        (dead / "trajectory.json").write_text("{}")

        run(source_root, corpus_root, FakeConverter())

        assert live.is_dir()
        assert (live / "trajectory.json").read_text() == '{"in":"flight"}'
        assert not dead.exists()

    def test_sweep_spares_an_aside_dir_whose_swapping_pid_is_live(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """`*.old-<pid>` is the PREVIOUS generation held aside inside the swap
        window: if that pid is still running the swap may yet fail and rename
        the aside dir back, so removing it destroys the only remaining copy."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        staging = CorpusLayout(corpus_root=corpus_root).staging_dir
        staging.mkdir(parents=True, exist_ok=True)

        aside = staging / f"{SESSION_A}.tmp-999999.old-{os.getpid()}"
        aside.mkdir()
        (aside / "meta.json").write_text('{"generation":"previous"}')

        run(source_root, corpus_root, FakeConverter())

        assert aside.is_dir()
        assert (aside / "meta.json").read_text() == '{"generation":"previous"}'

    @pytest.mark.skipif(
        os.geteuid() == 0,
        reason="root can signal every pid, so no pid reaches the PermissionError branch",
    )
    def test_sweep_spares_a_live_pid_owned_by_another_user(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """The motivating case for the sweep is a root cron refresh racing a manual
        user run, so the owning pid usually belongs to ANOTHER user. Signalling it
        raises PermissionError, which proves the process exists just as firmly as a
        delivered signal. Pid 1 is the portable stand-in: always running, never
        signallable by a normal user."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        staging = CorpusLayout(corpus_root=corpus_root).staging_dir
        staging.mkdir(parents=True, exist_ok=True)

        unsignallable = staging / f"{SESSION_A}.tmp-1"
        unsignallable.mkdir()
        (unsignallable / "trajectory.json").write_text('{"owner":"another user"}')

        run(source_root, corpus_root, FakeConverter())

        assert unsignallable.is_dir()
        assert (unsignallable / "trajectory.json").read_text() == '{"owner":"another user"}'


class TestUnmaterializedScanCost:
    def test_watermark_is_not_scanned_for_sessions_whose_dir_exists(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """`_unmaterialized_session_ids` must test the CHEAP condition first.
        The dir check is one stat; the watermark check is a linear scan of
        every entry, so putting the scan first makes the pass
        O(sessions x watermark_entries) rather than O(sessions) while flagging
        nothing.

        Asserted structurally rather than by wall-clock: a timing assertion at
        a size that reliably separates the two orderings takes seconds and
        would flake on a loaded box. Counting watermark iteration instead
        pins the ordering exactly and runs in microseconds."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        layout = CorpusLayout(corpus_root=corpus_root)
        watermark = json.loads(layout.watermark_path.read_text())
        assert len(watermark) == 4  # A: main + 2 side-files, B: main

        scanned: list[int] = []

        class _CountingWatermark(dict[str, int]):
            @override
            def __iter__(self) -> Iterator[str]:
                scanned.append(len(self))
                return super().__iter__()

        ids = _unmaterialized_session_ids(
            layout,
            _CountingWatermark(watermark),
            scan_source_root(source_root),
        )

        # Both session dirs are present, so nothing is unmaterialized AND the
        # watermark was never iterated. Under the expensive-first ordering this
        # list holds one entry per session.
        assert ids == frozenset()
        assert scanned == []

    def test_missing_dir_still_reaches_the_watermark_and_is_flagged(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """Gating the scan on the dir check must not weaken the recovery: a
        session whose dir is gone still has to consult the watermark."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        layout = CorpusLayout(corpus_root=corpus_root)
        watermark = json.loads(layout.watermark_path.read_text())
        shutil.rmtree(layout.session_dir(SESSION_A))

        sessions = scan_source_root(source_root)
        assert _unmaterialized_session_ids(layout, watermark, sessions) == frozenset({SESSION_A})
        # A session absent from the watermark was never materialized, so it is
        # ordinary first-pass work rather than a lost swap.
        assert _unmaterialized_session_ids(layout, {}, sessions) == frozenset()


class TestUnreadableVisibility:
    def test_unreadable_session_appears_in_the_report(
        self,
        source_root: Path,
        corpus_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A PERMANENTLY unreadable source (a side-file left at mode 000)
        starves its session forever: never materialized, never up_to_date,
        never skipped_live, and deliberately never ghosted. Without a counter
        of its own the pass reports all zeroes and the starvation is invisible
        to anyone not reading loguru warnings."""
        main = write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())

        real_stat = Path.stat

        def _denied(self: Path, *args: object, **kwargs: object) -> os.stat_result:
            if self == main:
                raise PermissionError(13, "Permission denied")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _denied)
        report = run(source_root, corpus_root, FakeConverter())
        monkeypatch.undo()

        assert report.unreadable_session_ids == (SESSION_A,)
        assert report.unreadable_count == 1
        # It is in NO other bucket — which is exactly why it needs this one.
        assert report.materialized_count == 0
        assert report.up_to_date_count == 1
        assert report.skipped_live_count == 0
        assert report.failed_count == 0
        assert report.sessions_removed == 0

    def test_clean_pass_reports_no_unreadable_sessions(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        report = run(source_root, corpus_root, FakeConverter())
        assert report.unreadable_session_ids == ()
        assert report.unreadable_count == 0


class TestUnlistableProjectDirs:
    def test_unlistable_project_dir_does_not_ghost_its_sessions(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """A project dir that cannot be LISTED discovers no sessions at all,
        and `Path.glob` swallows the PermissionError rather than raising — so
        the whole project looks deleted and every session under it gets its
        corpus dir removed and its watermark entries dropped. Unlistable is
        not evidence of deletion, at the directory level exactly as at the
        session level.

        A SECOND readable project keeps the scan non-empty, so the
        wrong-source-root guard cannot mask the defect: without the
        distinction this pass really does delete -proj-a's artifacts."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        survivor = source_root / "-proj-b" / "cccc.jsonl"
        survivor.parent.mkdir(parents=True)
        survivor.write_text('{"type":"user","uuid":"u-1"}\n')
        os.utime(survivor, ns=(STALE_NS, STALE_NS))
        run(source_root, corpus_root, FakeConverter())
        layout = CorpusLayout(corpus_root=corpus_root)
        watermark_before = json.loads(layout.watermark_path.read_text())

        project_dir = source_root / "-proj-a"
        project_dir.chmod(0o000)
        try:
            report = run(source_root, corpus_root, FakeConverter())
        finally:
            project_dir.chmod(0o755)

        assert report.removed_session_ids == ()
        assert layout.session_dir(SESSION_A).is_dir()
        assert layout.session_dir(SESSION_B).is_dir()
        assert layout.trajectory_path(SESSION_A).is_file()
        # Watermark entries survive, so the sessions stay retryable.
        assert json.loads(layout.watermark_path.read_text()) == watermark_before
        assert sorted(report.unreadable_session_ids) == sorted([SESSION_A, SESSION_B])

    def test_unlistable_dir_skips_ghost_removal_even_when_the_watermark_is_silent(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """Watermark resolution can only name sessions the watermark RECORDED.
        A session materialized before its entries existed (or whose entries
        were lost with a corrupt watermark) resolves to nothing, so the
        per-session unreadable guard never sees it and ghost removal would
        find its dir with no scanned source behind it. Skipping removal
        wholesale whenever a directory would not list is the only thing
        between that session and deletion.

        The watermark entry is dropped BEFORE the dir goes unreadable, which
        is what separates this from the narrow path: the assertions below
        require the session to be absent from `unreadable_session_ids`."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        survivor = source_root / "-proj-b" / "cccc.jsonl"
        survivor.parent.mkdir(parents=True)
        survivor.write_text('{"type":"user","uuid":"u-1"}\n')
        os.utime(survivor, ns=(STALE_NS, STALE_NS))
        run(source_root, corpus_root, FakeConverter())
        layout = CorpusLayout(corpus_root=corpus_root)

        watermark = json.loads(layout.watermark_path.read_text())
        pruned = {path: mtime for path, mtime in watermark.items() if SESSION_A not in path}
        assert len(pruned) < len(watermark)
        layout.watermark_path.write_text(json.dumps(pruned))

        project_dir = source_root / "-proj-a"
        project_dir.chmod(0o000)
        try:
            report = run(source_root, corpus_root, FakeConverter())
        finally:
            project_dir.chmod(0o755)

        # Nothing resolved it, so only the wholesale skip can have saved it.
        assert SESSION_A not in report.unreadable_session_ids
        assert report.removed_session_ids == ()
        assert layout.session_dir(SESSION_A).is_dir()
        assert layout.trajectory_path(SESSION_A).is_file()
        assert layout.meta_path(SESSION_A).is_file()

    def test_sessions_recover_once_the_directory_is_readable_again(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())

        project_dir = source_root / "-proj-a"
        project_dir.chmod(0o000)
        try:
            run(source_root, corpus_root, FakeConverter())
        finally:
            project_dir.chmod(0o755)

        recovered = run(source_root, corpus_root, FakeConverter())
        assert recovered.up_to_date_count == 1
        assert recovered.unreadable_session_ids == ()
        assert recovered.sessions_removed == 0

    def test_unlistable_source_root_keeps_the_corpus_and_reports_real_session_ids(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """An unlistable SOURCE ROOT discovers nothing at all, so on the raw
        counts it is indistinguishable from a wrong `source_root` — but it is
        diagnosed, and failing the pass every ten minutes over a permission
        problem adds nothing the warning already said. The root has to land in
        `unlistable_dirs` for that distinction to exist.

        It also pins the ids: session ids come from position under the SOURCE
        ROOT, so the field holds a session id and not the PROJECT directory
        name that stripping the unlistable prefix would yield here."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        layout = CorpusLayout(corpus_root=corpus_root)
        watermark_before = json.loads(layout.watermark_path.read_text())

        source_root.chmod(0o000)
        try:
            report = run(source_root, corpus_root, FakeConverter())
        finally:
            source_root.chmod(0o755)

        assert report.removed_session_ids == ()
        assert layout.session_dir(SESSION_A).is_dir()
        assert layout.session_dir(SESSION_B).is_dir()
        assert json.loads(layout.watermark_path.read_text()) == watermark_before
        # Real session ids, never the "-proj-a" project dir the sessions live in.
        assert sorted(report.unreadable_session_ids) == sorted([SESSION_A, SESSION_B])
        assert all(
            layout.session_dir(session_id).is_dir() for session_id in report.unreadable_session_ids
        )

    def test_root_level_watermark_entry_under_an_unlistable_root_does_not_abort(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """A watermark path directly under the source root has no
        ``<project>/<session>`` pair to read, so indexing the second segment
        unconditionally raises IndexError and takes the WHOLE pass down —
        including the ghost-removal skip that keeps the corpus alive while a
        directory is unreadable. Such an entry is off-contract but reachable:
        a stray root-level ``.jsonl``, or a corpus whose source_root moved a
        level. It must be skipped, never reported as a session id."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        layout = CorpusLayout(corpus_root=corpus_root)

        watermark = json.loads(layout.watermark_path.read_text())
        watermark[str(source_root / "stray.jsonl")] = STALE_NS
        layout.watermark_path.write_text(json.dumps(watermark))

        source_root.chmod(0o000)
        try:
            report = run(source_root, corpus_root, FakeConverter())
        finally:
            source_root.chmod(0o755)

        assert report.unreadable_session_ids == (SESSION_A,)
        assert "stray" not in report.unreadable_session_ids
        assert report.removed_session_ids == ()
        assert layout.session_dir(SESSION_A).is_dir()

    def test_genuinely_deleted_project_dir_still_ghosts_its_sessions(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """The unlistable guard must not blunt real deletion: a project dir
        that is GONE (not merely unreadable) still ghosts its sessions."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        # A second project keeps the scan non-empty, so the removal under test
        # is ghost GC rather than the wrong-source-root guard.
        survivor = source_root / "-proj-b" / "cccc.jsonl"
        survivor.parent.mkdir(parents=True)
        survivor.write_text('{"type":"user","uuid":"u-1"}\n')
        os.utime(survivor, ns=(STALE_NS, STALE_NS))
        run(source_root, corpus_root, FakeConverter())

        shutil.rmtree(source_root / "-proj-a")
        report = run(source_root, corpus_root, FakeConverter())

        assert sorted(report.removed_session_ids) == sorted([SESSION_A, SESSION_B])
        assert report.unreadable_session_ids == ()
        assert CorpusLayout(corpus_root=corpus_root).session_dir("cccc").is_dir()

    def test_session_dir_lost_in_the_swap_window_is_replanned(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """A SIGKILL between the aside-rename and the swap leaves NO
        generation at sessions/<id>/ and no handler can restore it. The
        watermark records SOURCE mtimes only, so it still matches and every
        later pass would report up_to_date with the session dir missing
        forever. Absence of the dir must force a replan."""
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        run(source_root, corpus_root, FakeConverter())
        layout = CorpusLayout(corpus_root=corpus_root)
        watermark_before = json.loads(layout.watermark_path.read_text())

        # Exactly the post-kill state: dir gone, watermark untouched.
        shutil.rmtree(layout.session_dir(SESSION_A))

        recovery = FakeConverter()
        report = run(source_root, corpus_root, recovery)

        assert report.up_to_date_count == 0
        assert report.materialized_count == 1
        assert [p.stem for p in recovery.converted] == [SESSION_A]
        assert layout.trajectory_path(SESSION_A).is_file()
        assert layout.meta_path(SESSION_A).is_file()
        assert json.loads(layout.watermark_path.read_text()) == watermark_before


class TestTransientStatErrors:
    def test_unreadable_session_is_not_ghosted(
        self,
        source_root: Path,
        corpus_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A transient stat failure (EIO/ESTALE/permission blip) is not
        evidence the source is gone. Ghosting on it DELETES a live session's
        artifacts, which is unrecoverable from the corpus side."""
        main = write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())
        layout = CorpusLayout(corpus_root=corpus_root)

        real_stat = Path.stat

        def _flaky_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
            if self == main:
                raise OSError(5, "Input/output error")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _flaky_stat)
        report = run(source_root, corpus_root, FakeConverter())
        monkeypatch.undo()

        assert report.sessions_removed == 0
        assert report.removed_session_ids == ()
        assert layout.session_dir(SESSION_A).is_dir()
        assert layout.trajectory_path(SESSION_A).is_file()

        # Once the blip clears the session is untouched and still current.
        recovered = run(source_root, corpus_root, FakeConverter())
        assert recovered.up_to_date_count == 2
        assert recovered.sessions_removed == 0

    def test_genuinely_deleted_session_is_still_ghosted(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """Only FileNotFoundError ghosts a session; the dir is deleted, not tombstoned."""
        main = write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=STALE_NS)
        run(source_root, corpus_root, FakeConverter())

        main.unlink()
        report = run(source_root, corpus_root, FakeConverter())

        assert report.removed_session_ids == (SESSION_A,)
        assert not CorpusLayout(corpus_root=corpus_root).session_dir(SESSION_A).exists()


class TestReport:
    def test_counts_and_durations(self, source_root: Path, corpus_root: Path) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, SESSION_B, mtime_ns=LIVE_NS)
        report = run(source_root, corpus_root, FakeConverter())
        assert report.materialized_count == 1
        assert report.skipped_live_count == 1
        assert report.up_to_date_count == 0
        assert report.total_seconds >= report.convert_seconds >= 0.0


class TestReadWatermark:
    """A corrupt watermark degrades to empty; the cost is one full pass, never a crash.

    `read_watermark` is shared with the read-only `status` command, so a raise
    here takes down a read that mutates nothing.
    """

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert read_watermark(tmp_path / "absent.json") == {}

    def test_unparseable_json_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "watermark.json"
        path.write_text("{not json", encoding="utf-8")
        assert read_watermark(path) == {}

    def test_wrong_top_level_shape_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "watermark.json"
        path.write_text('["a", "b"]', encoding="utf-8")
        assert read_watermark(path) == {}

    def test_non_numeric_mtime_is_empty(self, tmp_path: Path) -> None:
        """The `int()` coercion is inside the guard, not after it.

        A watermark that parses as JSON and has the right top-level shape can
        still carry a value `int()` refuses. That failure surfaces at the
        comprehension, past every check above it.
        """
        path = tmp_path / "watermark.json"
        path.write_text('{"/a/b.jsonl": "not-a-number"}', encoding="utf-8")
        assert read_watermark(path) == {}

    def test_structured_mtime_is_empty(self, tmp_path: Path) -> None:
        """`int()` raises TypeError, not ValueError, on a list or a dict."""
        path = tmp_path / "watermark.json"
        path.write_text('{"/a/b.jsonl": [1, 2]}', encoding="utf-8")
        assert read_watermark(path) == {}

    def test_numeric_string_still_reads(self, tmp_path: Path) -> None:
        """Degrading is for corruption; a value `int()` accepts is not corruption."""
        path = tmp_path / "watermark.json"
        path.write_text('{"/a/b.jsonl": "12345"}', encoding="utf-8")
        assert read_watermark(path) == {"/a/b.jsonl": 12345}
