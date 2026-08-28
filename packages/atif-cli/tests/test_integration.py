# SPDX-License-Identifier: Apache-2.0

"""Integration: materialize 2 synthetic sessions end-to-end, then query them.

Marked ``integration`` but kept fast (~seconds): two 4-event sessions
through the REAL converter (harbor) into a tmp corpus, then a count query
through the CLI command functions — no subprocess except one ``python -m``
smoke that pins the console entry point.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from cli_fixtures import write_synthetic_session

from atif_cli.app import materialize, query, status
from atif_cli.output import OutputFormat

SESSION_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def tiny_corpus(tmp_path: Path) -> tuple[Path, Path]:
    """(source_root, corpus_root) with two quiescent convertible sessions."""
    source_root = tmp_path / "projects"
    write_synthetic_session(source_root, SESSION_A)
    write_synthetic_session(source_root, SESSION_B)
    return source_root, tmp_path / "corpus"


@pytest.mark.integration
class TestEndToEnd:
    def test_materialize_then_query_counts_two_sessions(
        self,
        tiny_corpus: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source_root, corpus_root = tiny_corpus
        monkeypatch.delenv("ATIF_SQL_CORPUS_ROOT", raising=False)

        materialize(source_root=source_root, corpus_root=corpus_root, fmt=OutputFormat.JSON)
        report = json.loads(capsys.readouterr().out)
        assert report["materialized"] == 2
        assert report["failed"] == 0

        # Contract layout on disk, meta stamped with the CLI's provenance.
        meta = json.loads((corpus_root / "sessions" / SESSION_A / "meta.json").read_text())
        assert meta["harbor_version"]
        assert meta["converter_version"]
        assert meta["materialized_at"]

        query(
            "SELECT count(*) AS n FROM sessions",
            corpus_root=corpus_root,
            fmt=OutputFormat.JSON,
        )
        rows = json.loads(capsys.readouterr().out)
        assert rows == [{"n": 2}]

        status(source_root=source_root, corpus_root=corpus_root, fmt=OutputFormat.JSON)
        st = json.loads(capsys.readouterr().out)
        assert st["materialized_sessions"] == 2
        assert st["staleness"]["up_to_date"] == 2
        assert st["staleness"]["stale"] == 0

    def test_sessions_filter_materializes_only_named_session(
        self,
        tiny_corpus: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source_root, corpus_root = tiny_corpus
        monkeypatch.delenv("ATIF_SQL_CORPUS_ROOT", raising=False)

        materialize(
            source_root=source_root,
            corpus_root=corpus_root,
            sessions=SESSION_A,
            fmt=OutputFormat.JSON,
        )
        report = json.loads(capsys.readouterr().out)
        assert report["materialized"] == 1
        assert (corpus_root / "sessions" / SESSION_A).is_dir()
        assert not (corpus_root / "sessions" / SESSION_B).exists()

    def test_query_classifies_unknown_view_as_catalog_error(
        self,
        tiny_corpus: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from atif_cli.errors import EXIT_CODES

        source_root, corpus_root = tiny_corpus
        monkeypatch.delenv("ATIF_SQL_CORPUS_ROOT", raising=False)
        materialize(source_root=source_root, corpus_root=corpus_root, fmt=OutputFormat.JSON)
        capsys.readouterr()

        with pytest.raises(SystemExit) as excinfo:
            query(
                "SELECT * FROM no_such_view",
                corpus_root=corpus_root,
                fmt=OutputFormat.JSON,
            )
        assert excinfo.value.code == EXIT_CODES["catalog_error"]
        err = json.loads(capsys.readouterr().err)
        assert err["error"]["kind"] == "catalog_error"


@pytest.mark.integration
def test_python_m_smoke_schema() -> None:
    """One subprocess smoke: ``python -m atif_cli schema --format json`` works."""
    result = subprocess.run(
        [sys.executable, "-m", "atif_cli", "schema", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[:500]
    payload = json.loads(result.stdout)
    assert "sessions" in payload["views"]
    assert any(m["name"] == "ago" for m in payload["macros"])
