# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the atif-sql CLI surface (no live corpus, no subprocess)."""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cli_fixtures import write_analytics_parquets

from atif_cli import app as app_module
from atif_cli.app import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVEL_ENV,
    _stderr_log_level,
    app,
    convert,
    examples,
    materialize as materialize_cmd,
    query,
    schema,
    search,
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


def _expect_refusal(sql: str, corpus_root: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """Run ``sql`` expecting the sandbox to refuse it; return the message."""
    with pytest.raises(SystemExit) as excinfo:
        _run_query(sql, corpus_root)
    assert excinfo.value.code == EXIT_CODES["runtime_error"]
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["kind"] == "runtime_error"
    return str(payload["error"]["message"])


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

        _expect_refusal(f"COPY (SELECT 42 AS x) TO '{target}'", query_corpus, capsys)
        assert not target.exists()

    def test_attach_unrelated_database_is_refused(
        self, query_corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _expect_refusal(f"ATTACH '{tmp_path / 'other.db'}' AS o", query_corpus, capsys)

    def test_extension_install_is_refused(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No INSTALL/LOAD means no httpfs, which means no network egress."""
        _expect_refusal("INSTALL httpfs", query_corpus, capsys)

    def test_injected_sql_cannot_reopen_the_sandbox(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The same SQL string cannot disarm the settings that confine it."""
        for statement in (
            "SET enable_external_access=true",
            "SET allowed_directories=['/']",
            "SET memory_limit='512GB'",
            "SET temp_directory='/tmp'",
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

        message = _expect_refusal(
            f"COPY (SELECT 'destroyed' AS x) TO '{trajectory}'", query_corpus, capsys
        )
        assert "Permission" in message
        assert trajectory.read_text() == before

    def test_copy_new_file_into_the_corpus_is_refused(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = query_corpus / "pwned.csv"

        _expect_refusal(f"COPY (SELECT 42) TO '{target}'", query_corpus, capsys)
        assert not target.exists()

    def test_copy_into_the_lance_store_is_refused(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_lance_store(query_corpus / "embeddings_lance")
        target = query_corpus / "embeddings_lance" / "exfil.csv"

        _expect_refusal(f"COPY (SELECT 42) TO '{target}'", query_corpus, capsys)
        assert not target.exists()

    def test_traversal_out_of_the_spill_directory_is_refused(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``.duckdb_tmp`` is writable and sits inside the corpus — ``..`` must not escape.

        The spill dir is created up front on purpose: DuckDB reports a
        missing parent directory as an IO error, which would make this pass
        with no sandbox at all.
        """
        (query_corpus / ".duckdb_tmp").mkdir(exist_ok=True)
        trajectory = query_corpus / "sessions" / SESSION_ID / "trajectory.json"
        before = trajectory.read_text()
        escape = query_corpus / ".duckdb_tmp" / ".." / "sessions" / SESSION_ID / "trajectory.json"

        message = _expect_refusal(
            f"COPY (SELECT 'destroyed' AS x) TO '{escape}'", query_corpus, capsys
        )
        assert "Permission" in message
        assert trajectory.read_text() == before

    def test_plain_copy_over_an_analytics_parquet_is_refused(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The read grant is per-file; DuckDB stages ``COPY`` through an ungranted sibling."""
        parquet = query_corpus / "analytics" / "clusters.parquet"
        before = parquet.read_bytes()

        _expect_refusal(
            f"COPY (SELECT 'x' AS uuid) TO '{parquet}' (FORMAT PARQUET)", query_corpus, capsys
        )
        assert parquet.read_bytes() == before

    def test_documented_residual_holes_still_behave_as_documented(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Pins the two holes ``query``'s docstring admits, so the promise cannot rot.

        Both are recomputable derived data. If a future DuckDB closes either,
        this test fails and the docstring's Sandbox section must lose the
        corresponding paragraph.
        """
        parquet = query_corpus / "analytics" / "clusters.parquet"
        before = parquet.read_bytes()
        _run_query(
            f"COPY (SELECT 'x' AS uuid) TO '{parquet}' (FORMAT PARQUET, USE_TMP_FILE false)",
            query_corpus,
        )
        capsys.readouterr()
        assert parquet.read_bytes() != before, (
            "USE_TMP_FILE false no longer overwrites a granted parquet — "
            "drop that paragraph from query's Sandbox docstring"
        )

        _write_lance_store(query_corpus / "embeddings_lance")
        _run_query("DELETE FROM lance_store.main.embeddings", query_corpus)
        capsys.readouterr()
        _run_query("SELECT count(*) AS n FROM message_embeddings", query_corpus)
        assert json.loads(capsys.readouterr().out) == [{"n": 0}], (
            "the ATTACHed Lance store is no longer writable — "
            "drop that paragraph from query's Sandbox docstring"
        )


class TestQueryMemoryCap:
    """``_query_memory_limit_bytes`` is pure arithmetic over ``os.sysconf``."""

    @staticmethod
    def _limit_for(physical_bytes: int, monkeypatch: pytest.MonkeyPatch) -> int:
        import os

        from atif_cli import app as app_mod

        page = 4096

        def _sysconf(name: str | int) -> int:
            return page if name == "SC_PAGE_SIZE" else physical_bytes // page

        monkeypatch.setattr(os, "sysconf", _sysconf)
        return app_mod._query_memory_limit_bytes()

    def test_ceiling_wins_on_a_small_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """4 GiB of RAM: 50% is 2 GiB, but the 80% ceiling (3.2 GiB) is tighter than the floor."""
        from atif_cli.app import _QUERY_MEMORY_FLOOR_BYTES

        physical = 4 * 1024**3
        assert self._limit_for(physical, monkeypatch) == int(physical * 0.8)
        assert int(physical * 0.8) < _QUERY_MEMORY_FLOOR_BYTES

    def test_half_of_ram_wins_on_a_large_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        physical = 128 * 1024**3
        assert self._limit_for(physical, monkeypatch) == int(physical * 0.5)

    def test_floor_beats_half_between_the_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """12 GiB: half (6 GiB) is under the floor, and the floor is under the 80% ceiling."""
        from atif_cli.app import _QUERY_MEMORY_FLOOR_BYTES

        physical = 12 * 1024**3
        assert self._limit_for(physical, monkeypatch) == _QUERY_MEMORY_FLOOR_BYTES
        assert int(physical * 0.5) < _QUERY_MEMORY_FLOOR_BYTES < int(physical * 0.8)

    def test_never_exceeds_duckdbs_own_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for gib in (1, 4, 16, 64, 256):
            physical = gib * 1024**3
            assert self._limit_for(physical, monkeypatch) <= int(physical * 0.8)

    def test_connection_carries_the_cap_and_a_corpus_local_spill_dir(
        self, query_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The settings are read back through caller SQL — the only honest witness."""
        from atif_cli.app import _query_memory_limit_bytes

        _run_query(
            "SELECT current_setting('memory_limit') AS mem, "
            "current_setting('temp_directory') AS tmp",
            query_corpus,
        )
        row = json.loads(capsys.readouterr().out)[0]
        assert row["tmp"] == str(query_corpus / ".duckdb_tmp")

        expected_gib = _query_memory_limit_bytes() / 1024**3
        reported = row["mem"]
        assert reported.endswith("GiB"), reported
        assert float(reported.removesuffix(" GiB")) == pytest.approx(expected_gib, abs=0.1)
        assert reported != duckdb_default_memory_limit()


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
