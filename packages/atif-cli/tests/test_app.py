# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the atif-sql CLI surface (no live corpus, no subprocess)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cli_fixtures import write_analytics_parquets
from loguru import logger

from atif_cli import app as app_module
from atif_cli.app import (
    ALLOW_ROOT_ENV,
    DEFAULT_LOG_LEVEL,
    LOG_LEVEL_ENV,
    QUERY_MEMORY_LIMIT_ENV,
    QUERY_THREADS_ENV,
    _stderr_log_level,
    analyze,
    app,
    convert,
    embed,
    examples,
    materialize as materialize_cmd,
    query,
    schema,
    search,
    status,
)
from atif_cli.errors import EXIT_CODES
from atif_cli.output import (
    OutputFormat,
    emit_cursor,
    emit_rows,
    json_object_keys,
    resolve_format,
)

EMBED_MODEL = "global.cohere.embed-v4:0"
EMBED_DIM = 1024

STEP_TEXT = "A qualifying step text with clearly more than thirty-two characters."

#: Command bullets in ``atif_cli.app``'s module docstring: ``* ``<name>``  …``.
_DOCSTRING_COMMAND_RE = re.compile(r"^\* ``([a-z][a-z-]*)``", re.MULTILINE)

#: Number words the docstring may use for its command total, so the prose count
#: follows the registered count with no second place to edit.
_COUNT_WORDS: dict[int, str] = {
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
}


def _registered_command_names() -> set[str]:
    """Every command cyclopts resolves, minus the auto-registered help/version flags."""
    return {name for name in app.resolved_commands() if not name.startswith("-")}


class TestAppSurface:
    def test_app_is_named_for_the_distribution(self) -> None:
        assert app.name[0] == "atif-sql"

    def test_docstring_documents_exactly_the_registered_commands(self) -> None:
        """A command registered without a docstring bullet, or the reverse, fails here.

        The module docstring is the first thing an agent driving the CLI reads,
        and the commands it is worst to omit are the ones that spend money at
        Bedrock. Registration is the source of truth; this pins the prose to it
        in BOTH directions, so a removed command cannot linger in the list
        either.
        """
        doc = app_module.__doc__
        assert doc is not None
        documented = set(_DOCSTRING_COMMAND_RE.findall(doc))
        registered = _registered_command_names()
        assert documented == registered, (
            "the module docstring and the registered command set disagree.\n"
            f"  documented, not registered: {sorted(documented - registered)}\n"
            f"  registered, not documented: {sorted(registered - documented)}"
        )

    def test_docstring_states_the_real_command_total(self) -> None:
        """The docstring spells its command count in words; it must be the true one."""
        doc = app_module.__doc__
        assert doc is not None
        count = len(_registered_command_names())
        word = _COUNT_WORDS.get(count)
        assert word is not None, f"add {count} to _COUNT_WORDS"
        assert f"The {word} registered commands" in doc, (
            f"docstring must state 'The {word} registered commands' for {count} commands"
        )


class TestConvert:
    def test_convert_rejects_non_session_file(self, tmp_path: Path) -> None:
        bogus = tmp_path / "nope.txt"
        bogus.write_text("not a session")
        with pytest.raises(SystemExit) as excinfo:
            convert(bogus)
        assert excinfo.value.code == EXIT_CODES["invalid_input"]

    def test_convert_writes_edges_next_to_trajectory_out(
        self, synthetic_session: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "out" / "trajectory.json"
        convert(synthetic_session, trajectory_out=out)
        assert out.exists()
        edges = out.parent / "edges.jsonl"
        assert edges.exists()
        lines = edges.read_text().splitlines()
        assert lines, "edges.jsonl must carry one line per raw record"
        first = json.loads(lines[0])
        assert {"uuid", "parent_uuid", "type", "ts", "source_file"} <= set(first)
        captured = capsys.readouterr()
        assert str(out) in captured.out
        assert "loss_report" in captured.out


class TestSchema:
    def test_schema_json_payload_matches_catalog(self, capsys: pytest.CaptureFixture[str]) -> None:
        from atif_duck.domain.catalog import MACRO_SIGNATURES, VIEW_SCHEMA

        schema(fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert set(payload["views"]) == set(VIEW_SCHEMA)
        assert payload["views"]["sessions"][0] == {"column": "session_id", "type": "VARCHAR"}
        assert {m["name"] for m in payload["macros"]} == set(MACRO_SIGNATURES)

    def test_schema_table_lists_views_and_macros(self, capsys: pytest.CaptureFixture[str]) -> None:
        schema(fmt=OutputFormat.TABLE)
        out = capsys.readouterr().out
        assert "sessions" in out
        assert "tool_calls" in out
        assert "ago(interval_text)" in out


class TestExamples:
    def test_json_payload_matches_derived_examples(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from atif_duck.domain.examples import build_examples

        examples(fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert "test-executed" in payload["note"]
        derived = build_examples()
        assert len(payload["examples"]) == len(derived)
        by_name = {e["name"]: e for e in payload["examples"]}
        for example in derived:
            entry = by_name[example.name]
            assert entry == {
                "name": example.name,
                "sql": example.sql,
                "description": example.description,
                "requires": example.requires,
                "category": example.category,
            }

    def test_category_and_requires_filters(self, capsys: pytest.CaptureFixture[str]) -> None:
        examples(category="table-macro", requires="core", fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert payload["examples"], "core table-macro examples must exist"
        assert all(
            e["category"] == "table-macro" and e["requires"] == "core" for e in payload["examples"]
        )

    def test_unknown_filter_value_exits_64(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            examples(requires="bogus", fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["invalid_input"]
        err = json.loads(capsys.readouterr().err)
        assert err["error"]["kind"] == "invalid_input"
        assert "core" in err["error"]["hint"]

    def test_table_output_groups_by_requires(self, capsys: pytest.CaptureFixture[str]) -> None:
        examples(fmt=OutputFormat.TABLE)
        out = capsys.readouterr().out
        assert "test-executed" in out
        for group in ("[core]", "[analytics]", "[vss]"):
            assert group in out
        assert "SELECT * FROM sessions" in out

    def test_query_examples_flag_short_circuits(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``query --examples`` lists without SQL and without touching DuckDB."""
        query(None, examples_flag=True, fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert "test-executed" in payload["note"]
        assert payload["examples"]

    def test_query_without_sql_exits_64_with_examples_hint(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            query(None, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["parse_error"]
        err = json.loads(capsys.readouterr().err)
        assert "examples" in err["error"]["hint"]

    def test_schema_json_carries_examples_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        schema(fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert "atif-sql examples" in payload["examples_hint"]


def _write_contract_corpus(root: Path) -> Path:
    """One contract-shaped session — the minimum ``register`` can bind."""
    sid = "11111111-1111-1111-1111-111111111111"
    sdir = root / "sessions" / sid
    sdir.mkdir(parents=True)
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": sid,
        "agent": {"name": "claude-code", "version": "2.1.218", "model_name": "m"},
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-20T10:00:00.000Z",
                "source": "user",
                "message": STEP_TEXT,
                "extra": {"is_sidechain": False, "source_uuids": ["u-1"]},
            }
        ],
        "final_metrics": {"total_steps": 1},
    }
    edge: dict[str, Any] = {
        "uuid": "u-1",
        "parent_uuid": None,
        "message_id": None,
        "type": "user",
        "ts": "2026-08-20T10:00:00.000Z",
        "is_sidechain": False,
        "is_compact_summary": False,
        "source_file": "transcript.jsonl",
        "tool_use_ids": [],
    }
    loss: dict[str, Any] = {
        "record_counts": {"user": 1},
        "records_total": 1,
        "records_converted": 1,
        "records_dropped": 0,
        "gaps_observed": [],
        "subagent_files_found": 0,
        "subagent_files_convertible": 0,
        "workflow_subagent_files_found": 0,
    }
    meta = {
        "session_id": sid,
        "source_mtime_ns": 1,
        "source_files": ["transcript.jsonl"],
        "harbor_version": "0.22.0",
        "converter_version": "0.1.0",
        "materialized_at": "2026-08-22T00:00:00Z",
    }
    (sdir / "trajectory.json").write_text(json.dumps(trajectory, separators=(",", ":")))
    (sdir / "edges.jsonl").write_text(json.dumps(edge) + "\n")
    (sdir / "loss_report.json").write_text(json.dumps(loss, separators=(",", ":")))
    (sdir / "meta.json").write_text(json.dumps(meta, separators=(",", ":")))
    return root


def _write_lance_store(uri: Path, *, model: str = EMBED_MODEL, dim: int = EMBED_DIM) -> Path:
    """A tiny REAL Lance store, written with raw lancedb (no atif-embed)."""
    import lancedb
    import pyarrow as pa

    schema = pa.schema(
        [
            pa.field("uuid", pa.string(), nullable=False),
            pa.field("model", pa.string(), nullable=False),
            pa.field("dim", pa.int32(), nullable=False),
            pa.field("embedding", pa.list_(pa.float32(), dim), nullable=False),
            pa.field("embedded_at", pa.timestamp("us", tz="UTC"), nullable=False),
        ]
    )
    table = pa.table(
        {
            "uuid": ["u-1"],
            "model": [model],
            "dim": [dim],
            "embedding": pa.array([[1.0] + [0.0] * (dim - 1)], type=pa.list_(pa.float32(), dim)),
            "embedded_at": [datetime.now(UTC)],
        },
        schema=schema,
    )
    uri.mkdir(parents=True, exist_ok=True)
    lancedb.connect(str(uri)).create_table("embeddings", data=table, mode="create")
    return uri


SESSION_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def query_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bindable corpus with env-driven overrides cleared.

    Carries the analytics parquets too: they are the only registered surface
    the sandbox's file allowlist actually gates (the session artifacts are
    TEMP TABLEs by the time it arms), so without them the allowlist tests
    pass whether or not the allowlist exists.
    """
    for var in ("ATIF_SQL_CORPUS_ROOT", "ATIF_SQL_LANCE_URI", "ATIF_SQL_EMBED_MODEL_ID"):
        monkeypatch.delenv(var, raising=False)
    root = _write_contract_corpus(tmp_path / "corpus")
    write_analytics_parquets(root, SESSION_ID)
    return root


def _run_query(sql: str, corpus_root: Path) -> None:
    query(sql, corpus_root=corpus_root, fmt=OutputFormat.JSON)


def duckdb_default_memory_limit() -> str:
    """What an unhardened connection reports, so "capped" can't mean "untouched"."""
    import duckdb

    con = duckdb.connect()
    try:
        row = con.execute("SELECT current_setting('memory_limit')").fetchone()
        return str(row[0]) if row else ""
    finally:
        con.close()


def _expect_refusal(
    sql: str,
    corpus_root: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    kind: str = "runtime_error",
) -> str:
    """Run ``sql`` expecting the sandbox to refuse it; return the message.

    ``kind`` is ``runtime_error`` for a refusal DuckDB itself raises
    (allowlist, locked configuration) and ``sandbox_refused`` for a
    statement kind the CLI refuses by name before executing anything.
    """
    with pytest.raises(SystemExit) as excinfo:
        _run_query(sql, corpus_root)
    assert excinfo.value.code == EXIT_CODES[kind]
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["kind"] == kind
    return str(payload["error"]["message"])


def _tree_digest(root: Path) -> str:
    """One hash over every path, mode and byte under ``root``."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        digest.update(f"{rel}\0{oct(path.stat().st_mode)}\0".encode())
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def _capture_warnings() -> tuple[list[str], int]:
    warnings: list[str] = []
    sink_id = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    return warnings, sink_id


def _write_two_session_corpus(root: Path) -> Path:
    """The contract corpus plus a second session: two ``edges.jsonl`` for the glob reader.

    One file registers under any limit because the reader uses one thread
    per file; the per-thread reservation that finding 1 is about needs at
    least two.
    """
    _write_contract_corpus(root)
    second = "22222222-2222-2222-2222-222222222222"
    src = root / "sessions" / SESSION_ID
    dst = root / "sessions" / second
    shutil.copytree(src, dst)
    for name in ("trajectory.json", "meta.json"):
        path = dst / name
        path.write_text(path.read_text().replace(SESSION_ID, second))
    return root


class _RecordingConnection:
    """Wrap a DuckDB connection so every statement text is observable.

    ``execute`` is recorded; everything else (``read_parquet``,
    ``extract_statements``, ``close``) delegates. Registration only ever
    touches those, so the wrapper is transparent to the code under test.
    """

    def __init__(self, con: Any, statements: list[str]) -> None:
        self._con = con
        self._statements = statements

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self._statements.append(sql)
        return self._con.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


@pytest.fixture
def empty_extension_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[str]]:
    """Every ``duckdb.connect()`` in the command bodies sees NO installed extensions.

    The connection is pointed at an empty ``extension_directory`` and
    wrapped so the statements it runs are recorded. Returns the directory
    (still empty afterwards proves nothing was installed) and the recording.
    """
    import duckdb

    ext_dir = tmp_path / "no-extensions"
    ext_dir.mkdir()
    statements: list[str] = []
    real_connect = duckdb.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        con = real_connect(*args, **kwargs)
        con.execute(f"SET extension_directory='{ext_dir}'")
        return _RecordingConnection(con, statements)

    monkeypatch.setattr(duckdb, "connect", connect)
    return ext_dir, statements


class TestMaterializeReportOutput:
    """An unreadable session is counted nowhere else, so hiding it reads as success."""

    @staticmethod
    def _report(**overrides: Any) -> Any:
        from atif_corpus.application.materialize import MaterializationReport

        base: dict[str, Any] = {
            "materialized_count": 0,
            "up_to_date_count": 0,
            "skipped_live_count": 0,
            "failures": (),
            "total_seconds": 1.0,
            "convert_seconds": 0.5,
        }
        # Annotated so the unpack is `Any` per key: pyright otherwise unions the
        # merged literal's value types and reads `()` as `tuple[()]` for the int
        # and float counters.
        merged: dict[str, Any] = {**base, "unreadable_session_ids": (), **overrides}
        return MaterializationReport(**merged)

    def test_json_payload_carries_unreadable_ids(self, capsys: pytest.CaptureFixture[str]) -> None:
        from atif_cli.app import _print_report

        _print_report(self._report(unreadable_session_ids=("sess-a", "sess-b")), OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert payload["unreadable"] == 2
        assert payload["unreadable_session_ids"] == ["sess-a", "sess-b"]

    def test_table_line_shows_unreadable_beside_the_zeroes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every other counter is zero; only this distinguishes starved from idle."""
        from atif_cli.app import _print_report

        _print_report(self._report(unreadable_session_ids=("sess-a",)), OutputFormat.TABLE)
        captured = capsys.readouterr()
        assert "unreadable: 1" in captured.out
        assert "UNREADABLE sess-a" in captured.err

    def test_a_clean_pass_reports_zero_unreadable(self, capsys: pytest.CaptureFixture[str]) -> None:
        from atif_cli.app import _print_report

        _print_report(self._report(materialized_count=3), OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert payload["unreadable"] == 0
        assert payload["unreadable_session_ids"] == []


class TestQuerySandbox:
    """``query`` runs agent-composed SQL over third-party text — it is untrusted."""

    def test_read_text_outside_corpus_is_refused(
        self, query_corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        secret = tmp_path / "credential.token"
        secret.write_text("sk-not-a-real-token")

        message = _expect_refusal(f"SELECT * FROM read_text('{secret}')", query_corpus, capsys)
        assert "Permission" in message
        assert "sk-not-a-real-token" not in message

    def test_copy_out_of_tree_is_refused(
        self, query_corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "exfil.csv"

        _expect_refusal(
            f"COPY (SELECT 42 AS x) TO '{target}'", query_corpus, capsys, kind="sandbox_refused"
        )
        assert not target.exists()

    def test_attach_unrelated_database_is_refused(
        self, query_corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _expect_refusal(
            f"ATTACH '{tmp_path / 'other.db'}' AS o", query_corpus, capsys, kind="sandbox_refused"
        )

    def test_extension_install_and_load_are_refused(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No INSTALL/LOAD means no httpfs, which means no network egress."""
        _expect_refusal("INSTALL httpfs", query_corpus, capsys, kind="sandbox_refused")
        _expect_refusal("LOAD httpfs", query_corpus, capsys, kind="sandbox_refused")

    def test_injected_sql_cannot_reopen_the_sandbox(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The same SQL string cannot disarm the settings that confine it."""
        for statement in (
            "SET enable_external_access=true",
            "SET allowed_directories=['/']",
            "SET memory_limit='512GB'",
            "SET temp_directory='/tmp'",
            "SET autoinstall_known_extensions=true",
            "SET threads=64",
        ):
            _expect_refusal(statement, query_corpus, capsys)

    def test_corpus_views_still_work_when_armed(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A sandbox that breaks legitimate queries is worse than the hole."""
        for view, expected in (
            ("sessions", 1),
            ("steps", 1),
            ("messages", 1),
            ("message_embeddings", 0),
        ):
            _run_query(f"SELECT count(*) AS n FROM {view}", query_corpus)
            assert json.loads(capsys.readouterr().out) == [{"n": expected}]

        _run_query("SELECT * FROM steps", query_corpus)
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["message"] == STEP_TEXT

        _run_query("SELECT count(*) AS n FROM tool_rank(3650)", query_corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 0}]

    def test_corpus_root_containing_a_quote_still_binds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A single quote in the path must not break out of the DDL literal."""
        for var in ("ATIF_SQL_CORPUS_ROOT", "ATIF_SQL_LANCE_URI", "ATIF_SQL_EMBED_MODEL_ID"):
            monkeypatch.delenv(var, raising=False)
        corpus = _write_contract_corpus(tmp_path / "evil'root")
        write_analytics_parquets(corpus, SESSION_ID)

        _run_query("SELECT count(*) AS n FROM steps", corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 1}]

        _run_query("SELECT count(*) AS n FROM message_clusters", corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 1}]


class TestQueryStatementGate:
    """Statement kinds that name a file are refused by name before anything executes.

    DuckDB's ``allowed_paths`` grants are read-write and it has no read-only
    grant (pinned in :class:`TestHardenedConnectionLayer`), so this gate is
    what stops ``COPY ... TO <granted parquet> (USE_TMP_FILE false)`` for
    any uid.
    """

    @pytest.mark.parametrize(
        ("statement", "kind"),
        [
            ("COPY (SELECT 1) TO '{tmp}/a.csv'", "COPY"),
            ("COPY (SELECT 1) TO '{tmp}/a.csv' (USE_TMP_FILE false)", "COPY"),
            ("EXPORT DATABASE '{tmp}/exported'", "EXPORT"),
            ("PREPARE p AS COPY (SELECT 1) TO '{tmp}/a.csv'", "PREPARE"),
            ("EXECUTE p", "EXECUTE"),
            ("ATTACH '{tmp}/other.db' AS o", "ATTACH"),
            ("DETACH o", "DETACH"),
            # DuckDB parses INSTALL and LOAD to the same statement kind.
            ("INSTALL httpfs", "LOAD"),
            ("LOAD httpfs", "LOAD"),
        ],
    )
    def test_file_statement_kinds_are_refused_by_name(
        self,
        query_corpus: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        statement: str,
        kind: str,
    ) -> None:
        message = _expect_refusal(
            statement.format(tmp=tmp_path), query_corpus, capsys, kind="sandbox_refused"
        )
        assert kind in message
        assert not (tmp_path / "a.csv").exists()

    def test_a_refused_statement_stops_the_whole_batch(
        self, query_corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Nothing in a batch runs when any statement in it is refused."""
        target = tmp_path / "late.csv"
        message = _expect_refusal(
            f"SELECT 1; COPY (SELECT 1) TO '{target}'", query_corpus, capsys, kind="sandbox_refused"
        )
        assert "COPY" in message
        assert not target.exists()

    def test_select_batches_and_in_memory_ddl_still_run(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run_query("SELECT 1 AS a; SELECT 2 AS b", query_corpus)
        assert json.loads(capsys.readouterr().out) == [{"b": 2}]
        _run_query("CREATE TEMP TABLE t AS SELECT 1 AS x; SELECT * FROM t", query_corpus)
        assert json.loads(capsys.readouterr().out) == [{"x": 1}]
        _run_query("EXPLAIN SELECT count(*) FROM sessions", query_corpus)
        assert json.loads(capsys.readouterr().out)

    def test_parse_errors_still_exit_64(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _run_query("SELEC 1", query_corpus)
        assert excinfo.value.code == EXIT_CODES["parse_error"]
        assert json.loads(capsys.readouterr().err)["error"]["kind"] == "parse_error"

    def test_allowlist_names_real_statement_kinds_and_omits_the_file_facing_ones(self) -> None:
        import duckdb

        known = {member for member in dir(duckdb.StatementType) if member.isupper()}
        assert known >= app_module._QUERY_STATEMENT_KINDS
        for refused in (
            "COPY",
            "COPY_DATABASE",
            "EXPORT",
            "ATTACH",
            "DETACH",
            "LOAD",
            "EXTENSION",
            "PREPARE",
            "EXECUTE",
        ):
            assert refused in known
            assert refused not in app_module._QUERY_STATEMENT_KINDS


class TestQueryRefusesRoot:
    """A 0444 file mode does not bind uid 0, so the commands that run SQL refuse it."""

    @staticmethod
    def _as_root(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.delenv(ALLOW_ROOT_ENV, raising=False)

    def test_query_exits_77_before_opening_duckdb(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import duckdb

        self._as_root(monkeypatch)

        def refuse_connect(*args: object, **kwargs: object) -> Any:
            del args, kwargs
            pytest.fail("DuckDB was opened as root")

        monkeypatch.setattr(duckdb, "connect", refuse_connect)
        with pytest.raises(SystemExit) as excinfo:
            _run_query("SELECT 1", query_corpus)
        assert excinfo.value.code == EXIT_CODES["root_refused"] == 77
        err = json.loads(capsys.readouterr().err)["error"]
        assert err["kind"] == "root_refused"
        assert "root" in err["message"]
        assert ALLOW_ROOT_ENV in err["hint"]

    def test_search_and_analyze_refuse_root_too(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._as_root(monkeypatch)
        with pytest.raises(SystemExit) as excinfo:
            search("anything", corpus_root=query_corpus, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["root_refused"]
        assert json.loads(capsys.readouterr().err)["error"]["kind"] == "root_refused"
        with pytest.raises(SystemExit) as excinfo:
            analyze(corpus_root=query_corpus, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["root_refused"]
        assert json.loads(capsys.readouterr().err)["error"]["kind"] == "root_refused"

    def test_the_escape_hatch_runs_with_a_warning(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._as_root(monkeypatch)
        monkeypatch.setenv(ALLOW_ROOT_ENV, "1")
        warnings, sink_id = _capture_warnings()
        try:
            _run_query("SELECT count(*) AS n FROM sessions", query_corpus)
        finally:
            logger.remove(sink_id)
        assert json.loads(capsys.readouterr().out) == [{"n": 1}]
        assert any("root" in w and ALLOW_ROOT_ENV in w for w in warnings)

    def test_only_the_value_one_opens_the_hatch(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._as_root(monkeypatch)
        monkeypatch.setenv(ALLOW_ROOT_ENV, "yes")
        with pytest.raises(SystemExit) as excinfo:
            _run_query("SELECT 1", query_corpus)
        assert excinfo.value.code == EXIT_CODES["root_refused"]
        capsys.readouterr()

    def test_an_unprivileged_uid_is_untouched(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        _run_query("SELECT count(*) AS n FROM sessions", query_corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 1}]


class TestQuerySandboxFileAllowlist:
    """The allowlist gates LAZY reads only — the surface a corpus-only test misses.

    ``register_raw`` materializes the session artifacts into TEMP TABLEs and
    ``register_vss`` keeps the Lance store ATTACHed, so both keep working
    with the allowlist empty. Every assertion here goes through an analytics
    view, which is ``read_parquet`` opened when the caller selects from it.
    """

    def test_lazy_parquet_views_still_bind_when_armed(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for view, expected in (
            ("message_clusters", 1),
            ("cluster_terms", 1),
            ("session_classifications", 1),
            ("session_goals", 1),
        ):
            _run_query(f"SELECT count(*) AS n FROM {view}", query_corpus)
            assert json.loads(capsys.readouterr().out) == [{"n": expected}], view

        _run_query("SELECT term FROM cluster_top_terms(0, 5)", query_corpus)
        assert json.loads(capsys.readouterr().out) == [{"term": "auth"}]

        _run_query("SELECT count(*) AS n FROM autonomy_trend(3650)", query_corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 1}]

    def test_allowlist_names_every_analytics_parquet(self, query_corpus: Path) -> None:
        """The grant is per-FILE, so a new artifact must not be silently missed."""
        from atif_cli.app import _lazy_read_paths

        on_disk = sorted((query_corpus / "analytics").rglob("*.parquet"))
        assert on_disk, "the fixture corpus must carry analytics parquets"
        assert _lazy_read_paths(query_corpus) == on_disk

    def test_corpus_without_analytics_still_binds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty allowlist is legal: a freshly materialized corpus has no parquets."""
        for var in ("ATIF_SQL_CORPUS_ROOT", "ATIF_SQL_LANCE_URI", "ATIF_SQL_EMBED_MODEL_ID"):
            monkeypatch.delenv(var, raising=False)
        corpus = _write_contract_corpus(tmp_path / "bare")

        _run_query("SELECT count(*) AS n FROM steps", corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 1}]


class TestQuerySandboxWrites:
    """The corpus is the source of truth; injected SQL must not be able to edit it."""

    def test_copy_over_a_corpus_artifact_is_refused(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        trajectory = query_corpus / "sessions" / SESSION_ID / "trajectory.json"
        before = trajectory.read_text()

        _expect_refusal(
            f"COPY (SELECT 'destroyed' AS x) TO '{trajectory}'",
            query_corpus,
            capsys,
            kind="sandbox_refused",
        )
        assert trajectory.read_text() == before

    def test_copy_new_file_into_the_corpus_is_refused(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = query_corpus / "pwned.csv"

        _expect_refusal(
            f"COPY (SELECT 42) TO '{target}'", query_corpus, capsys, kind="sandbox_refused"
        )
        assert not target.exists()

    def test_copy_into_the_lance_store_is_refused(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_lance_store(query_corpus / "embeddings_lance")
        target = query_corpus / "embeddings_lance" / "exfil.csv"

        _expect_refusal(
            f"COPY (SELECT 42) TO '{target}'", query_corpus, capsys, kind="sandbox_refused"
        )
        assert not target.exists()

    def test_copy_into_the_legacy_spill_directory_is_refused_and_nothing_persists(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Probe d3 of the review: ``<corpus>/.duckdb_tmp`` used to be the one writable dir.

        It is pre-created here, as the review did, so a success would be the
        old grant and not a missing parent; it is no longer granted at all.
        """
        legacy = query_corpus / ".duckdb_tmp"
        legacy.mkdir()
        before = _tree_digest(query_corpus)

        _expect_refusal(
            f"COPY (SELECT 1) TO '{legacy}/probe.csv'", query_corpus, capsys, kind="sandbox_refused"
        )
        _expect_refusal(f"SELECT * FROM read_text('{legacy}/probe.csv')", query_corpus, capsys)
        assert _tree_digest(query_corpus) == before
        assert list(legacy.iterdir()) == []

    def test_use_tmp_file_false_over_a_granted_parquet_is_refused_for_any_uid(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Probe d2 of the review, the one that overwrote ``steps.parquet`` as root.

        The analytics parquet is a granted path and writable by this uid at
        the filesystem, so a refusal here is the statement gate and not a
        file mode. Repeated as uid 0 with the escape hatch open: still
        refused, because the gate does not look at the uid.
        """
        parquet = query_corpus / "analytics" / "clusters.parquet"
        before = parquet.read_bytes()
        statement = f"COPY (SELECT 'x' AS uuid) TO '{parquet}' (FORMAT PARQUET, USE_TMP_FILE false)"

        _expect_refusal(statement, query_corpus, capsys, kind="sandbox_refused")
        assert parquet.read_bytes() == before

        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setenv(ALLOW_ROOT_ENV, "1")
        _expect_refusal(statement, query_corpus, capsys, kind="sandbox_refused")
        assert parquet.read_bytes() == before

    def test_documented_residual_hole_still_behaves_as_documented(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Pins the one hole ``query``'s docstring admits, so the promise cannot rot.

        Recomputable derived data. If a future DuckDB closes it, this test
        fails and the docstring's Sandbox section must lose the paragraph.
        """
        _write_lance_store(query_corpus / "embeddings_lance")
        _run_query("DELETE FROM lance_store.main.embeddings", query_corpus)
        capsys.readouterr()
        _run_query("SELECT count(*) AS n FROM message_embeddings", query_corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 0}], (
            "the ATTACHed Lance store is no longer writable — "
            "drop that paragraph from query's Sandbox docstring"
        )


def _hardened_connection(corpus_root: Path, spill_dir: Path) -> Any:
    """What ``query`` builds, minus the statement gate: DuckDB's own layer alone."""
    import duckdb

    from atif_duck.infrastructure.registry import register

    con = duckdb.connect()
    app_module._configure_query_resources(con, app_module._query_resources(), spill_dir)
    sources = register(con, corpus_root)
    app_module._harden_query_connection(
        con,
        corpus_root=corpus_root,
        spill_dir=spill_dir,
        columnar_paths=sources.lazy_read_paths,
    )
    return con


class TestHardenedConnectionLayer:
    """DuckDB's allowlist, exercised WITHOUT the statement gate.

    The CLI tests above hit the gate first, so this class is what proves
    the second layer still holds on its own, and pins the two facts the
    gate exists for: a directory grant and a file grant are both read-write.
    """

    @pytest.fixture
    def layer(self, query_corpus: Path, tmp_path: Path) -> Any:
        spill = tmp_path / "spill"
        spill.mkdir(mode=0o700)
        con = _hardened_connection(query_corpus, spill)
        yield con, spill
        con.close()

    def test_duckdb_refuses_every_ungranted_path(
        self, layer: Any, query_corpus: Path, tmp_path: Path
    ) -> None:
        import duckdb

        con, spill = layer
        trajectory = query_corpus / "sessions" / SESSION_ID / "trajectory.json"
        parquet = query_corpus / "analytics" / "clusters.parquet"
        for statement in (
            f"COPY (SELECT 1) TO '{tmp_path / 'out.csv'}'",
            f"COPY (SELECT 1) TO '{trajectory}'",
            f"COPY (SELECT 1) TO '{trajectory}' (USE_TMP_FILE false)",
            f"COPY (SELECT 1) TO '{query_corpus / 'pwned.csv'}'",
            f"COPY (SELECT 'x' AS uuid) TO '{parquet}' (FORMAT PARQUET)",
            f"COPY (SELECT 1) TO '{spill}/../escape.csv'",
            f"SELECT * FROM read_text('{spill}/../../etc/passwd')",
            "SELECT * FROM read_text('/etc/passwd')",
            f"ATTACH '{tmp_path / 'other.db'}' AS o",
            "INSTALL httpfs",
        ):
            with pytest.raises(duckdb.Error) as excinfo:
                con.execute(statement)
            assert "Permission" in str(excinfo.value) or "disabled" in str(excinfo.value), statement
        assert not (tmp_path / "out.csv").exists()
        assert not (tmp_path / "escape.csv").exists()

    def test_grants_are_read_write_which_is_why_the_gate_exists(
        self, layer: Any, query_corpus: Path
    ) -> None:
        """DuckDB 1.5.5 has no read-only grant; if one appears, this fails and the design can simplify."""
        con, spill = layer
        con.execute(f"COPY (SELECT 1 AS x) TO '{spill}/probe.csv'")
        assert (spill / "probe.csv").is_file()

        parquet = query_corpus / "analytics" / "clusters.parquet"
        before = parquet.read_bytes()
        con.execute(
            f"COPY (SELECT 'x' AS uuid) TO '{parquet}' (FORMAT PARQUET, USE_TMP_FILE false)"
        )
        assert parquet.read_bytes() != before, (
            "DuckDB now refuses a write to a granted path: the statement gate is "
            "belt-and-braces and query's Sandbox docstring can say so"
        )

    def test_resources_and_extension_settings_are_in_force(self, layer: Any) -> None:
        con, spill = layer
        resources = app_module._query_resources()
        settings = dict(
            con.execute(
                "SELECT name, value FROM duckdb_settings() WHERE name IN "
                "('threads', 'temp_directory', 'autoinstall_known_extensions', "
                "'autoload_known_extensions', 'enable_external_access', 'lock_configuration')"
            ).fetchall()
        )
        assert int(settings["threads"]) == resources.threads
        assert settings["temp_directory"] == str(spill)
        assert settings["autoinstall_known_extensions"] == "false"
        assert settings["autoload_known_extensions"] == "false"
        assert settings["enable_external_access"] == "false"
        assert settings["lock_configuration"] == "true"


class TestQuerySpillDirectory:
    """The spill directory is private, outside the corpus, and gone when the process is."""

    @pytest.fixture
    def recorded_mkdtemp(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, int]]:
        import tempfile

        created: list[tuple[Path, int]] = []
        real = tempfile.mkdtemp

        def mkdtemp(*args: Any, **kwargs: Any) -> str:
            path = str(real(*args, **kwargs))
            created.append((Path(path), Path(path).stat().st_mode & 0o777))
            return path

        # ``app`` reads ``tempfile.mkdtemp`` at call time, so the module attribute is the seam.
        monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
        return created

    def test_private_dir_outside_the_corpus_removed_on_success(
        self,
        query_corpus: Path,
        recorded_mkdtemp: list[tuple[Path, int]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _run_query(
            "SELECT current_setting('temp_directory') AS tmp, "
            "current_setting('allowed_directories') AS dirs",
            query_corpus,
        )
        row = json.loads(capsys.readouterr().out)[0]
        [(spill, mode)] = recorded_mkdtemp
        assert row["tmp"] == str(spill)
        assert [Path(d) for d in row["dirs"]] == [spill]
        assert spill.name.startswith("atif-sql-query-")
        assert mode == 0o700
        assert not spill.is_relative_to(query_corpus)
        assert not spill.exists()
        assert not (query_corpus / ".duckdb_tmp").exists()

    def test_removed_on_error_too(
        self,
        query_corpus: Path,
        recorded_mkdtemp: list[tuple[Path, int]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit):
            _run_query("SELEC 1", query_corpus)
        capsys.readouterr()
        [(spill, _)] = recorded_mkdtemp
        assert not spill.exists()

    def test_a_query_process_leaves_the_corpus_tree_unchanged(
        self, query_corpus: Path, tmp_path: Path
    ) -> None:
        """Through a real process: the review's probe d3, then a plain read.

        Before the fix the COPY exited 0 and ``probe.csv`` persisted inside
        the corpus. Now the tree digest (paths, modes, bytes) is unchanged,
        the pre-created legacy dir stays empty, no spill dir is left under
        the process's TMPDIR, and a fresh corpus gains no ``.duckdb_tmp``.
        """
        legacy = query_corpus / ".duckdb_tmp"
        legacy.mkdir()
        scratch = tmp_path / "process-tmp"
        scratch.mkdir()
        env: dict[str, str] = dict(os.environ)
        env.update(
            TMPDIR=str(scratch),
            NO_COLOR="1",
            PYTHONHASHSEED="0",
            LITELLM_LOCAL_MODEL_COST_MAP="true",
        )
        for var in ("ATIF_SQL_CORPUS_ROOT", "ATIF_SQL_LANCE_URI", "ATIF_SQL_EMBED_MODEL_ID"):
            env.pop(var, None)
        before = _tree_digest(query_corpus)

        def run(sql: str) -> subprocess.CompletedProcess[str]:
            argv = [sys.executable, "-m", "atif_cli", "query", "--format", "json"]
            argv += ["--corpus-root", str(query_corpus), sql]
            return subprocess.run(  # noqa: S603 - argv is built from constants and tmp paths
                argv, capture_output=True, text=True, env=env, timeout=300, check=False
            )

        refused = run(f"COPY (SELECT 1) TO '{legacy}/probe.csv'")
        assert refused.returncode == EXIT_CODES["sandbox_refused"], refused.stderr
        assert json.loads(refused.stderr.strip().splitlines()[-1])["error"]["kind"] == (
            "sandbox_refused"
        )
        read = run("SELECT count(*) AS n FROM sessions")
        assert read.returncode == 0, read.stderr
        assert json.loads(read.stdout) == [{"n": 1}]

        assert _tree_digest(query_corpus) == before
        assert list(legacy.iterdir()) == []
        assert list(scratch.iterdir()) == [], "a spill dir outlived its process"

        legacy.rmdir()
        fresh = run("SELECT count(*) AS n FROM steps")
        assert fresh.returncode == 0, fresh.stderr
        assert not (query_corpus / ".duckdb_tmp").exists()


class TestQueryResources:
    """The cap and thread count are derived from the host and applied BEFORE registration."""

    GIB = 1024**3

    @staticmethod
    def _host(monkeypatch: pytest.MonkeyPatch, physical: int, available: int) -> None:
        monkeypatch.setattr(app_module, "_host_memory", lambda: (physical, available))

    def test_large_host_gets_half_of_ram(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._host(monkeypatch, 124 * self.GIB, 89 * self.GIB)
        assert app_module._query_memory_limit_bytes() == 62 * self.GIB

    def test_eight_gib_guest_is_capped_below_its_own_ram(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The old rule said 8 GiB here; the guest had 8 GiB in total and 6 free."""
        self._host(monkeypatch, 8 * self.GIB, 6 * self.GIB)
        limit = app_module._query_memory_limit_bytes()
        assert limit == int(6 * self.GIB * 0.8)
        assert limit < 8 * self.GIB

    def test_target_wins_between_half_and_eighty_percent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._host(monkeypatch, 12 * self.GIB, 12 * self.GIB)
        assert app_module._query_memory_limit_bytes() == 8 * self.GIB

    def test_never_exceeds_eighty_percent_of_physical_or_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for gib in (1, 4, 8, 16, 64, 256):
            for free_fraction in (0.1, 0.5, 1.0):
                physical = gib * self.GIB
                available = int(physical * free_fraction)
                self._host(monkeypatch, physical, available)
                limit = app_module._query_memory_limit_bytes()
                assert limit <= max(int(physical * 0.8), app_module._QUERY_MEMORY_MIN_BYTES)
                assert limit <= max(int(available * 0.8), app_module._QUERY_MEMORY_MIN_BYTES)

    def test_floor_when_almost_nothing_is_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._host(monkeypatch, 2 * self.GIB, 100 * 1024**2)
        assert app_module._query_memory_limit_bytes() == app_module._QUERY_MEMORY_MIN_BYTES

    @staticmethod
    def _sixteen_cpus(_pid: int) -> set[int]:
        return set(range(16))

    def test_threads_follow_the_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "sched_getaffinity", self._sixteen_cpus, raising=False)
        assert app_module._query_threads(int(4.8 * self.GIB)) == 2
        assert app_module._query_threads(62 * self.GIB) == 16
        assert app_module._query_threads(1 * self.GIB) == 1

    def test_host_memory_reads_this_host(self) -> None:
        physical, available = app_module._host_memory()
        assert physical > 0
        assert 0 < available <= physical

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("6GB", 6 * 10**9),
            ("512MiB", 512 * 1024**2),
            ("123B", 123),
            ("1.5GiB", int(1.5 * 1024**3)),
            (" 6 gb ", 6 * 10**9),
            ("4096", 4096),
        ],
    )
    def test_parse_size(self, text: str, expected: int) -> None:
        assert app_module._parse_size(text) == expected

    @pytest.mark.parametrize("text", ["", "abc", "6XB", "GB", "-1GB"])
    def test_parse_size_rejects_garbage(self, text: str) -> None:
        with pytest.raises(ValueError, match=r"size|unit"):
            app_module._parse_size(text)

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "sched_getaffinity", self._sixteen_cpus, raising=False)
        monkeypatch.setenv(QUERY_MEMORY_LIMIT_ENV, "6GB")
        monkeypatch.setenv(QUERY_THREADS_ENV, "4")
        assert app_module._query_resources() == app_module.QueryResources(6 * 10**9, 4)
        monkeypatch.delenv(QUERY_THREADS_ENV)
        assert app_module._query_resources().threads == 2, "threads follow the overridden cap"

    @pytest.mark.parametrize(
        ("var", "value"),
        [(QUERY_MEMORY_LIMIT_ENV, "lots"), (QUERY_THREADS_ENV, "0"), (QUERY_THREADS_ENV, "many")],
    )
    def test_malformed_override_exits_64(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        var: str,
        value: str,
    ) -> None:
        monkeypatch.setenv(var, value)
        with pytest.raises(SystemExit) as excinfo:
            _run_query("SELECT 1", query_corpus)
        assert excinfo.value.code == EXIT_CODES["invalid_input"]
        err = json.loads(capsys.readouterr().err)["error"]
        assert err["kind"] == "invalid_input"
        assert var in err["hint"]

    def test_connection_carries_the_cap_and_threads(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The settings are read back through caller SQL — the only honest witness."""
        monkeypatch.setenv(QUERY_MEMORY_LIMIT_ENV, "3GiB")
        monkeypatch.setenv(QUERY_THREADS_ENV, "3")
        _run_query(
            "SELECT current_setting('memory_limit') AS mem, "
            "current_setting('threads') AS threads, "
            "current_setting('autoinstall_known_extensions') AS autoinstall, "
            "current_setting('autoload_known_extensions') AS autoload",
            query_corpus,
        )
        row = json.loads(capsys.readouterr().out)[0]
        assert row["mem"] == "3.0 GiB"
        assert row["mem"] != duckdb_default_memory_limit()
        assert int(row["threads"]) == 3
        assert row["autoinstall"] is False
        assert row["autoload"] is False

    @pytest.mark.parametrize(("threads", "memory"), [(4, "6GB"), (16, "8GB")])
    def test_registration_fits_under_limits_that_used_to_oom(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        threads: int,
        memory: str,
    ) -> None:
        """Finding 1 of the MicroVM review, as the review reproduced it on a workstation.

        ``SET threads=N; SET memory_limit=...`` on every connection before the
        CLI touches it (the review's shim), plus the same values through the
        env so the CLI applies them too. Before the fix the edges reader
        reserved 2 GiB a thread and every query exited 70 out of memory once
        the glob matched two or more files (measured on main: one file
        registers, two do not); the readers are now sized from the files.
        """
        import duckdb

        for var in ("ATIF_SQL_CORPUS_ROOT", "ATIF_SQL_LANCE_URI", "ATIF_SQL_EMBED_MODEL_ID"):
            monkeypatch.delenv(var, raising=False)
        corpus = _write_two_session_corpus(tmp_path / "corpus")
        monkeypatch.setenv(QUERY_MEMORY_LIMIT_ENV, memory)
        monkeypatch.setenv(QUERY_THREADS_ENV, str(threads))
        real_connect = duckdb.connect

        def limited(*args: Any, **kwargs: Any) -> Any:
            con = real_connect(*args, **kwargs)
            con.execute(f"SET threads={threads}; SET memory_limit='{memory}'")
            return con

        monkeypatch.setattr(duckdb, "connect", limited)
        _run_query("SELECT count(*) AS n FROM sessions", corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 2}]


class TestQueryNeverInstallsExtensions:
    """Registration must not reach the network, even when an embeddings store exists."""

    def test_store_present_and_extension_absent_binds_empty_without_installing(
        self,
        query_corpus: Path,
        empty_extension_dir: tuple[Path, list[str]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        ext_dir, statements = empty_extension_dir
        _write_lance_store(query_corpus / "embeddings_lance")
        warnings, sink_id = _capture_warnings()
        try:
            _run_query("SELECT count(*) AS n FROM message_embeddings", query_corpus)
        finally:
            logger.remove(sink_id)
        assert json.loads(capsys.readouterr().out) == [{"n": 0}]
        assert not any(re.match(r"\s*INSTALL\b", s, re.IGNORECASE) for s in statements)
        assert list(ext_dir.rglob("*")) == [], "something was installed into the extension dir"
        assert any("--install-extension" in w for w in warnings)

    def test_no_store_means_no_load_at_all(
        self,
        query_corpus: Path,
        empty_extension_dir: tuple[Path, list[str]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, statements = empty_extension_dir
        _run_query("SELECT count(*) AS n FROM message_embeddings", query_corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 0}]
        assert not any(re.match(r"\s*(INSTALL|LOAD)\b", s, re.IGNORECASE) for s in statements)

    def test_status_reports_the_missing_extension(
        self,
        query_corpus: Path,
        tmp_path: Path,
        empty_extension_dir: tuple[Path, list[str]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_lance_store(query_corpus / "embeddings_lance")
        source = tmp_path / "source"
        source.mkdir()
        status(source_root=source, corpus_root=query_corpus, fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert payload["lance_extension_installed"] is False
        assert payload["embeddings_store_present"] is True
        assert payload["vector_search"] == "extension_missing"

    def test_search_exits_78_instead_of_pretending_the_store_is_empty(
        self,
        query_corpus: Path,
        empty_extension_dir: tuple[Path, list[str]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_lance_store(query_corpus / "embeddings_lance")
        with pytest.raises(SystemExit) as excinfo:
            search("anything", corpus_root=query_corpus, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["extension_missing"] == 78
        err = json.loads(capsys.readouterr().err)["error"]
        assert err["kind"] == "extension_missing"
        assert "--install-extension" in err["hint"]

    def test_status_reports_ready_and_no_store(
        self, query_corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        source = tmp_path / "source"
        source.mkdir()
        status(source_root=source, corpus_root=query_corpus, fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert payload["lance_extension_installed"] is True
        assert payload["vector_search"] == "no_store"
        _write_lance_store(query_corpus / "embeddings_lance")
        status(source_root=source, corpus_root=query_corpus, fmt=OutputFormat.JSON)
        assert json.loads(capsys.readouterr().out)["vector_search"] == "ready"

    def test_embed_install_extension_installs_and_exits(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The explicit install path: no scope flag needed, no Bedrock, JSON receipt."""
        from atif_duck.infrastructure import registry as registry_mod

        calls: list[object] = []

        def fake_install(con: object) -> str:
            calls.append(con)
            return "/ext/lance.duckdb_extension"

        monkeypatch.setattr(registry_mod, "install_lance_extension", fake_install)
        embed(install_extension=True, corpus_root=query_corpus, fmt=OutputFormat.JSON)
        assert json.loads(capsys.readouterr().out) == {
            "extension": "lance",
            "installed": True,
            "install_path": "/ext/lance.duckdb_extension",
        }
        assert len(calls) == 1
        assert not (query_corpus / "embeddings_lance").exists()


class TestQueryLockedConfiguration:
    """``lock_configuration`` is deliberate, and it costs ordinary ``SET`` calls."""

    def test_timezone_stays_settable(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Rendering timestamps in the reader's zone grants no filesystem reach."""
        _run_query(
            "SET TimeZone='America/New_York'; SELECT current_setting('TimeZone') AS tz",
            query_corpus,
        )
        assert json.loads(capsys.readouterr().out) == [{"tz": "America/New_York"}]

    def test_other_settings_are_refused(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Accepted regression: an unfreezable option is one injected SQL can turn off."""
        for statement in ("SET threads=1", "SET errors_as_json=true", "SET Calendar='gregorian'"):
            message = _expect_refusal(statement, query_corpus, capsys)
            assert "has been locked" in message


class TestQueryEmbeddingIdentity:
    """``query`` can run semantic_search, so it owes the same guard as ``search``."""

    def test_wrong_model_id_exits_65_with_an_envelope(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A raw traceback exits 1, which is not in EXIT_CODES at all."""
        _write_lance_store(query_corpus / "embeddings_lance")
        monkeypatch.setenv("ATIF_SQL_EMBED_MODEL_ID", "fake.model:0")

        with pytest.raises(SystemExit) as excinfo:
            _run_query("SELECT count(*) FROM message_embeddings", query_corpus)
        assert excinfo.value.code == EXIT_CODES["embedding_mismatch"]
        err = json.loads(capsys.readouterr().err)["error"]
        assert err["kind"] == "embedding_mismatch"
        assert "different provider" in err["message"]
        assert "atif-sql embed" in err["hint"]

    def test_search_wrong_model_id_exits_65_with_an_envelope(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_lance_store(query_corpus / "embeddings_lance")
        monkeypatch.setenv("ATIF_SQL_EMBED_MODEL_ID", "fake.model:0")

        with pytest.raises(SystemExit) as excinfo:
            search("anything", corpus_root=query_corpus, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["embedding_mismatch"]
        assert json.loads(capsys.readouterr().err)["error"]["kind"] == "embedding_mismatch"

    def test_matching_model_id_binds_the_store(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_lance_store(query_corpus / "embeddings_lance")

        _run_query("SELECT count(*) AS n FROM message_embeddings", query_corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 1}]

    def test_lance_uri_env_override_is_honored(
        self,
        query_corpus: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``search`` honors ATIF_SQL_LANCE_URI; ``query`` must read the same store."""
        external = _write_lance_store(tmp_path / "outside" / "store")
        monkeypatch.setenv("ATIF_SQL_LANCE_URI", str(external))
        assert not (query_corpus / "embeddings_lance").exists()

        _run_query("SELECT count(*) AS n FROM message_embeddings", query_corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 1}]

        _run_query("SELECT count(*) AS n FROM steps", query_corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 1}]


class TestEmbedExitCodes:
    """``embed`` splits DomainError by ``terminal``: 78 needs an operator,
    70 is transient and the next tick may clear it. Unattended lanes key
    retry suppression on 78, so the split must be observable at the exit."""

    def _run_embed(self, corpus_root: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> int:
        import atif_embed.application.embed as embed_mod
        from atif_cli.app import embed as embed_cmd

        async def _boom(**_kwargs: Any) -> int:
            raise exc

        monkeypatch.setattr(embed_mod, "run_backfill", _boom)
        with pytest.raises(SystemExit) as excinfo:
            embed_cmd(limit=1, corpus_root=corpus_root, fmt=OutputFormat.JSON)
        code = excinfo.value.code
        assert isinstance(code, int)
        return code

    def test_bare_embed_still_exits_64(self, tmp_path: Path) -> None:
        from atif_cli.app import embed as embed_cmd

        with pytest.raises(SystemExit) as excinfo:
            embed_cmd(corpus_root=tmp_path, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["invalid_input"]

    def test_terminal_domain_error_exits_78_with_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from atif_embed.domain.errors import EmbeddingStoreSchemaStale

        code = self._run_embed(
            tmp_path, monkeypatch, EmbeddingStoreSchemaStale("store needs an operator")
        )
        assert code == EXIT_CODES["terminal_state"] == 78
        err = json.loads(capsys.readouterr().err)["error"]
        assert err["kind"] == "terminal_state"
        assert "operator" in err["message"]

    def test_provider_mismatch_is_terminal_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from atif_embed.domain.errors import EmbeddingProviderMismatch

        code = self._run_embed(tmp_path, monkeypatch, EmbeddingProviderMismatch("wrong provider"))
        assert code == EXIT_CODES["terminal_state"]
        assert json.loads(capsys.readouterr().err)["error"]["kind"] == "terminal_state"

    def test_transient_domain_error_still_exits_70(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from atif_embed.domain.errors import EmbeddingProviderUnavailable

        code = self._run_embed(
            tmp_path, monkeypatch, EmbeddingProviderUnavailable("bedrock throttled")
        )
        assert code == EXIT_CODES["runtime_error"]
        assert json.loads(capsys.readouterr().err)["error"]["kind"] == "runtime_error"


class TestFormatResolution:
    def test_explicit_formats_pass_through(self) -> None:
        assert resolve_format(OutputFormat.JSON) is OutputFormat.JSON
        assert resolve_format("csv") is OutputFormat.CSV
        assert resolve_format(OutputFormat.TABLE) is OutputFormat.TABLE

    def test_auto_resolves_to_json_when_piped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        assert resolve_format(OutputFormat.AUTO) is OutputFormat.JSON

    def test_auto_resolves_to_table_on_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        assert resolve_format(OutputFormat.AUTO) is OutputFormat.TABLE


class TestEmitRows:
    def test_json_is_array_of_row_objects(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit_rows(["a", "b"], [(1, "x"), (2, None)], OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert payload == [{"a": 1, "b": "x"}, {"a": 2, "b": None}]

    def test_csv_has_header_and_rows(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit_rows(["a", "b"], [(1, "x")], OutputFormat.CSV)
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines == ["a,b", "1,x"]

    def test_table_renders_header_and_count(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit_rows(["col"], [("v",)], OutputFormat.TABLE)
        out = capsys.readouterr().out
        assert "col" in out
        assert "(1 row)" in out

    def test_empty_result_is_an_empty_json_array(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit_rows(["a"], [], OutputFormat.JSON)
        assert json.loads(capsys.readouterr().out) == []

    def test_empty_result_table_reports_zero_rows(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit_rows(["a"], [], OutputFormat.TABLE)
        assert "(0 rows)" in capsys.readouterr().out


class TestDuplicateColumnNames:
    """A dict keyed by column name drops repeats; the caller asked for every column."""

    def test_repeated_names_get_deterministic_suffixes(self) -> None:
        assert json_object_keys(["a", "a", "b"]) == ["a", "a_1", "b"]
        assert json_object_keys(["a", "a", "a"]) == ["a", "a_1", "a_2"]
        assert json_object_keys([]) == []
        assert json_object_keys(["a", "b"]) == ["a", "b"]

    def test_suffix_skips_a_name_an_alias_already_took(self) -> None:
        """``a_1`` is taken, so the second ``a`` must not collide onto it."""
        assert json_object_keys(["a", "a_1", "a"]) == ["a", "a_1", "a_2"]

    def test_suffix_skips_an_alias_that_comes_later_in_the_column_order(self) -> None:
        """``SELECT a, a, x AS a_1`` — the alias trails the duplicate it collides with.

        Only the full column list can answer this. A rule that checks the
        names already emitted has not seen ``a_1`` yet when it renames the
        second ``a``, so it takes the key the caller explicitly aliased and
        pushes the real ``a_1`` to ``a_1_1``.
        """
        assert json_object_keys(["a", "a", "a_1"]) == ["a", "a_2", "a_1"]

    def test_json_keeps_every_duplicate_column(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit_rows(["a", "a", "b"], [(1, 2, 3)], OutputFormat.JSON)
        assert json.loads(capsys.readouterr().out) == [{"a": 1, "a_1": 2, "b": 3}]

    def test_unaliased_join_returns_both_columns(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The realistic trigger: an agent writes a join without aliasing."""
        _run_query(
            "SELECT s.session_id, m.session_id FROM sessions s JOIN messages m USING (session_id)",
            query_corpus,
        )
        rows = json.loads(capsys.readouterr().out)
        assert rows == [{"session_id": SESSION_ID, "session_id_1": SESSION_ID}]


class TestEmitCursor:
    """``query`` streams the result; it must never hold the whole thing twice."""

    @staticmethod
    def _cursor(sql: str) -> object:
        import duckdb

        return duckdb.connect().execute(sql)

    def test_json_matches_the_materialized_shape(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit_cursor(self._cursor("SELECT 1 AS a, 'x' AS b"), OutputFormat.JSON)
        assert json.loads(capsys.readouterr().out) == [{"a": 1, "b": "x"}]

    def test_json_spanning_many_batches_is_one_valid_array(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Batch boundaries must not leak into the JSON (a missing comma, a second array)."""
        from atif_cli import output as output_mod

        monkeypatch.setattr(output_mod, "_STREAM_BATCH_ROWS", 7)
        emit_cursor(self._cursor("SELECT range AS n FROM range(50)"), OutputFormat.JSON)
        rows = json.loads(capsys.readouterr().out)
        assert [r["n"] for r in rows] == list(range(50))

    def test_the_shipped_batch_size_is_small_enough_to_bound_residency(self) -> None:
        """Every other test here patches ``_STREAM_BATCH_ROWS`` to a tiny value, so
        none of them can see the shipped constant. Widened far enough,
        ``fetchmany`` returns the whole result in one call and streaming becomes
        ``fetchall`` under another name."""
        from atif_cli import output as output_mod

        assert output_mod._STREAM_BATCH_ROWS <= 100_000

    def test_never_materializes_the_whole_result(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``fetchall`` is the defect: it holds every row before a byte is written."""

        class _RefusesFetchall:
            description = (("n",),)

            def __init__(self) -> None:
                self._remaining = [(i,) for i in range(25)]

            def fetchall(self) -> list[tuple[int]]:
                msg = "emit_cursor must stream via fetchmany, not fetchall"
                raise AssertionError(msg)

            def fetchmany(self, size: int) -> list[tuple[int]]:
                batch, self._remaining = self._remaining[:size], self._remaining[size:]
                return batch

        from atif_cli import output as output_mod

        monkeypatch.setattr(output_mod, "_STREAM_BATCH_ROWS", 4)
        emit_cursor(_RefusesFetchall(), OutputFormat.JSON)
        rows = json.loads(capsys.readouterr().out)
        assert [r["n"] for r in rows] == list(range(25))

    def test_csv_and_table_span_batches(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from atif_cli import output as output_mod

        monkeypatch.setattr(output_mod, "_STREAM_BATCH_ROWS", 3)
        emit_cursor(self._cursor("SELECT range AS n FROM range(10)"), OutputFormat.CSV)
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0] == "n"
        assert len(lines) == 11

        emit_cursor(self._cursor("SELECT range AS n FROM range(10)"), OutputFormat.TABLE)
        out = capsys.readouterr().out
        assert "(10 rows)" in out
        assert out.count("\n") == 13

    def test_table_preserves_the_null_marker_past_the_first_batch(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Later batches go through the same cell renderer as the width-setting one."""
        from atif_cli import output as output_mod

        monkeypatch.setattr(output_mod, "_STREAM_BATCH_ROWS", 2)
        emit_cursor(
            self._cursor(
                "SELECT * FROM (VALUES ('a'), ('b'), ('c'), (NULL)) t(v) ORDER BY v NULLS LAST"
            ),
            OutputFormat.TABLE,
        )
        out = capsys.readouterr().out
        assert "∅" in out
        assert "(4 rows)" in out

    def test_duplicate_columns_survive_the_stream(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit_cursor(self._cursor("SELECT 1 AS a, 2 AS a, 3 AS b"), OutputFormat.JSON)
        assert json.loads(capsys.readouterr().out) == [{"a": 1, "a_1": 2, "b": 3}]


class TestQueryStreamsItsResult:
    """``query`` itself must stream. Guarding ``emit_cursor`` alone leaves the
    call site free to go back to ``fetchall`` with the suite still green.

    Both hooks patch ``duckdb.DuckDBPyConnection`` at CLASS level, which is
    the only reachable seam: ``query`` builds its own connection internally,
    and the driver's instance attributes are read-only.
    """

    def test_query_drains_the_caller_result_in_batches(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``fetchmany`` drains the caller's result and ``fetchall`` never touches it.

        Scoped to the calls that follow the caller's own ``execute``:
        ``register`` reads its raw-meta table with ``fetchall`` legitimately,
        over a bounded internal query, before the sandbox even arms.
        """
        import duckdb

        from atif_cli import output as output_mod

        sql = "SELECT * FROM (VALUES (1), (2), (3)) t(v)"
        events: list[str] = []
        real_execute = duckdb.DuckDBPyConnection.execute
        real_fetchmany = duckdb.DuckDBPyConnection.fetchmany
        real_fetchall = duckdb.DuckDBPyConnection.fetchall

        def spy_execute(self: Any, query: Any = None, *args: Any, **kwargs: Any) -> Any:
            if query == sql:
                events.append("caller_execute")
            return real_execute(self, query, *args, **kwargs)

        def spy_fetchmany(self: Any, size: int = 1) -> Any:
            events.append("fetchmany")
            return real_fetchmany(self, size)

        def spy_fetchall(self: Any) -> Any:
            events.append("fetchall")
            return real_fetchall(self)

        monkeypatch.setattr(duckdb.DuckDBPyConnection, "execute", spy_execute)
        monkeypatch.setattr(duckdb.DuckDBPyConnection, "fetchmany", spy_fetchmany)
        monkeypatch.setattr(duckdb.DuckDBPyConnection, "fetchall", spy_fetchall)
        monkeypatch.setattr(output_mod, "_STREAM_BATCH_ROWS", 1)

        _run_query(sql, query_corpus)

        assert json.loads(capsys.readouterr().out) == [{"v": 1}, {"v": 2}, {"v": 3}]
        assert "caller_execute" in events, "the caller's SQL never reached execute"
        after = events[events.index("caller_execute") + 1 :]
        assert "fetchmany" in after, "query must drain the caller's result with fetchmany"
        assert "fetchall" not in after, "query must not materialize the caller's result"

    def test_query_writes_rows_before_the_scan_finishes(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Output interleaves with the scan; ``fetchall`` writes nothing until it ends."""
        import duckdb

        from atif_cli import output as output_mod

        real_fetchmany = duckdb.DuckDBPyConnection.fetchmany
        written_at_second_batch: list[int] = []
        batches = {"n": 0}

        def spy_fetchmany(self: Any, size: int = 1) -> Any:
            batches["n"] += 1
            if batches["n"] == 2:
                written_at_second_batch.append(len(capsys.readouterr().out))
            return real_fetchmany(self, size)

        monkeypatch.setattr(duckdb.DuckDBPyConnection, "fetchmany", spy_fetchmany)
        monkeypatch.setattr(output_mod, "_STREAM_BATCH_ROWS", 1)

        _run_query("SELECT * FROM (VALUES (1), (2), (3)) t(v)", query_corpus)

        assert written_at_second_batch, "fetchmany was never called a second time"
        assert written_at_second_batch[0] > 0, "no bytes on stdout before the scan finished"

    def test_mid_stream_non_duckdb_error_gets_an_envelope(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A lazily-bound Lance scan can trip mid-stream; exit 1 is not in EXIT_CODES.

        ``EmbeddingProviderMismatch`` derives from ``Exception``, so a
        ``duckdb.Error``-only handler around the streaming write lets it
        escape as an unhandled traceback.
        """
        import duckdb

        from atif_cli import output as output_mod
        from atif_duck.domain.embedding_guard import EmbeddingProviderMismatch

        real_fetchmany = duckdb.DuckDBPyConnection.fetchmany
        batches = {"n": 0}

        def spy_fetchmany(self: Any, size: int = 1) -> Any:
            batches["n"] += 1
            if batches["n"] > 1:
                msg = "Embedding store was written by a different provider/model."
                raise EmbeddingProviderMismatch(msg)
            return real_fetchmany(self, size)

        monkeypatch.setattr(duckdb.DuckDBPyConnection, "fetchmany", spy_fetchmany)
        monkeypatch.setattr(output_mod, "_STREAM_BATCH_ROWS", 1)

        with pytest.raises(SystemExit) as excinfo:
            _run_query("SELECT * FROM (VALUES (1), (2), (3)) t(v)", query_corpus)

        assert excinfo.value.code == EXIT_CODES["embedding_mismatch"]
        err = json.loads(capsys.readouterr().err)["error"]
        assert err["kind"] == "embedding_mismatch"
        assert "atif-sql embed" in err["hint"]

    def test_mid_stream_duckdb_error_still_classifies(
        self,
        query_corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Widening the handler must not drop the DuckDB classification it had."""
        import duckdb

        from atif_cli import output as output_mod

        real_fetchmany = duckdb.DuckDBPyConnection.fetchmany
        batches = {"n": 0}

        def spy_fetchmany(self: Any, size: int = 1) -> Any:
            batches["n"] += 1
            if batches["n"] > 1:
                msg = "scan died halfway"
                raise duckdb.IOException(msg)
            return real_fetchmany(self, size)

        monkeypatch.setattr(duckdb.DuckDBPyConnection, "fetchmany", spy_fetchmany)
        monkeypatch.setattr(output_mod, "_STREAM_BATCH_ROWS", 1)

        with pytest.raises(SystemExit) as excinfo:
            _run_query("SELECT * FROM (VALUES (1), (2), (3)) t(v)", query_corpus)

        assert excinfo.value.code == EXIT_CODES["runtime_error"]
        assert json.loads(capsys.readouterr().err)["error"]["kind"] == "runtime_error"


class TestMaterializeSuspiciousScan:
    """A refused ghost-removal gets its own exit code, not Python's uncaught 1.

    `SuspiciousEmptyScanError` is the corpus's loudest data-loss tripwire: the
    source scan found nothing while the corpus holds materialized sessions,
    almost always a wrong `source_root`. Uncaught it exits 1, which a caller
    cannot tell apart from a crash — and a crash is worth retrying while a
    wrong path is not.
    """

    @staticmethod
    def _raise_suspicious(**_kwargs: Any) -> None:
        from atif_corpus.application.materialize import SuspiciousEmptyScanError

        msg = "scan of /wrong/root found 0 sessions but the corpus holds 12 — refusing to remove"
        raise SuspiciousEmptyScanError(msg)

    def test_exits_78_with_an_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import atif_corpus.application.materialize as materialize_module

        monkeypatch.setattr(materialize_module, "materialize", self._raise_suspicious)
        with pytest.raises(SystemExit) as excinfo:
            materialize_cmd(corpus_root=tmp_path / "corpus", fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["suspicious_scan"] == 78
        err = json.loads(capsys.readouterr().err)["error"]
        assert err["kind"] == "suspicious_scan"
        assert "refusing to remove" in err["message"]
        assert err["hint"] is not None
        assert "source-root" in err["hint"] or "source_root" in err["hint"]

    def test_the_code_is_not_the_uncaught_default(self) -> None:
        """1 is what an unhandled exception exits with; this must not be that."""
        assert EXIT_CODES["suspicious_scan"] != 1


class TestMaterializeWorkers:
    """``--workers`` reaches the use case, and its default comes from settings.

    The use case is replaced with a recorder so these stay unit tests: what is
    under test is the plumbing from flag and env to the ``workers`` keyword,
    plus the per-process setup hook the CLI hands the pool.
    """

    @staticmethod
    def _recorder(calls: list[dict[str, Any]]) -> Any:
        from atif_corpus.application.materialize import MaterializationReport

        def _fake_materialize(**kwargs: Any) -> MaterializationReport:
            calls.append(kwargs)
            return MaterializationReport(
                materialized_count=0,
                up_to_date_count=0,
                skipped_live_count=0,
                failures=(),
                total_seconds=0.0,
                convert_seconds=0.0,
                workers=kwargs["workers"],
            )

        return _fake_materialize

    def _run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        import atif_corpus.application.materialize as materialize_module

        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(materialize_module, "materialize", self._recorder(calls))
        materialize_cmd(corpus_root=tmp_path / "corpus", fmt=OutputFormat.JSON, **kwargs)
        return calls

    def test_flag_reaches_the_use_case_with_the_worker_setup_hook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from atif_cli.app import _materialize_worker_setup

        calls = self._run(tmp_path, monkeypatch, workers=3)

        assert len(calls) == 1
        assert calls[0]["workers"] == 3
        assert calls[0]["worker_setup"] is _materialize_worker_setup
        assert json.loads(capsys.readouterr().out)["workers"] == 3

    def test_env_supplies_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATIF_SQL_MATERIALIZE_WORKERS", "2")
        calls = self._run(tmp_path, monkeypatch)
        assert calls[0]["workers"] == 2

    def test_unset_env_defaults_to_min_eight_and_cpu_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from atif_corpus.infrastructure.settings import default_materialize_workers

        monkeypatch.delenv("ATIF_SQL_MATERIALIZE_WORKERS", raising=False)
        calls = self._run(tmp_path, monkeypatch)
        assert calls[0]["workers"] == default_materialize_workers()

    def test_flag_beats_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATIF_SQL_MATERIALIZE_WORKERS", "2")
        calls = self._run(tmp_path, monkeypatch, workers=1)
        assert calls[0]["workers"] == 1

    def test_zero_workers_exits_64_before_the_use_case_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import atif_corpus.application.materialize as materialize_module

        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(materialize_module, "materialize", self._recorder(calls))
        with pytest.raises(SystemExit) as excinfo:
            materialize_cmd(corpus_root=tmp_path / "corpus", workers=0, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["invalid_input"] == 64
        assert calls == []
        err = json.loads(capsys.readouterr().err)["error"]
        assert err["kind"] == "invalid_input"
        assert "--workers" in err["message"]

    def test_worker_setup_installs_the_parent_sink_level(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Run the hook in-process: DEBUG must vanish and WARNING must stay."""
        from loguru import logger

        from atif_cli.app import _materialize_worker_setup

        monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
        _materialize_worker_setup()
        logger.debug("worker-debug-line")
        logger.warning("worker-warning-line")
        err = capsys.readouterr().err
        assert "worker-debug-line" not in err
        assert "worker-warning-line" in err
        logger.remove()


class TestStderrLogLevel:
    """`main` installs the ONLY log sink in the workspace.

    It calls `logger.remove()` first, which drops the default handler and with
    it the `LOGURU_LEVEL` that would have parameterized it. Without a knob of
    its own, every `logger.info` in every package is unreachable through the
    CLI.
    """

    def test_default_is_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
        assert _stderr_log_level() == DEFAULT_LOG_LEVEL == "WARNING"

    def test_env_wins_and_is_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, "  info ")
        assert _stderr_log_level() == "INFO"

    def test_blank_reads_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, "   ")
        assert _stderr_log_level() == DEFAULT_LOG_LEVEL

    @staticmethod
    def _emit_through_main(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> str:
        """Run `main`'s sink setup, then emit one line per level; return stderr.

        `app` is stubbed out because the sink is what is under test, not the
        command dispatch. The sink is removed afterwards: `main` calls
        `logger.remove()`, so leaving it installed would undo `_quiet_loguru`
        for every test that runs after this one.
        """
        from loguru import logger

        monkeypatch.setattr(app_module, "app", lambda: None)
        app_module.main()
        try:
            logger.info("an info line")
            logger.warning("a warning line")
        finally:
            logger.remove()
        return capsys.readouterr().err

    def test_info_env_makes_the_info_surface_reachable(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, "INFO")
        err = self._emit_through_main(monkeypatch, capsys)
        assert "an info line" in err
        assert "a warning line" in err

    def test_default_still_hides_info(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
        err = self._emit_through_main(monkeypatch, capsys)
        assert "an info line" not in err, "a piped read must stay quiet on stderr"
        assert "a warning line" in err

    def test_unknown_level_falls_back_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bad log level is not a reason to refuse to run the command."""
        monkeypatch.setenv(LOG_LEVEL_ENV, "CHATTY")
        err = self._emit_through_main(monkeypatch, capsys)
        assert "is not a loguru level" in err
        assert "an info line" not in err
        assert "a warning line" in err
