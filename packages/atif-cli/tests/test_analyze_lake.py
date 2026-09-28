# SPDX-License-Identifier: Apache-2.0

"""``analyze`` reads session data from the lake, and reads exactly what the files hold.

The corpus goes through the REAL converter and the lake is rebuilt over it,
because the question is whether a lake row reads back as the step the
pipelines would have parsed out of ``trajectory.json``. Built once for the
module (:func:`built`): every test here only reads it, except the one that
writes a session without the lake, which works on a copy.

What must be equal, source to source: every session's bounds (the
checkpoint compares them), its gate-level steps, its full steps (tool calls
and results in order), its raw-record uuids, and so every pipeline's dry-run
plan and every rendered transcript.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import shutil
import time
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import pytest
from cli_fixtures import write_synthetic_session
from loguru import logger

from atif_analytics.application.use_cases.classify import classify_sessions
from atif_analytics.application.use_cases.conflicts import detect_conflicts
from atif_analytics.application.use_cases.friction import detect_user_friction
from atif_analytics.application.use_cases.perceived import detect_perceived_errors
from atif_analytics.infrastructure.corpus_reader import CorpusReader, TrajectoryFileSource
from atif_analytics.infrastructure.settings import AnalyticsSettings
from atif_cli.app import analyze, materialize
from atif_cli.lake import rebuild
from atif_cli.lake_sessions import LakeSessionSource
from atif_duck.infrastructure.lake import LakeLayout, LakeUnavailable
from atif_models.domain.registry import resolve

pytestmark = pytest.mark.integration

RICH_A = "aaaaaaaa-0000-4000-8000-00000000000a"
RICH_B = "bbbbbbbb-0000-4000-8000-00000000000b"
PLAIN = "cccccccc-0000-4000-8000-00000000000c"

_STALE_NS = 3_600 * 1_000_000_000

_USE_CASE_MODULES = (
    "atif_analytics.application.use_cases.classify",
    "atif_analytics.application.use_cases.conflicts",
    "atif_analytics.application.use_cases.friction",
    "atif_analytics.application.use_cases.perceived",
)

_PIPELINES = {
    "classify": classify_sessions,
    "conflicts": detect_conflicts,
    "friction": detect_user_friction,
    "perceived": detect_perceived_errors,
}


def _record(session_id: str, n: int, kind: str, ts: str, message: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": kind,
        "uuid": f"{session_id[:8]}-{n:02d}",
        "parentUuid": f"{session_id[:8]}-{n - 1:02d}" if n > 1 else None,
        "sessionId": session_id,
        "timestamp": ts,
        "cwd": "/work/proj",
        "gitBranch": "main",
        "version": "2.0.0",
        "isSidechain": False,
        "message": message,
    }


def _assistant(session_id: str, n: int, ts: str, content: list[dict[str, Any]]) -> dict[str, Any]:
    record = _record(
        session_id,
        n,
        "assistant",
        ts,
        {
            "id": f"msg-{session_id[:8]}-{n}",
            "role": "assistant",
            "model": "claude-test-1",
            "content": content,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )
    record["requestId"] = f"req-{session_id[:8]}-{n}"
    return record


def write_rich_session(source_root: Path, session_id: str, day: int) -> None:
    """A session every gate has something to read in.

    Three parallel tool calls whose ids do NOT sort in call order (so a read
    that ordered them by id instead of source order would render them
    differently), one result an error followed by a short human question
    (friction rule 3), a short imperative (rule 2), a Stop hook message (a
    user-role step no human wrote), a list-content user message, non-ASCII
    arguments and a list-shaped result, and enough human-AI pairs for the
    perceived gate.
    """
    project = source_root / "-work-proj"
    project.mkdir(parents=True, exist_ok=True)
    stamp = f"2026-08-{day:02d}T09:00"
    ids = [f"toolu_{c}{session_id[:6]}" for c in ("c", "a", "b")]
    records = [
        _record(
            session_id,
            1,
            "user",
            f"{stamp}:01.250Z",
            {"role": "user", "content": "refactor the parser please"},
        ),
        _assistant(
            session_id,
            2,
            f"{stamp}:02.500Z",
            [
                {"type": "text", "text": "Looking at three files."},
                {"type": "tool_use", "id": ids[0], "name": "Read", "input": {"path": "café.py"}},
                {
                    "type": "tool_use",
                    "id": ids[1],
                    "name": "Grep",
                    "input": {"pattern": "def", "n": 1.5},
                },
                {"type": "tool_use", "id": ids[2], "name": "Bash", "input": {"command": "false"}},
            ],
        ),
        _record(
            session_id,
            3,
            "user",
            f"{stamp}:03.000Z",
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": ids[0], "content": "print('hi')"},
                    {
                        "type": "tool_result",
                        "tool_use_id": ids[1],
                        "content": [{"type": "text", "text": "parser.py:1: def parse"}],
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": ids[2],
                        "content": "exit 1",
                        "is_error": True,
                    },
                ],
            },
        ),
        _assistant(
            session_id, 4, f"{stamp}:04.000Z", [{"type": "text", "text": "The command failed."}]
        ),
        _record(session_id, 5, "user", f"{stamp}:05.000Z", {"role": "user", "content": "why?"}),
        _assistant(session_id, 6, f"{stamp}:06.000Z", [{"type": "text", "text": "It returned 1."}]),
        _record(
            session_id,
            7,
            "user",
            f"{stamp}:07.000Z",
            {"role": "user", "content": "Stop hook feedback: the claim has no evidence."},
        ),
        _assistant(
            session_id, 8, f"{stamp}:08.000Z", [{"type": "text", "text": "Here is the log."}]
        ),
        _record(
            session_id,
            9,
            "user",
            f"{stamp}:09.000Z",
            {"role": "user", "content": [{"type": "text", "text": "undo that"}]},
        ),
        _assistant(session_id, 10, f"{stamp}:10.000Z", [{"type": "text", "text": "Undone."}]),
    ]
    main = project / f"{session_id}.jsonl"
    main.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )
    stale = time.time_ns() - _STALE_NS
    os.utime(main, ns=(stale, stale))


@contextlib.contextmanager
def _env(lake_root: Path, base: Path) -> Generator[None]:
    with pytest.MonkeyPatch.context() as patch:
        for var in ("ATIF_SQL_CORPUS_ROOT", "ATIF_SQL_LANCE_URI", "ATIF_SQL_EMBED_MODEL_ID"):
            patch.delenv(var, raising=False)
        patch.setenv("ATIF_SQL_LAKE_ROOT", str(lake_root))
        patch.setenv("ATIF_SQL_CORPUS_BASE", str(base / "corpus-base"))
        yield


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    """``(source, corpus, lake root)``: three sessions materialized, the lake rebuilt over them."""
    base = tmp_path_factory.mktemp("analyze-lake")
    source = base / "projects"
    write_rich_session(source, RICH_A, day=20)
    write_rich_session(source, RICH_B, day=21)
    write_synthetic_session(source, PLAIN)
    corpus = base / "corpus"
    lake_root = base / "lake"
    with _env(lake_root, base), contextlib.redirect_stdout(io.StringIO()):
        materialize(source_root=source, corpus_root=corpus, fmt="json")  # type: ignore[arg-type]
        rebuild(corpus_root=[corpus], fmt="json")  # type: ignore[arg-type]
    return source, corpus, lake_root


@pytest.fixture
def lake_source(built: tuple[Path, Path, Path]) -> Iterator[LakeSessionSource]:
    _, corpus, lake_root = built
    opened = LakeSessionSource.open(LakeLayout(lake_root), corpus)
    assert isinstance(opened, LakeSessionSource), opened
    yield opened
    opened.close()


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every stage's provider is a stub: a dry run reads only the spec's prices."""
    spec = resolve("medium")

    def _build(settings: AnalyticsSettings, pipeline: str) -> tuple[Any, Any]:
        del settings, pipeline
        return object(), spec

    for module in _USE_CASE_MODULES:
        monkeypatch.setattr(f"{module}.build_provider", _build)


class TestTheLakeReadsWhatTheFilesHold:
    def test_the_fixture_orders_a_steps_results_against_their_ids(
        self, lake_source: LakeSessionSource
    ) -> None:
        """Without this the order checks below could pass on an id-sorted read."""
        steps = lake_source.load_steps([RICH_A])[RICH_A]
        call_ids = [call_id for step in steps for call_id, _ in step.tool_results]
        assert len(call_ids) == 3
        assert call_ids != sorted(call_ids)
        assert any(step.has_error_result for step in steps)

    def test_every_session_reads_the_same_from_both_sources(
        self, built: tuple[Path, Path, Path], lake_source: LakeSessionSource
    ) -> None:
        _, corpus, _ = built
        files = TrajectoryFileSource(corpus)
        bounds = files.session_bounds()
        assert set(bounds) == {RICH_A, RICH_B, PLAIN}
        assert lake_source.session_bounds() == bounds
        assert lake_source.from_files == ()
        ids = sorted(bounds)
        full = files.load_steps(ids)
        assert lake_source.load_steps(ids) == full
        turns = {
            sid: [dataclasses.replace(s, tool_calls=[], tool_results=[]) for s in steps]
            for sid, steps in full.items()
        }
        assert lake_source.load_turns(ids) == turns
        for sid in ids:
            assert lake_source.edges_uuids(sid) == files.edges_uuids(sid)

    def test_every_transcript_renders_the_same(
        self, built: tuple[Path, Path, Path], lake_source: LakeSessionSource
    ) -> None:
        _, corpus, _ = built
        on_files = CorpusReader(corpus)
        on_lake = CorpusReader(corpus, source=lake_source)
        ids = list(on_files.session_bounds(since_days=None))
        assert list(on_lake.session_bounds(since_days=None)) == ids
        for include_uuids in (False, True):
            texts = [on_files.session_text(sid, include_uuids=include_uuids) for sid in ids]
            assert list(on_lake.session_texts(ids, include_uuids=include_uuids)) == texts
            assert [on_lake.session_text(s, include_uuids=include_uuids) for s in ids] == texts

    @pytest.mark.parametrize("pipeline", sorted(_PIPELINES))
    def test_each_dry_run_plan_is_the_same(
        self,
        built: tuple[Path, Path, Path],
        lake_source: LakeSessionSource,
        fake_provider: None,
        pipeline: str,
    ) -> None:
        del fake_provider
        _, corpus, _ = built
        settings = AnalyticsSettings(corpus_root=corpus)
        run = _PIPELINES[pipeline]
        on_files = run(settings, since_days=None, dry_run=True, reader=CorpusReader(corpus))
        on_lake = run(
            settings,
            since_days=None,
            dry_run=True,
            reader=CorpusReader(corpus, source=lake_source),
        )
        assert isinstance(on_files, dict)
        assert on_files["capped_candidates"] > 0, "the fixture must give every pipeline work"
        assert on_lake == on_files


class TestAnalyzeCommand:
    def _analyze(
        self, built: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str], **kw: Any
    ) -> dict[str, Any]:
        _, corpus, lake_root = built
        with _env(lake_root, corpus.parent):
            analyze(corpus_root=corpus, since_days=None, fmt="json", **kw)  # type: ignore[arg-type]
        return json.loads(capsys.readouterr().out)

    def test_analyze_reads_the_lake_and_plans_what_the_files_plan(
        self,
        built: tuple[Path, Path, Path],
        fake_provider: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        del fake_provider
        on_lake = self._analyze(built, capsys)
        on_files = self._analyze(built, capsys, lake=False)
        assert on_lake.pop("session_source") == "lake"
        assert on_lake.pop("sessions_read_from_files") == 0
        assert on_lake.pop("lake_read_failed") is False
        assert on_files.pop("session_source") == "files"
        assert "sessions_read_from_files" not in on_files
        assert on_lake == on_files

    def test_without_a_lake_analyze_warns_once_and_reads_the_files(
        self,
        built: tuple[Path, Path, Path],
        fake_provider: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        del fake_provider
        _, corpus, _ = built
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
        try:
            with _env(tmp_path / "no-lake", tmp_path):
                analyze(corpus_root=corpus, since_days=None, fmt="json")  # type: ignore[arg-type]
        finally:
            logger.remove(sink)
        assert json.loads(capsys.readouterr().out)["session_source"] == "files"
        fallbacks = [line for line in lines if "reading the per-session artifacts" in line]
        assert len(fallbacks) == 1
        assert "no lake" in fallbacks[0]


def _own_lake(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``(source, corpus, lake root)`` for a test that writes the corpus or the lake."""
    source, corpus, lake = tmp_path / "projects", tmp_path / "corpus", tmp_path / "lake"
    write_rich_session(source, RICH_A, day=20)
    write_rich_session(source, RICH_B, day=21)
    with _env(lake, tmp_path), contextlib.redirect_stdout(io.StringIO()):
        materialize(source_root=source, corpus_root=corpus, fmt="json")  # type: ignore[arg-type]
        rebuild(corpus_root=[corpus], fmt="json")  # type: ignore[arg-type]
    return source, corpus, lake


def _open(lake: Path, corpus: Path) -> LakeSessionSource:
    opened = LakeSessionSource.open(LakeLayout(lake), corpus)
    assert isinstance(opened, LakeSessionSource), opened
    return opened


class TestALaggingLakeNeverServesAnOldWrite:
    def test_a_session_written_without_the_lake_is_read_from_its_files(
        self, tmp_path: Path
    ) -> None:
        """``materialize --no-lake`` rewrites a session; the lake still holds the old rows."""
        source, corpus, lake = _own_lake(tmp_path)
        main = source / "-work-proj" / f"{RICH_B}.jsonl"
        extra = _record(
            RICH_B,
            11,
            "user",
            "2026-08-21T09:00:11.000Z",
            {"role": "user", "content": "one more thing"},
        )
        with main.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(extra) + "\n")
        stale = time.time_ns() - _STALE_NS + 1_000_000_000
        os.utime(main, ns=(stale, stale))
        with _env(lake, tmp_path), contextlib.redirect_stdout(io.StringIO()):
            materialize(source_root=source, corpus_root=corpus, lake=False, fmt="json")  # type: ignore[arg-type]

        opened = _open(lake, corpus)
        try:
            files = TrajectoryFileSource(corpus)
            assert opened.session_bounds() == files.session_bounds()
            assert opened.from_files == (RICH_B,)
            ids = [RICH_A, RICH_B]
            assert opened.load_steps(ids) == files.load_steps(ids)
            assert opened.load_steps([RICH_B])[RICH_B][-1].text == "one more thing"
            assert opened.edges_uuids(RICH_B) == files.edges_uuids(RICH_B)
            # The fallback is parsed only on request, and its turns drop the
            # payloads like the lake's do.
            assert (opened.batchable(RICH_A), opened.batchable(RICH_B)) == (True, False)
            turns = opened.load_turns([RICH_B])[RICH_B]
            assert turns and all(not s.tool_calls and not s.tool_results for s in turns)
            # And through the reader, a batch mixing the two reads as the files do.
            on_files, on_lake = CorpusReader(corpus), CorpusReader(corpus, source=opened)
            assert list(on_lake.session_bounds(since_days=None)) == list(
                on_files.session_bounds(since_days=None)
            )
            for sid in ids:
                assert on_lake.load_steps(sid) == [
                    dataclasses.replace(s, tool_calls=[], tool_results=[])
                    for s in on_files.load_steps(sid)
                ]
            assert list(on_lake.session_texts(ids, include_uuids=True)) == [
                on_files.session_text(sid, include_uuids=True) for sid in ids
            ]
        finally:
            opened.close()

    def test_a_lake_that_does_not_hold_the_corpus_is_not_used(
        self, built: tuple[Path, Path, Path], tmp_path: Path
    ) -> None:
        _, corpus, lake_root = built
        other = tmp_path / corpus.name
        shutil.copytree(corpus, other)
        opened = LakeSessionSource.open(LakeLayout(lake_root), other)
        assert isinstance(opened, LakeUnavailable)
        assert str(other) in opened.reason


class TestALakeRebuiltMidRun:
    def test_a_rebuild_under_an_open_source_falls_back_to_the_files(self, tmp_path: Path) -> None:
        """``lake rebuild`` swaps the lake out while a long ``analyze`` holds the old attach."""
        _, corpus, lake = _own_lake(tmp_path)
        opened = _open(lake, corpus)
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
        try:
            on_lake, on_files = CorpusReader(corpus, source=opened), CorpusReader(corpus)
            ids = list(on_lake.session_bounds(since_days=None))
            assert opened.from_files == ()
            with _env(lake, tmp_path), contextlib.redirect_stdout(io.StringIO()):
                rebuild(corpus_root=[corpus], fmt="json")  # type: ignore[arg-type]
            texts = list(on_lake.session_texts(ids, include_uuids=True))
            assert opened.failed, "the rebuild must break the open attach for this to test anything"
            assert texts == [on_files.session_text(sid, include_uuids=True) for sid in ids]
            assert [on_lake.load_steps(sid) for sid in ids] == [
                on_files.load_steps(sid) for sid in ids
            ]
            assert opened.edges_uuids(ids[0]) == on_files.edges_uuids(ids[0])
            assert on_lake.session_bounds(since_days=None) == on_files.session_bounds(
                since_days=None
            )
            assert set(opened.from_files) == set(ids)
        finally:
            logger.remove(sink)
            opened.close()
        assert len([line for line in lines if "a lake read failed" in line]) == 1
