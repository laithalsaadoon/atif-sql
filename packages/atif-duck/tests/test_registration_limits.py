# SPDX-License-Identifier: Apache-2.0

"""Registration fits the host, and never reaches the network.

Two findings of the MicroVM review, pinned at the registry layer:

* The eager ``read_json`` readers reserve about twice their
  ``maximum_object_size`` per thread. At the old 1 GiB constant that was
  2 GiB a thread, so ``SET threads=4; SET memory_limit='6GB'`` (a 4 vCPU,
  8 GiB guest) failed to register a 131 MB corpus with an out-of-memory
  error, and so did 16 threads under 25 GB. The bound is now sized from the
  largest file a one-document reader will open, and from the longest line a
  newline-delimited reader will meet.
* ``register_vss`` used to ``INSTALL lance`` on every registration, a 242 MB
  download from the extension repository. It now LOADs the extension only
  when it is already installed and binds the vector surface empty otherwise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
import zstandard
from duck_fixtures import SESSION_IDS
from loguru import logger
from test_vss import _write_lance

from atif_duck.infrastructure import registry as registry_mod
from atif_duck.infrastructure.registry import (
    _OBJECT_SIZE_CAP,
    _OBJECT_SIZE_FLOOR,
    _line_size_bound,
    _object_size_bound,
    install_lance_extension,
    lance_extension_installed,
    load_lance_extension,
    register,
    register_vss,
)

MIB = 1024**2


class TestRegistrationUnderHostLimits:
    """The review's reproduction: limits set before ``register`` over the fixture corpus."""

    @pytest.mark.parametrize(("threads", "memory"), [(4, "6GB"), (16, "8GB"), (16, "25GB")])
    def test_register_succeeds_under_limits_that_used_to_oom(
        self, corpus_root: Path, threads: int, memory: str
    ) -> None:
        con = duckdb.connect()
        try:
            con.execute(f"SET threads={threads}; SET memory_limit='{memory}'")
            sources = register(con, corpus_root)
            assert set(sources.json_session_ids) == set(SESSION_IDS)
            assert con.execute("SELECT count(*) FROM sessions").fetchone() == (2,)
            messages = con.execute("SELECT count(*) FROM messages").fetchone()
            assert messages is not None
            assert int(messages[0]) > 0
        finally:
            con.close()


class TestObjectSizeBound:
    def test_no_files_gives_duckdbs_default(self) -> None:
        assert _object_size_bound([]) == _OBJECT_SIZE_FLOOR == 16 * MIB

    def test_small_files_stay_at_the_floor(self, tmp_path: Path) -> None:
        small = tmp_path / "edges.jsonl"
        small.write_bytes(b"x" * 1000)
        assert _object_size_bound([small]) == _OBJECT_SIZE_FLOOR

    def test_a_large_file_gets_a_quarter_plus_one_mib_of_headroom(self, tmp_path: Path) -> None:
        big = tmp_path / "trajectory.json"
        with big.open("wb") as handle:
            handle.truncate(200 * MIB)  # sparse: no bytes written
        assert _object_size_bound([big]) == 200 * MIB + 50 * MIB + MIB

    def test_the_largest_file_wins(self, tmp_path: Path) -> None:
        paths = []
        for i, size in enumerate((40 * MIB, 120 * MIB, 80 * MIB)):
            path = tmp_path / f"{i}.json"
            with path.open("wb") as handle:
                handle.truncate(size)
            paths.append(path)
        assert _object_size_bound(paths) == 120 * MIB + 30 * MIB + MIB

    def test_capped_at_one_gib(self, tmp_path: Path) -> None:
        huge = tmp_path / "huge.json"
        with huge.open("wb") as handle:
            handle.truncate(2 * 1024**3)
        assert _object_size_bound([huge]) == _OBJECT_SIZE_CAP == 1024**3

    def test_a_vanished_path_counts_as_zero(self, tmp_path: Path) -> None:
        assert _object_size_bound([tmp_path / "gone.json"]) == _OBJECT_SIZE_FLOOR

    def test_the_bound_reaches_every_eager_reader(
        self, corpus_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The DDL carries the sized bound, not the old constant: by file, or by line."""
        seen: dict[str, list[int]] = {"_object_size_bound": [], "_line_size_bound": []}
        for name, calls in seen.items():
            real = getattr(registry_mod, name)

            def spy(paths: Any, real: Any = real, calls: list[int] = calls) -> int:
                bound = real(paths)
                calls.append(bound)
                return bound

            monkeypatch.setattr(registry_mod, name, spy)
        con = duckdb.connect()
        try:
            register(con, corpus_root)
        finally:
            con.close()
        assert seen == {
            # trajectory.json: one document per file
            "_object_size_bound": [_OBJECT_SIZE_FLOOR],
            # session_events.jsonl and edges.jsonl: one object per line
            "_line_size_bound": [_OBJECT_SIZE_FLOOR, _OBJECT_SIZE_FLOOR],
        }


def _short_lines(path: Path, total_bytes: int, *, line_bytes: int = 999) -> Path:
    """``total_bytes`` (rounded up to whole lines) of ``line_bytes``-byte lines."""
    line = b"x" * line_bytes + b"\n"
    path.write_bytes(line * -(-total_bytes // len(line)))
    return path


class TestLineSizeBound:
    """A newline-delimited reader's objects are its lines: the bound follows the longest one."""

    def test_a_large_file_of_short_lines_stays_at_the_floor(self, tmp_path: Path) -> None:
        path = _short_lines(tmp_path / "edges.jsonl", 40 * MIB)
        assert _object_size_bound([path]) == path.stat().st_size * 5 // 4 + MIB
        assert _line_size_bound([path]) == _OBJECT_SIZE_FLOOR

    def test_a_long_line_gets_a_quarter_plus_one_mib_of_headroom(self, tmp_path: Path) -> None:
        path = tmp_path / "edges.jsonl"
        path.write_bytes(b"{}\n" + b"y" * (20 * MIB) + b"\n{}\n")
        assert _line_size_bound([path]) == 20 * MIB + 5 * MIB + MIB

    def test_the_longest_line_of_any_file_wins(self, tmp_path: Path) -> None:
        short = _short_lines(tmp_path / "a.jsonl", 60 * MIB)
        long = tmp_path / "b.jsonl"
        long.write_bytes(b"z" * (24 * MIB) + b"\n")
        assert _line_size_bound([short, long]) == 24 * MIB + 6 * MIB + MIB

    def test_a_compressed_file_is_measured_decompressed(self, tmp_path: Path) -> None:
        path = tmp_path / "edges.jsonl.zst"
        path.write_bytes(zstandard.ZstdCompressor().compress(b"{}\n" + b"y" * (20 * MIB) + b"\n"))
        assert path.stat().st_size < MIB
        assert _line_size_bound([path]) == 20 * MIB + 5 * MIB + MIB

    def test_a_file_the_floor_already_covers_is_not_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        read: list[Path] = []
        real = registry_mod.longest_line

        def spy(path: Path) -> int:
            read.append(path)
            return real(path)

        monkeypatch.setattr(registry_mod, "longest_line", spy)
        small = _short_lines(tmp_path / "small.jsonl", 8 * MIB)
        large = _short_lines(tmp_path / "large.jsonl", 13 * MIB)
        assert _line_size_bound([small, large]) == _OBJECT_SIZE_FLOOR
        assert read == [large]

    def test_capped_at_one_gib(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        huge = tmp_path / "huge.jsonl"
        with huge.open("wb") as handle:
            handle.truncate(3 * 1024**3)  # sparse: no bytes written

        def two_gib(_path: Path) -> int:
            return 2 * 1024**3

        monkeypatch.setattr(registry_mod, "longest_line", two_gib)
        assert _line_size_bound([huge]) == _OBJECT_SIZE_CAP

    def test_no_files_and_vanished_paths_give_duckdbs_default(self, tmp_path: Path) -> None:
        assert _line_size_bound([]) == _OBJECT_SIZE_FLOOR
        assert _line_size_bound([tmp_path / "gone.jsonl"]) == _OBJECT_SIZE_FLOOR


class _Recording:
    def __init__(self, con: duckdb.DuckDBPyConnection, statements: list[str]) -> None:
        self._con = con
        self._statements = statements

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self._statements.append(sql)
        return self._con.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


def _connection_without_extensions(tmp_path: Path) -> tuple[Any, Path, list[str]]:
    ext_dir = tmp_path / "no-extensions"
    ext_dir.mkdir()
    statements: list[str] = []
    con = duckdb.connect()
    con.execute(f"SET extension_directory='{ext_dir}'")
    return _Recording(con, statements), ext_dir, statements


def _installs(statements: list[str]) -> list[str]:
    return [s for s in statements if s.lstrip().upper().startswith("INSTALL")]


def _loads(statements: list[str]) -> list[str]:
    return [s for s in statements if s.lstrip().upper().startswith("LOAD")]


class TestVssNeverInstalls:
    def test_store_present_extension_absent_binds_empty_and_installs_nothing(
        self, tmp_path: Path
    ) -> None:
        store = _write_lance(tmp_path / "store")
        con, ext_dir, statements = _connection_without_extensions(tmp_path)
        warnings: list[str] = []
        sink_id = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
        try:
            bound = register_vss(con, lance_uri=store, expected_model="test-embedder:1")
        finally:
            logger.remove(sink_id)
            con.close()
        assert bound is False
        assert _installs(statements) == []
        assert _loads(statements) == []
        assert list(ext_dir.rglob("*")) == []
        assert any("--install-extension" in w for w in warnings)

    def test_autoinstall_left_on_still_downloads_nothing(self, tmp_path: Path) -> None:
        """The installed check runs before LOAD, so DuckDB's default autoinstall never triggers."""
        store = _write_lance(tmp_path / "store")
        con, ext_dir, _ = _connection_without_extensions(tmp_path)
        assert con.execute("SELECT current_setting('autoinstall_known_extensions')").fetchone() == (
            True,
        )
        try:
            register_vss(con, lance_uri=store)
            assert con.execute("SELECT count(*) FROM message_embeddings").fetchone() == (0,)
        finally:
            con.close()
        assert list(ext_dir.rglob("*")) == []

    def test_no_store_means_the_extension_is_not_even_loaded(self, tmp_path: Path) -> None:
        con, _, statements = _connection_without_extensions(tmp_path)
        try:
            assert register_vss(con, lance_uri=tmp_path / "absent") is False
        finally:
            con.close()
        assert _loads(statements) == []
        assert _installs(statements) == []

    def test_installed_check_reads_the_extension_directory(self, tmp_path: Path) -> None:
        con, _, _ = _connection_without_extensions(tmp_path)
        try:
            assert lance_extension_installed(con) is False
            assert load_lance_extension(con) is False
        finally:
            con.close()
        default = duckdb.connect()
        try:
            assert lance_extension_installed(default) is True
            assert load_lance_extension(default) is True
        finally:
            default.close()

    def test_register_over_a_corpus_with_a_store_still_binds_every_other_view(
        self, corpus_root: Path, tmp_path: Path
    ) -> None:
        _write_lance(corpus_root / "embeddings_lance")
        con, _, statements = _connection_without_extensions(tmp_path)
        try:
            register(con, corpus_root)
            assert con.execute("SELECT count(*) FROM sessions").fetchone() == (2,)
            assert con.execute("SELECT count(*) FROM message_embeddings").fetchone() == (0,)
            assert (
                con.execute(
                    "SELECT count(*) FROM duckdb_functions() WHERE function_name = 'semantic_search'"
                ).fetchone()[0]
                >= 1
            )
        finally:
            con.close()
        assert _installs(statements) == []

    def test_explicit_install_returns_the_path(self) -> None:
        """A no-op where the extension is present; the one INSTALL atif-duck still owns."""
        con = duckdb.connect()
        try:
            path = install_lance_extension(con)
        finally:
            con.close()
        assert "lance" in path

    def test_registry_source_has_no_install_outside_the_explicit_helper(self) -> None:
        import inspect

        source = inspect.getsource(registry_mod)
        helper = inspect.getsource(install_lance_extension)
        assert source.count('"INSTALL') + source.count("INSTALL {LANCE_EXTENSION}") == (
            helper.count('"INSTALL') + helper.count("INSTALL {LANCE_EXTENSION}")
        )
