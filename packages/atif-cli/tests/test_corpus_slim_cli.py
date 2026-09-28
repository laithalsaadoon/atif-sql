# SPDX-License-Identifier: Apache-2.0

"""``atif-sql corpus slim``: an old-layout corpus to the compressed layout, losing nothing.

The corpus here is built the way a pre-upgrade corpus looks (plain JSON plus
the five per-session parquet files, through ``to_legacy_layout``) and the lake
is rebuilt from it, as the live lake was. Slim must leave every query answer,
the lake and the analytics bounds exactly as they were.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cli_fixtures import to_legacy_layout, write_synthetic_session
from test_lake_cli import _append_turn
from test_storage_layout_cli import NEW_LAYOUT, PANEL_QUERIES

from atif_analytics.infrastructure.corpus_reader import TrajectoryFileSource
from atif_cli.app import materialize, query, status
from atif_cli.corpus import slim
from atif_cli.errors import EXIT_CODES
from atif_cli.lake import rebuild, verify

SESSION_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

pytestmark = pytest.mark.integration


def _json(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out)


def _run(fn: Any, capsys: pytest.CaptureFixture[str], **kwargs: Any) -> tuple[int, Any]:
    try:
        fn(fmt="json", **kwargs)
    except SystemExit as exc:
        return int(exc.code or 0), _json(capsys)
    return 0, _json(capsys)


def _tree(corpus: Path) -> dict[str, str]:
    return {
        str(p.relative_to(corpus)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((corpus / "sessions").rglob("*"))
        if p.is_file()
    }


def _answers(corpus: Path, capsys: pytest.CaptureFixture[str]) -> list[str]:
    out: list[str] = []
    for lake in (True, False):
        for sql in PANEL_QUERIES:
            query(sql, corpus_root=corpus, lake=lake, fmt="json")  # type: ignore[arg-type]
            out.append(capsys.readouterr().out)
    return out


@pytest.fixture
def source_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("ATIF_SQL_CORPUS_ROOT", "ATIF_SQL_LANCE_URI", "ATIF_SQL_EMBED_MODEL_ID"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "projects"
    write_synthetic_session(root, SESSION_A)
    write_synthetic_session(root, SESSION_B)
    return root


@pytest.fixture
def legacy_corpus(source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """Two sessions in the old layout, and a lake rebuilt from them."""
    corpus = tmp_path / "corpus"
    materialize(source_root=source_root, corpus_root=corpus, fmt="json")  # type: ignore[arg-type]
    capsys.readouterr()
    for session_id in (SESSION_A, SESSION_B):
        to_legacy_layout(corpus, session_id)
    code, _ = _run(rebuild, capsys, corpus_root=[corpus])
    assert code == 0
    return corpus


def test_a_dry_run_changes_nothing_and_predicts_the_bytes(
    legacy_corpus: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _tree(legacy_corpus)
    code, report = _run(slim, capsys, corpus_root=[legacy_corpus])
    assert code == 0
    assert report["dry_run"] is True
    assert _tree(legacy_corpus) == before
    (row,) = report["corpora"]
    assert (row["sessions"], row["legacy_sessions"]) == (2, 2)
    assert (row["plain_files"], row["parquet_files"]) == (6, 10)
    assert row["parquet_action"] == "would_delete"
    assert 0 < row["compressed_bytes"] < row["plain_bytes"]
    assert row["bytes_freed"] == row["plain_bytes"] - row["compressed_bytes"] + row["parquet_bytes"]

    code, done = _run(slim, capsys, corpus_root=[legacy_corpus], dry_run=False)
    assert code == 0
    (acted,) = done["corpora"]
    # The dry run's numbers were exact.
    for key in ("plain_bytes", "compressed_bytes", "parquet_bytes", "bytes_after", "bytes_freed"):
        assert acted[key] == row[key], key


def test_slim_keeps_every_answer_the_lake_and_the_analytics_bounds(
    legacy_corpus: Path, source_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = _answers(legacy_corpus, capsys)
    bounds = TrajectoryFileSource(legacy_corpus).session_bounds()
    steps = TrajectoryFileSource(legacy_corpus).load_steps(sorted(bounds))

    code, report = _run(slim, capsys, corpus_root=[legacy_corpus], dry_run=False)
    assert code == 0, report
    (row,) = report["corpora"]
    assert row["parquet_action"] == "deleted"
    assert row["bytes_after"] < row["bytes_before"]
    for session_id in (SESSION_A, SESSION_B):
        session_dir = legacy_corpus / "sessions" / session_id
        assert sorted(p.name for p in session_dir.iterdir()) == NEW_LAYOUT

    assert _answers(legacy_corpus, capsys) == answers
    assert TrajectoryFileSource(legacy_corpus).session_bounds() == bounds
    assert TrajectoryFileSource(legacy_corpus).load_steps(sorted(bounds)) == steps
    assert _run(verify, capsys, corpus_root=[legacy_corpus])[0] == 0
    status(source_root=source_root, corpus_root=legacy_corpus, fmt="json")  # type: ignore[arg-type]
    assert _json(capsys)["layout"]["legacy_sessions"] == 0

    # Nothing left to do.
    code, again = _run(slim, capsys, corpus_root=[legacy_corpus], dry_run=False)
    assert code == 0
    assert (again["corpora"][0]["plain_files"], again["corpora"][0]["parquet_files"]) == (0, 0)


def test_a_materialize_tick_after_slim_keeps_the_lake_equal_to_the_corpus(
    legacy_corpus: Path, source_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(slim, capsys, corpus_root=[legacy_corpus], dry_run=False)
    _append_turn(source_root, SESSION_A)
    materialize(source_root=source_root, corpus_root=legacy_corpus, fmt="json")  # type: ignore[arg-type]
    report = _json(capsys)
    assert (report["materialized"], report["lake_synced"], report["lake_pending"]) == (1, 1, 0)
    assert _run(verify, capsys, corpus_root=[legacy_corpus])[0] == 0
    query(
        "SELECT count(*) AS n FROM steps WHERE message = 'one more thing'",
        corpus_root=legacy_corpus,
        fmt="json",  # type: ignore[arg-type]
    )
    assert _json(capsys) == [{"n": 1}]


def test_without_a_lake_it_compresses_but_keeps_the_parquet(
    source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "corpus"
    materialize(source_root=source_root, corpus_root=corpus, lake=False, fmt="json")  # type: ignore[arg-type]
    capsys.readouterr()
    to_legacy_layout(corpus, SESSION_A)
    code, report = _run(slim, capsys, corpus_root=[corpus], dry_run=False)
    assert code == EXIT_CODES["lake_unavailable"]
    (row,) = report["corpora"]
    assert (row["plain_files"], row["parquet_action"]) == (3, "kept")
    assert "no lake" in row["parquet_note"]
    assert (corpus / "sessions" / SESSION_A / "steps.parquet").is_file()
    assert (corpus / "sessions" / SESSION_A / "trajectory.json.zst").is_file()


def test_a_session_that_differs_from_its_lake_rows_keeps_the_parquet(
    legacy_corpus: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The verify must read the trajectory: its parquet still matches the lake."""
    path = legacy_corpus / "sessions" / SESSION_B / "trajectory.json"
    trajectory = json.loads(path.read_text())
    trajectory["steps"] = trajectory["steps"][:-1]
    path.write_text(json.dumps(trajectory, separators=(",", ":")) + "\n")
    assert _run(verify, capsys, corpus_root=[legacy_corpus])[0] == 0
    code, report = _run(slim, capsys, corpus_root=[legacy_corpus], dry_run=False)
    assert code == EXIT_CODES["lake_mismatch"]
    (row,) = report["corpora"]
    assert row["parquet_action"] == "kept"
    assert SESSION_B in row["parquet_note"]
    assert (legacy_corpus / "sessions" / SESSION_B / "steps.parquet").is_file()
