# SPDX-License-Identifier: Apache-2.0

"""SQL text boundaries: adversarial data is data, bad names register nothing, text is constant.

Three properties, each pinned behaviorally and then structurally:

* Corpus CONTENT carrying SQL text (quotes, ``;``, ``--``, ``$1``, ``?``,
  backslashes, newlines, deep nesting) registers as rows with those exact
  values, from a corpus root whose own path carries the same characters, on
  both the JSON and the columnar path. The paths reach DuckDB as bound
  parameters, so there is nothing for the text to escape from.
* A session DIRECTORY whose name fails the session id boundary registers
  nothing, is reported once, and is invisible to every view and to the
  coverage count ``atif-sql status`` prints.
* The two statement kinds DuckDB will not prepare (``ATTACH``, the producer's
  one-row session projection) still go through ``sql_literal``; a test each
  fails the moment that wrapping is removed. The AST audit in
  ``sql_text_audit`` then proves every other placeholder in the four SQL
  modules resolves to a constant, a projection call, or ``sql_literal``, and
  a planted ``{user_input}`` is caught.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
from duck_fixtures import SESSION_IDS, build_corpus
from loguru import logger
from sql_text_audit import audit_source
from test_columnar import add_columnar
from test_vss import DIM, MODEL, _write_lance

from atif_duck.domain.catalog import VIEW_NAMES
from atif_duck.infrastructure.columnar import ColumnarArtifactProducer, columnar_coverage
from atif_duck.infrastructure.registry import register, register_raw, register_vss

#: packages/atif-duck/tests/ -> packages/
_PACKAGES_DIR = Path(__file__).resolve().parents[2]
_DUCK_INFRA = _PACKAGES_DIR / "atif-duck" / "src" / "atif_duck" / "infrastructure"
_EMBED_INFRA = _PACKAGES_DIR / "atif-embed" / "src" / "atif_embed" / "infrastructure"

#: The four modules that build SQL text.
SQL_MODULES: dict[str, Path] = {
    "registry": _DUCK_INFRA / "registry.py",
    "columnar": _DUCK_INFRA / "columnar.py",
    "analytics": _DUCK_INFRA / "analytics.py",
    "corpus_text_rows": _EMBED_INFRA / "corpus_text_rows.py",
}

#: A corpus root whose path carries every character that would matter if it
#: were spliced into a statement. Every test below builds under it. (``[``
#: and a backslash are absent on purpose: DuckDB's glob reader, which the
#: meta/edges/loss readers still use for speed, treats them as glob syntax.)
HOSTILE_ROOT_NAME = "o'brien ?; --$1"

#: Text payloads that would alter a statement if they were text in one.
INJECTION = "'); DROP TABLE x; --"
PARAMS = "$1 and ? and $2"
BACKSLASHES = "C:\\temp\\\\file \\' \\n"
NEWLINES = "line one\nline two\r\nline three"

#: Session directory names the boundary refuses. Written with full artifacts
#: so that only the name, not a missing file, keeps them out.
BAD_DIR_NAMES = ("'); DROP TABLE x; --", "$1", "two words", 'say "hi"', "a;b", "sessión")

ADVERSARIAL_SESSION = "33333333-3333-3333-3333-333333333333"


def _deep(depth: int) -> Any:
    value: Any = {"leaf": INJECTION}
    for level in range(depth):
        value = {f"level_{level}": [value, {"sibling": PARAMS}]}
    return value


def _adversarial_trajectory() -> dict[str, Any]:
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": ADVERSARIAL_SESSION,
        "agent": {
            "name": "claude-code",
            "version": INJECTION,
            "model_name": f"model-{PARAMS}",
            "extra": {"cwds": [BACKSLASHES], "git_branches": [NEWLINES]},
        },
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-20T10:00:00.000Z",
                "source": "user",
                "message": INJECTION,
                "extra": {"is_sidechain": False, "source_uuids": ["x-1"]},
            },
            {
                "step_id": 2,
                "timestamp": "2026-08-20T10:00:10.000Z",
                "source": "agent",
                "model_name": f"model-{PARAMS}",
                "message": [
                    {"type": "text", "text": BACKSLASHES},
                    {"type": "text", "text": NEWLINES},
                ],
                "tool_calls": [
                    {
                        "tool_call_id": INJECTION,
                        "function_name": PARAMS,
                        "arguments": {"command": INJECTION, "nested": _deep(12)},
                    }
                ],
                "observation": {"results": [{"source_call_id": INJECTION, "content": NEWLINES}]},
                "metrics": {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 0},
                "llm_call_count": 1,
                "extra": {"is_sidechain": False, "source_uuids": ["x-2"]},
            },
        ],
        "final_metrics": {
            "total_prompt_tokens": 10,
            "total_completion_tokens": 2,
            "total_cached_tokens": 0,
            "total_cost_usd": 0.0,
            "total_steps": 2,
            "extra": {},
        },
        "extra": {"note": _deep(20)},
    }


def _write_session(session_dir: Path, session_id: str, trajectory: dict[str, Any]) -> None:
    session_dir.mkdir(parents=True)
    (session_dir / "trajectory.json").write_text(json.dumps(trajectory, separators=(",", ":")))
    edge = {
        "uuid": "x-1",
        "parent_uuid": None,
        "message_id": INJECTION,
        "type": "user",
        "ts": "2026-08-20T10:00:00Z",
        "is_sidechain": False,
        "is_compact_summary": False,
        "source_file": BACKSLASHES,
        "tool_use_ids": [INJECTION],
    }
    (session_dir / "edges.jsonl").write_text(json.dumps(edge) + "\n")
    (session_dir / "loss_report.json").write_text(
        json.dumps(
            {
                "record_counts": {"user": 1},
                "records_total": 1,
                "records_converted": 1,
                "records_dropped": 0,
                "gaps_observed": [INJECTION],
                "subagent_files_found": 0,
                "subagent_files_convertible": 0,
                "workflow_subagent_files_found": 0,
            }
        )
    )
    (session_dir / "meta.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "source_mtime_ns": 1,
                "source_files": [BACKSLASHES],
                "harbor_version": "0.22.0",
                "converter_version": "0.1.0",
                "materialized_at": "2026-08-22T00:00:00Z",
            }
        )
    )


@pytest.fixture
def hostile_root(tmp_path: Path) -> Path:
    """The fixture corpus plus the adversarial session, under a hostile path."""
    root = build_corpus(tmp_path / HOSTILE_ROOT_NAME)
    _write_session(
        root / "sessions" / ADVERSARIAL_SESSION, ADVERSARIAL_SESSION, _adversarial_trajectory()
    )
    return root


def _rows(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[object] | None = None
) -> list[Any]:
    return con.execute(sql, params or []).fetchall()


def _assert_adversarial_content_is_data(con: duckdb.DuckDBPyConnection, root: Path) -> None:
    sid = ADVERSARIAL_SESSION
    assert _rows(con, "SELECT count(*) FROM sessions") == [(3,)]
    (agent_version, model, cwd, branch, path) = _rows(
        con,
        "SELECT agent_version, model_name, cwd, git_branch, trajectory_path FROM sessions "
        "WHERE session_id = ?",
        [sid],
    )[0]
    assert agent_version == INJECTION
    assert model == f"model-{PARAMS}"
    assert cwd == BACKSLASHES
    assert branch == NEWLINES
    assert path == str(root / "sessions" / sid / "trajectory.json")
    messages = _rows(
        con, "SELECT step_id, message FROM steps WHERE session_id = ? ORDER BY 1", [sid]
    )
    assert messages == [(1, INJECTION), (2, f"{BACKSLASHES}\n\n{NEWLINES}")]
    calls = _rows(
        con,
        "SELECT tool_name, tool_use_id, json_extract_string(tool_input, '$.command'), "
        "json_extract_string(tool_input, '$.nested.level_11[0].level_10[0].level_9[0].level_8[0]"
        ".level_7[0].level_6[0].level_5[0].level_4[0].level_3[0].level_2[0].level_1[0].level_0[0].leaf') "
        "FROM tool_calls WHERE session_id = ?",
        [sid],
    )
    assert calls == [(PARAMS, INJECTION, INJECTION, INJECTION)]
    results = _rows(
        con,
        "SELECT tool_use_id, json_extract_string(content, '$') FROM tool_results WHERE session_id = ?",
        [sid],
    )
    assert results == [(INJECTION, NEWLINES)]
    assert _rows(
        con, "SELECT message_id, source_file FROM messages WHERE session_id = ?", [sid]
    ) == [(INJECTION, BACKSLASHES)]
    assert _rows(
        con, "SELECT gaps_observed::VARCHAR FROM loss_reports WHERE session_id = ?", [sid]
    ) == [(json.dumps([INJECTION]),)]
    # No statement was altered: the fixture's tables are all still there.
    assert _rows(con, "SELECT count(*) FROM sessions WHERE session_id = ?", [SESSION_IDS[0]]) == [
        (1,)
    ]
    for view in VIEW_NAMES:
        con.execute(f"SELECT count(*) FROM {view}").fetchone()


class TestAdversarialContentIsData:
    def test_json_path(self, hostile_root: Path) -> None:
        con = duckdb.connect(":memory:")
        sources = register(con, hostile_root)
        assert sources.json_session_ids == (*sorted(SESSION_IDS), ADVERSARIAL_SESSION)
        assert sources.rejected_session_ids == ()
        _assert_adversarial_content_is_data(con, hostile_root)

    def test_columnar_path(self, hostile_root: Path) -> None:
        add_columnar(hostile_root, (*SESSION_IDS, ADVERSARIAL_SESSION))
        con = duckdb.connect(":memory:")
        sources = register(con, hostile_root)
        assert set(sources.columnar_session_ids) == {*SESSION_IDS, ADVERSARIAL_SESSION}
        assert sources.json_session_ids == ()
        _assert_adversarial_content_is_data(con, hostile_root)

    def test_both_paths_agree_row_for_row(self, hostile_root: Path) -> None:
        json_con = duckdb.connect(":memory:")
        register(json_con, hostile_root)
        json_rows = {
            view: sorted(map(repr, json_con.execute(f"SELECT * FROM {view}").fetchall()))
            for view in VIEW_NAMES
        }
        add_columnar(hostile_root, (*SESSION_IDS, ADVERSARIAL_SESSION))
        columnar_con = duckdb.connect(":memory:")
        register(columnar_con, hostile_root)
        for view in VIEW_NAMES:
            live = sorted(map(repr, columnar_con.execute(f"SELECT * FROM {view}").fetchall()))
            assert live == json_rows[view], view

    def test_analytics_parquets_under_the_hostile_root_bind(self, hostile_root: Path) -> None:
        analytics = hostile_root / "analytics" / "session_classifications"
        analytics.mkdir(parents=True)
        con = duckdb.connect(":memory:")
        con.sql(
            "SELECT ? AS session_id, ? AS goal, 0.5 AS confidence, "
            "TIMESTAMP '2026-08-20' AS classified_at, 'L2' AS autonomy_tier, "
            "'success' AS success, 'build' AS work_category",
            params=[ADVERSARIAL_SESSION, INJECTION],
        ).write_parquet(str(analytics / "part-0.parquet"))
        register(con, hostile_root)
        assert _rows(con, "SELECT goal, autonomy FROM session_classifications") == [
            (INJECTION, "L2")
        ]
        assert _rows(con, "SELECT goal FROM session_goals") == [(INJECTION,)]


class TestBadDirectoryNamesRegisterNothing:
    @pytest.fixture
    def root_with_bad_dirs(self, hostile_root: Path) -> Path:
        for name in BAD_DIR_NAMES:
            _write_session(
                hostile_root / "sessions" / name,
                name,
                _adversarial_trajectory() | {"session_id": name},
            )
        return hostile_root

    def test_rejected_dirs_are_reported_once_and_absent_from_every_view(
        self, root_with_bad_dirs: Path
    ) -> None:
        warnings: list[str] = []
        sink_id = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
        try:
            con = duckdb.connect(":memory:")
            sources = register(con, root_with_bad_dirs)
        finally:
            logger.remove(sink_id)
        assert sources.rejected_session_ids == tuple(sorted(BAD_DIR_NAMES))
        assert set(sources.json_session_ids) == {*SESSION_IDS, ADVERSARIAL_SESSION}
        rejecting = [w for w in warnings if "Rejecting session dir" in w]
        assert len(rejecting) == len(BAD_DIR_NAMES)
        for name in BAD_DIR_NAMES:
            assert sum(repr(name) in w for w in rejecting) == 1, name
        ids = set()
        for view in ("sessions", "steps", "tool_calls", "tool_results", "messages", "loss_reports"):
            ids |= {row[0] for row in _rows(con, f"SELECT DISTINCT session_id FROM {view}")}
        assert ids == {*SESSION_IDS, ADVERSARIAL_SESSION}
        assert not (ids & set(BAD_DIR_NAMES))

    def test_coverage_count_agrees_with_the_registry(self, root_with_bad_dirs: Path) -> None:
        coverage = columnar_coverage(root_with_bad_dirs)
        con = duckdb.connect(":memory:")
        sources = register_raw(con, root_with_bad_dirs)
        assert coverage == sources.coverage
        assert coverage.total_sessions == 3

    def test_a_bad_dir_with_columnar_artifacts_is_still_rejected(
        self, root_with_bad_dirs: Path
    ) -> None:
        bad = BAD_DIR_NAMES[0]
        # The producer accepts any id (it escapes it); the registry still refuses the dir.
        add_columnar(root_with_bad_dirs, (bad,))
        con = duckdb.connect(":memory:")
        sources = register_raw(con, root_with_bad_dirs)
        assert bad in sources.rejected_session_ids
        assert bad not in sources.columnar_session_ids
        assert all(bad not in str(path) for path in sources.lazy_read_paths)


class TestRemainingSqlLiteralSites:
    """The two statements DuckDB will not prepare; each fails without ``sql_literal``."""

    def test_producer_stamps_a_quoted_session_id_as_data(self, tmp_path: Path) -> None:
        session_id = "o'brien'); DROP TABLE x; --"
        session_dir = tmp_path / "staged"
        session_dir.mkdir()
        ColumnarArtifactProducer().produce(
            session_dir, session_id=session_id, trajectory=_adversarial_trajectory()
        )
        con = duckdb.connect(":memory:")
        rows = con.execute(
            "SELECT session_id_path FROM read_parquet(?)", [str(session_dir / "session.parquet")]
        ).fetchall()
        assert rows == [(session_id,)]
        steps = con.execute(
            "SELECT DISTINCT session_id FROM read_parquet(?)", [str(session_dir / "steps.parquet")]
        ).fetchall()
        assert steps == [(session_id,)]

    def test_lance_store_under_a_quoted_path_attaches(self, tmp_path: Path) -> None:
        lance_uri = _write_lance(tmp_path / HOSTILE_ROOT_NAME / "lance")
        con = duckdb.connect(":memory:")
        assert (
            register_vss(con, lance_uri=lance_uri, expected_model=MODEL, expected_dim=DIM) is True
        )
        assert con.execute("SELECT count(*) FROM message_embeddings").fetchone() == (4,)


class TestEveryPlaceholderIsTrusted:
    @pytest.mark.parametrize("module", list(SQL_MODULES), ids=list(SQL_MODULES))
    def test_module_has_no_untrusted_placeholder(self, module: str) -> None:
        audit = audit_source(SQL_MODULES[module].read_text(encoding="utf-8"))
        assert audit.audited, (
            f"{module}: the audit selected no SQL f-string; the marker set is broken"
        )
        assert not audit.violations, f"{module}:\n  " + "\n  ".join(map(str, audit.violations))

    def test_the_audit_is_not_vacuous(self) -> None:
        total = sum(
            len(audit_source(path.read_text(encoding="utf-8")).audited)
            for path in SQL_MODULES.values()
        )
        assert total >= 30, total

    def test_a_planted_user_input_placeholder_is_caught(self) -> None:
        planted = (
            "T = 't'\n"
            "def bad(con, user_input):\n"
            '    con.execute(f"SELECT * FROM {T} WHERE id = {user_input}")\n'
            "def worse(con):\n"
            "    name = input()\n"
            '    where = f" WHERE n = {name}"\n'
            '    return f"SELECT * FROM {T}{where}"\n'
            "def indirect(con, table):\n"
            '    return f"SELECT * FROM {table}"\n'
            "def caller(con, external):\n"
            "    indirect(con, external)\n"
        )
        audit = audit_source(planted)
        assert sorted(v.expression for v in audit.violations) == [
            "name",
            "table",
            "user_input",
            "where",
        ]

    def test_the_trusted_shapes_pass(self) -> None:
        accepted = (
            "from atif_duck.domain.catalog import VIEW_SCHEMA\n"
            "from atif_duck.domain.sql_literal import SqlFragment, sql_literal\n"
            "from atif_duck.infrastructure.projections import render\n"
            "T = 't'\n"
            "COLS = {'a': 'VARCHAR'}\n"
            "def helper(columns) -> SqlFragment:\n"
            "    body = ', '.join(f'CAST({n} AS {t}) AS {n}' for n, t in columns)\n"
            '    return SqlFragment(f"SELECT {body} FROM {T}")\n'
            "def ok(con, path, width):\n"
            "    parts = []\n"
            '    parts.append(f"{sql_literal(path)} AS p")\n'
            "    dim = int(width)\n"
            "    con.execute(f\"SELECT {helper(VIEW_SCHEMA['steps'])}, {', '.join(parts)}, FLOAT[{dim}] FROM {T}\")\n"
            "    for table, columns in ((T, COLS),):\n"
            '        con.execute(f"CREATE VIEW {table} AS {render(columns)}")\n'
        )
        audit = audit_source(accepted)
        assert audit.audited
        assert not audit.violations, [str(v) for v in audit.violations]
