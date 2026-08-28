# SPDX-License-Identifier: Apache-2.0

"""Pure-domain tests: quiescence edges, watermark delta, plan determinism, layout."""

from __future__ import annotations

from pathlib import Path

from atif_corpus.domain.layout import CorpusLayout
from atif_corpus.domain.sessions import (
    NANOS_PER_SECOND,
    QuiescencePolicy,
    SessionSource,
    build_plan,
    owns_path,
)
from atif_corpus.domain.watermark import diff_source_mtimes

NOW = 1_000_000 * NANOS_PER_SECOND


def _session(session_id: str, mtimes: dict[str, int]) -> SessionSource:
    main = f"/src/-proj/{session_id}.jsonl"
    return SessionSource(
        session_id=session_id,
        session_jsonl=main,
        source_mtimes={main: mtimes.pop("__main__", NOW - 3600 * NANOS_PER_SECOND), **mtimes},
    )


class TestQuiescence:
    policy = QuiescencePolicy(quiesce_seconds=300)

    def test_fresh_write_is_live(self) -> None:
        assert not self.policy.is_quiescent(NOW - 10 * NANOS_PER_SECOND, NOW)

    def test_old_write_is_quiescent(self) -> None:
        assert self.policy.is_quiescent(NOW - 3600 * NANOS_PER_SECOND, NOW)

    def test_boundary_is_inclusive(self) -> None:
        exactly = NOW - 300 * NANOS_PER_SECOND
        assert self.policy.is_quiescent(exactly, NOW)
        assert not self.policy.is_quiescent(exactly + 1, NOW)

    def test_future_mtime_stays_skipped_but_warns_loudly(self) -> None:
        """A clock step on the writing host makes is_quiescent false FOREVER,
        so the session starves in skipped_live. Staying skipped is right;
        starving MUTELY is not — the operator has to be able to see it."""
        from loguru import logger

        warnings: list[str] = []
        sink_id = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
        try:
            assert not self.policy.is_quiescent(NOW + 3600 * NANOS_PER_SECOND, NOW)
        finally:
            logger.remove(sink_id)

        assert len(warnings) == 1
        assert "FUTURE" in warnings[0]
        assert "3600.0s" in warnings[0]

    def test_normal_mtime_does_not_warn(self) -> None:
        from loguru import logger

        warnings: list[str] = []
        sink_id = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
        try:
            self.policy.is_quiescent(NOW - 3600 * NANOS_PER_SECOND, NOW)
            self.policy.is_quiescent(NOW, NOW)
        finally:
            logger.remove(sink_id)
        assert warnings == []


class TestWatermarkDelta:
    def test_partitions_added_modified_removed(self) -> None:
        previous = {"/a": 1, "/b": 2, "/c": 3}
        current = {"/b": 2, "/c": 9, "/d": 4}
        delta = diff_source_mtimes(previous, current)
        assert delta.added == ("/d",)
        assert delta.modified == ("/c",)
        assert delta.removed == ("/a",)
        assert delta.touched == ("/d", "/c")
        assert delta.changed_count == 3

    def test_backwards_mtime_counts_as_modified(self) -> None:
        delta = diff_source_mtimes({"/a": 100}, {"/a": 50})
        assert delta.modified == ("/a",)

    def test_identical_scans_are_empty(self) -> None:
        scan = {"/a": 1, "/b": 2}
        assert diff_source_mtimes(scan, dict(scan)).is_empty


class TestBuildPlan:
    policy = QuiescencePolicy(quiesce_seconds=300)

    def test_live_session_is_skipped(self) -> None:
        live = _session("s-live", {"__main__": NOW - 10 * NANOS_PER_SECOND})
        plan = build_plan([live], watermark={}, policy=self.policy, now_ns=NOW)
        assert plan.skipped_live == (live,)
        assert plan.is_noop

    def test_quiescent_and_stale_is_materialized(self) -> None:
        stale = _session("s-stale", {})
        plan = build_plan([stale], watermark={}, policy=self.policy, now_ns=NOW)
        assert plan.to_materialize == (stale,)

    def test_quiescent_and_fresh_is_up_to_date(self) -> None:
        fresh = _session("s-fresh", {})
        plan = build_plan(
            [fresh], watermark=dict(fresh.source_mtimes), policy=self.policy, now_ns=NOW
        )
        assert plan.up_to_date == (fresh,)
        assert plan.is_noop

    def test_force_rematerializes_fresh_but_not_live(self) -> None:
        fresh = _session("s-fresh", {})
        live = _session("s-live", {"__main__": NOW - 10 * NANOS_PER_SECOND})
        plan = build_plan(
            [fresh, live],
            watermark={**fresh.source_mtimes, **live.source_mtimes},
            policy=self.policy,
            now_ns=NOW,
            force=True,
        )
        assert plan.to_materialize == (fresh,)
        assert plan.skipped_live == (live,)

    def test_side_file_change_alone_makes_session_stale(self) -> None:
        side = "/src/-proj/s-1/subagents/workflows/wf_1/agent-a.jsonl"
        session = _session("s-1", {side: NOW - 3600 * NANOS_PER_SECOND})
        recorded = dict(session.source_mtimes)
        recorded[side] -= 1  # side-file moved; main file did not
        plan = build_plan([session], watermark=recorded, policy=self.policy, now_ns=NOW)
        assert plan.to_materialize == (session,)

    def test_deleted_side_file_makes_session_stale(self) -> None:
        session = _session("s-1", {})
        recorded = dict(session.source_mtimes)
        recorded["/src/-proj/s-1/subagents/agent-gone.jsonl"] = 123
        plan = build_plan([session], watermark=recorded, policy=self.policy, now_ns=NOW)
        assert plan.to_materialize == (session,)

    def test_plan_is_deterministic_and_sorted(self) -> None:
        sessions = [_session(f"s-{i}", {}) for i in (3, 1, 2)]
        plan_a = build_plan(sessions, watermark={}, policy=self.policy, now_ns=NOW)
        plan_b = build_plan(list(reversed(sessions)), watermark={}, policy=self.policy, now_ns=NOW)
        assert plan_a == plan_b
        assert [s.session_id for s in plan_a.to_materialize] == ["s-1", "s-2", "s-3"]


class TestStalenessConsumesTheDeltaRule:
    """_is_stale delegates to diff_source_mtimes, so the ported rule has
    exactly one home. These pin the delegation: every partition of a
    SourceDelta must be enough on its own to make a session stale, and an
    empty delta must not be."""

    policy = QuiescencePolicy(quiesce_seconds=300)

    def _stale(self, session: SessionSource, watermark: dict[str, int]) -> bool:
        plan = build_plan([session], watermark=watermark, policy=self.policy, now_ns=NOW)
        return plan.to_materialize == (session,)

    def test_each_delta_partition_alone_makes_a_session_stale(self) -> None:
        session = _session(
            "s-1", {"/src/-proj/s-1/subagents/agent-a.jsonl": NOW - 3600 * NANOS_PER_SECOND}
        )
        current = dict(session.source_mtimes)

        added = {k: v for k, v in current.items() if "agent-a" not in k}
        assert diff_source_mtimes(added, current).added
        assert self._stale(session, added)

        modified = {**current, session.session_jsonl: current[session.session_jsonl] - 1}
        assert diff_source_mtimes(modified, current).modified
        assert self._stale(session, modified)

        removed = {**current, "/src/-proj/s-1/subagents/agent-gone.jsonl": 7}
        assert diff_source_mtimes(removed, current).removed
        assert self._stale(session, removed)

    def test_empty_delta_is_not_stale(self) -> None:
        session = _session(
            "s-1", {"/src/-proj/s-1/subagents/agent-a.jsonl": NOW - 3600 * NANOS_PER_SECOND}
        )
        recorded = dict(session.source_mtimes)
        assert diff_source_mtimes(recorded, session.source_mtimes).is_empty
        assert not self._stale(session, recorded)

    def test_another_sessions_entries_never_leak_into_staleness(self) -> None:
        """Scope is owns_path: a sibling session's paths are outside it, so
        they can neither create nor mask staleness."""
        session = _session("s-1", {})
        recorded = {**session.source_mtimes, "/src/-proj/s-2.jsonl": 42}
        assert not self._stale(session, recorded)


class TestOwnsPath:
    main = "/src/-proj/s-1.jsonl"

    def test_main_and_side_files_are_owned(self) -> None:
        assert owns_path(self.main, self.main)
        assert owns_path(self.main, "/src/-proj/s-1/subagents/agent-a.jsonl")
        assert owns_path(self.main, "/src/-proj/s-1/subagents/workflows/wf_1/agent-b.jsonl")

    def test_lookalike_siblings_are_not_owned(self) -> None:
        assert not owns_path(self.main, "/src/-proj/s-10.jsonl")
        assert not owns_path(self.main, "/src/-proj/s-10/subagents/agent-a.jsonl")
        assert not owns_path(self.main, "/src/-proj/s-1-backup/subagents/agent-a.jsonl")


class TestUnmaterializedReplan:
    policy = QuiescencePolicy(quiesce_seconds=300)

    def test_a_session_with_no_artifacts_replans_despite_a_matching_watermark(self) -> None:
        session = _session("s-1", {})
        watermark = dict(session.source_mtimes)
        plan = build_plan(
            [session],
            watermark=watermark,
            policy=self.policy,
            now_ns=NOW,
            unmaterialized_session_ids={"s-1"},
        )
        assert plan.to_materialize == (session,)

    def test_a_live_session_is_still_not_converted(self) -> None:
        """Absent artifacts do not license converting a half-written transcript."""
        live = _session("s-live", {"__main__": NOW - 10 * NANOS_PER_SECOND})
        plan = build_plan(
            [live],
            watermark=dict(live.source_mtimes),
            policy=self.policy,
            now_ns=NOW,
            unmaterialized_session_ids={"s-live"},
        )
        assert plan.skipped_live == (live,)
        assert plan.is_noop


class TestCorpusLayout:
    layout = CorpusLayout(corpus_root=Path("/corpus"))

    def test_contract_paths(self) -> None:
        sid = "11111111-1111-1111-1111-111111111111"
        assert self.layout.trajectory_path(sid) == Path(f"/corpus/sessions/{sid}/trajectory.json")
        assert self.layout.loss_report_path(sid) == Path(f"/corpus/sessions/{sid}/loss_report.json")
        assert self.layout.edges_path(sid) == Path(f"/corpus/sessions/{sid}/edges.jsonl")
        assert self.layout.meta_path(sid) == Path(f"/corpus/sessions/{sid}/meta.json")
        assert self.layout.watermark_path == Path("/corpus/watermark.json")
