# SPDX-License-Identifier: Apache-2.0

"""The storage layout through the CLI: what materialize writes, and that the old layout still reads.

materialize stores each session's JSON artifacts zstd-compressed and writes no
per-session parquet. A corpus written before that (plain JSON plus the five
parquet files, built here by ``to_legacy_layout``) keeps working until
``atif-sql corpus slim`` converts it: ``query`` without the lake reads either
layout, and a corpus holding both, to the same bytes.

Everything goes through the REAL converter into a tmp corpus (seconds, not
minutes), because the composition is the point.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from cli_fixtures import to_legacy_layout, write_synthetic_session

from atif_cli.app import materialize, query, status
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
    query(sql, corpus_root=corpus_root, lake=False, fmt=OutputFormat.JSON)
    return capsys.readouterr().out


NEW_LAYOUT = [
    "edges.jsonl.zst",
    "loss_report.json",
    "meta.json",
    "session_events.jsonl.zst",
    "source",
    "trajectory.json.zst",
]
PARQUET = (
    "session.parquet",
    "steps.parquet",
    "tool_calls.parquet",
    "tool_results.parquet",
    "session_events.parquet",
)


@pytest.mark.integration
class TestMaterializeWritesTheCompressedLayout:
    def test_a_pass_writes_compressed_json_and_no_parquet(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        corpus = tmp_path / "corpus"
        report = _materialize(source_root, corpus, capsys)
        assert report["materialized"] == 2
        assert report["failed"] == 0
        assert report["artifact_seconds"] == 0.0
        for session_id in (SESSION_A, SESSION_B):
            session_dir = corpus / "sessions" / session_id
            assert sorted(p.name for p in session_dir.iterdir()) == NEW_LAYOUT
            meta = json.loads((session_dir / "meta.json").read_text())
            assert "columnar_schema" not in meta
        st = _status(source_root, corpus, capsys)
        assert st["query_path"] == "json"
        assert st["columnar_sessions"] == 0
        assert st["layout"] == {
            "sessions": 2,
            "legacy_sessions": 0,
            "plain_bytes": 0,
            "parquet_bytes": 0,
        }
        # Nothing is stale: the layout is not part of what "current" means.
        assert st["staleness"]["stale"] == 0

    def test_an_old_layout_session_is_current_and_not_rewritten(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Deploying this version converts nothing on its own."""
        corpus = tmp_path / "corpus"
        _materialize(source_root, corpus, capsys)
        legacy = to_legacy_layout(corpus, SESSION_A)
        before = {p.name: p.read_bytes() for p in legacy.iterdir() if p.is_file()}
        st = _status(source_root, corpus, capsys)
        assert st["staleness"]["stale"] == 0
        assert st["layout"]["legacy_sessions"] == 1
        assert st["layout"]["plain_bytes"] > 0
        assert st["layout"]["parquet_bytes"] > 0
        assert st["query_path"] == "mixed"
        report = _materialize(source_root, corpus, capsys)
        assert report["materialized"] == 0
        assert {p.name: p.read_bytes() for p in legacy.iterdir() if p.is_file()} == before


@pytest.mark.integration
class TestQueryReadsEitherLayout:
    def test_panel_output_is_byte_identical_across_the_layouts(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        compressed = tmp_path / "compressed"
        legacy = tmp_path / "legacy"
        plain = tmp_path / "plain"
        mixed = tmp_path / "mixed"
        for corpus in (compressed, legacy, plain, mixed):
            _materialize(source_root, corpus, capsys)
        for session_id in (SESSION_A, SESSION_B):
            to_legacy_layout(legacy, session_id)
            to_legacy_layout(plain, session_id, parquet=False)
        to_legacy_layout(mixed, SESSION_A)
        assert _status(source_root, legacy, capsys)["query_path"] == "columnar"

        for sql in PANEL_QUERIES:
            expected = _query_json(sql, compressed, capsys)
            for corpus in (legacy, plain, mixed):
                assert _query_json(sql, corpus, capsys) == expected, (corpus.name, sql)
        # Not vacuous: the synthetic sessions produce rows on every panel.
        assert json.loads(_query_json(PANEL_QUERIES[0], compressed, capsys)) == [
            {"n": 2, "models": 1}
        ]
        assert json.loads(_query_json(PANEL_QUERIES[4], compressed, capsys))
        # The path column names the logical document on every layout.
        paths = json.loads(
            _query_json("SELECT trajectory_path FROM sessions ORDER BY 1", compressed, capsys)
        )
        assert [row["trajectory_path"] for row in paths] == [
            str(compressed / "sessions" / sid / "trajectory.json") for sid in (SESSION_A, SESSION_B)
        ]

    def test_query_does_not_fall_back_to_json_when_old_parquet_is_present(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Destroy every trajectory.json of an old-layout corpus: the sandboxed query must still answer.

        A registry that silently parsed JSON would fail here, so this is the
        CLI-level proof that the parquet reader serves an old-layout session.
        """
        corpus = tmp_path / "corpus"
        _materialize(source_root, corpus, capsys)
        for session_id in (SESSION_A, SESSION_B):
            to_legacy_layout(corpus, session_id)
        expected = [_query_json(sql, corpus, capsys) for sql in PANEL_QUERIES]
        for session_id in (SESSION_A, SESSION_B):
            (corpus / "sessions" / session_id / "trajectory.json").write_text("not json")
        assert _status(source_root, corpus, capsys)["query_path"] == "columnar"
        for sql, before in zip(PANEL_QUERIES, expected, strict=True):
            assert _query_json(sql, corpus, capsys) == before, sql

    def test_sandbox_grants_the_old_parquet_read_only(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A view may read steps.parquet; injected SQL may not rewrite it."""
        corpus = tmp_path / "corpus"
        _materialize(source_root, corpus, capsys)
        to_legacy_layout(corpus, SESSION_A)
        target = corpus / "sessions" / SESSION_A / "steps.parquet"
        assert stat.S_IMODE(target.stat().st_mode) == 0o444
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
                query(statement, corpus_root=corpus, lake=False, fmt=OutputFormat.JSON)
            assert excinfo.value.code == EXIT_CODES["runtime_error"], statement
            capsys.readouterr()
        assert target.read_bytes() == before
        assert not target.with_name("pwned.parquet").exists()


class TestPerSessionMemoryHint:
    """A per-session query that runs out of memory points at the lake.

    The per-session path holds each document it reads in memory, so a full
    scan of a large compressed corpus can exceed the query cap; the error
    must name the fix.
    """

    def test_an_out_of_memory_error_on_the_per_session_path_names_the_lake(
        self,
        source_root: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import duckdb

        import atif_cli.app as app_mod

        corpus = tmp_path / "corpus"
        _materialize(source_root, corpus, capsys)

        def out_of_memory(*_args: object, **_kwargs: object) -> None:
            msg = "could not allocate block"
            raise duckdb.OutOfMemoryException(msg)

        monkeypatch.setattr(app_mod, "emit_cursor", out_of_memory)
        with pytest.raises(SystemExit) as excinfo:
            query(
                "SELECT count(*) FROM tool_calls",
                corpus_root=corpus,
                lake=False,
                fmt=OutputFormat.JSON,
            )
        assert excinfo.value.code == EXIT_CODES["runtime_error"]
        error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])["error"]
        assert error["hint"] == app_mod.PER_SESSION_MEMORY_HINT

    def test_other_errors_and_the_lake_path_keep_their_hint(self) -> None:
        import duckdb

        from atif_cli.app import _per_session_memory_hint
        from atif_cli.errors import ClassifiedError

        err = ClassifiedError(kind="runtime_error", exit_code=70, message="m")
        oom = duckdb.OutOfMemoryException("x")
        assert _per_session_memory_hint(err, oom, object()) is err
        assert _per_session_memory_hint(err, duckdb.InvalidInputException("x"), None) is err
        assert _per_session_memory_hint(err, oom, None).hint is not None
