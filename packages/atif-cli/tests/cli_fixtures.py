# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for atif-cli tests: tiny synthetic Claude Code sessions.

``write_synthetic_session`` builds one minimal-but-convertible session
(user -> assistant+tool_use -> tool_result -> assistant) under a
``projects/<proj>/`` source root — enough raw shape for the REAL converter
(harbor) to produce a valid trajectory, which is what the integration test
materializes end-to-end. Mtimes are backdated so the sessions are quiescent
at the default 300s policy.

``write_analytics_parquets`` adds the analytics artifacts a corpus only has
once ``atif-sql analyze`` has run. They are what makes the query sandbox's
file allowlist observable: the session artifacts land in TEMP TABLEs at
register time and keep working with no allowlist at all, while an analytics
view is ``read_parquet`` evaluated when the caller selects from it. A corpus
without them cannot tell an armed allowlist from a missing one.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

#: One hour ago in epoch ns — comfortably quiescent at the default 300s.
_STALE_OFFSET_NS = 3_600 * 1_000_000_000


# An autouse fixture reaches its tests through pytest's registration, never
# through a reference a checker can see, so `reportUnusedFunction` is suppressed
# here rather than answered by a rename — the name IS the fixture's identity.
@pytest.fixture(autouse=True)
def _quiet_loguru() -> None:  # pyright: ignore[reportUnusedFunction]
    """Drop loguru's default DEBUG stderr sink for every atif-cli test.

    Mirrors ``main()``'s sink discipline. Without this the tests are
    ORDER-DEPENDENT: loguru's default sink binds to whatever ``sys.stderr``
    is live at first import, so when a command body's deferred import pulls
    loguru in mid-test, DEBUG registration logs land inside capsys's stderr
    and corrupt tests that parse the JSON error envelope from stderr
    (seen: test_vss_commands run as a lone file fails; full suite passes).
    """
    logger.remove()


@pytest.fixture(autouse=True)
def isolated_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every atif-cli test's lake (and the corpus base) into its own tmp dir.

    ``materialize`` writes the lake at ``ATIF_SQL_LAKE_ROOT`` whenever one
    exists there, and its default is the user's own ``~/.atif-sql/lake``: a
    test that inherited the default would write its synthetic sessions into
    the real lake. Public on purpose (``conftest.py`` re-exports this module
    with ``import *``, which skips underscore names).
    """
    lake_root = tmp_path / "isolated-lake"
    monkeypatch.setenv("ATIF_SQL_LAKE_ROOT", str(lake_root))
    monkeypatch.setenv("ATIF_SQL_CORPUS_BASE", str(tmp_path / "isolated-corpus-base"))
    return lake_root


def write_synthetic_session(source_root: Path, session_id: str) -> Path:
    """Create one convertible session JSONL with a backdated mtime."""
    project_dir = source_root / "-tmp-proj"
    project_dir.mkdir(parents=True, exist_ok=True)
    main = project_dir / f"{session_id}.jsonl"

    events: list[dict[str, Any]] = [
        {
            "type": "user",
            "uuid": f"{session_id}-u1",
            "parentUuid": None,
            "sessionId": session_id,
            "timestamp": "2026-08-22T00:00:01Z",
            "cwd": "/home/alice/proj",
            "gitBranch": "main",
            "version": "2.0.0",
            "isSidechain": False,
            "message": {"role": "user", "content": "hello please run a tool"},
        },
        {
            "type": "assistant",
            "uuid": f"{session_id}-a1",
            "parentUuid": f"{session_id}-u1",
            "sessionId": session_id,
            "timestamp": "2026-08-22T00:00:02Z",
            "isSidechain": False,
            "requestId": "req_001",
            "message": {
                "id": f"msg_{session_id[:8]}",
                "role": "assistant",
                "model": "claude-test-1",
                "content": [
                    {"type": "text", "text": "running the tool"},
                    {
                        "type": "tool_use",
                        "id": f"toolu_{session_id[:8]}",
                        "name": "Bash",
                        "input": {"command": "echo hi"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 50,
                },
            },
        },
        {
            "type": "user",
            "uuid": f"{session_id}-u2",
            "parentUuid": f"{session_id}-a1",
            "sessionId": session_id,
            "timestamp": "2026-08-22T00:00:03Z",
            "isSidechain": False,
            "toolUseResult": {"stdout": "hi", "stderr": "", "interrupted": False},
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"toolu_{session_id[:8]}",
                        "content": "hi",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": f"{session_id}-a2",
            "parentUuid": f"{session_id}-u2",
            "sessionId": session_id,
            "timestamp": "2026-08-22T00:00:04Z",
            "isSidechain": False,
            "requestId": "req_002",
            "message": {
                "id": f"msg2_{session_id[:8]}",
                "role": "assistant",
                "model": "claude-test-1",
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 20, "output_tokens": 3},
            },
        },
    ]
    main.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    stale_ns = time.time_ns() - _STALE_OFFSET_NS
    os.utime(main, ns=(stale_ns, stale_ns))
    return main


def read_artifact_bytes(path: Path) -> bytes:
    """A stored artifact's bytes, decompressed when it is a ``.zst`` file."""
    import zstandard

    data = path.read_bytes()
    if path.name.endswith(".zst"):
        with zstandard.ZstdDecompressor().stream_reader(data) as reader:
            return reader.readall()
    return data


def read_artifact_text(path: Path) -> str:
    """A stored artifact's text, decompressed when it is a ``.zst`` file."""
    return read_artifact_bytes(path).decode("utf-8")


def write_artifact_text(path: Path, text: str) -> None:
    """Replace a stored artifact's content, compressing it when it is a ``.zst`` file."""
    import zstandard

    data = text.encode("utf-8")
    if path.name.endswith(".zst"):
        data = zstandard.ZstdCompressor().compress(data)
    path.chmod(0o644)
    path.write_bytes(data)


def to_legacy_layout(corpus_root: Path, session_id: str, *, parquet: bool = True) -> Path:
    """Rewrite one materialized session into the layout materialize wrote before compression.

    Decompresses ``trajectory.json.zst``, ``edges.jsonl.zst`` and
    ``session_events.jsonl.zst`` into their plain names (keeping each file's
    mtime) and, with ``parquet``, writes the five per-session parquet files
    through the columnar producer and stamps ``meta.columnar_schema``, which
    is exactly what a default materialize pass wrote then. Returns the
    session directory.
    """
    import zstandard

    from atif_duck.domain.columnar import COLUMNAR_SCHEMA_VERSION, META_COLUMNAR_KEY
    from atif_duck.infrastructure.columnar import ColumnarArtifactProducer

    session_dir = corpus_root / "sessions" / session_id
    for name in ("trajectory.json", "edges.jsonl", "session_events.jsonl"):
        compressed = session_dir / f"{name}.zst"
        st = compressed.stat()
        with (
            compressed.open("rb") as handle,
            zstandard.ZstdDecompressor().stream_reader(handle) as reader,
        ):
            (session_dir / name).write_bytes(reader.readall())
        os.utime(session_dir / name, ns=(st.st_atime_ns, st.st_mtime_ns))
        compressed.unlink()
    if parquet:
        trajectory = json.loads((session_dir / "trajectory.json").read_text())
        ColumnarArtifactProducer().produce(
            session_dir, session_id=session_id, trajectory=trajectory
        )
        meta_path = session_dir / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta[META_COLUMNAR_KEY] = COLUMNAR_SCHEMA_VERSION
        meta_path.write_text(json.dumps(meta) + "\n")
    return session_dir


@pytest.fixture
def synthetic_session(tmp_path: Path) -> Path:
    """One convertible session's main JSONL under a tmp source root."""
    return write_synthetic_session(tmp_path / "projects", "11111111-1111-1111-1111-111111111111")


#: analytics shard -> the rows and column names atif-analytics writes.
#: Layout pinned against ``atif_duck.infrastructure.analytics._ANALYTICS_SOURCES``
#: (every artifact is a sharded dir of ``part-*.parquet``).
_ANALYTICS_PARQUETS: dict[str, tuple[str, str]] = {
    "user_friction/part-1.parquet": (
        (
            "('u-1', '{sid}', TIMESTAMPTZ '2026-08-22 00:30:00+00', 'undo that', 'correction', "
            "'rule 2', 'regex', CAST(0.8 AS FLOAT), TIMESTAMPTZ '2026-08-22 01:00:00+00')"
        ),
        "uuid, session_id, ts, text_snippet, label, rationale, source, confidence, classified_at",
    ),
}


def write_analytics_parquets(corpus_root: Path, session_id: str) -> list[Path]:
    """Write the analytics parquets ``register_analytics`` binds lazily.

    Returns every parquet written, so a test can name the exact paths the
    sandbox has to keep readable. Uses DuckDB's own ``COPY`` (atif-duck's
    test-suite precedent) rather than pyarrow.
    """
    import duckdb

    analytics = corpus_root / "analytics"
    written: list[Path] = []
    scratch = duckdb.connect()
    try:
        classifications = (
            analytics / "session_classifications" / "part-1.parquet",
            (
                f"('{session_id}', 'assisted', 'sde', 'success', 'Fix the flaky test.', "
                "CAST(0.9 AS FLOAT), TIMESTAMPTZ '2026-08-22 01:00:00+00')"
            ),
            "session_id, autonomy_tier, work_category, success, goal, confidence, classified_at",
        )
        targets = [
            classifications,
            *(
                (analytics / name, values.format(sid=session_id), columns)
                for name, (values, columns) in _ANALYTICS_PARQUETS.items()
            ),
        ]
        for path, values, columns in targets:
            path.parent.mkdir(parents=True, exist_ok=True)
            literal = "'" + str(path).replace("'", "''") + "'"
            scratch.execute(
                f"COPY (SELECT * FROM (VALUES {values}) t({columns})) "
                f"TO {literal} (FORMAT PARQUET);"
            )
            written.append(path)
    finally:
        scratch.close()
    return sorted(written)


# ``register_vss`` no longer installs the lance extension (that download at
# query time was finding 4 of the MicroVM review), so the suite installs it
# once up front. A no-op where it is already present; a one-time download on a
# fresh runner, exactly what every register() call used to do implicitly.
# A public name on purpose: ``conftest.py`` re-exports this module with
# ``import *``, which skips underscore names, so an underscore here would leave
# the fixture unregistered (it did, on a runner with no extension cached).
@pytest.fixture(scope="session", autouse=True)
def lance_extension_present() -> None:
    import duckdb

    con = duckdb.connect()
    try:
        con.execute("INSTALL lance")
    finally:
        con.close()


# The lake tests need the ducklake extension; `query` only LOADs it, so the
# suite installs it once up front, as it does lance.
@pytest.fixture(scope="session", autouse=True)
def ducklake_extension_present() -> None:
    from atif_duck.infrastructure.lake import install_ducklake_extension

    install_ducklake_extension()
