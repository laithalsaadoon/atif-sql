# SPDX-License-Identifier: Apache-2.0

"""VSS tests: register_vss over a tiny REAL lance table + the fixture corpus.

The lance table is written with raw lancedb + pyarrow (a local library — no
network), NOT via atif-embed: the independence contract keeps even the test
surface free of sibling-package imports.

Vectors are hand-placed on known directions AND at deliberately UNEVEN
magnitudes, mirroring the live store's un-normalized int8-cast vectors. That
spread is what makes cosine and L2 rank the same probe differently, so
``semantic_search``'s ordering pins the metric rather than merely pinning that
some order exists.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import lancedb
import pyarrow as pa
import pytest
from duck_fixtures import SESSION_IDS

from atif_duck.domain.embedding_guard import EmbeddingProviderMismatch
from atif_duck.infrastructure.registry import register, register_vss

DIM = 4
MODEL = "test-embedder:1"

#: (uuid, vector) rows — uuids match session 1's edges (a-1 etc.) where it
#: matters. Directions are angularly spread AND magnitudes are uneven, the
#: shape the live un-normalized store has: three rows sit at live-like norms
#: (~950-1400) while ``u-2`` is deliberately tiny. Against the unit-norm
#: ``QUERY`` that makes ``u-2`` the L2-NEAREST row despite being the
#: cosine-FARTHEST — the two metrics disagree even about the top hit, so an
#: ordering assertion cannot pass under both.
_ROWS: list[tuple[str, list[float]]] = [
    ("a-1", [1200.0, 0.0, 0.0, 0.0]),
    ("a-3", [900.0, 300.0, 0.0, 0.0]),
    ("sa-1", [1000.0, 1000.0, 0.0, 0.0]),
    ("u-2", [0.6, 0.8, 0.0, 0.0]),
]

QUERY = [1.0, 0.0, 0.0, 0.0]

#: Cosine order for ``QUERY`` — what ``semantic_search`` must return.
EXPECTED_ORDER = ["a-1", "a-3", "sa-1", "u-2"]

#: L2 order for the same probe. Disjoint from EXPECTED_ORDER at every rank;
#: asserted against explicitly so a silent regression back to array_distance
#: fails loudly instead of merely reshuffling.
EXPECTED_L2_ORDER = ["u-2", "a-3", "a-1", "sa-1"]


def _write_lance(uri: Path, *, model: str = MODEL, dim: int = DIM) -> Path:
    schema = pa.schema(
        [
            pa.field("uuid", pa.string(), nullable=False),
            pa.field("model", pa.string(), nullable=False),
            pa.field("dim", pa.int32(), nullable=False),
            pa.field("embedding", pa.list_(pa.float32(), dim), nullable=False),
            pa.field("embedded_at", pa.timestamp("us", tz="UTC"), nullable=False),
        ]
    )
    now = datetime.now(UTC)
    table = pa.table(
        {
            "uuid": [u for u, _ in _ROWS],
            "model": [model] * len(_ROWS),
            "dim": [dim] * len(_ROWS),
            "embedding": pa.array([v for _, v in _ROWS], type=pa.list_(pa.float32(), dim)),
            "embedded_at": [now] * len(_ROWS),
        },
        schema=schema,
    )
    uri.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(uri))
    db.create_table("embeddings", data=table, mode="create")
    return uri


@pytest.fixture
def lance_uri(tmp_path: Path) -> Path:
    return _write_lance(tmp_path / "lance")


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


class TestRegisterVss:
    def test_binds_view_over_populated_store(
        self, con: duckdb.DuckDBPyConnection, lance_uri: Path
    ) -> None:
        bound = register_vss(con, lance_uri=lance_uri, expected_model=MODEL, expected_dim=DIM)
        assert bound is True
        rows = con.execute("SELECT count(*), any_value(model) FROM message_embeddings").fetchone()
        assert rows == (len(_ROWS), MODEL)
        # The view's embedding column binds at the STORED width (4), not the
        # 1024 default arg.
        described = {r[0]: r[1] for r in con.execute("DESCRIBE message_embeddings").fetchall()}
        assert described["embedding"] == f"FLOAT[{DIM}]"

    def test_empty_store_creates_fallback_table(
        self, con: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        bound = register_vss(con, lance_uri=tmp_path / "missing", expected_model=MODEL)
        assert bound is False
        row = con.execute("SELECT count(*) FROM message_embeddings").fetchone()
        assert row == (0,)

    def test_guard_fires_on_model_drift(
        self, con: duckdb.DuckDBPyConnection, lance_uri: Path
    ) -> None:
        with pytest.raises(EmbeddingProviderMismatch):
            register_vss(con, lance_uri=lance_uri, expected_model="other-model:2", expected_dim=DIM)

    def test_guard_fires_on_dim_drift(
        self, con: duckdb.DuckDBPyConnection, lance_uri: Path
    ) -> None:
        with pytest.raises(EmbeddingProviderMismatch):
            register_vss(con, lance_uri=lance_uri, expected_model=MODEL, expected_dim=1024)

    def test_no_expected_model_skips_guard(
        self, con: duckdb.DuckDBPyConnection, lance_uri: Path
    ) -> None:
        assert register_vss(con, lance_uri=lance_uri) is True

    def test_signature_is_pinned(self) -> None:
        """register_vss's keyword surface is a cross-package contract.

        Dropping or renaming a parameter turns every external caller's kwarg
        into a TypeError at call time, which no other test here would reach.
        ``metric`` is deliberately absent: cosine is fixed by the
        ``semantic_search`` macro's ``list_cosine_*`` calls, so a per-bind
        metric argument could only ever disagree with the query that reads it.
        """
        signature = inspect.signature(register_vss)
        assert list(signature.parameters) == [
            "con",
            "lance_uri",
            "expected_model",
            "expected_dim",
            "dim",
        ]
        keyword_only = [
            name
            for name, parameter in signature.parameters.items()
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        ]
        assert keyword_only == ["lance_uri", "expected_model", "expected_dim", "dim"]


class TestSemanticSearch:
    def test_returns_k_rows_ordered_by_distance(
        self, con: duckdb.DuckDBPyConnection, corpus_root: Path, lance_uri: Path
    ) -> None:
        register(con, corpus_root, lance_uri=lance_uri, expected_model=MODEL, expected_dim=DIM)
        rows = con.execute(
            f"SELECT uuid, sim, distance FROM semantic_search(CAST(? AS FLOAT[{DIM}]), 3)",
            [QUERY],
        ).fetchall()
        assert [r[0] for r in rows] == EXPECTED_ORDER[:3]
        distances = [float(r[2]) for r in rows]
        assert distances == sorted(distances)
        # Nearest hit is the exact query direction: sim == 1.
        assert float(rows[0][1]) == pytest.approx(1.0)

    def test_ranks_by_cosine_not_l2(
        self, con: duckdb.DuckDBPyConnection, corpus_root: Path, lance_uri: Path
    ) -> None:
        """Ordering must follow ANGLE, not vector length.

        Stored vectors are un-normalized (int8-cast) while real probes are
        unit-norm, so L2 sorts partly by magnitude and would rank the
        cosine-FARTHEST row (``u-2``, norm 1) first. Asserting the full k=4
        order pins cosine: the L2 order is disjoint from it at every rank.
        """
        register(con, corpus_root, lance_uri=lance_uri)
        rows = con.execute(
            f"SELECT uuid FROM semantic_search(CAST(? AS FLOAT[{DIM}]), 4)", [QUERY]
        ).fetchall()
        observed = [r[0] for r in rows]
        assert observed == EXPECTED_ORDER
        assert observed != EXPECTED_L2_ORDER

    def test_sim_and_distance_are_one_metric(
        self, con: duckdb.DuckDBPyConnection, corpus_root: Path, lance_uri: Path
    ) -> None:
        """``distance`` must be cosine too, not L2 alongside a cosine ``sim``.

        Mixing metrics makes the columns disagree: under L2 ``distance``, the
        ``sim``-best row is not the ``distance``-best one.
        """
        register(con, corpus_root, lance_uri=lance_uri)
        rows = con.execute(
            f"SELECT sim, distance FROM semantic_search(CAST(? AS FLOAT[{DIM}]), 4)", [QUERY]
        ).fetchall()
        for sim, distance in rows:
            assert float(distance) == pytest.approx(1.0 - float(sim), abs=1e-6)

    def test_accepts_a_computed_unit_norm_probe(
        self, con: duckdb.DuckDBPyConnection, corpus_root: Path, lance_uri: Path
    ) -> None:
        """A probe computed in SQL (a LIST, not a fixed-size ARRAY) must bind.

        This is the shape ``ARG_EXEMPLARS['query_vec']`` builds: normalizing a
        stored vector yields FLOAT[] rather than FLOAT[dim], which the
        ``array_*`` variants reject outright.
        """
        register(con, corpus_root, lance_uri=lance_uri)
        rows = con.execute(
            """
            SELECT uuid, sim FROM semantic_search(
                (SELECT list_transform(
                     me.embedding,
                     v -> (v / sqrt(list_sum(
                         list_transform(me.embedding, x -> x::DOUBLE * x)
                     )))::FLOAT
                 )
                 FROM message_embeddings me WHERE me.uuid = 'a-3'),
                4
            )
            """
        ).fetchall()
        # Normalizing a-3 preserves its direction, so it is its own top hit
        # and the rest follow a-3's cosine order — not the stored magnitudes.
        assert [r[0] for r in rows] == ["a-3", "a-1", "sa-1", "u-2"]
        assert float(rows[0][1]) == pytest.approx(1.0, abs=1e-6)

    def test_k_caps_result_size(
        self, con: duckdb.DuckDBPyConnection, corpus_root: Path, lance_uri: Path
    ) -> None:
        register(con, corpus_root, lance_uri=lance_uri)
        rows = con.execute(
            f"SELECT uuid FROM semantic_search(CAST(? AS FLOAT[{DIM}]), 2)", [QUERY]
        ).fetchall()
        assert len(rows) == 2

    def test_binds_over_empty_fallback(
        self, con: duckdb.DuckDBPyConnection, corpus_root: Path, tmp_path: Path
    ) -> None:
        """The macro must bind (and return zero rows) with no store at all."""
        register(con, corpus_root, lance_uri=tmp_path / "missing")
        rows = con.execute(
            "SELECT uuid FROM semantic_search(CAST(? AS FLOAT[1024]), 5)", [[0.0] * 1024]
        ).fetchall()
        assert rows == []

    def test_skip_vss_registers_neither_view_nor_macro(
        self, con: duckdb.DuckDBPyConnection, corpus_root: Path
    ) -> None:
        register(con, corpus_root, skip_vss=True)
        view_row = con.execute(
            "SELECT count(*) FROM duckdb_views() WHERE view_name = 'message_embeddings'"
        ).fetchone()
        assert view_row == (0,)
        macro_row = con.execute(
            """
            SELECT count(*) FROM duckdb_functions()
            WHERE function_name = 'semantic_search'
            """
        ).fetchone()
        assert macro_row == (0,)

    def test_join_back_to_messages_view(
        self, con: duckdb.DuckDBPyConnection, corpus_root: Path, lance_uri: Path
    ) -> None:
        """Hits join to the uuid-keyed messages surface (the CLI's snippet path)."""
        register(con, corpus_root, lance_uri=lance_uri)
        rows = con.execute(
            f"""
            SELECT ss.uuid, m.session_id
            FROM semantic_search(CAST(? AS FLOAT[{DIM}]), 1) ss
            JOIN messages m ON m.uuid = ss.uuid
            """,
            [QUERY],
        ).fetchall()
        assert rows == [("a-1", SESSION_IDS[0])]
