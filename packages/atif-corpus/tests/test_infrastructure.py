# SPDX-License-Identifier: Apache-2.0

"""Scanner discovery rules + settings default factories."""

from __future__ import annotations

from pathlib import Path

import pytest
from corpus_fixtures import SESSION_A, STALE_NS, write_session

from atif_corpus.infrastructure.scanner import scan_source_root
from atif_corpus.infrastructure.settings import CorpusSettings


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
