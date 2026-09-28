# SPDX-License-Identifier: Apache-2.0

"""Embed discovery, orphan pruning and search over the lake, through the CLI.

Real converter, real materialize, real DuckLake and real Lance in tmp dirs;
only the embedder is fake (no Bedrock). The corpus is built so the lake path
has something to get wrong:

* session B resumes session A: it repeats A's records under the same uuids,
  and bundles one more record into A's assistant step, so B's step for that
  uuid carries DIFFERENT text. The first-wins rule has to pick A's text, on a
  full read and on an incremental one that only B's change triggered;
* texts under the 32-character floor, list-shaped messages, and a tool call.

The per-session reader (``--no-lake``) is the oracle throughout.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from atif_cli.app import embed, materialize, query, search
from atif_cli.embed_lake import lake_steps_port
from atif_cli.errors import EXIT_CODES
from atif_cli.lake import compact, rebuild
from atif_cli.output import OutputFormat
from atif_embed.domain.text_stamp import text_hash
from atif_embed.infrastructure.corpus_text_rows import DuckDbTextRows
from atif_embed.infrastructure.lake_text_rows import (
    LAKE_WATERMARK_FILE,
    LakeTextRows,
    read_watermark,
)
from atif_embed.infrastructure.lance_store import LanceVectorStore, get_embedded_hashes

pytestmark = pytest.mark.integration

MODEL = "global.cohere.embed-v4:0"
DIM = 1024

SESSION_A = "aaaaaaaa-0000-4000-8000-000000000001"
SESSION_B = "bbbbbbbb-0000-4000-8000-000000000002"
SESSION_C = "cccccccc-0000-4000-8000-000000000003"

_STALE_NS = 3_000 * 1_000_000_000


def _user(uuid: str, parent: str | None, sid: str, ts: str, content: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": sid,
        "timestamp": ts,
        "cwd": "/home/alice/proj",
        "gitBranch": "main",
        "version": "2.0.0",
        "isSidechain": False,
        "message": {"role": "user", "content": content},
    }


def _assistant(uuid: str, parent: str, sid: str, ts: str, msg_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": sid,
        "timestamp": ts,
        "isSidechain": False,
        "requestId": f"req-{uuid}",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-test-1",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }


def _session_a(sid: str) -> list[dict[str, Any]]:
    return [
        _user(
            "a-u1", None, sid, "2026-08-22T00:00:01Z", "please explain how the lake stores steps"
        ),
        _assistant(
            "a-a1",
            "a-u1",
            sid,
            "2026-08-22T00:00:02Z",
            "msg-a1",
            "The lake keeps one table per artifact kind, partitioned by corpus.",
        ),
        _user("a-u2", "a-a1", sid, "2026-08-22T00:00:03Z", "ok thanks"),
    ]


def _session_b(sid: str) -> list[dict[str, Any]]:
    """Resumes A: A's records under A's uuids, plus a second part in A's assistant message."""
    head = _session_a(sid)[:2]
    extra = _assistant(
        "b-a1b",
        "a-a1",
        sid,
        "2026-08-22T00:00:02Z",
        "msg-a1",
        "A second part the resumed session bundles into the same step.",
    )
    tail = [
        _user(
            "b-u3",
            "b-a1b",
            sid,
            "2026-08-22T00:01:00Z",
            [{"type": "text", "text": "now a list-shaped message that clears the floor easily"}],
        ),
        _assistant(
            "b-a3",
            "b-u3",
            sid,
            "2026-08-22T00:01:01Z",
            "msg-b3",
            "An answer in session B that is long enough to embed.",
        ),
    ]
    return [*head, extra, *tail]


def _session_c(sid: str) -> list[dict[str, Any]]:
    return [
        _user(
            "c-u1", None, sid, "2026-08-22T00:02:00Z", "a brand new session with a long question"
        ),
        _assistant(
            "c-a1",
            "c-u1",
            sid,
            "2026-08-22T00:02:01Z",
            "msg-c1",
            "A brand new answer, long enough to be worth a vector.",
        ),
    ]


def _write_session(source_root: Path, sid: str, events: list[dict[str, Any]]) -> None:
    project = source_root / "-tmp-proj"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    stale = time.time_ns() - _STALE_NS
    os.utime(path, ns=(stale, stale))


def _append(source_root: Path, sid: str, event: dict[str, Any]) -> None:
    path = source_root / "-tmp-proj" / f"{sid}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    stale = time.time_ns() - _STALE_NS + 1_000_000_000
    os.utime(path, ns=(stale, stale))


class _FakeCohere:
    """Stands in for CohereBedrockEmbedder: the configured identity, a fixed vector."""

    def __init__(self, settings: object) -> None:
        del settings

    @property
    def model_id(self) -> str:
        return MODEL

    @property
    def dimension(self) -> int:
        return DIM

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        # A second coordinate from the text's digest: distinct texts get distinct
        # similarities, so a top-k has no ties to order arbitrarily.
        return [
            [1.0, int(text_hash(text)[:6], 16) / 0xFFFFFF] + [0.0] * (DIM - 2) for text in texts
        ]

    def embed_query(self, text: str) -> list[float]:
        del text
        return [1.0, 0.5] + [0.0] * (DIM - 2)


@pytest.fixture(autouse=True)
def _fake_cohere(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    for var in ("ATIF_SQL_CORPUS_ROOT", "ATIF_SQL_LANCE_URI", "ATIF_SQL_EMBED_MODEL_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "atif_embed.infrastructure.cohere_bedrock.CohereBedrockEmbedder", _FakeCohere
    )
    monkeypatch.setattr("atif_cli.app._install_lance_extension", _no_install)


def _no_install(*args: object, **kwargs: object) -> None:
    """A real embed run installs the lance extension first; the suite already has it."""
    del args, kwargs


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "src" / "projects"
    _write_session(root, SESSION_A, _session_a(SESSION_A))
    _write_session(root, SESSION_B, _session_b(SESSION_B))
    return root


def _json(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out)


def _materialize(source: Path, corpus: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    materialize(source_root=source, corpus_root=corpus, fmt=OutputFormat.JSON)
    return _json(capsys)


@pytest.fixture
def corpus(source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """The corpus materialized, with the lake rebuilt over it."""
    root = tmp_path / "corpus-one"
    _materialize(source_root, root, capsys)
    rebuild(corpus_root=[root], fmt=OutputFormat.JSON)
    capsys.readouterr()
    return root


def _plan(corpus: Path, capsys: pytest.CaptureFixture[str], **kw: Any) -> dict[str, Any]:
    embed(dry_run=True, corpus_root=corpus, fmt=OutputFormat.JSON, **kw)
    return _json(capsys)


def _embed_all(corpus: Path, capsys: pytest.CaptureFixture[str], **kw: Any) -> dict[str, Any]:
    embed(all_steps=True, corpus_root=corpus, fmt=OutputFormat.JSON, **kw)
    return _json(capsys)


def _store(corpus: Path) -> Path:
    return corpus / "embeddings_lance"


def _watermark(corpus: Path) -> dict[str, Any] | None:
    return read_watermark(_store(corpus) / LAKE_WATERMARK_FILE)


def _candidates(corpus: Path, *, lake: bool) -> list[tuple[str, str, bool]]:
    """The discovery's rows against the store's current contents, as the use case sees them."""
    embedded = get_embedded_hashes(_store(corpus)) if _store(corpus).exists() else {}
    rows: Any = DuckDbTextRows()
    if lake:
        rows = LakeTextRows(
            lake=lake_steps_port(),
            watermark_path=_store(corpus) / LAKE_WATERMARK_FILE,
            fallback=rows,
        )
    return [
        (p.uuid, p.text_hash, p.replaces_existing)
        for p in rows.iter_unembedded(corpus, embedded=embedded)
    ]


def _exit_code(fn: Any, *args: Any, **kwargs: Any) -> int:
    with pytest.raises(SystemExit) as info:
        fn(*args, **kwargs)
    return int(info.value.code or 0)


class TestDiscoveryFromTheLake:
    def test_a_full_lake_read_yields_the_per_session_rows_in_order(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lake_rows = _candidates(corpus, lake=True)
        assert lake_rows == _candidates(corpus, lake=False)
        assert len(lake_rows) >= 4
        assert _plan(corpus, capsys)["discovery"] == "lake-full"
        assert _plan(corpus, capsys, lake=False)["discovery"] == "corpus"

    def test_the_resumed_session_does_not_steal_the_earlier_sessions_text(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The premise: B really holds a step under A's uuid with other text.
        query(
            "SELECT session_id, message FROM steps "
            "WHERE json_extract_string(source_uuids, '$[0]') = 'a-a1' ORDER BY session_id",
            corpus_root=corpus,
            fmt=OutputFormat.JSON,
        )
        steps = _json(capsys)
        assert [step["session_id"] for step in steps] == [SESSION_A, SESSION_B]
        assert "second part" in steps[1]["message"]
        assert "second part" not in steps[0]["message"]
        rows = DuckDbTextRows()
        by_uuid = {p.uuid: p.text for p in rows.iter_unembedded(corpus, embedded={})}
        assert "second part" not in by_uuid["a-a1"]
        assert {p.uuid: p.text_hash for p in rows.iter_unembedded(corpus, embedded={})} == {
            uuid: text_hash for uuid, text_hash, _ in _candidates(corpus, lake=True)
        }

    def test_a_complete_run_sets_the_watermark_and_the_next_run_reads_nothing(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _watermark(corpus) is None
        written = _embed_all(corpus, capsys)["rows_processed"]
        mark = _watermark(corpus)
        assert mark is not None
        assert mark["stored_rows"] == written
        plan = _plan(corpus, capsys)
        assert (plan["discovery"], plan["candidates"]) == ("lake-unchanged", 0)

    def test_a_dry_run_never_moves_the_watermark(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _plan(corpus, capsys)
        assert _watermark(corpus) is None

    def test_a_materialized_change_is_read_incrementally_and_matches_a_full_read(
        self, corpus: Path, source_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _embed_all(corpus, capsys)
        before = _watermark(corpus)
        assert before is not None
        # B changes (so every uuid it shares with A is in the change set) and
        # C is new.
        _append(
            source_root,
            SESSION_B,
            _user(
                "b-u4",
                "b-a3",
                SESSION_B,
                "2026-08-22T00:01:05Z",
                "and one more long question to embed here",
            ),
        )
        _write_session(source_root, SESSION_C, _session_c(SESSION_C))
        report = _materialize(source_root, corpus, capsys)
        assert report["lake_synced"] == 2
        incremental = _candidates(corpus, lake=True)
        assert incremental == _candidates(corpus, lake=False)
        assert {uuid for uuid, _, _ in incremental} == {"b-u4", "c-u1", "c-a1"}
        plan = _plan(corpus, capsys)
        assert (plan["discovery"], plan["candidates"]) == ("lake-incremental", 3)
        _embed_all(corpus, capsys)
        after = _watermark(corpus)
        assert after is not None
        assert after["snapshot_id"] > before["snapshot_id"]
        assert _plan(corpus, capsys)["candidates"] == 0

    def test_a_run_cut_short_by_limit_leaves_the_watermark(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        embed(limit=1, corpus_root=corpus, fmt=OutputFormat.JSON)
        assert _json(capsys)["rows_processed"] == 1
        assert _watermark(corpus) is None
        assert _plan(corpus, capsys)["discovery"] == "lake-full"

    def test_a_rebuild_starts_a_new_lineage_and_forces_a_full_read(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _embed_all(corpus, capsys)
        rebuild(corpus_root=[corpus], fmt=OutputFormat.JSON)
        capsys.readouterr()
        plan = _plan(corpus, capsys)
        assert (plan["discovery"], plan["candidates"]) == ("lake-full", 0)

    def test_compaction_changes_no_row_and_expiry_forces_a_full_read(
        self, corpus: Path, source_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _embed_all(corpus, capsys)
        _write_session(source_root, SESSION_C, _session_c(SESSION_C))
        _materialize(source_root, corpus, capsys)
        compact(expire_older_than_days=30, fmt=OutputFormat.JSON)
        capsys.readouterr()
        plan = _plan(corpus, capsys)
        assert (plan["discovery"], plan["candidates"]) == ("lake-incremental", 2)
        compact(expire_older_than_days=0, fmt=OutputFormat.JSON)
        capsys.readouterr()
        plan = _plan(corpus, capsys)
        assert (plan["discovery"], plan["candidates"]) == ("lake-full", 2)

    def test_rows_deleted_from_the_store_outside_a_run_force_a_full_read(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _embed_all(corpus, capsys)
        LanceVectorStore(_store(corpus), dim=DIM).delete_uuids(["a-u1"])
        plan = _plan(corpus, capsys)
        assert (plan["discovery"], plan["candidates"]) == ("lake-full", 1)

    def test_without_a_lake_discovery_reads_the_corpus(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "no-lake-corpus"
        _materialize(source_root, root, capsys)
        plan = _plan(root, capsys)
        assert plan["discovery"] == "corpus"
        assert plan["candidates"] == len(_candidates(root, lake=False))


def _add_orphans(corpus: Path, uuids: list[str]) -> None:
    from datetime import UTC, datetime

    import polars as pl

    n = len(uuids)
    frame = pl.DataFrame(
        {
            "uuid": uuids,
            "model": [MODEL] * n,
            "dim": [DIM] * n,
            "embedding": [[0.0, 1.0] + [0.0] * (DIM - 2)] * n,
            "embedded_at": [datetime(2026, 8, 1, tzinfo=UTC)] * n,
            "text_hash": [text_hash(u) for u in uuids],
            "truncated": [False] * n,
        },
        schema={
            "uuid": pl.Utf8,
            "model": pl.Utf8,
            "dim": pl.Int32,
            "embedding": pl.Array(pl.Float32, DIM),
            "embedded_at": pl.Datetime("us", "UTC"),
            "text_hash": pl.Utf8,
            "truncated": pl.Boolean,
        },
    )
    LanceVectorStore(_store(corpus), dim=DIM).add_chunk(frame)


class TestPruneOrphans:
    @pytest.mark.parametrize(
        ("argv", "dry_run"),
        [
            pytest.param([], "unset", id="bare-is-a-dry-run"),
            pytest.param(["--no-dry-run"], False, id="explicit-no-dry-run-deletes"),
            pytest.param(["--dry-run"], True, id="explicit-dry-run"),
        ],
    )
    def test_the_flag_parses_to_a_dry_run_unless_told_otherwise(
        self, argv: list[str], dry_run: object
    ) -> None:
        from atif_cli.app import app

        _, bound, *_ = app.parse_args(["embed", "--prune-orphans", *argv])
        assert bound.arguments.get("dry_run", "unset") == dry_run

    def test_the_default_is_a_dry_run_that_counts(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stored = _embed_all(corpus, capsys)["rows_processed"]
        _add_orphans(corpus, ["gone-1", "gone-2"])
        embed(prune_orphans=True, corpus_root=corpus, fmt=OutputFormat.JSON)
        report = _json(capsys)
        assert (report["stored"], report["orphans"], report["deleted"], report["dry_run"]) == (
            stored + 2,
            2,
            0,
            True,
        )
        assert len(get_embedded_hashes(_store(corpus))) == stored + 2

    def test_no_dry_run_deletes_only_the_orphans(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _embed_all(corpus, capsys)
        kept = set(get_embedded_hashes(_store(corpus)))
        _add_orphans(corpus, ["gone-1", "gone-2"])
        embed(prune_orphans=True, dry_run=False, corpus_root=corpus, fmt=OutputFormat.JSON)
        assert _json(capsys)["deleted"] == 2
        assert set(get_embedded_hashes(_store(corpus))) == kept
        assert _plan(corpus, capsys)["candidates"] == 0

    def test_without_a_lake_it_exits_78(
        self, source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "no-lake-corpus"
        _materialize(source_root, root, capsys)
        code = _exit_code(embed, prune_orphans=True, corpus_root=root, fmt=OutputFormat.JSON)
        assert code == EXIT_CODES["lake_unavailable"]

    def test_pending_lake_writes_block_a_delete(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _embed_all(corpus, capsys)
        (corpus / "sink_pending.json").write_text(
            json.dumps({"session_ids": [SESSION_A]}), encoding="utf-8"
        )
        code = _exit_code(
            embed, prune_orphans=True, dry_run=False, corpus_root=corpus, fmt=OutputFormat.JSON
        )
        assert code == EXIT_CODES["lake_unavailable"]


def _search(corpus: Path, capsys: pytest.CaptureFixture[str], **kw: Any) -> list[dict[str, Any]]:
    search("anything", corpus_root=corpus, fmt=OutputFormat.JSON, **kw)
    rows = _json(capsys)
    assert isinstance(rows, list)
    return rows


class TestSearchOverTheLake:
    def test_lake_and_per_session_search_agree(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _embed_all(corpus, capsys)

        # A uuid two sessions share joins both steps at one similarity, and
        # the order within such a tie is DuckDB's to pick, on either path.
        def ranked(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
            return sorted(
                (-row["sim"], row["uuid"], row["session_id"], row["snippet"]) for row in rows
            )

        lake_hits = _search(corpus, capsys, k=50)
        assert lake_hits
        assert ranked(lake_hits) == ranked(_search(corpus, capsys, k=50, lake=False))
        assert ranked(_search(corpus, capsys, session_id=SESSION_B)) == ranked(
            _search(corpus, capsys, session_id=SESSION_B, lake=False)
        )

    def test_all_corpora_searches_every_corpus_store(
        self,
        corpus: Path,
        source_root: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        other_source = tmp_path / "src-two" / "projects"
        _write_session(other_source, SESSION_C, _session_c(SESSION_C))
        other = tmp_path / "corpus-two"
        _materialize(other_source, other, capsys)
        rebuild(corpus_root=[corpus, other], fmt=OutputFormat.JSON)
        capsys.readouterr()
        del source_root
        _embed_all(corpus, capsys)
        _embed_all(other, capsys)
        hits = _search(corpus, capsys, k=50, all_corpora=True)
        assert {hit["corpus"] for hit in hits} == {"corpus-one", "corpus-two"}
        one = _search(corpus, capsys, k=50)
        two = _search(other, capsys, k=50)
        assert len(hits) == len(one) + len(two)
        query(
            "SELECT count(*) AS n FROM semantic_search((SELECT embedding FROM message_embeddings "
            "LIMIT 1), 100)",
            corpus_root=corpus,
            all_corpora=True,
            fmt=OutputFormat.JSON,
        )
        stored = len(get_embedded_hashes(_store(corpus))) + len(get_embedded_hashes(_store(other)))
        assert _json(capsys) == [{"n": stored}]

    def test_all_corpora_needs_the_lake(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _embed_all(corpus, capsys)
        code = _exit_code(
            search, "x", corpus_root=corpus, all_corpora=True, lake=False, fmt=OutputFormat.JSON
        )
        assert code == EXIT_CODES["invalid_input"]


class TestCompactBudget:
    def test_an_explicit_budget_replaces_the_host_sizing(
        self, corpus: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from atif_duck.infrastructure import lake as lake_infra

        del corpus
        seen: list[object] = []
        real = lake_infra.compact_lake

        def recording(*args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs["memory_limit_bytes"])
            return real(*args, **kwargs)

        monkeypatch.setattr("atif_duck.infrastructure.lake.compact_lake", recording)
        monkeypatch.setattr("atif_cli.lake.writer_memory_limit", lambda: 512 * 1024**2)
        compact(memory_limit="1GiB", fmt=OutputFormat.JSON)
        assert _json(capsys)["memory_limit_bytes"] == 1024**3
        compact(fmt=OutputFormat.JSON)
        assert _json(capsys)["memory_limit_bytes"] == 512 * 1024**2
        assert seen == [1024**3, 512 * 1024**2]

    def test_the_writer_ceiling_still_applies(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        del corpus
        compact(memory_limit="64GiB", fmt=OutputFormat.JSON)
        assert _json(capsys)["memory_limit_bytes"] == 2 * 1024**3

    @pytest.mark.parametrize("budget", ["lots", "0GiB"])
    def test_a_malformed_budget_exits_64(self, corpus: Path, budget: str) -> None:
        del corpus
        code = _exit_code(compact, memory_limit=budget, fmt=OutputFormat.JSON)
        assert code == EXIT_CODES["invalid_input"]

    def test_a_merge_that_runs_out_of_memory_exits_70(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import duckdb

        del corpus

        def out_of_memory(*args: object, **kwargs: object) -> None:
            del args, kwargs
            msg = "Out of Memory Error: could not allocate block"
            raise duckdb.OutOfMemoryException(msg)

        monkeypatch.setattr("atif_duck.infrastructure.lake.compact_lake", out_of_memory)
        code = _exit_code(compact, memory_limit="512MiB", fmt=OutputFormat.JSON)
        assert code == EXIT_CODES["runtime_error"]
