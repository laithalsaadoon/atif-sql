# SPDX-License-Identifier: Apache-2.0

"""The lake discovery reader, its watermark, the shared selection rules, and orphan pruning.

Everything runs over an in-memory :class:`FakeLake` (the port atif-cli
implements over the real lake), so each rule is pinned without DuckDB. The
real-lake composition is covered in atif-cli's ``test_embed_lake.py``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import pytest
from embed_fixtures import FakeEmbedder

from atif_embed.application.embed import run_backfill
from atif_embed.application.prune import prune_orphans
from atif_embed.domain.discovery import PendingSelection
from atif_embed.domain.ports import LakePosition, LakeStepSnapshot, LakeStepsPort
from atif_embed.domain.text_stamp import text_hash
from atif_embed.infrastructure.corpus_text_rows import MIN_TEXT_CHARS, DuckDbTextRows
from atif_embed.infrastructure.lake_text_rows import (
    LAKE_WATERMARK_FILE,
    WATERMARK_VERSION,
    LakeTextRows,
    read_watermark,
    write_watermark,
)
from atif_embed.infrastructure.lance_store import LanceVectorStore
from atif_embed.infrastructure.settings import EmbedSettings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

DIM = 8

ROWS: list[tuple[str, str]] = [
    ("u1", "the first step text, long enough to embed"),
    ("u2", "the second step text, long enough to embed"),
    ("u1", "a later repeat of u1 whose text must lose to the first"),
    ("u3", "the third step text, long enough to embed"),
]


@dataclass
class FakeSnapshot:
    lake: FakeLake

    @property
    def position(self) -> LakePosition:
        return LakePosition(lineage=self.lake.lineage, snapshot_id=self.lake.snapshot_id)

    def can_read_changes_since(self, since: LakePosition) -> bool:
        return since.lineage == self.lake.lineage and since.snapshot_id + 1 >= self.lake.oldest

    def step_texts(
        self, *, min_chars: int, changed_since: LakePosition | None
    ) -> Iterator[tuple[str, str]]:
        self.lake.reads.append(changed_since)
        assert min_chars == MIN_TEXT_CHARS
        if changed_since is None:
            yield from self.lake.rows
        else:
            yield from (row for row in self.lake.rows if row[0] in self.lake.changed)

    def step_keys(self) -> frozenset[str]:
        return frozenset(uuid for uuid, _ in self.lake.rows) | self.lake.short_keys

    def close(self) -> None:
        self.lake.closed += 1

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


@dataclass
class FakeLake:
    rows: list[tuple[str, str]] = field(default_factory=lambda: list(ROWS))
    lineage: str = "L1"
    snapshot_id: int = 5
    oldest: int = 1
    changed: set[str] = field(default_factory=set)
    short_keys: frozenset[str] = frozenset()
    present: bool = True
    reads: list[LakePosition | None] = field(default_factory=list)
    closed: int = 0

    def open_steps(self, corpus_root: Path) -> FakeSnapshot | None:
        del corpus_root
        return FakeSnapshot(self) if self.present else None


class RecordingFallback:
    discovery = "corpus"

    def __init__(self) -> None:
        self.calls = 0
        self.commits: list[int] = []

    def iter_unembedded(
        self, corpus_root: Path, *, embedded: dict[str, str] | None = None, limit: int | None = None
    ) -> Iterator[Any]:
        del corpus_root, embedded, limit
        self.calls += 1
        yield from ()

    def commit(self, *, stored_rows: int) -> None:
        self.commits.append(stored_rows)


def _reader(tmp_path: Path, lake: FakeLake) -> tuple[LakeTextRows, RecordingFallback]:
    fallback = RecordingFallback()
    return (
        LakeTextRows(
            lake=lake, watermark_path=tmp_path / "store" / LAKE_WATERMARK_FILE, fallback=fallback
        ),
        fallback,
    )


def _mark(tmp_path: Path, **overrides: Any) -> None:
    payload = {
        "version": WATERMARK_VERSION,
        "lineage": "L1",
        "snapshot_id": 3,
        "min_text_chars": MIN_TEXT_CHARS,
        "stored_rows": 0,
    }
    payload.update(overrides)
    write_watermark(tmp_path / "store" / LAKE_WATERMARK_FILE, payload)


def _uuids(rows: LakeTextRows, **kw: Any) -> list[str]:
    return [p.uuid for p in rows.iter_unembedded(Path("corpus"), **kw)]


class TestSelection:
    def test_first_row_wins_same_hash_skips_other_hash_replaces(self) -> None:
        embedded = {"u2": text_hash(ROWS[1][1]), "u3": "stale"}
        picked = list(PendingSelection(embedded=embedded, limit=None).select(ROWS))
        assert [(p.uuid, p.text, p.replaces_existing) for p in picked] == [
            ("u1", ROWS[0][1], False),
            ("u3", ROWS[3][1], True),
        ]

    def test_limit_counts_after_the_store_filter_and_marks_the_pass_incomplete(self) -> None:
        selection = PendingSelection(embedded={"u1": text_hash(ROWS[0][1])}, limit=1)
        assert [p.uuid for p in selection.select(ROWS)] == ["u2"]
        assert selection.exhausted is False
        complete = PendingSelection(embedded=None, limit=10)
        assert len(list(complete.select(ROWS))) == 3
        assert complete.exhausted is True

    def test_the_corpus_reader_and_the_lake_reader_share_the_rules(
        self, corpus_root: Path, tmp_path: Path
    ) -> None:
        corpus = [(p.uuid, p.text) for p in DuckDbTextRows().iter_unembedded(corpus_root)]
        lake = FakeLake(rows=corpus + corpus[:1])
        rows, _ = _reader(tmp_path, lake)
        assert _uuids(rows) == [uuid for uuid, _ in corpus]


class TestLakeTextRows:
    def test_no_lake_falls_back_and_commit_goes_to_the_fallback(self, tmp_path: Path) -> None:
        rows, fallback = _reader(tmp_path, FakeLake(present=False))
        assert _uuids(rows) == []
        assert (rows.discovery, fallback.calls) == ("corpus", 1)
        rows.commit(stored_rows=7)
        assert fallback.commits == [7]
        assert read_watermark(tmp_path / "store" / LAKE_WATERMARK_FILE) is None

    def test_no_watermark_reads_in_full_and_a_complete_pass_commits(self, tmp_path: Path) -> None:
        lake = FakeLake()
        rows, _ = _reader(tmp_path, lake)
        assert _uuids(rows) == ["u1", "u2", "u3"]
        assert (rows.discovery, lake.reads, lake.closed) == ("lake-full", [None], 1)
        rows.commit(stored_rows=3)
        mark = read_watermark(tmp_path / "store" / LAKE_WATERMARK_FILE)
        assert mark == {
            "version": WATERMARK_VERSION,
            "lineage": "L1",
            "snapshot_id": 5,
            "min_text_chars": MIN_TEXT_CHARS,
            "stored_rows": 3,
        }

    def test_a_pass_cut_short_by_limit_commits_nothing(self, tmp_path: Path) -> None:
        rows, _ = _reader(tmp_path, FakeLake())
        assert _uuids(rows, limit=1) == ["u1"]
        rows.commit(stored_rows=1)
        assert read_watermark(tmp_path / "store" / LAKE_WATERMARK_FILE) is None

    def test_a_usable_watermark_reads_only_the_changes(self, tmp_path: Path) -> None:
        _mark(tmp_path, stored_rows=2)
        lake = FakeLake(changed={"u3"})
        rows, _ = _reader(tmp_path, lake)
        embedded = {"u1": text_hash(ROWS[0][1]), "u2": text_hash(ROWS[1][1])}
        assert _uuids(rows, embedded=embedded) == ["u3"]
        assert rows.discovery == "lake-incremental"
        assert lake.reads == [LakePosition(lineage="L1", snapshot_id=3)]

    def test_a_watermark_at_the_current_snapshot_reads_nothing(self, tmp_path: Path) -> None:
        _mark(tmp_path, snapshot_id=5)
        lake = FakeLake()
        rows, _ = _reader(tmp_path, lake)
        assert _uuids(rows) == []
        assert (rows.discovery, lake.reads) == ("lake-unchanged", [])

    @pytest.mark.parametrize(
        ("overrides", "lake_kw"),
        [
            pytest.param({"lineage": "L0"}, {}, id="rebuilt-lake"),
            pytest.param({"stored_rows": 99}, {}, id="store-count-moved"),
            pytest.param({"min_text_chars": MIN_TEXT_CHARS + 1}, {}, id="text-floor-changed"),
            pytest.param({"version": WATERMARK_VERSION + 1}, {}, id="other-version"),
            pytest.param({"snapshot_id": 9}, {}, id="ahead-of-the-lake"),
            pytest.param({}, {"oldest": 5}, id="snapshots-expired"),
        ],
    )
    def test_an_untrustworthy_watermark_reads_in_full(
        self, tmp_path: Path, overrides: dict[str, Any], lake_kw: dict[str, Any]
    ) -> None:
        _mark(tmp_path, **overrides)
        lake = FakeLake(changed={"u3"}, **lake_kw)
        rows, _ = _reader(tmp_path, lake)
        assert _uuids(rows) == ["u1", "u2", "u3"]
        assert (rows.discovery, lake.reads) == ("lake-full", [None])

    def test_an_unreadable_watermark_reads_in_full(self, tmp_path: Path) -> None:
        path = tmp_path / "store" / LAKE_WATERMARK_FILE
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        rows, _ = _reader(tmp_path, FakeLake())
        assert _uuids(rows) == ["u1", "u2", "u3"]
        assert rows.discovery == "lake-full"

    def test_the_port_types_are_satisfied(self) -> None:
        lake: LakeStepsPort = FakeLake()
        snapshot: LakeStepSnapshot | None = lake.open_steps(Path("c"))
        assert snapshot is not None


class TestBackfillCommits:
    def _run(self, tmp_path: Path, rows: LakeTextRows, **kw: Any) -> Any:
        settings = EmbedSettings(lance_uri=tmp_path / "lance", batch_size=2, embed_concurrency=2)
        return asyncio.run(
            run_backfill(
                corpus_root=Path("corpus"),
                settings=settings,
                embedder=kw.pop("embedder", FakeEmbedder(dim=DIM)),
                text_rows=rows,
                store=LanceVectorStore(tmp_path / "lance", dim=DIM),
                **kw,
            )
        )

    def test_a_complete_run_commits_the_store_count(self, tmp_path: Path) -> None:
        rows, _ = _reader(tmp_path, FakeLake())
        assert self._run(tmp_path, rows) == 3
        mark = read_watermark(tmp_path / "store" / LAKE_WATERMARK_FILE)
        assert mark is not None
        assert mark["stored_rows"] == 3

    def test_a_run_with_nothing_pending_still_commits(self, tmp_path: Path) -> None:
        rows, _ = _reader(tmp_path, FakeLake(rows=[]))
        assert self._run(tmp_path, rows) == 0
        mark = read_watermark(tmp_path / "store" / LAKE_WATERMARK_FILE)
        assert mark is not None
        assert mark["stored_rows"] == 0

    def test_a_dry_run_never_commits(self, tmp_path: Path) -> None:
        rows, _ = _reader(tmp_path, FakeLake())
        plan = self._run(tmp_path, rows, dry_run=True)
        assert (plan["discovery"], plan["candidates"]) == ("lake-full", 3)
        assert read_watermark(tmp_path / "store" / LAKE_WATERMARK_FILE) is None

    def test_a_failed_row_keeps_the_watermark_where_it_was(self, tmp_path: Path) -> None:
        rows, _ = _reader(tmp_path, FakeLake())
        assert (
            self._run(tmp_path, rows, embedder=FakeEmbedder(dim=DIM, fail_texts={ROWS[1][1]})) == 2
        )
        assert read_watermark(tmp_path / "store" / LAKE_WATERMARK_FILE) is None


class _FakeStore:
    def __init__(self, uuids: list[str]) -> None:
        self.rows = dict.fromkeys(uuids, "h")
        self.deleted: list[list[str]] = []
        self.optimized = 0

    def get_embedded_hashes(self) -> dict[str, str]:
        return dict(self.rows)

    def delete_uuids(self, uuids: Any) -> int:
        names = list(uuids)
        self.deleted.append(names)
        for name in names:
            self.rows.pop(name, None)
        return len(names)

    def optimize(self) -> None:
        self.optimized += 1


class TestPruneOrphans:
    def test_a_dry_run_counts_and_deletes_nothing(self) -> None:
        store = _FakeStore(["u1", "u2", "gone"])
        report = prune_orphans(Path("c"), lake=FakeLake(), store=store, dry_run=True)  # type: ignore[arg-type]
        assert report is not None
        assert (report["stored"], report["orphans"], report["deleted"]) == (3, 1, 0)
        assert store.deleted == []

    def test_short_texts_are_steps_too(self) -> None:
        store = _FakeStore(["u1", "short"])
        lake = FakeLake(short_keys=frozenset({"short"}))
        report = prune_orphans(Path("c"), lake=lake, store=store, dry_run=True)  # type: ignore[arg-type]
        assert report is not None
        assert report["orphans"] == 0

    def test_a_real_run_deletes_exactly_the_orphans(self) -> None:
        store = _FakeStore(["u1", "gone-b", "gone-a", "u3"])
        report = prune_orphans(Path("c"), lake=FakeLake(), store=store, dry_run=False)  # type: ignore[arg-type]
        assert report is not None
        assert report["deleted"] == 2
        assert store.deleted == [["gone-a", "gone-b"]]
        assert set(store.rows) == {"u1", "u3"}

    def test_a_corpus_the_lake_holds_no_steps_for_is_refused(self) -> None:
        store = _FakeStore(["u1", "u2"])
        report = prune_orphans(Path("c"), lake=FakeLake(rows=[]), store=store, dry_run=False)  # type: ignore[arg-type]
        assert report is not None
        assert (report["orphans"], report["deleted"], "refused" in report) == (2, 0, True)
        assert store.deleted == []

    def test_no_lake_is_none(self) -> None:
        store = _FakeStore(["u1"])
        assert prune_orphans(Path("c"), lake=FakeLake(present=False), store=store) is None  # type: ignore[arg-type]


def test_the_watermark_is_written_atomically(tmp_path: Path) -> None:
    path = tmp_path / "store" / LAKE_WATERMARK_FILE
    write_watermark(path, {"version": WATERMARK_VERSION})
    assert json.loads(path.read_text(encoding="utf-8")) == {"version": WATERMARK_VERSION}
    assert [p.name for p in path.parent.iterdir()] == [LAKE_WATERMARK_FILE]
