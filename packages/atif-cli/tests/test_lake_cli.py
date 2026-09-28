# SPDX-License-Identifier: Apache-2.0

"""The lake through the CLI: materialize writes it, query reads it, lake * maintains it.

Everything goes through the REAL converter, the REAL columnar producer and the
REAL DuckLake sink into tmp dirs, because the composition is the point: atif-cli
is the one place atif-corpus's ``SessionSink`` port meets atif-duck's lake.
The autouse ``isolated_lake`` fixture points ``ATIF_SQL_LAKE_ROOT`` into each
test's tmp dir.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from cli_fixtures import (
    read_artifact_text,
    write_analytics_parquets,
    write_artifact_text,
    write_synthetic_session,
)
from loguru import logger

from atif_cli import app as app_mod
from atif_cli.app import materialize, query, status
from atif_cli.converter_adapter import RealConverter
from atif_cli.errors import EXIT_CODES
from atif_cli.lake import compact, rebuild, status as lake_status_cmd, verify
from atif_corpus.application.materialize import materialize as materialize_use_case
from atif_corpus.domain.source_layout import CLAUDE_CODE_LAYOUT
from atif_duck.domain.columnar import COLUMNAR_SCHEMA_VERSION, META_COLUMNAR_KEY
from atif_duck.domain.lake import LAKE_ALIAS, LAKE_TABLES
from atif_duck.infrastructure import lake as lake_mod
from atif_duck.infrastructure.columnar import ColumnarArtifactProducer
from atif_duck.infrastructure.lake import DuckLakeSessionSink, LakeLayout, rebuild_lake
from atif_duck.infrastructure.lake_settings import LakeSettings

SESSION_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SESSION_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"

pytestmark = pytest.mark.integration


@pytest.fixture
def source_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("ATIF_SQL_CORPUS_ROOT", "ATIF_SQL_LANCE_URI", "ATIF_SQL_EMBED_MODEL_ID"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "src-one" / "projects"
    write_synthetic_session(root, SESSION_A)
    write_synthetic_session(root, SESSION_B)
    return root


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    """Every WARNING-and-up loguru line (the autouse fixture drops the default sink)."""
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message).strip()), level="WARNING")
    yield lines
    logger.remove(sink)


def _json(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out)


def _materialize(
    source: Path, corpus: Path, capsys: pytest.CaptureFixture[str], **kw: Any
) -> dict[str, Any]:
    materialize(source_root=source, corpus_root=corpus, fmt="json", **kw)  # type: ignore[arg-type]
    return _json(capsys)


def _query(
    sql: str, corpus: Path, capsys: pytest.CaptureFixture[str], **kw: Any
) -> list[dict[str, Any]]:
    query(sql, corpus_root=corpus, fmt="json", **kw)  # type: ignore[arg-type]
    rows = _json(capsys)
    assert isinstance(rows, list)
    return rows


def _exit_code(fn: Any, *args: Any, **kwargs: Any) -> int:
    with pytest.raises(SystemExit) as info:
        fn(*args, **kwargs)
    return int(info.value.code or 0)


def _lake_root() -> Path:
    return LakeSettings().lake_root


def _append_turn(source_root: Path, session_id: str) -> None:
    """Add one user record to a session and backdate it so it is quiescent again."""
    main = source_root / "-tmp-proj" / f"{session_id}.jsonl"
    record = {
        "type": "user",
        "uuid": f"{session_id}-u9",
        "parentUuid": f"{session_id}-a2",
        "sessionId": session_id,
        "timestamp": "2026-08-22T00:00:09Z",
        "isSidechain": False,
        "message": {"role": "user", "content": "one more thing"},
    }
    with main.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    # Quiescent at the default 300 s, and distinct from every earlier mtime.
    stale = time.time_ns() - 3_000 * 1_000_000_000
    os.utime(main, ns=(stale, stale))


@pytest.fixture
def lake_corpus(source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """A materialized corpus with the lake rebuilt over it."""
    corpus = tmp_path / "corpus-one"
    _materialize(source_root, corpus, capsys)
    rebuild(corpus_root=[corpus], fmt="json")  # type: ignore[arg-type]
    capsys.readouterr()
    return corpus


@pytest.fixture(scope="module")
def built_lake(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    """``(source, corpus, lake)`` materialized and rebuilt ONCE for the module.

    What :func:`lake_corpus` builds per test, for the tests that leave the
    corpus alone. Nothing may write to these paths: :func:`shared_lake_corpus`
    hands each test a copy of the lake and the corpus itself, read-only.
    """
    base = tmp_path_factory.mktemp("built-lake")
    source = base / "src-one" / "projects"
    write_synthetic_session(source, SESSION_A)
    write_synthetic_session(source, SESSION_B)
    corpus = base / "corpus-one"
    lake_root = base / "lake"
    with pytest.MonkeyPatch.context() as patch:
        for var in ("ATIF_SQL_CORPUS_ROOT", "ATIF_SQL_LANCE_URI", "ATIF_SQL_EMBED_MODEL_ID"):
            patch.delenv(var, raising=False)
        patch.setenv("ATIF_SQL_LAKE_ROOT", str(lake_root))
        patch.setenv("ATIF_SQL_CORPUS_BASE", str(base / "corpus-base"))
        with contextlib.redirect_stdout(io.StringIO()):
            materialize(source_root=source, corpus_root=corpus, fmt="json")  # type: ignore[arg-type]
            rebuild(corpus_root=[corpus], fmt="json")  # type: ignore[arg-type]
    return source, corpus, lake_root


@pytest.fixture
def shared_lake_corpus(built_lake: tuple[Path, Path, Path]) -> Path:
    """The module's corpus, with a private copy of its lake at this test's lake root.

    For a test that may write the lake (or try to) but never the corpus: the
    copy is the test's own, the corpus is shared. A copied lake still serves
    the corpus because the catalog names the corpus by its root, which is the
    same path, and its data path is overridden on every attach.
    """
    _, corpus, lake_root = built_lake
    shutil.copytree(lake_root, _lake_root())
    return corpus


def test_the_suite_never_sees_the_default_lake(tmp_path: Path) -> None:
    assert _lake_root().is_relative_to(tmp_path)
    assert LakeSettings().corpus_base.is_relative_to(tmp_path)


class TestMaterializeKeepsTheLakeCurrent:
    def test_without_a_lake_materialize_writes_none(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = _materialize(source_root, tmp_path / "corpus", capsys)
        assert report["materialized"] == 2
        assert report["lake_pending"] == 0
        assert not _lake_root().exists()

    def test_an_incremental_pass_replaces_the_changed_sessions_rows(
        self,
        source_root: Path,
        lake_corpus: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        count = "SELECT count(*) AS n FROM messages WHERE session_id = ?"
        before = _query(count.replace("?", f"'{SESSION_A}'"), lake_corpus, capsys)
        _append_turn(source_root, SESSION_A)
        write_synthetic_session(source_root, SESSION_C)
        report = _materialize(source_root, lake_corpus, capsys)
        assert report["materialized"] == 2
        assert report["lake_synced"] == 2
        assert report["lake_pending"] == 0
        after = _query(count.replace("?", f"'{SESSION_A}'"), lake_corpus, capsys)
        assert after[0]["n"] == before[0]["n"] + 1
        sessions = _query("SELECT session_id FROM sessions ORDER BY 1", lake_corpus, capsys)
        assert [row["session_id"] for row in sessions] == [SESSION_A, SESSION_B, SESSION_C]
        verify(fmt="json")  # type: ignore[arg-type]
        assert _json(capsys)["clean"] is True

    def test_a_lake_write_that_fails_is_retried_by_the_next_pass(
        self,
        source_root: Path,
        lake_corpus: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _append_turn(source_root, SESSION_B)
        monkeypatch.setenv("ATIF_SQL_LAKE_LOCK_TIMEOUT_SECONDS", "0.2")
        with lake_mod.writer_lock(LakeLayout(_lake_root()), timeout_seconds=1):
            report = _materialize(source_root, lake_corpus, capsys)
        assert report["materialized"] == 1
        assert report["lake_pending_session_ids"] == [SESSION_B]
        assert "LakeLockTimeoutError" in report["lake_error"]
        assert _exit_code(verify, fmt="json") == EXIT_CODES["lake_mismatch"]
        capsys.readouterr()
        # Nothing moved in the source; the pending session is synced anyway.
        retry = _materialize(source_root, lake_corpus, capsys)
        assert retry["materialized"] == 0
        assert retry["lake_synced"] == 1
        assert retry["lake_pending"] == 0
        verify(fmt="json")  # type: ignore[arg-type]
        assert _json(capsys)["clean"] is True

    def test_no_lake_flag_leaves_the_lake_alone(
        self,
        source_root: Path,
        lake_corpus: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _append_turn(source_root, SESSION_A)
        report = _materialize(source_root, lake_corpus, capsys, lake=False)
        assert report["materialized"] == 1
        assert report["lake_synced"] == 0
        assert _exit_code(verify, fmt="json") == EXIT_CODES["lake_mismatch"]
        mismatch = _json(capsys)
        assert {m["session_id"] for m in mismatch["mismatches"]} == {SESSION_A}


class TestPoolAndSingleWorkerWriteTheSameLake:
    def test_lake_contents_are_identical(self, source_root: Path, tmp_path: Path) -> None:
        write_synthetic_session(source_root, SESSION_C)
        corpus = tmp_path / "corpus-shared"

        def run(workers: int, lake_root: Path) -> dict[str, list[Any]]:
            # Same corpus path both times (paths are part of the rows), so the
            # corpus is wiped between the two runs and each run gets a fresh
            # lake holding the corpus before its pass writes.
            import shutil

            shutil.rmtree(corpus, ignore_errors=True)
            layout = LakeLayout(lake_root)
            rebuild_lake(layout, [lake_mod.LakeCorpus(root=corpus, agent="claude-code")])
            report = materialize_use_case(
                source_root=source_root,
                corpus_root=corpus,
                converter=RealConverter(agent=CLAUDE_CODE_LAYOUT.agent),
                source_layout=CLAUDE_CODE_LAYOUT,
                materialized_at="2026-09-01T00:00:00+00:00",
                harbor_version="0.23.0",
                converter_version="test",
                converter_schema=1,
                expected_meta={META_COLUMNAR_KEY: COLUMNAR_SCHEMA_VERSION},
                workers=workers,
                artifact_producer=ColumnarArtifactProducer(),
                session_sink=DuckLakeSessionSink(layout),
                sink_batch_size=2,
            )
            assert report.materialized_count == 3
            assert report.workers == workers
            assert report.sink_synced_count == 3
            con = duckdb.connect()
            lake_mod.load_ducklake(con)
            lake_mod._attach(con, layout.reader_catalog_path, layout.data_dir, read_only=True)
            try:
                return {
                    t.name: sorted(
                        map(repr, con.execute(f"SELECT * FROM {LAKE_ALIAS}.{t.name}").fetchall())
                    )
                    for t in LAKE_TABLES
                }
            finally:
                con.close()

        single = run(1, tmp_path / "lake-single")
        pooled = run(3, tmp_path / "lake-pooled")
        assert all(single[name] for name in ("sessions", "steps", "edges", "session_meta"))
        assert pooled == single


class TestQueryReadsTheLake:
    def test_query_takes_the_lake_path_and_grants_only_its_files(
        self, shared_lake_corpus: Path, capsys: pytest.CaptureFixture[str], warnings: list[str]
    ) -> None:
        rows = _query(
            "SELECT current_setting('allowed_paths') AS paths, "
            "current_setting('allowed_directories') AS dirs",
            shared_lake_corpus,
            capsys,
        )
        paths, dirs = rows[0]["paths"], rows[0]["dirs"]
        data_dir = str(_lake_root() / "data")
        assert paths
        # DuckDB itself grants the attached reader catalog and its WAL files
        # when external access is disabled; the READ_ONLY attach never writes
        # them, and COPY is refused before execution.
        catalog = str(_lake_root() / "catalog.reader.duckdb")
        assert all(p.startswith((data_dir, catalog)) for p in paths), paths
        assert not any(d.startswith(str(_lake_root())) for d in dirs)
        assert not [line for line in warnings if "per-session artifacts" in line]
        same = _query("SELECT count(*) AS n FROM steps", shared_lake_corpus, capsys)
        assert same == _query(
            "SELECT count(*) AS n FROM steps", shared_lake_corpus, capsys, lake=False
        )

    def test_every_panel_query_agrees_between_the_two_paths(
        self, shared_lake_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from test_storage_layout_cli import PANEL_QUERIES

        for sql in PANEL_QUERIES:
            assert _query(sql, shared_lake_corpus, capsys) == _query(
                sql, shared_lake_corpus, capsys, lake=False
            )

    def test_without_a_lake_query_warns_once_and_reads_the_artifacts(
        self,
        source_root: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        warnings: list[str],
    ) -> None:
        corpus = tmp_path / "corpus"
        _materialize(source_root, corpus, capsys)
        rows = _query("SELECT count(*) AS n FROM sessions", corpus, capsys)
        assert rows == [{"n": 2}]
        fallbacks = [line for line in warnings if "reading the per-session artifacts" in line]
        assert len(fallbacks) == 1
        assert "no lake" in fallbacks[0]
        warnings.clear()
        _query("SELECT count(*) AS n FROM sessions", corpus, capsys, lake=False)
        assert not [line for line in warnings if "per-session artifacts" in line]

    def test_a_stale_lake_falls_back(
        self, shared_lake_corpus: Path, capsys: pytest.CaptureFixture[str], warnings: list[str]
    ) -> None:
        layout = LakeLayout(_lake_root())
        con = duckdb.connect()
        lake_mod.load_ducklake(con)
        lake_mod._attach(con, layout.catalog_path, layout.data_dir, read_only=False)
        con.execute(
            f"UPDATE {LAKE_ALIAS}.lake_info SET value = 'x' WHERE key = 'lake_schema_digest'"
        )
        con.execute(f"DETACH {LAKE_ALIAS}")
        con.close()
        lake_mod._publish_reader_catalog(layout)
        assert _query("SELECT count(*) AS n FROM sessions", shared_lake_corpus, capsys) == [
            {"n": 2}
        ]
        assert any("schema is stale" in line for line in warnings)

    def test_all_corpora_spans_every_corpus_and_needs_the_lake(
        self,
        source_root: Path,
        shared_lake_corpus: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        second_source = tmp_path / "src-two" / "projects"
        write_synthetic_session(second_source, SESSION_C)
        second = tmp_path / "corpus-two"
        report = _materialize(second_source, second, capsys)
        # The lake did not hold this corpus: the pass loaded it whole.
        assert report["lake_synced"] == 1
        rows = _query(
            "SELECT corpus, count(*) AS n FROM sessions GROUP BY 1 ORDER BY 1",
            shared_lake_corpus,
            capsys,
            all_corpora=True,
        )
        assert rows == [{"corpus": "corpus-one", "n": 2}, {"corpus": "corpus-two", "n": 1}]
        scoped = _query("SELECT DISTINCT corpus FROM sessions", second, capsys)
        assert scoped == [{"corpus": "corpus-two"}]
        assert (
            _exit_code(
                query, "SELECT 1", corpus_root=second, all_corpora=True, lake=False, fmt="json"
            )
            == EXIT_CODES["invalid_input"]
        )
        capsys.readouterr()
        rebuilt = LakeLayout(_lake_root())
        for path in (rebuilt.catalog_path, rebuilt.reader_catalog_path):
            path.chmod(0o644)
            path.unlink()
        assert (
            _exit_code(query, "SELECT 1", corpus_root=second, all_corpora=True, fmt="json")
            == EXIT_CODES["lake_unavailable"]
        )


class TestEveryCorpusCoversTheAnalytics:
    def test_all_corpora_reads_and_grants_every_corpus_analytics(
        self, lake_corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        second_source = tmp_path / "src-two" / "projects"
        write_synthetic_session(second_source, SESSION_C)
        second = tmp_path / "corpus-two"
        _materialize(second_source, second, capsys)
        write_analytics_parquets(lake_corpus, SESSION_A)
        write_analytics_parquets(second, SESSION_C)
        sql = (
            "SELECT s.corpus, count(*) AS n FROM user_friction f "
            "JOIN sessions s USING (session_id) GROUP BY 1 ORDER BY 1"
        )
        assert _query(sql, lake_corpus, capsys, all_corpora=True) == [
            {"corpus": "corpus-one", "n": 1},
            {"corpus": "corpus-two", "n": 1},
        ]
        assert _query(sql, lake_corpus, capsys) == [{"corpus": "corpus-one", "n": 1}]


class TestTheSandboxHoldsOnTheLakePath:
    @pytest.mark.parametrize(
        ("sql", "kind"),
        [
            ("SELECT * FROM read_text('/etc/hostname')", "runtime_error"),
            ("ATTACH '/tmp/other.duckdb' AS other", "sandbox_refused"),
            ("DETACH atif_lake", "sandbox_refused"),
            (f"DELETE FROM {LAKE_ALIAS}.steps", "runtime_error"),
            (f"CREATE TABLE {LAKE_ALIAS}.planted (a INT)", "runtime_error"),
            ("SELECT * FROM sqlite_scan('/tmp/x.db', 't')", "catalog_error"),
            ("INSTALL sqlite", "sandbox_refused"),
            ("LOAD sqlite", "sandbox_refused"),
            ("SET allowed_directories=['/']", "runtime_error"),
        ],
    )
    def test_refused(
        self, shared_lake_corpus: Path, capsys: pytest.CaptureFixture[str], sql: str, kind: str
    ) -> None:
        code = _exit_code(query, sql, corpus_root=shared_lake_corpus, fmt="json")
        assert code == EXIT_CODES[kind]
        assert json.loads(capsys.readouterr().err)["error"]["kind"] == kind

    def test_copy_over_a_granted_lake_file_is_refused(
        self, shared_lake_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = next((_lake_root() / "data").rglob("*.parquet"))
        before = target.read_bytes()
        sql = f"COPY (SELECT 1 AS a) TO '{target}' (FORMAT PARQUET, USE_TMP_FILE false)"
        assert (
            _exit_code(query, sql, corpus_root=shared_lake_corpus, fmt="json")
            == EXIT_CODES["sandbox_refused"]
        )
        assert target.read_bytes() == before

    def test_maintenance_functions_cannot_touch_a_file(
        self,
        source_root: Path,
        lake_corpus: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Leave files scheduled for deletion (merge + expire, inside the grace
        # window), then let caller SQL try every DuckLake maintenance call.
        _append_turn(source_root, SESSION_A)
        _materialize(source_root, lake_corpus, capsys)
        compact(expire_older_than_days=0, fmt="json")  # type: ignore[arg-type]
        capsys.readouterr()
        data = _lake_root() / "data"
        before = sorted(p.relative_to(data) for p in data.rglob("*"))
        for sql in (
            f"SELECT * FROM ducklake_cleanup_old_files('{LAKE_ALIAS}', cleanup_all => true)",
            f"SELECT * FROM ducklake_delete_orphaned_files('{LAKE_ALIAS}', cleanup_all => true)",
            f"CALL ducklake_rewrite_data_files('{LAKE_ALIAS}', delete_threshold => 0)",
            f"CALL ducklake_merge_adjacent_files('{LAKE_ALIAS}')",
            f"CALL ducklake_expire_snapshots('{LAKE_ALIAS}', older_than => now())",
        ):
            # Some refuse outright, some are no-ops on a READ_ONLY attach;
            # either way the files on disk are the test.
            with contextlib.suppress(SystemExit):
                query(sql, corpus_root=lake_corpus, fmt="json")  # type: ignore[arg-type]
            capsys.readouterr()
        assert sorted(p.relative_to(data) for p in data.rglob("*")) == before
        verify(fmt="json")  # type: ignore[arg-type]
        assert _json(capsys)["clean"] is True


class TestLakeCommands:
    def test_status_folds_the_lake_in(
        self, source_root: Path, lake_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status(source_root=source_root, corpus_root=lake_corpus, fmt="json")  # type: ignore[arg-type]
        block = _json(capsys)["lake"]
        assert block["state"] == "ready"
        assert block["sessions"] == 2
        assert block["pending"] == 0
        assert block["snapshots"] >= 1
        assert block["data_files"] >= len(LAKE_TABLES)
        lake_status_cmd(fmt="json")  # type: ignore[arg-type]
        state = _json(capsys)
        assert state["schema_current"] is True
        assert [c["corpus"] for c in state["corpora"]] == ["corpus-one"]
        assert state["last_write"] is not None

    def test_rebuild_finds_corpora_under_the_base_when_none_is_named(
        self,
        source_root: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        base = LakeSettings().corpus_base
        _materialize(source_root, base / "corpus-x", capsys)
        rebuild(fmt="json")  # type: ignore[arg-type]
        report = _json(capsys)
        assert [(c["corpus"], c["sessions"]) for c in report["corpora"]] == [("corpus-x", 2)]

    def test_rebuild_with_nothing_to_load_exits_64(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _exit_code(rebuild, fmt="json") == EXIT_CODES["invalid_input"]

    def test_verify_exits_65_on_a_tampered_artifact_and_78_without_a_lake(
        self, lake_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        edges = lake_corpus / "sessions" / SESSION_B / "edges.jsonl.zst"
        text = read_artifact_text(edges)
        write_artifact_text(edges, text + text.splitlines()[0] + "\n")
        assert _exit_code(verify, fmt="json") == EXIT_CODES["lake_mismatch"]
        payload = _json(capsys)
        assert payload["mismatched_sessions"] == 1
        assert payload["mismatches"][0]["table"] == "edges"
        assert (
            _exit_code(verify, lake_root=_lake_root().parent / "no-lake", fmt="json")
            == EXIT_CODES["lake_unavailable"]
        )

    def test_compact_reports_before_and_after(
        self, source_root: Path, lake_corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Each pass adds a session, so each leaves small files of its own
        # (re-syncing one session would not: DuckLake drops a file once every
        # row in it is deleted, so there would be nothing to merge).
        for index in range(3):
            write_synthetic_session(source_root, f"dddddddd-dddd-dddd-dddd-00000000000{index}")
            _materialize(source_root, lake_corpus, capsys)
        compact(expire_older_than_days=0, fmt="json")  # type: ignore[arg-type]
        report = _json(capsys)
        assert report["data_files_after"] < report["data_files_before"]
        assert report["snapshots_after"] < report["snapshots_before"]
        verify(fmt="json")  # type: ignore[arg-type]
        assert _json(capsys)["clean"] is True


# ---------------------------------------------------------------------------
# Query memory sizing from the cgroup
# ---------------------------------------------------------------------------


GIB = 1024**3


@pytest.fixture
def fake_cgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake /proc/self/cgroup, /proc/meminfo and cgroup v2 tree (64 GiB free on the host)."""
    root = tmp_path / "cgroup"
    (root / "user.slice" / "run-q.scope").mkdir(parents=True)
    proc = tmp_path / "proc-self-cgroup"
    proc.write_text("0::/user.slice/run-q.scope\n")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal: {128 * GIB // 1024} kB\nMemAvailable: {64 * GIB // 1024} kB\n")
    monkeypatch.setattr(app_mod, "_PROC_SELF_CGROUP", proc)
    monkeypatch.setattr(app_mod, "_CGROUP_ROOT", root)
    monkeypatch.setattr(app_mod, "_PROC_MEMINFO", meminfo)

    def sysconf(name: str) -> int:
        return {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 128 * GIB // 4096}[name]

    monkeypatch.setattr(app_mod.os, "sysconf", sysconf)
    return root


def _write_level(level: Path, maximum: str, current: int) -> None:
    (level / "memory.max").write_text(f"{maximum}\n")
    (level / "memory.current").write_text(f"{current}\n")


class TestCgroupSizing:
    def test_an_unlimited_cgroup_sizes_from_the_host(self, fake_cgroup: Path) -> None:
        _write_level(fake_cgroup / "user.slice" / "run-q.scope", "max", 0)
        assert app_mod._host_memory() == (128 * GIB, 64 * GIB)
        assert app_mod._query_memory_limit_bytes() == int(64 * GIB * 0.8)

    def test_a_cgroup_below_the_host_caps_the_query(self, fake_cgroup: Path) -> None:
        _write_level(fake_cgroup / "user.slice" / "run-q.scope", str(4 * GIB), GIB)
        assert app_mod._host_memory() == (4 * GIB, 3 * GIB)
        # 80% of what the cgroup has left, where the host alone said 51 GiB.
        assert app_mod._query_memory_limit_bytes() == int(3 * GIB * 0.8)

    def test_the_tightest_ancestor_wins(self, fake_cgroup: Path) -> None:
        _write_level(fake_cgroup / "user.slice" / "run-q.scope", "max", GIB)
        _write_level(fake_cgroup / "user.slice", str(8 * GIB), 6 * GIB)
        assert app_mod._host_memory() == (8 * GIB, 2 * GIB)

    def test_reclaimable_page_cache_counts_as_room(self, fake_cgroup: Path) -> None:
        # A slice at 7 GiB of an 8 GiB cap, 5 GiB of it page cache: 6 GiB of room,
        # not the 1 GiB memory.current alone implies.
        level = fake_cgroup / "user.slice"
        _write_level(level, str(8 * GIB), 7 * GIB)
        (level / "memory.stat").write_text(
            f"anon {2 * GIB}\nfile {5 * GIB}\nactive_file {3 * GIB}\ninactive_file {2 * GIB}\n"
        )
        assert app_mod._host_memory() == (8 * GIB, 6 * GIB)

    def test_a_missing_memory_stat_keeps_current_as_usage(self, fake_cgroup: Path) -> None:
        _write_level(fake_cgroup / "user.slice", str(8 * GIB), 7 * GIB)
        assert app_mod._host_memory() == (8 * GIB, GIB)

    def test_no_cgroup_v2_line_means_no_cap(self, fake_cgroup: Path, tmp_path: Path) -> None:
        (tmp_path / "proc-self-cgroup").write_text("12:memory:/legacy\n")
        assert app_mod._host_memory() == (128 * GIB, 64 * GIB)
