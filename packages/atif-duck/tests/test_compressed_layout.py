# SPDX-License-Identifier: Apache-2.0

"""The compressed layout: ``<name>.zst`` artifacts, and the lake loaded from them.

materialize stores ``trajectory.json``, ``edges.jsonl`` and
``session_events.jsonl`` zstd-compressed and writes no per-session parquet;
the lake loads a session by staging parquet from its trajectory. Every view is
already run over a compressed corpus (``duck_fixtures.READ_PATHS``); this
module covers what only the layout has: how a stored file is found and sized,
a session holding both spellings, the logical path columns, and the staged
loader (the same rows as DuckDB's JSON reader, in bounded memory, cleaned up,
the same with a pool).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import pytest
import zstandard
from duck_fixtures import SESSION_IDS, build_corpus, compress_artifacts
from test_columnar import ALL_SESSION_IDS, _write_three_sessions, add_columnar

from atif_duck.domain.artifacts import COMPRESSED_ARTIFACTS, stored_names
from atif_duck.domain.lake import LAKE_ALIAS, LAKE_TABLES
from atif_duck.infrastructure import lake as lake_mod
from atif_duck.infrastructure.lake import LakeCorpus, LakeLayout, rebuild_lake, verify_lake
from atif_duck.infrastructure.registry import register, register_raw
from atif_duck.infrastructure.stored_artifacts import (
    decoded_size,
    iter_lines,
    read_bytes,
    stored_artifact,
)


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[Any]:
    return con.execute(sql).fetchall()


def _lake_rows(layout: LakeLayout) -> dict[str, list[tuple[Any, ...]]]:
    con = duckdb.connect()
    lake_mod.load_ducklake(con)
    lake_mod._attach(con, layout.reader_catalog_path, layout.data_dir, read_only=True)
    try:
        return {
            table.name: _rows(con, f"SELECT * FROM {LAKE_ALIAS}.{table.name} ORDER BY ALL")
            for table in LAKE_TABLES
        }
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Finding and sizing a stored artifact
# ---------------------------------------------------------------------------


class TestStoredArtifacts:
    def test_the_compressed_spelling_is_tried_first(self, tmp_path: Path) -> None:
        assert stored_names("trajectory.json") == ("trajectory.json.zst", "trajectory.json")
        assert stored_names("meta.json") == ("meta.json",)
        (tmp_path / "trajectory.json").write_text("{}")
        assert stored_artifact(tmp_path, "trajectory.json") == tmp_path / "trajectory.json"
        (tmp_path / "trajectory.json.zst").write_bytes(zstandard.ZstdCompressor().compress(b"{}"))
        assert stored_artifact(tmp_path, "trajectory.json") == tmp_path / "trajectory.json.zst"
        assert stored_artifact(tmp_path, "edges.jsonl") is None

    def test_decoded_size_reads_the_frame_header(self, tmp_path: Path) -> None:
        data = b"x" * 5_000_000
        path = tmp_path / "trajectory.json.zst"
        path.write_bytes(zstandard.ZstdCompressor(write_content_size=True).compress(data))
        assert path.stat().st_size < 10_000
        assert decoded_size(path) == len(data)
        assert read_bytes(path) == data

    def test_a_frame_without_a_content_size_is_measured(self, tmp_path: Path) -> None:
        data = b"line\n" * 100_000
        path = tmp_path / "edges.jsonl.zst"
        with (
            path.open("wb") as handle,
            zstandard.ZstdCompressor(write_content_size=False).stream_writer(handle) as writer,
        ):
            writer.write(data)
        assert zstandard.frame_content_size(path.read_bytes()[:18]) == -1
        assert decoded_size(path) == len(data)
        assert sum(1 for _ in iter_lines(path)) == 100_000

    def test_an_unreadable_file_sizes_as_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "edges.jsonl.zst"
        path.write_bytes(b"not a zstd frame at all")
        assert decoded_size(path) == 0
        assert decoded_size(tmp_path / "gone.jsonl") == 0


# ---------------------------------------------------------------------------
# The per-session path over the compressed layout
# ---------------------------------------------------------------------------


def _views(con: duckdb.DuckDBPyConnection) -> dict[str, list[Any]]:
    return {
        view: _rows(con, f"SELECT * FROM {view} ORDER BY ALL")
        for view in (
            "sessions",
            "steps",
            "tool_calls",
            "tool_results",
            "session_events",
            "messages",
        )
    }


class TestPerSessionPath:
    def test_mixed_and_mid_slim_corpora_read_as_the_plain_one(self, tmp_path: Path) -> None:
        root = build_corpus(tmp_path / "corpus")
        plain = duckdb.connect()
        register(plain, root, skip_vss=True)
        expected = _views(plain)

        # One session compressed, one plain.
        session_dir = root / "sessions" / SESSION_IDS[0]
        originals = {name: (session_dir / name).read_bytes() for name in COMPRESSED_ARTIFACTS}
        compressor = zstandard.ZstdCompressor(write_content_size=True)
        for name, data in originals.items():
            (session_dir / f"{name}.zst").write_bytes(compressor.compress(data))
            (session_dir / name).unlink()
        mixed = duckdb.connect()
        register(mixed, root, skip_vss=True)
        assert _views(mixed) == expected

        # Caught mid-slim: both spellings present. Nothing is read twice.
        for name, data in originals.items():
            (session_dir / name).write_bytes(data)
        both = duckdb.connect()
        register(both, root, skip_vss=True)
        assert _views(both) == expected

    def test_path_columns_name_the_logical_artifact(self, tmp_path: Path) -> None:
        root = build_corpus(tmp_path / "corpus")
        compress_artifacts(root)
        con = duckdb.connect()
        register_raw(con, root)
        sessions = root / "sessions"
        assert sorted(_rows(con, "SELECT trajectory_path FROM v_raw_trajectories")) == [
            (str(sessions / sid / "trajectory.json"),) for sid in sorted(SESSION_IDS)
        ]
        assert sorted({row[0] for row in _rows(con, "SELECT edges_path FROM v_raw_edges")}) == [
            str(sessions / sid / "edges.jsonl") for sid in sorted(SESSION_IDS)
        ]

    def test_the_json_bound_is_the_decompressed_size(self, tmp_path: Path) -> None:
        """A document far larger than its file must still parse.

        DuckDB's JSON reader refuses an object above ``maximum_object_size``,
        and the registry sizes that from the files. Sized from a compressed
        file's own few kilobytes, a 20 MB document would not parse.
        """
        root = build_corpus(tmp_path / "corpus")
        session_dir = root / "sessions" / SESSION_IDS[1]
        trajectory = json.loads((session_dir / "trajectory.json").read_text())
        trajectory["steps"][0]["message"] = "y" * 20_000_000
        (session_dir / "trajectory.json").write_text(json.dumps(trajectory, separators=(",", ":")))
        compress_artifacts(root)
        assert (session_dir / "trajectory.json.zst").stat().st_size < 1_000_000
        con = duckdb.connect()
        register(con, root, skip_vss=True)
        assert _rows(
            con,
            f"SELECT length(message) FROM steps WHERE session_id = '{SESSION_IDS[1]}' "
            "AND step_id = 1",
        ) == [(20_000_000,)]


# ---------------------------------------------------------------------------
# The lake loader: parquet staged from the trajectory
# ---------------------------------------------------------------------------


@contextmanager
def _no_staging(
    staging: lake_mod.Staging, corpus_root: Path
) -> Generator[Callable[[Sequence[str]], dict[str, Path]]]:
    """A stager that stages nothing, so the registry reads every session through DuckDB's JSON reader."""
    del staging, corpus_root
    yield lambda _ids: {}


class TestStagedLoad:
    def test_staged_rows_equal_the_json_readers_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _write_three_sessions(tmp_path / "corpus")
        compress_artifacts(root)
        corpus = LakeCorpus(root, "claude-code")
        staged = LakeLayout(tmp_path / "staged")
        rebuild_lake(staged, [corpus])
        with monkeypatch.context() as patch:
            patch.setattr(lake_mod, "_staged_columnar", _no_staging)
            json_layout = LakeLayout(tmp_path / "json")
            rebuild_lake(json_layout, [corpus])
        assert _lake_rows(staged) == _lake_rows(json_layout)
        assert verify_lake(staged).clean

    def test_the_loader_stages_every_session_without_parquet_and_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = build_corpus(tmp_path / "corpus")
        compress_artifacts(root)
        seen: list[tuple[str, ...]] = []
        original = register_raw

        def spy(*args: Any, **kwargs: Any) -> Any:
            sources = original(*args, **kwargs)
            seen.append(sources.staged_session_ids)
            return sources

        monkeypatch.setattr("atif_duck.infrastructure.registry.register_raw", spy)
        layout = LakeLayout(tmp_path / "lake")
        rebuild_lake(layout, [LakeCorpus(root, "claude-code")])
        assert seen == [tuple(sorted(SESSION_IDS))]
        leftovers = [p.name for p in layout.root.parent.iterdir() if ".load-" in p.name]
        assert leftovers == []

    def test_a_pool_stages_the_same_rows(self, tmp_path: Path) -> None:
        root = _write_three_sessions(tmp_path / "corpus")
        compress_artifacts(root)
        corpus = LakeCorpus(root, "claude-code")
        inline = LakeLayout(tmp_path / "inline")
        rebuild_lake(inline, [corpus], stage_workers=1)
        pooled = LakeLayout(tmp_path / "pooled")
        rebuild_lake(pooled, [corpus], stage_workers=2)
        assert _lake_rows(pooled) == _lake_rows(inline)

    def test_own_parquet_is_used_and_verify_can_ignore_it(self, tmp_path: Path) -> None:
        """An old-layout session loads from its parquet; slim's verify reads its trajectory instead."""
        root = _write_three_sessions(tmp_path / "corpus")
        add_columnar(root, ALL_SESSION_IDS)
        con = duckdb.connect()
        sources = register_raw(con, root, stage_columnar=lambda ids: pytest.fail(f"staged {ids}"))
        assert sources.columnar_session_ids == tuple(sorted(ALL_SESSION_IDS))
        layout = LakeLayout(tmp_path / "lake")
        rebuild_lake(layout, [LakeCorpus(root, "claude-code")])
        assert verify_lake(layout).clean
        assert verify_lake(layout, use_session_parquet=False).clean

    def test_verify_without_parquet_sees_a_trajectory_the_parquet_hides(
        self, tmp_path: Path
    ) -> None:
        """The check slim runs before it deletes parquet must read the trajectory, not the parquet."""
        root = _write_three_sessions(tmp_path / "corpus")
        add_columnar(root, ALL_SESSION_IDS)
        layout = LakeLayout(tmp_path / "lake")
        rebuild_lake(layout, [LakeCorpus(root, "claude-code")])
        path = root / "sessions" / SESSION_IDS[0] / "trajectory.json"
        trajectory = json.loads(path.read_text())
        trajectory["steps"] = trajectory["steps"][:-1]
        path.write_text(json.dumps(trajectory, separators=(",", ":")))
        assert verify_lake(layout).clean
        report = verify_lake(layout, use_session_parquet=False)
        assert {m.session_id for m in report.mismatches} == {SESSION_IDS[0]}

    def test_a_dead_processs_staging_dir_is_swept(self, tmp_path: Path) -> None:
        root = build_corpus(tmp_path / "corpus")
        layout = LakeLayout(tmp_path / "lake")
        dead = layout.root.with_name(f"{layout.root.name}.load-999999999")
        (dead / "x").mkdir(parents=True)
        rebuild_lake(layout, [LakeCorpus(root, "claude-code")])
        assert not dead.exists()
