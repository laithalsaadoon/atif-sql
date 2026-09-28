# SPDX-License-Identifier: Apache-2.0

"""Step texts and keys read from the lake for the embedding store, and the multi-store view.

``open_lake_steps`` is what atif-embed's lake discovery stands on, so these
tests pin its promises against the per-session registry: a full read is the
``steps`` view's qualifying rows in session/step order; a change read after a
sink write names exactly the rewritten sessions' uuids; file maintenance
changes nothing, while snapshot expiry is reported.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from duck_fixtures import SESSION_IDS, build_corpus
from test_vss import DIM, MODEL, _write_lance

from atif_duck.domain.embedding_guard import EmbeddingProviderMismatch
from atif_duck.infrastructure.lake import (
    DuckLakeSessionSink,
    LakeCorpus,
    LakeLayout,
    LakeUnavailable,
    compact_lake,
    rebuild_lake,
)
from atif_duck.infrastructure.lake_steps import LakeSteps, open_lake_steps
from atif_duck.infrastructure.registry import register, register_vss_stores

MIN_CHARS = 32

#: The same selection over the per-session ``steps`` view: the oracle.
_ORACLE_SQL = (
    "SELECT uuid, message FROM (SELECT session_id, step_id, "
    "json_extract_string(source_uuids, '$[0]') AS uuid, message FROM steps) "
    "WHERE uuid IS NOT NULL AND message IS NOT NULL AND length(message) >= ? "
    "ORDER BY session_id, step_id"
)


def _oracle(corpus_root: Path) -> list[tuple[str, str]]:
    con = duckdb.connect()
    try:
        register(con, corpus_root, skip_vss=True)
        return [(str(u), str(m)) for u, m in con.execute(_ORACLE_SQL, [MIN_CHARS]).fetchall()]
    finally:
        con.close()


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    return build_corpus(tmp_path / "corpus")


@pytest.fixture
def layout(tmp_path: Path, corpus: Path) -> LakeLayout:
    lake = LakeLayout(tmp_path / "lake")
    rebuild_lake(lake, [LakeCorpus(root=corpus, agent="claude-code")])
    return lake


def _open(layout: LakeLayout, corpus: Path) -> LakeSteps:
    opened = open_lake_steps(layout, corpus)
    assert isinstance(opened, LakeSteps), opened
    return opened


def _rewrite_first_step(corpus: Path, session_id: str, text: str) -> None:
    """Change one step's text in both the JSON and the columnar artifact (drop the parquet)."""
    session = corpus / "sessions" / session_id
    trajectory = json.loads((session / "trajectory.json").read_text(encoding="utf-8"))
    trajectory["steps"][0]["message"] = text
    (session / "trajectory.json").write_text(json.dumps(trajectory), encoding="utf-8")
    for parquet in session.glob("*.parquet"):
        parquet.unlink()


class TestReads:
    def test_a_full_read_is_the_steps_views_rows_in_order(
        self, layout: LakeLayout, corpus: Path
    ) -> None:
        with _open(layout, corpus) as steps:
            rows = list(steps.step_texts(min_chars=MIN_CHARS, since_snapshot=None))
        assert rows
        assert rows == _oracle(corpus)

    def test_keys_cover_every_step_whatever_its_text(
        self, layout: LakeLayout, corpus: Path
    ) -> None:
        with _open(layout, corpus) as steps:
            keys = steps.step_keys()
            texts = {uuid for uuid, _ in steps.step_texts(min_chars=MIN_CHARS, since_snapshot=None)}
            short = {uuid for uuid, _ in steps.step_texts(min_chars=1, since_snapshot=None)}
        assert texts < keys or texts == keys
        assert short <= keys

    def test_a_change_read_names_only_the_rewritten_sessions_uuids(
        self, layout: LakeLayout, corpus: Path
    ) -> None:
        with _open(layout, corpus) as steps:
            before = steps.snapshot_id
            lineage = steps.lineage
            assert list(steps.step_texts(min_chars=MIN_CHARS, since_snapshot=before)) == []
        _rewrite_first_step(corpus, SESSION_IDS[0], "a rewritten first step, long enough to embed")
        DuckLakeSessionSink(layout).sync_sessions(
            corpus_root=corpus, agent="claude-code", session_ids=[SESSION_IDS[0]]
        )
        oracle = _oracle(corpus)
        with _open(layout, corpus) as steps:
            assert steps.lineage == lineage
            assert steps.snapshot_id > before
            assert steps.can_read_changes_since(before)
            changed = list(steps.step_texts(min_chars=MIN_CHARS, since_snapshot=before))
            session_uuids = {
                str(row[0])
                for row in steps.con.execute(
                    "SELECT json_extract_string(source_uuids, '$[0]') FROM atif_lake.steps "
                    "WHERE session_id = ?",
                    [SESSION_IDS[0]],
                ).fetchall()
            }
        assert "a rewritten first step, long enough to embed" in {t for _, t in changed}
        assert {uuid for uuid, _ in changed} <= session_uuids
        # Every occurrence of a changed uuid, in the full read's order.
        assert changed == [row for row in oracle if row[0] in {u for u, _ in changed}]

    def test_compaction_changes_no_row_and_expiry_is_reported(
        self, layout: LakeLayout, corpus: Path
    ) -> None:
        sink = DuckLakeSessionSink(layout)
        with _open(layout, corpus) as steps:
            before = steps.snapshot_id
        sink.sync_sessions(corpus_root=corpus, agent="claude-code", session_ids=[SESSION_IDS[1]])
        with _open(layout, corpus) as steps:
            first_write = steps.snapshot_id
        sink.sync_sessions(corpus_root=corpus, agent="claude-code", session_ids=[SESSION_IDS[1]])
        with _open(layout, corpus) as steps:
            second_write = steps.snapshot_id
        compact_lake(layout, expire_older_than_days=30)
        with _open(layout, corpus) as steps:
            assert steps.can_read_changes_since(second_write)
            assert list(steps.step_texts(min_chars=MIN_CHARS, since_snapshot=second_write)) == []
            assert steps.can_read_changes_since(before)
        compact_lake(layout, expire_older_than_days=0)
        with _open(layout, corpus) as steps:
            # Only the newest snapshot survives: changes after the first write
            # (the second write) are still readable, changes after the rebuild
            # are not.
            assert steps.oldest_snapshot_id == second_write
            assert steps.can_read_changes_since(first_write)
            assert list(steps.step_texts(min_chars=MIN_CHARS, since_snapshot=first_write))
            assert not steps.can_read_changes_since(before)

    def test_a_rebuild_starts_a_new_lineage(self, layout: LakeLayout, corpus: Path) -> None:
        with _open(layout, corpus) as steps:
            lineage = steps.lineage
        rebuild_lake(layout, [LakeCorpus(root=corpus, agent="claude-code")])
        with _open(layout, corpus) as steps:
            assert steps.lineage != lineage

    def test_no_lake_and_a_foreign_corpus_are_reasons(self, tmp_path: Path, corpus: Path) -> None:
        missing = open_lake_steps(LakeLayout(tmp_path / "nowhere"), corpus)
        assert isinstance(missing, LakeUnavailable)
        lake = LakeLayout(tmp_path / "lake")
        rebuild_lake(lake, [LakeCorpus(root=corpus, agent="claude-code")])
        other = build_corpus(tmp_path / "other")
        assert isinstance(open_lake_steps(lake, other), LakeUnavailable)

    def test_close_removes_the_spill_directory(self, layout: LakeLayout, corpus: Path) -> None:
        steps = _open(layout, corpus)
        assert steps.spill_dir.is_dir()
        steps.close()
        assert not steps.spill_dir.exists()


class TestStoresUnion:
    def test_two_stores_bind_as_one_view(self, tmp_path: Path) -> None:
        one = _write_lance(tmp_path / "one")
        two = _write_lance(tmp_path / "two")
        con = duckdb.connect()
        con.execute("LOAD lance")
        assert register_vss_stores(
            con, lance_uris=[one, two, tmp_path / "absent"], expected_model=MODEL, expected_dim=DIM
        )
        single = duckdb.connect()
        single.execute("LOAD lance")
        register_vss_stores(con=single, lance_uris=[one], expected_model=MODEL, expected_dim=DIM)
        n_one = single.execute("SELECT count(*) FROM message_embeddings").fetchone()
        n_all = con.execute("SELECT count(*) FROM message_embeddings").fetchone()
        assert n_one is not None
        assert n_all == (2 * n_one[0],)
        assert [
            d[0] for d in con.execute("SELECT * FROM message_embeddings LIMIT 0").description
        ] == [d[0] for d in single.execute("SELECT * FROM message_embeddings LIMIT 0").description]

    def test_a_store_from_another_provider_is_refused(self, tmp_path: Path) -> None:
        one = _write_lance(tmp_path / "one")
        other = _write_lance(tmp_path / "two", model="another-model:1")
        con = duckdb.connect()
        with pytest.raises(EmbeddingProviderMismatch):
            register_vss_stores(
                con, lance_uris=[one, other], expected_model=MODEL, expected_dim=DIM
            )

    def test_stores_of_two_widths_are_refused(self, tmp_path: Path) -> None:
        import lancedb
        import pyarrow as pa

        one = _write_lance(tmp_path / "one")
        wide = tmp_path / "two"
        wide.mkdir()
        width = DIM * 2
        lancedb.connect(str(wide)).create_table(
            "embeddings",
            data=pa.table(
                {
                    "uuid": ["w"],
                    "model": [MODEL],
                    "dim": pa.array([width], type=pa.int32()),
                    "embedding": pa.array([[0.5] * width], type=pa.list_(pa.float32(), width)),
                    "embedded_at": pa.array([0], type=pa.timestamp("us", tz="UTC")),
                }
            ),
        )
        con = duckdb.connect()
        with pytest.raises(EmbeddingProviderMismatch):
            register_vss_stores(con, lance_uris=[one, wide])

    def test_no_store_binds_the_empty_table(self, tmp_path: Path) -> None:
        con = duckdb.connect()
        assert not register_vss_stores(con, lance_uris=[tmp_path / "a", tmp_path / "b"])
        assert con.execute("SELECT count(*) FROM message_embeddings").fetchone() == (0,)
