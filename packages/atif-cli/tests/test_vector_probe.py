# SPDX-License-Identifier: Apache-2.0

"""``status --vector-probe``: ``search``'s exact kNN, timed over a tiny synthetic store.

No Bedrock and no network. The probe's query vector is one the store already
holds, and the store is a real Lance dataset written here with raw DuckDB SQL
through the lance extension, not through atif-embed's writer, so these tests
pin what the probe reads rather than restating what the writer writes.
"""

from __future__ import annotations

import json
import math
import re
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from cli_fixtures import truncate_newest_manifest
from duckdb import connect as _unpatched_duckdb_connect

from atif_cli.app import _SEARCH_SQL, status
from atif_cli.errors import EXIT_CODES
from atif_cli.output import OutputFormat
from atif_cli.vector_probe import PROBE_K, probe_sql, probe_store
from atif_embed.domain.errors import LanceExtensionMissing
from atif_embed.infrastructure import lance_store

DIM = 8
N_ROWS = 25

#: The store's table as an open store connection names it.
TABLE = f"{lance_store.STORE_ALIAS}.main.{lance_store.TABLE_NAME}"


def _f32(value: float) -> float:
    """``value`` rounded to float32, as the store keeps it."""
    return float(struct.unpack("f", struct.pack("f", value))[0])


def _vectors() -> dict[str, list[float]]:
    """``N_ROWS`` fixed vectors keyed by uuid, inserted in an order that isn't uuid order.

    7 is coprime with 25, so ``(7 * i + 3) % 25`` visits every index once, and
    the smallest uuid (the probe's query vector) is the twenty-second row written.
    """
    order = [(7 * i + 3) % N_ROWS for i in range(N_ROWS)]
    return {
        f"u-{k:03d}": [_f32(math.sin(1.3 * k + 0.7 * j + 0.11 * k * j)) for j in range(DIM)]
        for k in order
    }


def _write_store(uri: Path, vectors: dict[str, list[float]]) -> Path:
    """A real Lance store holding ``vectors``, in the store's seven-column shape."""
    n = len(vectors)
    table = pa.table(
        {
            "uuid": list(vectors),
            "model": ["fixture-model:1"] * n,
            "dim": pa.array([DIM] * n, type=pa.int32()),
            "embedding": pa.array(list(vectors.values()), type=pa.list_(pa.float32(), DIM)),
            "embedded_at": pa.array([datetime.now(UTC)] * n, type=pa.timestamp("us", tz="UTC")),
            "text_hash": ["h"] * n,
            "truncated": [False] * n,
        }
    )
    uri.mkdir(parents=True, exist_ok=True)
    quoted = "'" + str(uri).replace("'", "''") + "'"
    writer = _unpatched_duckdb_connect(":memory:")
    try:
        writer.execute("SET TimeZone = 'UTC'")
        writer.execute("LOAD lance")
        writer.execute(f"ATTACH {quoted} AS fixture_store (TYPE LANCE)")
        writer.register("fixture_rows", table)
        writer.execute("CREATE TABLE fixture_store.main.embeddings AS SELECT * FROM fixture_rows")
    finally:
        writer.close()
    return uri


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty corpus root, with every variable that moves the store path cleared."""
    for var in (
        "ATIF_SQL_CORPUS_ROOT",
        "ATIF_SQL_LANCE_URI",
        "ATIF_SQL_EMBED_PROVIDER",
        "ATIF_SQL_OUTPUT_DIMENSION",
    ):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "corpus"
    root.mkdir()
    return root


class TestStatusVectorProbe:
    def test_json_carries_corpus_store_rows_and_scan_seconds(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store = _write_store(corpus / "embeddings_lance", _vectors())
        status(corpus_root=corpus, vector_probe=True, fmt=OutputFormat.JSON)
        payload: dict[str, Any] = json.loads(capsys.readouterr().out)
        assert set(payload) == {"corpus", "store", "vector_rows", "scan_s"}
        assert payload["corpus"] == "corpus"
        assert payload["store"] == str(store)
        assert payload["vector_rows"] == N_ROWS
        assert isinstance(payload["scan_s"], float)
        assert payload["scan_s"] >= 0
        assert round(payload["scan_s"], 3) == payload["scan_s"]

    def test_table_format_is_one_line(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_store(corpus / "embeddings_lance", _vectors())
        status(corpus_root=corpus, vector_probe=True, fmt=OutputFormat.TABLE)
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        assert lines[0].startswith(f"vector probe: {N_ROWS} vectors, exact top-{PROBE_K} scan in ")

    def test_no_store_exits_2_and_creates_no_store_directory(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Opening a store creates its directory, so the probe checks first."""
        with pytest.raises(SystemExit) as excinfo:
            status(corpus_root=corpus, vector_probe=True, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["no_embeddings"] == 2
        assert json.loads(capsys.readouterr().err)["error"]["kind"] == "no_embeddings"
        assert not (corpus / "embeddings_lance").exists()

    def test_a_store_directory_without_a_table_exits_2(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (corpus / "embeddings_lance").mkdir()
        with pytest.raises(SystemExit) as excinfo:
            status(corpus_root=corpus, vector_probe=True, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["no_embeddings"]
        capsys.readouterr()

    def test_a_store_the_extension_cant_open_exits_78_not_2(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A damaged store is no "no embeddings": the probe says what's wrong."""
        store = _write_store(corpus / "embeddings_lance", _vectors())
        truncate_newest_manifest(store)
        with pytest.raises(SystemExit) as excinfo:
            status(corpus_root=corpus, vector_probe=True, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["terminal_state"] == 78
        err = json.loads(capsys.readouterr().err)["error"]
        assert err["kind"] == "terminal_state"
        assert "can't open" in err["message"]

    def test_a_missing_extension_exits_78_and_downloads_nothing(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_store(corpus / "embeddings_lance", _vectors())
        asked: list[bool] = []

        def missing(con: object, *, install: bool) -> None:
            del con
            asked.append(install)
            msg = "the lance DuckDB extension is not installed"
            raise LanceExtensionMissing(msg)

        monkeypatch.setattr(lance_store, "load_lance_extension", missing)
        with pytest.raises(SystemExit) as excinfo:
            status(corpus_root=corpus, vector_probe=True, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["extension_missing"] == 78
        err = json.loads(capsys.readouterr().err)["error"]
        assert err["kind"] == "extension_missing"
        assert "--install-extension" in err["hint"]
        assert asked == [False]

    def test_plain_status_never_runs_the_probe(
        self,
        corpus: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The probe reads every stored vector, so only the flag may pay for it."""
        _write_store(corpus / "embeddings_lance", _vectors())

        def forbidden(*args: object, **kwargs: object) -> None:
            del args, kwargs
            msg = "plain status ran the vector probe"
            raise AssertionError(msg)

        monkeypatch.setattr("atif_cli.vector_probe.probe_store", forbidden)
        source = tmp_path / "source"
        source.mkdir()
        status(source_root=source, corpus_root=corpus, fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert payload["vector_search"] == "ready"
        assert "scan_s" not in payload


class TestProbeScan:
    def test_ranks_like_a_brute_force_cosine_scan(self, corpus: Path) -> None:
        vectors = _vectors()
        store = _write_store(corpus / "embeddings_lance", vectors)
        probe = probe_store(store, corpus="corpus")
        assert probe is not None
        query = vectors[min(vectors)]
        expected = sorted(vectors, key=lambda uuid: -_cosine(query, vectors[uuid]))[:PROBE_K]
        assert [uuid for uuid, _ in probe.hits] == expected
        assert probe.hits[0] == ("u-000", pytest.approx(1.0, abs=1e-6))
        for uuid, sim in probe.hits:
            assert sim == pytest.approx(_cosine(query, vectors[uuid]), abs=1e-5)

    def test_the_statement_is_searchs_knn_without_the_join(self) -> None:
        """The probe does ``search``'s per-row vector work: no less, no more.

        Every probe line but ``FROM`` is a line of ``search``'s statement, and
        every line of ``search``'s that touches the query vector ``qv`` (the
        CTE, the similarity column, the ranking) is in the probe, with the
        ``LIMIT``.
        """
        search_sql = _SEARCH_SQL.format(dim=DIM, corpus="", corpus_join="", session_filter="")
        search_lines = [line.strip() for line in search_sql.splitlines() if line.strip()]
        probe_lines = [line.strip() for line in probe_sql(dim=DIM, table="t").splitlines()]
        probe_lines = [line for line in probe_lines if line]
        assert "FROM t me" in probe_lines
        extra = [line for line in probe_lines if line != "FROM t me" and line not in search_lines]
        assert extra == []
        vector_work = [line for line in search_lines if "qv" in line or line.startswith("LIMIT")]
        assert len(vector_work) == 4
        assert [line for line in vector_work if line not in probe_lines] == []

    def test_the_plan_is_a_full_lance_scan_under_a_top_n(self, corpus: Path) -> None:
        """No index and no pushed-down limit: every vector is read and ranked.

        If a later lance extension ever rewrites this statement into an index
        search or a limited scan, the probe would stop timing what ``search``
        pays, and this fails.
        """
        vectors = _vectors()
        store = _write_store(corpus / "embeddings_lance", vectors)
        with lance_store.connect_db(store) as conn:
            rows = conn.con.execute(
                "EXPLAIN " + probe_sql(dim=DIM, table=TABLE), [vectors[min(vectors)], PROBE_K]
            ).fetchall()
        plan = "\n".join(str(row[1]) for row in rows)
        assert "Lance Table Scan" in plan
        assert "Lance Limit: none" in plan
        assert "Top N" in plan

    def test_runs_on_the_stores_own_bounded_connection(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _write_store(corpus / "embeddings_lance", _vectors())
        real = lance_store.connect_db
        opened: list[tuple[Path, dict[str, object], int]] = []

        def recording(uri: Path, **kwargs: object) -> lance_store.StoreConnection:
            conn = real(uri, **kwargs)  # pyright: ignore[reportArgumentType]
            threads = conn.con.execute("SELECT current_setting('threads')").fetchall()[0][0]
            opened.append((Path(uri), kwargs, int(threads)))
            return conn

        monkeypatch.setattr(lance_store, "connect_db", recording)
        assert probe_store(store, corpus="corpus") is not None
        assert opened == [(store, {}, lance_store.WRITER_THREADS)]


class _RecordingCon:
    """A DuckDB connection whose statements are recorded."""

    def __init__(self, con: Any, statements: list[str]) -> None:
        self._con = con
        self._statements = statements

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self._statements.append(sql)
        return self._con.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


def test_the_probe_vector_comes_from_one_statement(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking the uuid and reading its vector in two statements let a delete between them
    leave nothing to read (an IndexError)."""
    store = _write_store(corpus / "embeddings_lance", _vectors())
    real = lance_store.connect_db
    statements: list[str] = []

    def recording(uri: Path, **kwargs: object) -> lance_store.StoreConnection:
        conn = real(uri, **kwargs)  # pyright: ignore[reportArgumentType]
        conn._con = _RecordingCon(conn._con, statements)  # pyright: ignore[reportAttributeAccessIssue]
        return conn

    monkeypatch.setattr(lance_store, "connect_db", recording)
    probe = probe_store(store, corpus="corpus")
    assert probe is not None
    assert probe.hits[0][0] == "u-000"
    scan = next(i for i, sql in enumerate(statements) if "array_cosine_distance" in sql)
    # Before the scan: the row count, and one statement that picks the probe
    # row and reads its vector together.
    reads = [sql for sql in statements[:scan] if f"FROM {TABLE}" in sql and "count(*)" not in sql]
    assert len(reads) == 1
    assert re.search(r"\bembedding\b", reads[0])
