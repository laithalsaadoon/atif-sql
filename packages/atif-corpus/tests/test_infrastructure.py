# SPDX-License-Identifier: Apache-2.0

"""Scanner discovery rules + settings default factories."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from corpus_fixtures import SESSION_A, STALE_NS, write_session
from pydantic import ValidationError

from atif_corpus.domain.agents import AgentSource
from atif_corpus.infrastructure.scanner import scan_source_root
from atif_corpus.infrastructure.settings import (
    MAX_DEFAULT_MATERIALIZE_WORKERS,
    CorpusSettings,
    default_materialize_workers,
)


class TestScanner:
    def test_discovers_main_and_all_side_files_excluding_meta(self, source_root: Path) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS, with_side_files=True)
        sessions = scan_source_root(source_root)
        assert len(sessions) == 1
        session = sessions[0]
        assert session.session_id == SESSION_A
        names = [Path(p).name for p in session.source_files]
        assert f"{SESSION_A}.jsonl" in names
        assert "agent-aaaa.jsonl" in names
        assert "agent-bbbb.jsonl" in names  # workflow-nested, per contract rglob
        assert "agent-aaaa.meta.json" not in names
        assert session.newest_mtime_ns == STALE_NS

    def test_missing_root_yields_empty(self, tmp_path: Path) -> None:
        assert scan_source_root(tmp_path / "nope") == ()

    def test_scan_is_sorted_by_session_id(self, source_root: Path) -> None:
        write_session(source_root, "bbbb", mtime_ns=STALE_NS)
        write_session(source_root, "aaaa", mtime_ns=STALE_NS)
        sessions = scan_source_root(source_root)
        assert [s.session_id for s in sessions] == ["aaaa", "bbbb"]


class TestSettings:
    def test_defaults_follow_claude_config_dir_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config = tmp_path / "cfg"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
        settings = CorpusSettings()
        assert settings.source_root == config / "projects"
        # corpus_root defaults under ~/.atif-sql/corpus/<slug of source root>
        assert settings.corpus_root.parent == Path.home() / ".atif-sql" / "corpus"
        assert settings.corpus_root.name.startswith("projects-")
        assert settings.quiesce_seconds == 300

    def test_repointing_config_dir_changes_slug_without_reload(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "one"))
        first = CorpusSettings().corpus_root
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "two"))
        second = CorpusSettings().corpus_root
        assert first != second

    def test_env_overrides_win(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ATIF_SQL_CORPUS_ROOT", str(tmp_path / "explicit"))
        monkeypatch.setenv("ATIF_SQL_QUIESCE_SECONDS", "60")
        settings = CorpusSettings()
        assert settings.corpus_root == tmp_path / "explicit"
        assert settings.quiesce_seconds == 60

    def test_the_default_agent_is_claude_code(self) -> None:
        """Every caller predating Codex support observes exactly the old defaults."""
        assert CorpusSettings().agent is AgentSource.CLAUDE_CODE

    def test_materialize_workers_defaults_to_min_eight_and_cpu_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATIF_SQL_MATERIALIZE_WORKERS", raising=False)
        expected = max(1, min(MAX_DEFAULT_MATERIALIZE_WORKERS, os.cpu_count() or 1))
        assert CorpusSettings().materialize_workers == expected == default_materialize_workers()
        assert 1 <= expected <= 8

    def test_materialize_workers_reads_the_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATIF_SQL_MATERIALIZE_WORKERS", "3")
        assert CorpusSettings().materialize_workers == 3

    def test_materialize_workers_below_one_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATIF_SQL_MATERIALIZE_WORKERS", "0")
        with pytest.raises(ValidationError):
            CorpusSettings()

    def test_codex_takes_the_codex_home_and_its_own_corpus(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``--agent codex`` alone must not read the Claude Code source root."""
        monkeypatch.delenv("ATIF_SQL_SOURCE_ROOT", raising=False)
        monkeypatch.delenv("ATIF_SQL_CORPUS_ROOT", raising=False)
        codex_home = tmp_path / "codex-home"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        claude = CorpusSettings()
        codex = CorpusSettings(agent=AgentSource.CODEX)
        assert codex.source_root == codex_home / "sessions"
        assert codex.source_root != claude.source_root
        assert codex.corpus_root != claude.corpus_root

    def test_codex_home_is_read_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("ATIF_SQL_SOURCE_ROOT", raising=False)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "one"))
        first = CorpusSettings(agent=AgentSource.CODEX).source_root
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "two"))
        second = CorpusSettings(agent=AgentSource.CODEX).source_root
        assert first != second

    def test_an_explicit_source_root_beats_the_agent_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``ATIF_SQL_SOURCE_ROOT`` keeps meaning what it meant before Codex existed."""
        monkeypatch.delenv("ATIF_SQL_CORPUS_ROOT", raising=False)
        monkeypatch.setenv("ATIF_SQL_SOURCE_ROOT", str(tmp_path / "pinned"))
        settings = CorpusSettings(agent=AgentSource.CODEX)
        assert settings.source_root == tmp_path / "pinned"
        # ...and the corpus root still derives from the root actually in use.
        assert settings.corpus_root.name.startswith("pinned-")

    def test_an_explicit_corpus_root_beats_the_agent_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ATIF_SQL_CORPUS_ROOT", str(tmp_path / "explicit"))
        settings = CorpusSettings(agent=AgentSource.CODEX)
        assert settings.corpus_root == tmp_path / "explicit"

    def test_the_agent_is_settable_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATIF_SQL_AGENT", "codex")
        assert CorpusSettings().agent is AgentSource.CODEX
