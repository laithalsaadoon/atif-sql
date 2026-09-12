# SPDX-License-Identifier: Apache-2.0

"""The columnar artifacts: produced correctly, read in preference, equal to JSON.

The one property that matters is EQUIVALENCE: every view and macro returns the
same rows whether a session is read from ``trajectory.json`` or from its
parquet artifacts. It is asserted with ``EXCEPT ALL`` in both directions over
a three-session corpus (the two Claude Code sessions plus the Codex one, which
carries the other cache-write key and an empty assistant message), in every
source mix the registry can meet: all JSON, all columnar, and mixed.

The second property is that the columnar path is actually TAKEN. A registry
that silently fell back to JSON would pass every equivalence test, so one test
destroys every ``trajectory.json`` after producing the artifacts and asserts
the views still answer.
"""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path
from typing import Any

import duckdb
import pytest
from duck_fixtures import CODEX_SESSION_ID, SESSION_IDS, build_corpus, write_codex_session

from atif_duck.domain.catalog import VIEW_NAMES, VIEW_SCHEMA
from atif_duck.domain.columnar import (
    COLUMNAR_FILENAMES,
    COLUMNAR_SCHEMA_VERSION,
    COLUMNAR_SCHEMAS,
    META_COLUMNAR_KEY,
    SESSION_COLUMNS,
    STEPS_PARQUET,
    TOOL_CALLS_PARQUET,
    columnar_paths,
)
from atif_duck.infrastructure.columnar import (
    ColumnarArtifactProducer,
    columnar_coverage,
    session_has_columnar,
)
from atif_duck.infrastructure.registry import (
    _TRAJECTORY_COLUMNS,
    RawSources,
    register,
    register_raw,
)

ALL_SESSION_IDS = (*SESSION_IDS, CODEX_SESSION_ID)

#: The panel statements the lever is measured on (byte-identical output is
#: asserted at the CLI layer; here the fetched rows must be equal), plus two
#: more windows: one that isolates the sidechain surface, one that joins the
#: two step-derived views.
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
        "SELECT session_id, count(*) AS n, sum(cache_creation) AS created FROM subagent_steps "
        "GROUP BY 1 ORDER BY 1"
    ),
    (
        "SELECT tc.tool_name, count(*) AS n, count(tr.content) AS answered FROM tool_calls tc "
        "LEFT JOIN tool_results tr USING (tool_use_id) "
        "WHERE tc.ts >= TIMESTAMP '2026-08-01' AND tc.ts < TIMESTAMP '2026-10-01' "
        "GROUP BY 1 ORDER BY 1"
    ),
)


def _write_three_sessions(root: Path) -> Path:
    build_corpus(root)
    write_codex_session(root)
    return root


def add_columnar(corpus_root: Path, session_ids: tuple[str, ...] = ALL_SESSION_IDS) -> None:
    """Produce the columnar artifacts for ``session_ids`` in place and stamp meta.

    Tests drive the producer directly against published session dirs; the
    corpus writer normally does this inside its staging dir before the swap.
    Upgrading a root IN PLACE is what makes the equivalence tests exact: the
    ``sessions.trajectory_path`` and ``loss_reports.report_path`` columns carry
    the corpus root, so a second root would differ on every row.
    """
    producer = ColumnarArtifactProducer()
    for session_id in session_ids:
        session_dir = corpus_root / "sessions" / session_id
        trajectory = json.loads((session_dir / "trajectory.json").read_text())
        extras = producer.produce(session_dir, session_id=session_id, trajectory=trajectory)
        meta = json.loads((session_dir / "meta.json").read_text())
        meta.update(extras)
        (session_dir / "meta.json").write_text(json.dumps(meta, separators=(",", ":")))


@pytest.fixture
def json_corpus(tmp_path: Path) -> Path:
    """Three sessions, JSON artifacts only (the pre-columnar materialization)."""
    return _write_three_sessions(tmp_path / "json")


@pytest.fixture
def columnar_corpus(tmp_path: Path) -> Path:
    """The same three sessions with columnar artifacts on every one."""
    root = _write_three_sessions(tmp_path / "columnar")
    add_columnar(root)
    return root


def snapshot_views(con: duckdb.DuckDBPyConnection, prefix: str) -> None:
    """Materialize every catalog view into ``<prefix>_<view>`` tables on ``con``."""
    for view in VIEW_NAMES:
        con.execute(f"CREATE OR REPLACE TABLE {prefix}_{view} AS SELECT * FROM {view}")


def snapshot_json_reading(con: duckdb.DuckDBPyConnection, corpus_root: Path) -> None:
    """Register ``corpus_root`` (which must be JSON-only) and snapshot every view as ``json_*``."""
    sources = register(con, corpus_root)
    assert sources.columnar_session_ids == ()
    assert set(sources.json_session_ids) == set(ALL_SESSION_IDS)
    snapshot_views(con, "json")
    # Not vacuous: the fixture exercises every step-derived surface.
    for view in ("steps", "tool_calls", "tool_results", "skill_usage", "tasks_state_current"):
        n = con.execute(f"SELECT count(*) FROM json_{view}").fetchone()
        assert n is not None and n[0] > 0, view


def assert_views_equal_snapshot(con: duckdb.DuckDBPyConnection, prefix: str) -> None:
    """Every view on ``con`` equals its snapshot: same DESCRIBE, empty EXCEPT ALL both ways."""
    for view in VIEW_NAMES:
        live = con.execute(f"DESCRIBE {view}").fetchall()
        snap = con.execute(f"DESCRIBE {prefix}_{view}").fetchall()
        assert [(r[0], r[1]) for r in live] == [(r[0], r[1]) for r in snap], view
        for left, right in ((view, f"{prefix}_{view}"), (f"{prefix}_{view}", view)):
            missing = con.execute(
                f"SELECT count(*) FROM ((SELECT * FROM {left}) EXCEPT ALL (SELECT * FROM {right}))"
            ).fetchone()
            assert missing is not None
            assert missing[0] == 0, f"{left} has rows {right} lacks"
        n_live = con.execute(f"SELECT count(*) FROM {view}").fetchone()
        n_snap = con.execute(f"SELECT count(*) FROM {prefix}_{view}").fetchone()
        assert n_live is not None and n_snap is not None
        assert n_live[0] == n_snap[0], view


def panel_rows(con: duckdb.DuckDBPyConnection) -> list[list[tuple[Any, ...]]]:
    return [con.execute(sql).fetchall() for sql in PANEL_QUERIES]


def described(con: duckdb.DuckDBPyConnection, relation: str) -> tuple[tuple[str, str], ...]:
    """``(column, type)`` pairs as DuckDB's DESCRIBE prints them for ``relation``."""
    rows = con.execute(f"DESCRIBE {relation}").fetchall()
    return tuple((str(r[0]), str(r[1])) for r in rows)


def assert_parquet_matches(
    con: duckdb.DuckDBPyConnection, path: Path, expected: tuple[tuple[str, str], ...]
) -> None:
    """The file's schema equals ``expected``, both rendered by DuckDB's type printer.

    DESCRIBE quotes reserved words inside STRUCT members (``"name" VARCHAR``)
    while the catalog spells the canonical cast form, so the expected side is
    rendered through the same printer rather than string-matched.
    """
    typed_nulls = ", ".join(f"NULL::{sql_type} AS {name}" for name, sql_type in expected)
    assert described(con, f"SELECT * FROM read_parquet('{path}')") == described(
        con, f"SELECT {typed_nulls}"
    ), path


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------


class TestProducer:
    def test_writes_four_read_only_parquets_and_returns_the_meta_key(
        self, json_corpus: Path
    ) -> None:
        session_dir = json_corpus / "sessions" / SESSION_IDS[0]
        trajectory = json.loads((session_dir / "trajectory.json").read_text())

        extras = ColumnarArtifactProducer().produce(
            session_dir, session_id=SESSION_IDS[0], trajectory=trajectory
        )

        assert dict(extras) == {META_COLUMNAR_KEY: COLUMNAR_SCHEMA_VERSION}
        for path in columnar_paths(session_dir):
            assert path.is_file(), path
            assert stat.S_IMODE(path.stat().st_mode) == 0o444, path
        assert session_has_columnar(session_dir, COLUMNAR_SCHEMA_VERSION)

    def test_parquet_schemas_match_the_catalog(self, columnar_corpus: Path) -> None:
        """Each file carries exactly its view's columns and types (drift catcher)."""
        con = duckdb.connect(":memory:")
        for session_id in ALL_SESSION_IDS:
            for name, expected in COLUMNAR_SCHEMAS.items():
                assert_parquet_matches(
                    con, columnar_corpus / "sessions" / session_id / name, expected
                )

    def test_session_without_tool_calls_still_gets_typed_empty_files(self, tmp_path: Path) -> None:
        """An empty parquet with the right columns is the correct artifact for no calls."""
        session_dir = tmp_path / "sessions" / "empty"
        session_dir.mkdir(parents=True)
        trajectory: dict[str, Any] = {
            "schema_version": "ATIF-v1.7",
            "session_id": "empty",
            "agent": {"name": "claude-code", "version": "1", "model_name": "m"},
            "steps": [{"step_id": 1, "timestamp": "2026-08-20T10:00:00.000Z", "source": "user"}],
            "final_metrics": {"total_steps": 1},
        }
        ColumnarArtifactProducer().produce(session_dir, session_id="empty", trajectory=trajectory)
        con = duckdb.connect(":memory:")
        for name in (TOOL_CALLS_PARQUET, "tool_results.parquet"):
            path = session_dir / name
            count = con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()
            assert count is not None
            assert count[0] == 0
            assert_parquet_matches(con, path, COLUMNAR_SCHEMAS[name])
        steps = con.execute(
            f"SELECT session_id, step_id, message, is_sidechain, prompt_tokens "
            f"FROM read_parquet('{session_dir / STEPS_PARQUET}')"
        ).fetchall()
        assert steps == [("empty", 1, None, False, None)]

    def test_trajectory_projection_and_session_columns_agree(self) -> None:
        """The JSON reader's top-level projection IS session.parquet's shape plus steps."""
        without_steps = {k: v for k, v in _TRAJECTORY_COLUMNS.items() if k != "steps"}
        assert without_steps == dict(SESSION_COLUMNS[1:])


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------


class TestEquivalence:
    def test_every_view_is_identical_between_json_and_columnar(self, json_corpus: Path) -> None:
        con = duckdb.connect(":memory:")
        snapshot_json_reading(con, json_corpus)
        json_panel = panel_rows(con)

        add_columnar(json_corpus)
        columnar_sources = register(con, json_corpus)
        assert set(columnar_sources.columnar_session_ids) == set(ALL_SESSION_IDS)
        assert columnar_sources.json_session_ids == ()

        assert_views_equal_snapshot(con, "json")
        assert panel_rows(con) == json_panel

    def test_mixed_corpus_unions_both_sources_identically(self, json_corpus: Path) -> None:
        con = duckdb.connect(":memory:")
        snapshot_json_reading(con, json_corpus)
        json_panel = panel_rows(con)

        add_columnar(json_corpus, (SESSION_IDS[0],))
        sources = register(con, json_corpus)
        assert sources.columnar_session_ids == (SESSION_IDS[0],)
        assert set(sources.json_session_ids) == {SESSION_IDS[1], CODEX_SESSION_ID}
        assert sources.coverage.query_path == "mixed"

        assert_views_equal_snapshot(con, "json")
        assert panel_rows(con) == json_panel

    def test_macros_agree_between_the_two_paths(
        self, json_corpus: Path, columnar_corpus: Path
    ) -> None:
        statements = (
            f"SELECT model_used('{SESSION_IDS[0]}')",
            f"SELECT * FROM cost_estimate('{SESSION_IDS[0]}')",
            f"SELECT * FROM cost_estimate('{SESSION_IDS[1]}')",
            f"SELECT * FROM cost_estimate('{CODEX_SESSION_ID}')",
            f"SELECT todo_velocity('{SESSION_IDS[0]}')",
            f"SELECT subagent_fanout('{SESSION_IDS[0]}')",
            "SELECT * FROM tool_rank(36500) ORDER BY n DESC, tool_name",
            "SELECT * FROM skill_rank(36500) ORDER BY skill_id",
            "SELECT * FROM skill_source_mix(36500) ORDER BY skill_id",
        )
        json_con = duckdb.connect(":memory:")
        register(json_con, json_corpus)
        columnar_con = duckdb.connect(":memory:")
        register(columnar_con, columnar_corpus)
        for sql in statements:
            assert json_con.execute(sql).fetchall() == columnar_con.execute(sql).fetchall(), sql


# ---------------------------------------------------------------------------
# The columnar path is taken, and only when it is safe to take
# ---------------------------------------------------------------------------


class TestSourceSelection:
    def test_columnar_registration_never_opens_trajectory_json(self, json_corpus: Path) -> None:
        """Destroying every trajectory.json must not change a single row.

        This is the test that fails if the registry silently falls back to
        JSON: the fallback would raise on the garbage, and a partial fallback
        would lose rows against the snapshot.
        """
        con = duckdb.connect(":memory:")
        snapshot_json_reading(con, json_corpus)
        add_columnar(json_corpus)
        for session_id in ALL_SESSION_IDS:
            (json_corpus / "sessions" / session_id / "trajectory.json").write_text("not json")

        sources = register(con, json_corpus)

        assert sources.json_session_ids == ()
        assert set(sources.columnar_session_ids) == set(ALL_SESSION_IDS)
        tables = {
            str(r[0])
            for r in con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE temporary AND NOT internal"
            ).fetchall()
        }
        assert "v_raw_trajectories_json" not in tables
        assert_views_equal_snapshot(con, "json")

    def test_lazy_read_paths_name_exactly_the_columnar_files_bound(
        self, columnar_corpus: Path
    ) -> None:
        con = duckdb.connect(":memory:")
        sources: RawSources = register_raw(con, columnar_corpus)
        expected = sorted(
            columnar_corpus / "sessions" / sid / name
            for sid in ALL_SESSION_IDS
            for name in COLUMNAR_FILENAMES
        )
        assert sorted(sources.lazy_read_paths) == expected

    def test_stale_schema_version_reads_from_json(self, json_corpus: Path) -> None:
        """A parquet written against another schema is ignored, not misread."""
        con = duckdb.connect(":memory:")
        snapshot_json_reading(con, json_corpus)
        add_columnar(json_corpus)
        meta_path = json_corpus / "sessions" / SESSION_IDS[0] / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta[META_COLUMNAR_KEY] = COLUMNAR_SCHEMA_VERSION + 1
        meta_path.write_text(json.dumps(meta))

        sources = register(con, json_corpus)

        assert sources.json_session_ids == (SESSION_IDS[0],)
        assert set(sources.columnar_session_ids) == {SESSION_IDS[1], CODEX_SESSION_ID}
        assert_views_equal_snapshot(con, "json")

    def test_torn_parquet_reads_from_json(self, json_corpus: Path) -> None:
        """A zero-length parquet (crashed writer) sends the session down the JSON path."""
        con = duckdb.connect(":memory:")
        snapshot_json_reading(con, json_corpus)
        add_columnar(json_corpus)
        torn = json_corpus / "sessions" / SESSION_IDS[1] / STEPS_PARQUET
        torn.chmod(0o644)
        torn.write_bytes(b"")

        sources = register(con, json_corpus)

        assert sources.json_session_ids == (SESSION_IDS[1],)
        assert_views_equal_snapshot(con, "json")

    def test_meta_without_the_key_reads_from_json(self, json_corpus: Path) -> None:
        """Parquet files beside a meta.json that never claimed them are ignored."""
        add_columnar(json_corpus, (SESSION_IDS[0],))
        meta_path = json_corpus / "sessions" / SESSION_IDS[0] / "meta.json"
        meta = json.loads(meta_path.read_text())
        del meta[META_COLUMNAR_KEY]
        meta_path.write_text(json.dumps(meta))

        con = duckdb.connect(":memory:")
        sources = register(con, json_corpus)
        assert sources.columnar_session_ids == ()
        assert set(sources.json_session_ids) == set(ALL_SESSION_IDS)

    def test_torn_session_dir_is_still_invisible(self, columnar_corpus: Path) -> None:
        """The meta gate still governs: parquet without meta.json contributes nothing."""
        torn = columnar_corpus / "sessions" / "33333333-3333-3333-3333-333333333333"
        shutil.copytree(columnar_corpus / "sessions" / SESSION_IDS[0], torn)
        (torn / "meta.json").unlink()
        con = duckdb.connect(":memory:")
        sources = register(con, columnar_corpus)
        assert torn.name not in sources.columnar_session_ids
        assert torn.name not in sources.json_session_ids
        n = con.execute("SELECT count(*) FROM steps WHERE session_id = ?", [torn.name]).fetchone()
        assert n is not None and n[0] == 0


class TestCoverage:
    def test_query_path_reports_each_mix(self, json_corpus: Path, tmp_path: Path) -> None:
        assert columnar_coverage(json_corpus).query_path == "json"
        assert columnar_coverage(tmp_path / "absent").query_path == "empty"

        mixed = tmp_path / "mixed"
        shutil.copytree(json_corpus, mixed)
        add_columnar(mixed, (SESSION_IDS[0],))
        coverage = columnar_coverage(mixed)
        assert (coverage.columnar_sessions, coverage.json_sessions) == (1, 2)
        assert coverage.query_path == "mixed"

        add_columnar(mixed, (SESSION_IDS[1], CODEX_SESSION_ID))
        assert columnar_coverage(mixed).query_path == "columnar"

    def test_coverage_and_registry_apply_the_same_predicate(self, columnar_corpus: Path) -> None:
        torn = columnar_corpus / "sessions" / SESSION_IDS[1] / STEPS_PARQUET
        torn.chmod(0o644)
        torn.write_bytes(b"")
        coverage = columnar_coverage(columnar_corpus)
        con = duckdb.connect(":memory:")
        sources = register_raw(con, columnar_corpus)
        assert sources.coverage == coverage
        assert coverage.query_path == "mixed"
        assert VIEW_SCHEMA["steps"] == COLUMNAR_SCHEMAS[STEPS_PARQUET]
