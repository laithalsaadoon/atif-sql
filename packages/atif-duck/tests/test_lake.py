# SPDX-License-Identifier: Apache-2.0

"""The DuckLake writer, reader, verifier and maintenance (atif_duck.*.lake).

The views over the lake are covered where the views are: every view test
module takes its connection through ``duck_fixtures.register_via``, which
runs it over the per-session artifacts and over a lake loaded from them.
This module covers what only the lake has: its schema identity, the sink's
per-session replace, the published reader catalog, the writer lock, rebuild,
verify and compact.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from duck_fixtures import SESSION_IDS, build_corpus, write_codex_session
from test_columnar import add_columnar

from atif_duck.domain import lake as lake_domain
from atif_duck.domain.catalog import VIEW_SCHEMA
from atif_duck.domain.lake import (
    LAKE_ALIAS,
    LAKE_SCHEMA_VERSION,
    LAKE_TABLES,
    lake_schema_digest,
)
from atif_duck.domain.raw_readers import EDGE_COLUMNS, META_COLUMNS
from atif_duck.infrastructure import lake as lake_mod
from atif_duck.infrastructure.lake import (
    DuckLakeSessionSink,
    LakeCorpus,
    LakeCorpusConflictError,
    LakeLayout,
    LakeLockTimeoutError,
    LakeReader,
    LakeUnavailable,
    attach_lake_for_query,
    compact_lake,
    lake_status,
    rebuild_lake,
    verify_lake,
    writer_lock,
)
from atif_duck.infrastructure.registry import register, register_raw

#: The pinned identity of the lake schema. A change to any shape the lake
#: derives from (the catalog's step views, the raw reader columns, a
#: partition spec) moves the digest. Decide whether the change also needs
#: LAKE_SCHEMA_VERSION bumped, then re-pin: a lake recording another digest is
#: rebuilt by the next materialize, so the digest alone already forces the
#: rebuild; the version is for a change the definitions do not show.
PINNED_SCHEMA = (1, "0e0beccb2e63487387ccfc66d2bcc3f53b74b3b68ea1e498dc53710a67422039")


def _corpus(root: Path) -> LakeCorpus:
    return LakeCorpus(root=build_corpus(root), agent="claude-code")


def _lake(tmp_path: Path, *corpora: LakeCorpus) -> LakeLayout:
    layout = LakeLayout(tmp_path / "lake")
    rebuild_lake(layout, list(corpora))
    return layout


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[Any]:
    return con.execute(sql, params or []).fetchall()


def _reader(
    layout: LakeLayout, corpus_root: Path, *, all_corpora: bool = False
) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    reader = attach_lake_for_query(con, layout, corpus_root=corpus_root, all_corpora=all_corpora)
    assert isinstance(reader, LakeReader), reader
    register(con, corpus_root, lake=reader, skip_vss=True)
    return con


def _writer(layout: LakeLayout) -> duckdb.DuckDBPyConnection:
    """A raw read-write attach of the writer catalog, for tampering in tests."""
    con = duckdb.connect()
    lake_mod.load_ducklake(con)
    lake_mod._attach(con, layout.catalog_path, layout.data_dir, read_only=False)
    return con


def _publish(con: duckdb.DuckDBPyConnection, layout: LakeLayout) -> None:
    con.execute(f"DETACH {LAKE_ALIAS}")
    con.close()
    lake_mod._publish_reader_catalog(layout)


def _all_rows(con: duckdb.DuckDBPyConnection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        table.name: sorted(
            _rows(con, f"SELECT * FROM {LAKE_ALIAS}.{table.name}"),
            key=repr,
        )
        for table in LAKE_TABLES
    }


# ---------------------------------------------------------------------------
# Schema: derived, pinned, recorded
# ---------------------------------------------------------------------------


class TestSchema:
    def test_step_tables_carry_their_views_columns_after_the_identity(self) -> None:
        by_name = {table.name: table for table in LAKE_TABLES}
        for view in ("steps", "tool_calls", "tool_results", "session_events"):
            table = by_name[view]
            assert table.columns[:3] == (
                ("corpus", "VARCHAR"),
                ("agent", "VARCHAR"),
                ("session_id", "VARCHAR"),
            )
            assert table.columns[3:] == tuple(c for c in VIEW_SCHEMA[view] if c[0] != "session_id")

    def test_json_artifact_tables_carry_the_raw_reader_columns(self) -> None:
        by_name = {table.name: table for table in LAKE_TABLES}
        assert tuple((c.source, c.sql_type) for c in by_name["edges"].payload) == tuple(
            EDGE_COLUMNS.items()
        )
        # meta.json's own session_id and agent collide with the identity
        # columns, so they are stored under the prefix and read back renamed.
        meta = by_name["session_meta"].payload
        assert tuple((c.source, c.sql_type) for c in meta) == tuple(META_COLUMNS.items())
        renamed = {c.source: c.name for c in meta if c.name != c.source}
        assert renamed == {"session_id": "src_session_id", "agent": "src_agent"}

    def test_the_schema_identity_is_pinned(self) -> None:
        assert (LAKE_SCHEMA_VERSION, lake_schema_digest()) == PINNED_SCHEMA

    def test_a_column_change_moves_the_digest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        before = lake_schema_digest()
        steps = next(t for t in LAKE_TABLES if t.name == "steps")
        widened = lake_domain.LakeTable(
            name=steps.name,
            relation=steps.relation,
            load_source=steps.load_source,
            key_source=steps.key_source,
            payload=(*steps.payload, lake_domain.LakeColumn("extra_col", "VARCHAR", "extra_col")),
            partition_by=steps.partition_by,
        )
        monkeypatch.setattr(
            lake_domain,
            "LAKE_TABLES",
            tuple(widened if t.name == "steps" else t for t in LAKE_TABLES),
        )
        assert lake_schema_digest() != before

    def test_a_lake_records_its_identity_and_partitions_by_agent_corpus_month(
        self, tmp_path: Path
    ) -> None:
        corpus = _corpus(tmp_path / "corpus-a")
        layout = _lake(tmp_path, corpus)
        state = lake_status(layout)
        assert state.stale == ()
        assert state.schema == lake_domain.expected_lake_info()
        con = duckdb.connect()
        lake_mod.load_ducklake(con)
        lake_mod._attach(con, layout.reader_catalog_path, layout.data_dir, read_only=True)
        steps_files = [
            str(row[0])
            for row in _rows(
                con, "SELECT data_file FROM ducklake_list_files(?, 'steps')", [LAKE_ALIAS]
            )
        ]
        assert steps_files
        assert all("agent=claude-code/corpus=corpus-a/year=2026/month=" in f for f in steps_files)
        meta_files = [
            str(row[0])
            for row in _rows(
                con, "SELECT data_file FROM ducklake_list_files(?, 'session_meta')", [LAKE_ALIAS]
            )
        ]
        assert all("agent=claude-code/corpus=corpus-a/ducklake-" in f for f in meta_files)
        # Nothing is inlined into the catalog: every row lives in a data file.
        inlined = _rows(
            con,
            "SELECT count(*) FROM duckdb_tables() WHERE database_name = ? "
            "AND table_name LIKE 'ducklake_inlined_data%'",
            [lake_domain.LAKE_METADATA_ALIAS],
        )
        assert inlined == [(0,)]


# ---------------------------------------------------------------------------
# Loading: the lake holds exactly the per-session rows
# ---------------------------------------------------------------------------


class TestLoad:
    def test_columnar_and_json_sessions_load_to_the_same_rows(self, tmp_path: Path) -> None:
        json_root = build_corpus(tmp_path / "same")
        json_layout = _lake(tmp_path / "j", LakeCorpus(json_root, "claude-code"))
        json_rows = _all_rows(_writer(json_layout))
        add_columnar(json_root, tuple(SESSION_IDS))
        col_layout = _lake(tmp_path / "c", LakeCorpus(json_root, "claude-code"))
        col_rows = _all_rows(_writer(col_layout))
        # session_meta differs by design: add_columnar stamps columnar_schema.
        assert {t: r for t, r in col_rows.items() if t != "session_meta"} == {
            t: r for t, r in json_rows.items() if t != "session_meta"
        }
        assert all(row[-4] == 2 for row in col_rows["session_meta"])
        assert all(row[-4] is None for row in json_rows["session_meta"])

    def test_a_filtered_registration_holds_only_the_named_sessions(self, tmp_path: Path) -> None:
        root = build_corpus(tmp_path / "corpus")
        con = duckdb.connect()
        register_raw(con, root, session_ids=[SESSION_IDS[1], "no-such-session", "bad id"])
        assert _rows(con, "SELECT DISTINCT session_id_path FROM v_raw_meta") == [(SESSION_IDS[1],)]
        assert _rows(con, "SELECT DISTINCT session_id FROM v_raw_steps") == [(SESSION_IDS[1],)]
        assert _rows(con, "SELECT DISTINCT session_id_path FROM v_raw_edges") == [(SESSION_IDS[1],)]
        register_raw(con, root, session_ids=[])
        for relation in ("v_raw_meta", "v_raw_steps", "v_raw_edges", "v_raw_loss_reports"):
            assert _rows(con, f"SELECT count(*) FROM {relation}") == [(0,)], relation


# ---------------------------------------------------------------------------
# The sink
# ---------------------------------------------------------------------------


def _drop_last_step(session_dir: Path) -> None:
    path = session_dir / "trajectory.json"
    trajectory = json.loads(path.read_text())
    trajectory["steps"] = trajectory["steps"][:-1]
    path.write_text(json.dumps(trajectory, separators=(",", ":")))


class TestSink:
    def test_without_a_lake_it_writes_nothing(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        layout = LakeLayout(tmp_path / "lake")
        sink = DuckLakeSessionSink(layout)
        sink.sync_sessions(corpus_root=corpus.root, agent="claude-code", session_ids=SESSION_IDS)
        assert not layout.root.exists()
        assert sink.writes == 0

    def test_a_sync_replaces_exactly_the_named_sessions_rows(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        layout = _lake(tmp_path, corpus)
        before = _rows(
            _reader(layout, corpus.root),
            "SELECT session_id, count(*) FROM steps GROUP BY 1 ORDER BY 1",
        )
        _drop_last_step(corpus.root / "sessions" / SESSION_IDS[0])
        _drop_last_step(corpus.root / "sessions" / SESSION_IDS[1])
        # Only the first session is handed over: the lake must change for it
        # alone, and verify must then name the second.
        DuckLakeSessionSink(layout).sync_sessions(
            corpus_root=corpus.root, agent="claude-code", session_ids=[SESSION_IDS[0]]
        )
        after = dict(
            _rows(_reader(layout, corpus.root), "SELECT session_id, count(*) FROM steps GROUP BY 1")
        )
        assert after[SESSION_IDS[0]] == dict(before)[SESSION_IDS[0]] - 1
        assert after[SESSION_IDS[1]] == dict(before)[SESSION_IDS[1]]
        report = verify_lake(layout)
        assert {m.session_id for m in report.mismatches} == {SESSION_IDS[1]}
        DuckLakeSessionSink(layout).sync_sessions(
            corpus_root=corpus.root, agent="claude-code", session_ids=[SESSION_IDS[1]]
        )
        assert verify_lake(layout).clean

    def test_a_corpus_the_lake_does_not_hold_is_loaded_whole(self, tmp_path: Path) -> None:
        first = _corpus(tmp_path / "corpus-a")
        layout = _lake(tmp_path, first)
        second_root = tmp_path / "corpus-codex"
        write_codex_session(second_root)
        DuckLakeSessionSink(layout).sync_sessions(
            corpus_root=second_root, agent="codex", session_ids=["not-even-a-session"]
        )
        state = lake_status(layout)
        assert [(name, agent, n) for name, agent, _, n in state.corpora] == [
            ("corpus-a", "claude-code", 2),
            ("corpus-codex", "codex", 1),
        ]
        assert verify_lake(layout).clean
        both = _reader(layout, first.root, all_corpora=True)
        assert _rows(
            both, "SELECT corpus, agent, count(*) FROM sessions GROUP BY 1, 2 ORDER BY 1"
        ) == [
            ("corpus-a", "claude-code", 2),
            ("corpus-codex", "codex", 1),
        ]
        scoped = _reader(layout, first.root)
        assert _rows(scoped, "SELECT DISTINCT corpus FROM sessions") == [("corpus-a",)]
        assert _rows(scoped, "SELECT count(DISTINCT session_id) FROM steps") == [(2,)]

    def test_a_stale_schema_is_rebuilt_by_the_next_sync(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        layout = _lake(tmp_path, corpus)
        con = _writer(layout)
        con.execute(
            f"UPDATE {LAKE_ALIAS}.lake_info SET value = 'old' WHERE key = 'lake_schema_digest'"
        )
        _publish(con, layout)
        assert lake_status(layout).stale == ("lake_schema_digest",)
        refused = attach_lake_for_query(
            duckdb.connect(), layout, corpus_root=corpus.root, all_corpora=False
        )
        assert isinstance(refused, LakeUnavailable)
        assert "stale" in refused.reason
        DuckLakeSessionSink(layout).sync_sessions(
            corpus_root=corpus.root, agent="claude-code", session_ids=[SESSION_IDS[0]]
        )
        assert lake_status(layout).stale == ()
        assert verify_lake(layout).clean

    def test_two_roots_with_one_name_are_refused(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "a" / "corpus")
        layout = _lake(tmp_path, corpus)
        other = build_corpus(tmp_path / "b" / "corpus")
        with pytest.raises(LakeCorpusConflictError):
            DuckLakeSessionSink(layout).sync_sessions(
                corpus_root=other, agent="claude-code", session_ids=[SESSION_IDS[0]]
            )
        with pytest.raises(LakeCorpusConflictError):
            rebuild_lake(layout, [corpus, LakeCorpus(other, "claude-code")])

    def test_a_held_writer_lock_times_the_sink_out(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        layout = _lake(tmp_path, corpus)
        sink = DuckLakeSessionSink(layout, lock_timeout_seconds=0.3)
        with writer_lock(layout, timeout_seconds=1), pytest.raises(LakeLockTimeoutError):
            sink.sync_sessions(
                corpus_root=corpus.root, agent="claude-code", session_ids=[SESSION_IDS[0]]
            )


# ---------------------------------------------------------------------------
# Readers never wait on a writer
# ---------------------------------------------------------------------------


class TestConcurrentReader:
    def test_a_reader_during_an_open_write_sees_the_last_published_state(
        self, tmp_path: Path
    ) -> None:
        corpus = _corpus(tmp_path / "corpus")
        layout = _lake(tmp_path, corpus)
        writer = _writer(layout)
        writer.execute("BEGIN TRANSACTION")
        writer.execute(f"DELETE FROM {LAKE_ALIAS}.steps WHERE session_id = ?", [SESSION_IDS[0]])
        # The write is open on catalog.duckdb; a reader attaches the published
        # copy and sees every step.
        during = _reader(layout, corpus.root)
        assert _rows(during, "SELECT count(DISTINCT session_id) FROM steps") == [(2,)]
        writer.execute("COMMIT")
        _publish(writer, layout)
        # The reader that attached before the publish keeps its snapshot; a
        # new one sees the commit.
        assert _rows(during, "SELECT count(DISTINCT session_id) FROM steps") == [(2,)]
        after = _reader(layout, corpus.root)
        assert _rows(after, "SELECT count(DISTINCT session_id) FROM steps") == [(1,)]

    def test_the_reader_catalog_is_read_only_on_disk(self, tmp_path: Path) -> None:
        layout = _lake(tmp_path, _corpus(tmp_path / "corpus"))
        assert layout.reader_catalog_path.stat().st_mode & 0o222 == 0


# ---------------------------------------------------------------------------
# The query-side attach
# ---------------------------------------------------------------------------


class TestAttachForQuery:
    def test_no_lake_is_a_reason_not_an_error(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        result = attach_lake_for_query(
            duckdb.connect(),
            LakeLayout(tmp_path / "none"),
            corpus_root=corpus.root,
            all_corpora=False,
        )
        assert isinstance(result, LakeUnavailable)
        assert "no lake" in result.reason

    def test_a_corpus_the_lake_does_not_hold_falls_back(self, tmp_path: Path) -> None:
        layout = _lake(tmp_path, _corpus(tmp_path / "corpus-a"))
        other = build_corpus(tmp_path / "corpus-b")
        result = attach_lake_for_query(
            duckdb.connect(), layout, corpus_root=other, all_corpora=False
        )
        assert isinstance(result, LakeUnavailable)
        assert "no corpus 'corpus-b'" in result.reason

    def test_a_same_named_corpus_elsewhere_falls_back(self, tmp_path: Path) -> None:
        layout = _lake(tmp_path, _corpus(tmp_path / "a" / "corpus"))
        moved = build_corpus(tmp_path / "b" / "corpus")
        result = attach_lake_for_query(
            duckdb.connect(), layout, corpus_root=moved, all_corpora=False
        )
        assert isinstance(result, LakeUnavailable)

    def test_grants_name_every_live_data_and_delete_file(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        layout = _lake(tmp_path, corpus)
        _drop_last_step(corpus.root / "sessions" / SESSION_IDS[0])
        DuckLakeSessionSink(layout).sync_sessions(
            corpus_root=corpus.root, agent="claude-code", session_ids=[SESSION_IDS[0]]
        )
        con = duckdb.connect()
        reader = attach_lake_for_query(con, layout, corpus_root=corpus.root, all_corpora=False)
        assert isinstance(reader, LakeReader)
        on_disk = set(layout.data_dir.rglob("*.parquet"))
        assert reader.grants
        assert set(reader.grants) <= on_disk
        # The sync's DELETE left delete files, and a scan must open them too.
        assert any(p.name.endswith("-delete.parquet") for p in reader.grants)


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


class TestVerify:
    def test_a_fresh_lake_verifies_clean(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        report = verify_lake(_lake(tmp_path, corpus))
        assert report.clean
        assert report.corpora == (("corpus", 2),)

    def test_a_row_missing_from_the_lake_is_named(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        layout = _lake(tmp_path, corpus)
        con = _writer(layout)
        con.execute(
            f"DELETE FROM {LAKE_ALIAS}.tool_calls WHERE session_id = ? AND step_id = 2",
            [SESSION_IDS[0]],
        )
        _publish(con, layout)
        report = verify_lake(layout)
        assert not report.clean
        assert {(m.table, m.session_id) for m in report.mismatches} == {
            ("tool_calls", SESSION_IDS[0])
        }

    def test_a_changed_value_with_the_same_row_count_is_named(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        layout = _lake(tmp_path, corpus)
        edges = corpus.root / "sessions" / SESSION_IDS[1] / "edges.jsonl"
        lines = edges.read_text().splitlines()
        first = json.loads(lines[0])
        first["type"] = "tampered"
        edges.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n")
        report = verify_lake(layout)
        assert [
            (m.table, m.session_id, m.artifact_rows, m.lake_rows) for m in report.mismatches
        ] == [("edges", SESSION_IDS[1], len(lines), len(lines))]

    def test_a_stale_lake_does_not_verify(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        layout = _lake(tmp_path, corpus)
        con = _writer(layout)
        con.execute(
            f"UPDATE {LAKE_ALIAS}.lake_info SET value = '0' WHERE key = 'lake_schema_version'"
        )
        _publish(con, layout)
        assert verify_lake(layout).stale == ("lake_schema_version",)


# ---------------------------------------------------------------------------
# Rebuild and compact
# ---------------------------------------------------------------------------


class TestRebuildAndCompact:
    def test_rebuild_swaps_the_root_and_sweeps_dead_leftovers(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        layout = _lake(tmp_path, corpus)
        first_files = set(layout.data_dir.rglob("*.parquet"))
        dead = layout.root.with_name(f"{layout.root.name}.rebuild-999999999")
        dead.mkdir()
        rebuild_lake(layout, [corpus])
        assert not dead.exists()
        assert not (set(layout.data_dir.rglob("*.parquet")) & first_files)
        assert [p.name for p in layout.root.parent.iterdir() if p.name.startswith("lake.")] == [
            "lake.lock"
        ]
        assert verify_lake(layout).clean

    def test_compact_merges_small_files_and_expires_old_snapshots(self, tmp_path: Path) -> None:
        corpus = _corpus(tmp_path / "corpus")
        layout = _lake(tmp_path, corpus)
        sink = DuckLakeSessionSink(layout)
        for _ in range(4):
            for session_id in SESSION_IDS:
                sink.sync_sessions(
                    corpus_root=corpus.root, agent="claude-code", session_ids=[session_id]
                )
        grown = lake_status(layout)
        report = compact_lake(layout, expire_older_than_days=0)
        assert report.files_after < report.files_before == grown.data_files
        assert report.snapshots_after < report.snapshots_before
        assert verify_lake(layout).clean
        assert lake_status(layout).data_files == report.files_after


class _ThreadsAtMaintenance:
    """A writer connection that records DuckDB's thread count at every file merge or rewrite."""

    def __init__(self, con: duckdb.DuckDBPyConnection, seen: list[int]) -> None:
        self._con = con
        self._seen = seen

    def execute(self, sql: str, *args: Any) -> Any:
        if "merge_adjacent_files" in sql or "rewrite_data_files" in sql:
            row = self._con.execute("SELECT current_setting('threads')").fetchone()
            assert row is not None
            self._seen.append(int(row[0]))
        return self._con.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


def test_file_merges_and_rewrites_run_on_one_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merging ``tool_results`` at more than one thread outgrew the writer's memory cap."""
    corpus = _corpus(tmp_path / "corpus")
    layout = _lake(tmp_path, corpus)
    seen: list[int] = []
    real = lake_mod._writer_connection  # pyright: ignore[reportPrivateUsage]

    @contextlib.contextmanager
    def recording(memory_limit_bytes: int | None) -> Generator[Any]:
        with real(memory_limit_bytes) as con:
            yield _ThreadsAtMaintenance(con, seen)

    monkeypatch.setattr(lake_mod, "_writer_connection", recording)
    rebuild_lake(layout, [corpus])
    compact_lake(layout)
    # One merge in rebuild; merge, rewrite, merge in compact.
    assert seen == [1, 1, 1, 1]
    assert verify_lake(layout).clean
