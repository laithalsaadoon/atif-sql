# SPDX-License-Identifier: Apache-2.0

"""The columnar lever through the CLI: materialize writes it, status names it, query uses it.

Everything here goes through the REAL converter and the REAL producer into a
tmp corpus (seconds, not minutes), because the composition is the point:
atif-cli is the one place the atif-corpus port and the atif-duck producer meet.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from cli_fixtures import write_synthetic_session

from atif_cli.app import _print_report, materialize, query, status
from atif_cli.errors import EXIT_CODES
from atif_cli.output import OutputFormat

SESSION_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

#: The exact panel statements the lever is measured on, plus two more windows.
PANEL_QUERIES: tuple[str, ...] = (
    "SELECT count(*) AS n, count(DISTINCT model_name) AS models FROM sessions",
    (
        "SELECT tool_name, count(*) AS n FROM tool_calls "
        "WHERE ts >= TIMESTAMP '2026-03-01' AND ts < TIMESTAMP '2026-09-01' "
        "AND tool_name IS NOT NULL GROUP BY 1 ORDER BY n DESC, tool_name LIMIT 40"
    ),
    (
        "SELECT source, sum(prompt_tokens) AS prompt, sum(completion_tokens) AS completion, "
        "sum(cached_tokens) AS cached, count(*) AS steps FROM steps "
        "WHERE ts >= TIMESTAMP '2026-03-01' AND ts < TIMESTAMP '2026-09-01' GROUP BY 1 ORDER BY 1"
    ),
    (
        "SELECT session_id, step_id, ts, source, model_name, message, is_sidechain, "
        "prompt_tokens, completion_tokens, cached_tokens, cache_creation, llm_call_count, "
        "source_uuids FROM steps ORDER BY session_id, step_id"
    ),
    (
        "SELECT session_id, step_id, tool_name, tool_use_id, tool_input FROM tool_calls "
        "ORDER BY session_id, step_id, tool_use_id"
    ),
)


@pytest.fixture
def source_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("ATIF_SQL_CORPUS_ROOT", "ATIF_SQL_LANCE_URI", "ATIF_SQL_EMBED_MODEL_ID"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "projects"
    write_synthetic_session(root, SESSION_A)
    write_synthetic_session(root, SESSION_B)
    return root


def _materialize(
    source_root: Path, corpus_root: Path, capsys: pytest.CaptureFixture[str], **kw: object
) -> dict[str, Any]:
    materialize(source_root=source_root, corpus_root=corpus_root, fmt=OutputFormat.JSON, **kw)  # type: ignore[arg-type]
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return payload


def _status(
    source_root: Path, corpus_root: Path, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    status(source_root=source_root, corpus_root=corpus_root, fmt=OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return payload


def _query_json(sql: str, corpus_root: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """The raw ``--format json`` bytes ``query`` writes, for byte-identity checks."""
    query(sql, corpus_root=corpus_root, fmt=OutputFormat.JSON)
    return capsys.readouterr().out


@pytest.mark.integration
class TestMaterializeWritesColumnar:
    def test_default_pass_writes_the_artifacts_and_status_says_columnar(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        corpus = tmp_path / "corpus"
        report = _materialize(source_root, corpus, capsys)
        assert report["materialized"] == 2
        assert report["failed"] == 0
        assert isinstance(report["artifact_seconds"], float)
        assert report["artifact_seconds"] > 0.0

        for session_id in (SESSION_A, SESSION_B):
            session_dir = corpus / "sessions" / session_id
            for name in (
                "session.parquet",
                "steps.parquet",
                "tool_calls.parquet",
                "tool_results.parquet",
            ):
                path = session_dir / name
                assert path.is_file(), path
                assert stat.S_IMODE(path.stat().st_mode) == 0o444
            meta = json.loads((session_dir / "meta.json").read_text())
            assert meta["columnar_schema"] == 1

        st = _status(source_root, corpus, capsys)
        assert st["query_path"] == "columnar"
        assert st["columnar_sessions"] == 2
        assert st["json_sessions"] == 0

    def test_no_columnar_writes_only_the_contract_artifacts(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        corpus = tmp_path / "plain"
        report = _materialize(source_root, corpus, capsys, columnar=False)
        assert report["materialized"] == 2
        assert report["artifact_seconds"] == 0.0
        names = sorted(p.name for p in (corpus / "sessions" / SESSION_A).iterdir())
        assert names == ["edges.jsonl", "loss_report.json", "meta.json", "trajectory.json"]
        st = _status(source_root, corpus, capsys)
        assert st["query_path"] == "json"
        assert st["columnar_sessions"] == 0

    def test_status_reports_mixed_when_only_some_sessions_carry_them(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        corpus = tmp_path / "mixed"
        _materialize(source_root, corpus, capsys, columnar=False)
        _materialize(source_root, corpus, capsys, force=True, sessions=SESSION_A)
        st = _status(source_root, corpus, capsys)
        assert st["query_path"] == "mixed"
        assert (st["columnar_sessions"], st["json_sessions"]) == (1, 1)

    def test_table_report_prints_the_columnar_seconds(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from atif_corpus.application.materialize import MaterializationReport

        report = MaterializationReport(
            materialized_count=1,
            up_to_date_count=0,
            skipped_live_count=0,
            failures=(),
            total_seconds=2.0,
            convert_seconds=1.0,
            artifact_seconds=0.25,
        )
        _print_report(report, OutputFormat.TABLE)
        assert "columnar: 0.25s" in capsys.readouterr().out


@pytest.mark.integration
class TestQueryUsesColumnar:
    def test_panel_output_is_byte_identical_across_the_two_paths(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        columnar = tmp_path / "columnar"
        plain = tmp_path / "plain"
        _materialize(source_root, columnar, capsys)
        _materialize(source_root, plain, capsys, columnar=False)

        for sql in PANEL_QUERIES:
            assert _query_json(sql, columnar, capsys) == _query_json(sql, plain, capsys), sql
        # Not vacuous: the synthetic sessions produce rows on every panel.
        assert json.loads(_query_json(PANEL_QUERIES[0], columnar, capsys)) == [
            {"n": 2, "models": 1}
        ]
        assert json.loads(_query_json(PANEL_QUERIES[4], columnar, capsys))

    def test_query_does_not_fall_back_to_json_when_columnar_is_present(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Destroy every trajectory.json: the sandboxed query must still answer.

        A registry that silently parsed JSON would fail here, so this is the
        CLI-level proof that the parquet reader is what serves the query.
        """
        corpus = tmp_path / "corpus"
        _materialize(source_root, corpus, capsys)
        expected = [_query_json(sql, corpus, capsys) for sql in PANEL_QUERIES]
        for session_id in (SESSION_A, SESSION_B):
            (corpus / "sessions" / session_id / "trajectory.json").write_text("not json")

        assert _status(source_root, corpus, capsys)["query_path"] == "columnar"
        for sql, before in zip(PANEL_QUERIES, expected, strict=True):
            assert _query_json(sql, corpus, capsys) == before, sql

    def test_sandbox_grants_the_columnar_files_read_only(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A view may read steps.parquet; injected SQL may not rewrite it."""
        corpus = tmp_path / "corpus"
        _materialize(source_root, corpus, capsys)
        target = corpus / "sessions" / SESSION_A / "steps.parquet"
        before = target.read_bytes()

        rows = json.loads(
            _query_json(f"SELECT count(*) AS n FROM read_parquet('{target}')", corpus, capsys)
        )
        assert rows[0]["n"] > 0

        for statement in (
            f"COPY (SELECT 1 AS a) TO '{target}' (USE_TMP_FILE false)",
            f"COPY (SELECT 1 AS a) TO '{target}'",
            f"COPY (SELECT 1 AS a) TO '{target.with_name('pwned.parquet')}'",
        ):
            with pytest.raises(SystemExit) as excinfo:
                query(statement, corpus_root=corpus, fmt=OutputFormat.JSON)
            assert excinfo.value.code == EXIT_CODES["runtime_error"], statement
            capsys.readouterr()
        assert target.read_bytes() == before
        assert not target.with_name("pwned.parquet").exists()
