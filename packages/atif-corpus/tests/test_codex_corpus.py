# SPDX-License-Identifier: Apache-2.0

"""Codex discovery and materialization: the depth-3 layout end to end.

The Claude Code equivalents live in ``test_infrastructure.py`` and
``test_materialize.py``; this module covers what a three-level source root
changes, and pins the behaviors that would silently destroy a corpus if the
generalized scanner lost them: a directory that cannot be LISTED at ANY level
must not make its sessions look deleted.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from corpus_fixtures import LIVE_NS, NOW_NS, STALE_NS, write_session

from atif_corpus.application.materialize import CorpusAgentMismatchError, materialize
from atif_corpus.domain.agents import AgentSource
from atif_corpus.domain.layout import CorpusLayout
from atif_corpus.domain.source_layout import CLAUDE_CODE_LAYOUT, CODEX_LAYOUT
from atif_corpus.infrastructure.fake_converter import FakeConverter
from atif_corpus.infrastructure.scanner import scan_source_root, scan_sources

SESSION_A = "01a09182-2858-7f42-936b-7f027b341fdf"
SESSION_B = "01a08db0-09a9-7b02-b690-62b5323644ff"


def write_rollout(
    sessions_root: Path,
    session_id: str,
    *,
    mtime_ns: int,
    day: tuple[str, str, str] = ("2026", "09", "11"),
) -> Path:
    """Create one rollout in Codex's ``<YYYY>/<MM>/<DD>/`` layout with a fixed mtime."""
    day_dir = sessions_root.joinpath(*day)
    day_dir.mkdir(parents=True, exist_ok=True)
    rollout = day_dir / f"rollout-{day[0]}-{day[1]}-{day[2]}T00-00-00-{session_id}.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": session_id}}) + "\n")
    os.utime(rollout, ns=(mtime_ns, mtime_ns))
    return rollout


@pytest.fixture
def codex_source_root(tmp_path: Path) -> Path:
    root = tmp_path / "sessions"
    root.mkdir()
    return root


class TestCodexDiscovery:
    def test_finds_rollouts_three_levels_down(self, codex_source_root: Path) -> None:
        write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        write_rollout(codex_source_root, SESSION_B, mtime_ns=STALE_NS, day=("2026", "09", "10"))
        sessions = scan_source_root(codex_source_root, CODEX_LAYOUT)
        assert {session.session_id for session in sessions} == {SESSION_A, SESSION_B}

    def test_session_id_is_the_filename_uuid_not_the_stem(self, codex_source_root: Path) -> None:
        write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        session = scan_source_root(codex_source_root, CODEX_LAYOUT)[0]
        assert session.session_id == SESSION_A
        assert Path(session.session_jsonl).stem != SESSION_A

    def test_a_rollout_carries_exactly_one_source_file(self, codex_source_root: Path) -> None:
        """No side-file tree exists for a rollout, so the watermark has one entry."""
        rollout = write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        session = scan_source_root(codex_source_root, CODEX_LAYOUT)[0]
        assert session.source_files == (str(rollout),)

    def test_a_sibling_directory_named_after_a_rollout_is_not_walked(
        self, codex_source_root: Path
    ) -> None:
        """``has_side_files=False`` means a lookalike dir cannot inflate the watermark."""
        rollout = write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        decoy = rollout.parent / rollout.stem
        decoy.mkdir()
        (decoy / "agent-x.jsonl").write_text("{}\n")
        session = scan_source_root(codex_source_root, CODEX_LAYOUT)[0]
        assert session.source_files == (str(rollout),)

    def test_a_non_rollout_jsonl_is_skipped(self, codex_source_root: Path) -> None:
        """Codex keeps other JSONL beside its rollouts; only rollouts are sessions."""
        write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        (codex_source_root / "2026" / "09" / "11" / "history.jsonl").write_text("{}\n")
        sessions = scan_source_root(codex_source_root, CODEX_LAYOUT)
        assert [session.session_id for session in sessions] == [SESSION_A]

    def test_a_missing_root_yields_an_empty_scan(self, tmp_path: Path) -> None:
        assert scan_source_root(tmp_path / "nope", CODEX_LAYOUT) == ()

    def test_scan_is_sorted_by_session_id(self, codex_source_root: Path) -> None:
        write_rollout(codex_source_root, SESSION_B, mtime_ns=STALE_NS)
        write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        sessions = scan_source_root(codex_source_root, CODEX_LAYOUT)
        assert [session.session_id for session in sessions] == sorted(
            [session.session_id for session in sessions]
        )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
class TestUnlistableIntermediateDirectories:
    def test_an_unlistable_day_dir_is_reported_not_treated_as_empty(
        self, codex_source_root: Path
    ) -> None:
        """Three levels means three chances to mistake 'cannot open' for 'empty'."""
        write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        day_dir = codex_source_root / "2026" / "09" / "11"
        day_dir.chmod(0o000)
        try:
            scan = scan_sources(codex_source_root, CODEX_LAYOUT)
        finally:
            day_dir.chmod(stat.S_IRWXU)
        assert scan.sessions == ()
        assert str(day_dir) in scan.unlistable_dirs

    def test_an_unlistable_month_dir_is_reported(self, codex_source_root: Path) -> None:
        write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        month_dir = codex_source_root / "2026" / "09"
        month_dir.chmod(0o000)
        try:
            scan = scan_sources(codex_source_root, CODEX_LAYOUT)
        finally:
            month_dir.chmod(stat.S_IRWXU)
        assert scan.sessions == ()
        assert str(month_dir) in scan.unlistable_dirs

    def test_an_unlistable_day_dir_does_not_ghost_its_sessions(
        self, codex_source_root: Path, tmp_path: Path
    ) -> None:
        """The data-loss case: absence behind a closed door is not deletion."""
        corpus_root = tmp_path / "corpus"
        write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        first = _materialize_codex(codex_source_root, corpus_root)
        assert first.materialized_count == 1
        session_dir = CorpusLayout(corpus_root=corpus_root).session_dir(SESSION_A)
        assert session_dir.is_dir()

        day_dir = codex_source_root / "2026" / "09" / "11"
        day_dir.chmod(0o000)
        try:
            second = _materialize_codex(codex_source_root, corpus_root)
        finally:
            day_dir.chmod(stat.S_IRWXU)
        assert second.removed_session_ids == ()
        assert session_dir.is_dir(), "artifacts were removed over a permission error"
        assert SESSION_A in second.unreadable_session_ids


def _materialize_codex(source_root: Path, corpus_root: Path, **kwargs: object):
    """One Codex materialization pass with the fake converter and a pinned clock."""
    return materialize(
        source_root=source_root,
        corpus_root=corpus_root,
        converter=FakeConverter(),
        materialized_at="2026-09-11T00:00:00Z",
        harbor_version="0.22.0",
        converter_version="0.1.0",
        now_ns=NOW_NS,
        source_layout=CODEX_LAYOUT,
        **kwargs,  # type: ignore[arg-type]
    )


class TestCodexMaterialization:
    def test_materializes_a_rollout_with_the_full_contract_layout(
        self, codex_source_root: Path, tmp_path: Path
    ) -> None:
        corpus_root = tmp_path / "corpus"
        write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        report = _materialize_codex(codex_source_root, corpus_root)
        assert report.materialized_count == 1
        layout = CorpusLayout(corpus_root=corpus_root)
        for path in (
            layout.trajectory_path(SESSION_A),
            layout.loss_report_path(SESSION_A),
            layout.edges_path(SESSION_A),
            layout.meta_path(SESSION_A),
        ):
            assert path.is_file(), f"missing artifact {path.name}"

    def test_meta_json_records_the_agent(self, codex_source_root: Path, tmp_path: Path) -> None:
        """Provenance: a corpus copied out of place still says what wrote it."""
        corpus_root = tmp_path / "corpus"
        write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        _materialize_codex(codex_source_root, corpus_root)
        meta = json.loads(CorpusLayout(corpus_root=corpus_root).meta_path(SESSION_A).read_text())
        assert meta["agent"] == AgentSource.CODEX.value
        assert meta["session_id"] == SESSION_A

    def test_a_live_rollout_is_skipped_then_materialized_once_quiescent(
        self, codex_source_root: Path, tmp_path: Path
    ) -> None:
        corpus_root = tmp_path / "corpus"
        rollout = write_rollout(codex_source_root, SESSION_A, mtime_ns=LIVE_NS)
        first = _materialize_codex(codex_source_root, corpus_root)
        assert first.materialized_count == 0
        assert first.skipped_live_count == 1
        os.utime(rollout, ns=(STALE_NS, STALE_NS))
        second = _materialize_codex(codex_source_root, corpus_root)
        assert second.materialized_count == 1

    def test_a_second_pass_is_a_noop(self, codex_source_root: Path, tmp_path: Path) -> None:
        corpus_root = tmp_path / "corpus"
        write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        _materialize_codex(codex_source_root, corpus_root)
        second = _materialize_codex(codex_source_root, corpus_root)
        assert second.materialized_count == 0
        assert second.up_to_date_count == 1

    def test_a_deleted_rollout_removes_its_corpus_dir(
        self, codex_source_root: Path, tmp_path: Path
    ) -> None:
        corpus_root = tmp_path / "corpus"
        rollout = write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        write_rollout(codex_source_root, SESSION_B, mtime_ns=STALE_NS)
        _materialize_codex(codex_source_root, corpus_root)
        rollout.unlink()
        report = _materialize_codex(codex_source_root, corpus_root)
        assert report.removed_session_ids == (SESSION_A,)
        assert not CorpusLayout(corpus_root=corpus_root).session_dir(SESSION_A).exists()

    def test_a_touched_rollout_rematerializes(
        self, codex_source_root: Path, tmp_path: Path
    ) -> None:
        corpus_root = tmp_path / "corpus"
        rollout = write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        _materialize_codex(codex_source_root, corpus_root)
        newer = STALE_NS + 1_000_000_000
        os.utime(rollout, ns=(newer, newer))
        report = _materialize_codex(codex_source_root, corpus_root)
        assert report.materialized_count == 1


class TestOneCorpusHoldsOneAgent:
    """The corpus's own ``meta.agent`` is a discriminator, not just provenance.

    Per-agent default roots keep the two agents apart on their own, but an
    explicit ``--corpus-root`` / ``ATIF_SQL_CORPUS_ROOT`` defeats them. Aimed at
    the other agent's corpus, every session there is a ghost by construction, so
    the pass would delete all of them and report it as "source vanished".
    """

    def test_a_codex_pass_refuses_a_claude_corpus(
        self, tmp_path: Path, codex_source_root: Path
    ) -> None:
        claude_source = tmp_path / "projects"
        claude_source.mkdir()
        write_session(claude_source, SESSION_A, mtime_ns=STALE_NS)
        corpus_root = tmp_path / "corpus"
        first = materialize(
            source_root=claude_source,
            corpus_root=corpus_root,
            converter=FakeConverter(),
            materialized_at="2026-09-11T00:00:00Z",
            harbor_version="0.22.0",
            converter_version="0.1.0",
            now_ns=NOW_NS,
            source_layout=CLAUDE_CODE_LAYOUT,
        )
        assert first.materialized_count == 1

        write_rollout(codex_source_root, SESSION_B, mtime_ns=STALE_NS)
        with pytest.raises(CorpusAgentMismatchError) as caught:
            _materialize_codex(codex_source_root, corpus_root)

        assert "one agent" in str(caught.value)
        layout = CorpusLayout(corpus_root)
        assert layout.session_dir(SESSION_A).is_dir(), "the Claude session was deleted"
        assert not layout.session_dir(SESSION_B).exists(), "the refusal wrote anyway"

    def test_a_claude_pass_refuses_a_codex_corpus(
        self, tmp_path: Path, codex_source_root: Path
    ) -> None:
        corpus_root = tmp_path / "corpus"
        write_rollout(codex_source_root, SESSION_B, mtime_ns=STALE_NS)
        assert _materialize_codex(codex_source_root, corpus_root).materialized_count == 1

        claude_source = tmp_path / "projects"
        claude_source.mkdir()
        write_session(claude_source, SESSION_A, mtime_ns=STALE_NS)
        with pytest.raises(CorpusAgentMismatchError):
            materialize(
                source_root=claude_source,
                corpus_root=corpus_root,
                converter=FakeConverter(),
                materialized_at="2026-09-11T00:00:00Z",
                harbor_version="0.22.0",
                converter_version="0.1.0",
                now_ns=NOW_NS,
                source_layout=CLAUDE_CODE_LAYOUT,
            )
        assert CorpusLayout(corpus_root).session_dir(SESSION_B).is_dir()

    def test_a_corpus_written_before_the_agent_key_reads_as_claude_code(
        self, tmp_path: Path, codex_source_root: Path
    ) -> None:
        """No corpus predating Codex support can hold Codex sessions.

        So a ``meta.json`` with no ``agent`` key answers ``claude-code`` — which
        is what protects the corpora that were on disk before this feature.
        """
        claude_source = tmp_path / "projects"
        claude_source.mkdir()
        write_session(claude_source, SESSION_A, mtime_ns=STALE_NS)
        corpus_root = tmp_path / "corpus"
        materialize(
            source_root=claude_source,
            corpus_root=corpus_root,
            converter=FakeConverter(),
            materialized_at="2026-09-11T00:00:00Z",
            harbor_version="0.22.0",
            converter_version="0.1.0",
            now_ns=NOW_NS,
            source_layout=CLAUDE_CODE_LAYOUT,
        )
        meta_path = CorpusLayout(corpus_root).meta_path(SESSION_A)
        meta = json.loads(meta_path.read_text())
        del meta["agent"]
        meta_path.write_text(json.dumps(meta))

        write_rollout(codex_source_root, SESSION_B, mtime_ns=STALE_NS)
        with pytest.raises(CorpusAgentMismatchError):
            _materialize_codex(codex_source_root, corpus_root)
        assert CorpusLayout(corpus_root).session_dir(SESSION_A).is_dir()

    def test_the_same_agent_twice_is_not_a_mismatch(
        self, tmp_path: Path, codex_source_root: Path
    ) -> None:
        """The guard must not fire on the normal case: two passes, one agent."""
        corpus_root = tmp_path / "corpus"
        write_rollout(codex_source_root, SESSION_A, mtime_ns=STALE_NS)
        assert _materialize_codex(codex_source_root, corpus_root).materialized_count == 1
        write_rollout(codex_source_root, SESSION_B, mtime_ns=STALE_NS)
        second = _materialize_codex(codex_source_root, corpus_root)
        assert second.materialized_count == 1
        assert second.removed_session_ids == ()
