# SPDX-License-Identifier: Apache-2.0

"""DuckDbTextRows: contract-layout selection, keying, and staleness semantics."""

from __future__ import annotations

import json
import tracemalloc
from pathlib import Path
from typing import Any, TypedDict, Unpack

import pytest
from embed_fixtures import (
    ARRAY_PART_ONE,
    ARRAY_PART_TWO,
    EXPECTED_EMBEDDABLE_UUIDS,
    LONG_SIDECHAIN_TEXT,
    LONG_USER_TEXT,
    rewrite_step_text,
)

from atif_embed.domain.text_stamp import PendingText, text_hash
from atif_embed.infrastructure import corpus_text_rows
from atif_embed.infrastructure.corpus_text_rows import DuckDbTextRows


class _SelectionOverrides(TypedDict, total=False):
    """The `iter_unembedded` keywords these tests vary, with its own types."""

    embedded: dict[str, str]
    limit: int


def _rows(corpus_root: Path, **kwargs: Unpack[_SelectionOverrides]) -> list[PendingText]:
    return list(DuckDbTextRows().iter_unembedded(corpus_root, **kwargs))


#: Steps / text width of one residency-corpus session. Fat enough that one
#: session's texts and a many-session corpus's texts differ by an order of
#: magnitude, small enough to build and read in well under a second.
_RESIDENCY_SESSIONS = 12
_RESIDENCY_STEPS_PER_SESSION = 4
_RESIDENCY_TEXT_CHARS = 40_000

#: One session's total text characters — the claimed residency bound.
_SESSION_TEXT_CHARS = _RESIDENCY_STEPS_PER_SESSION * _RESIDENCY_TEXT_CHARS


#: Text width of a TINY session's steps — the many-small-sessions shape that
#: dominates a real corpus (its median trajectory.json is well under 1 MB).
_TINY_TEXT_CHARS = 200


def _write_tiny_corpus_of(root: Path, sessions: int) -> Path:
    """Write a corpus of ``sessions`` sessions far below the batch budget."""
    return _write_corpus_of(root, sessions, text_chars=_TINY_TEXT_CHARS)


def _write_corpus_of(root: Path, sessions: int, *, text_chars: int | None = None) -> Path:
    """Write a corpus of ``sessions`` uniform sessions."""
    chars = _RESIDENCY_TEXT_CHARS if text_chars is None else text_chars
    step = 0
    for s in range(sessions):
        session_id = f"{s:08d}-0000-0000-0000-000000000000"
        sdir = root / "sessions" / session_id
        sdir.mkdir(parents=True)
        steps: list[dict[str, Any]] = []
        for i in range(_RESIDENCY_STEPS_PER_SESSION):
            steps.append(
                {
                    "step_id": i + 1,
                    "timestamp": "2026-08-20T10:00:00.000Z",
                    "source": "agent",
                    # Distinct per step so no interning or sharing hides residency.
                    "message": f"{step:08d}" + "x" * (chars - 8),
                    "extra": {"is_sidechain": False, "source_uuids": [f"u-{step}"]},
                }
            )
            step += 1
        (sdir / "trajectory.json").write_text(
            json.dumps(
                {
                    "schema_version": "ATIF-v1.7",
                    "session_id": session_id,
                    "agent": {"name": "claude-code", "version": "2.1.218", "model_name": "m"},
                    "steps": steps,
                    "final_metrics": {"total_steps": len(steps)},
                },
                separators=(",", ":"),
            )
        )
        (sdir / "meta.json").write_text(json.dumps({"session_id": session_id}))
    return root


def _drain_peak(root: Path, **kwargs: Any) -> tuple[int, int]:
    """Return ``(text_chars_consumed, peak_traced_bytes)`` for a full drain.

    Rows are consumed and dropped, exactly as the backfill does per chunk, so
    the peak reflects what the ADAPTER holds rather than what the caller keeps.
    """
    tracemalloc.start()
    try:
        consumed = 0
        for pending in DuckDbTextRows().iter_unembedded(root, **kwargs):
            consumed += len(pending.text)
        return consumed, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


class _RecordingResult:
    """DuckDB result proxy recording which fetch shape the adapter uses."""

    def __init__(self, inner: Any, calls: dict[str, int]) -> None:
        self._inner = inner
        self._calls = calls

    def fetchmany(self, size: int) -> Any:
        self._calls["fetchmany"] += 1
        return self._inner.fetchmany(size)

    def fetchall(self) -> Any:
        self._calls["fetchall"] += 1
        return self._inner.fetchall()

    def fetch_arrow_table(self) -> Any:
        self._calls["fetch_arrow_table"] += 1
        return self._inner.fetch_arrow_table()

    def fetchdf(self) -> Any:
        self._calls["fetchdf"] += 1
        return self._inner.fetchdf()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _RecordingConnection:
    """DuckDB connection proxy counting ``execute`` calls and fetch shapes."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.executes = 0
        self.fetch_calls: dict[str, int] = dict.fromkeys(
            ("fetchmany", "fetchall", "fetch_arrow_table", "fetchdf"), 0
        )

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> _RecordingResult:
        self.executes += 1
        return _RecordingResult(self._inner.execute(sql, *args, **kwargs), self.fetch_calls)

    def close(self) -> None:
        self._inner.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture
def recording_duckdb(monkeypatch: pytest.MonkeyPatch) -> list[_RecordingConnection]:
    """Make the adapter's ``duckdb.connect`` hand back recording connections."""
    import duckdb

    made: list[_RecordingConnection] = []
    real_connect = duckdb.connect

    def fake_connect(*args: Any, **kwargs: Any) -> _RecordingConnection:
        con = _RecordingConnection(real_connect(*args, **kwargs))
        made.append(con)
        return con

    monkeypatch.setattr(duckdb, "connect", fake_connect)
    monkeypatch.setattr(corpus_text_rows, "_FETCH_PAGE_ROWS", 2, raising=True)
    return made


class TestSelection:
    def test_yields_expected_uuids_in_order(self, corpus_root: Path) -> None:
        assert [p.uuid for p in _rows(corpus_root)] == EXPECTED_EMBEDDABLE_UUIDS

    def test_short_texts_and_keyless_steps_are_skipped(self, corpus_root: Path) -> None:
        uuids = {p.uuid for p in _rows(corpus_root)}
        assert "short-1" not in uuids  # < 32 chars

    def test_sidechain_steps_are_included(self, corpus_root: Path) -> None:
        texts = {p.uuid: p.text for p in _rows(corpus_root)}
        assert texts["sa-1"] == LONG_SIDECHAIN_TEXT

    def test_uuid_is_first_source_uuid(self, corpus_root: Path) -> None:
        uuids = {p.uuid for p in _rows(corpus_root)}
        assert "ua-1" in uuids  # first entry of ["ua-1", "ua-1b"]
        assert "ua-1b" not in uuids

    def test_array_message_flattens_with_blank_lines(self, corpus_root: Path) -> None:
        texts = {p.uuid: p.text for p in _rows(corpus_root)}
        assert texts["uc-1"] == f"{ARRAY_PART_ONE}\n\n{ARRAY_PART_TWO}"

    def test_torn_session_dirs_are_excluded(self, corpus_root: Path) -> None:
        uuids = {p.uuid for p in _rows(corpus_root)}
        assert "torn-1" not in uuids  # session dir has no meta.json

    def test_empty_corpus_returns_empty(self, tmp_path: Path) -> None:
        assert _rows(tmp_path / "nowhere") == []


class TestResidency:
    """Peak resident text is ONE BATCH's, not the whole corpus's.

    A generator SHAPE proves nothing: an adapter that calls ``fetchall`` and
    yields from the resulting list is still a generator while holding every
    text. These tests measure allocated bytes and count the fetch shapes, so
    the shape alone cannot satisfy them.

    The bound is ``max(batch byte budget, largest single session)``. These
    tests pin the budget below one session's bytes so the corpora here span
    many batches, which is the regime where growth would show.
    """

    def test_peak_does_not_grow_with_corpus_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corpus 4x larger, peak resident text unchanged — that IS the bound.

        Comparing two drains cancels fixed costs (first-touch imports, DuckDB's
        per-connection arenas), which an absolute ceiling cannot separate from
        the text under measurement. A corpus-proportional adapter grows ~4x
        here; a batch-bounded one does not grow at all.
        """
        small = _write_corpus_of(tmp_path / "small", 3)
        large = _write_corpus_of(tmp_path / "large", 12)
        monkeypatch.setattr(corpus_text_rows, "_BATCH_MAX_BYTES", 1024, raising=True)

        _drain_peak(_write_corpus_of(tmp_path / "warmup", 1))  # first-touch costs

        small_text, small_peak = _drain_peak(small)
        large_text, large_peak = _drain_peak(large)

        assert small_text == 3 * _SESSION_TEXT_CHARS
        assert large_text == 12 * _SESSION_TEXT_CHARS
        # 4x the text must not buy even 1.5x the peak.
        assert large_peak < small_peak * 1.5
        # And the peak must stay near ONE session, not the 12 the drain read.
        assert large_peak < _SESSION_TEXT_CHARS * 3

    def test_statement_count_is_bounded_by_bytes_not_by_file_count(
        self, tmp_path: Path, recording_duckdb: list[_RecordingConnection]
    ) -> None:
        """Many small sessions must NOT cost one statement each.

        Statement count is what drives wall time: a per-file split over 8,000
        tiny sessions measured 57-64 s against 2.2-2.4 s for a single query.
        Batching by bytes must collapse a corpus whose every file is far under
        the budget into far fewer statements, while still reading every row.
        """
        sessions = 40
        root = _write_tiny_corpus_of(tmp_path / "tiny", sessions)
        rows = list(DuckDbTextRows().iter_unembedded(root))

        assert len(rows) == sessions * _RESIDENCY_STEPS_PER_SESSION
        executes = recording_duckdb[0].executes
        assert executes < sessions / 4, (
            f"{executes} statements for {sessions} tiny sessions — batching collapsed nothing"
        )

    def test_a_session_over_the_budget_is_not_batched_with_others(
        self,
        tmp_path: Path,
        recording_duckdb: list[_RecordingConnection],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The residency bound requires a fat session to hold a statement alone.

        With the budget set below one session's bytes, every session must get
        its own statement — that is the mechanism keeping peak near the largest
        session rather than near the whole corpus.
        """
        root = _write_corpus_of(tmp_path / "fat", 5)
        monkeypatch.setattr(corpus_text_rows, "_BATCH_MAX_BYTES", 1024, raising=True)

        rows = list(DuckDbTextRows().iter_unembedded(root))

        assert len(rows) == 5 * _RESIDENCY_STEPS_PER_SESSION
        assert recording_duckdb[0].executes == 5

    def test_batching_preserves_row_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Batch boundaries must not reorder rows, or --limit picks other rows.

        A batch reads several files in ONE query, so row order now rests on the
        SQL ordering rather than on the Python loop. The batched read must
        agree with a one-file-per-statement read of the same corpus.
        """
        root = _write_tiny_corpus_of(tmp_path / "tiny", 30)

        batched = [p.uuid for p in DuckDbTextRows().iter_unembedded(root)]

        monkeypatch.setattr(corpus_text_rows, "_BATCH_MAX_BYTES", 1, raising=True)
        per_file = [p.uuid for p in DuckDbTextRows().iter_unembedded(root)]

        assert batched == per_file
        assert len(batched) == 30 * _RESIDENCY_STEPS_PER_SESSION

    def test_whole_result_fetches_are_never_used(
        self, corpus_root: Path, recording_duckdb: list[_RecordingConnection]
    ) -> None:
        rows = list(DuckDbTextRows().iter_unembedded(corpus_root))
        assert [p.uuid for p in rows] == EXPECTED_EMBEDDABLE_UUIDS
        calls = recording_duckdb[0].fetch_calls
        assert calls["fetchmany"] > 0
        assert calls["fetchall"] == 0
        assert calls["fetch_arrow_table"] == 0
        assert calls["fetchdf"] == 0

    def test_limit_does_not_pay_for_sessions_past_the_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--limit 1`` on a 12-session corpus must cost like a 1-session read."""
        one = _write_corpus_of(tmp_path / "one", 1)
        many = _write_corpus_of(tmp_path / "many", 12)
        monkeypatch.setattr(corpus_text_rows, "_BATCH_MAX_BYTES", 1024, raising=True)

        _drain_peak(_write_corpus_of(tmp_path / "warmup", 1))  # first-touch costs

        _, one_peak = _drain_peak(one, limit=1)
        capped_text, capped_peak = _drain_peak(many, limit=1)

        assert capped_text == _RESIDENCY_TEXT_CHARS
        assert capped_peak < one_peak * 1.5


class TestStamping:
    def test_every_row_carries_the_hash_of_its_text(self, corpus_root: Path) -> None:
        for pending in _rows(corpus_root):
            assert pending.text_hash == text_hash(pending.text)

    def test_short_text_is_not_flagged_truncated(self, corpus_root: Path) -> None:
        assert all(not p.truncated for p in _rows(corpus_root))


class TestStaleness:
    def test_matching_hash_is_skipped(self, corpus_root: Path) -> None:
        embedded = {p.uuid: p.text_hash for p in _rows(corpus_root)}
        assert _rows(corpus_root, embedded=embedded) == []

    def test_changed_text_under_same_uuid_is_re_yielded(self, corpus_root: Path) -> None:
        """A re-converted step must not be skipped just because its uuid is known."""
        embedded = {p.uuid: p.text_hash for p in _rows(corpus_root)}
        rewrite_step_text(corpus_root, "ua-1", LONG_USER_TEXT + " Now with a corrected tail.")
        stale = _rows(corpus_root, embedded=embedded)
        assert [p.uuid for p in stale] == ["ua-1"]
        assert stale[0].replaces_existing is True
        assert stale[0].text.endswith("corrected tail.")

    def test_new_uuid_is_not_marked_as_replacing(self, corpus_root: Path) -> None:
        assert all(not p.replaces_existing for p in _rows(corpus_root, embedded={}))

    def test_unknown_hash_for_known_uuid_replaces(self, corpus_root: Path) -> None:
        stale = _rows(corpus_root, embedded={"ua-1": "0" * 32})
        by_uuid = {p.uuid: p for p in stale}
        assert by_uuid["ua-1"].replaces_existing is True
        assert by_uuid["aa-1"].replaces_existing is False


class TestExcludeAndLimit:
    def test_embedded_filter_is_applied_before_limit(self, corpus_root: Path) -> None:
        """--limit N must make N rows of forward progress past embedded rows."""
        all_rows = _rows(corpus_root)
        embedded = {p.uuid: p.text_hash for p in all_rows[:2]}
        rows = _rows(corpus_root, embedded=embedded, limit=2)
        assert [p.uuid for p in rows] == EXPECTED_EMBEDDABLE_UUIDS[2:4]

    def test_limit_caps_rows(self, corpus_root: Path) -> None:
        assert [p.uuid for p in _rows(corpus_root, limit=3)] == EXPECTED_EMBEDDABLE_UUIDS[:3]
