# SPDX-License-Identifier: Apache-2.0

"""The CLI's Codex path: flag parsing, adapter routing, and one real end-to-end.

The integration test converts a synthetic rollout through the REAL harbor Codex
adapter into a tmp corpus and queries it, because that is the only way to prove
the three things ``--agent`` selects — discovery layout, converter, default
roots — actually move together.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from atif_cli.app import convert, embed, materialize, query, search, status
from atif_cli.converter_adapter import RealConverter
from atif_cli.errors import EXIT_CODES
from atif_cli.output import OutputFormat
from atif_converter.domain.agents import AgentSource
from atif_converter.domain.fidelity import LossReport
from atif_converter.infrastructure.harbor_adapter import ConversionResult
from atif_corpus.domain.ports import ConverterPort

#: A rollout carrying one user turn and one assistant reply — the smallest
#: shape harbor's Codex converter turns into a valid trajectory.
_SESSION_ID = "01a09182-2858-7f42-936b-7f027b341fdf"
_ROLLOUT_NAME = f"rollout-2026-09-11T17-27-01-{_SESSION_ID}.jsonl"


def write_rollout(sessions_root: Path, session_id: str = _SESSION_ID) -> Path:
    """Write a minimal convertible rollout in Codex's on-disk layout."""
    day_dir = sessions_root / "2026" / "09" / "11"
    day_dir.mkdir(parents=True, exist_ok=True)
    rollout = day_dir / _ROLLOUT_NAME.replace(_SESSION_ID, session_id)
    records: list[dict[str, Any]] = [
        {
            "timestamp": "2026-09-11T17:27:01.000Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": "/home/alice/proj",
                "cli_version": "0.154.0",
                "git": {"branch": "main"},
            },
        },
        {
            "timestamp": "2026-09-11T17:27:01.200Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-6-astra"},
        },
        {
            "timestamp": "2026-09-11T17:27:01.500Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_user_1",
                "role": "user",
                "content": [{"type": "input_text", "text": "say ok"}],
            },
        },
        {
            "timestamp": "2026-09-11T17:27:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_agent_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            },
        },
        {
            "timestamp": "2026-09-11T17:27:02.100Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 90,
                        "output_tokens": 2,
                        "total_tokens": 102,
                    },
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 90,
                        "output_tokens": 2,
                        "total_tokens": 102,
                    },
                },
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    # Backdate so the session is quiescent at the default 300s policy.
    import os
    import time

    stale = time.time_ns() - 3_600 * 1_000_000_000
    os.utime(rollout, ns=(stale, stale))
    return rollout


@pytest.fixture
def codex_corpus(tmp_path: Path) -> tuple[Path, Path]:
    """(source_root, corpus_root) with one quiescent convertible rollout."""
    source_root = tmp_path / "sessions"
    write_rollout(source_root)
    return source_root, tmp_path / "corpus"


class TestAgentFlagParsing:
    def test_an_unknown_agent_exits_64_and_names_the_accepted_values(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        session = tmp_path / "x.jsonl"
        session.write_text("{}\n")
        with pytest.raises(SystemExit) as excinfo:
            convert(session, agent="gemini")
        assert excinfo.value.code == EXIT_CODES["invalid_input"]
        err = capsys.readouterr().err
        assert "claude-code" in err
        assert "codex" in err


class TestAdapterRouting:
    def test_the_default_adapter_is_the_claude_code_one(self) -> None:
        assert RealConverter().agent is AgentSource.CLAUDE_CODE

    def test_codex_adapter_calls_the_codex_use_case(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Routing, asserted without harbor: the seam picks the use case, nothing else."""
        calls: list[Path] = []

        def _stub(path: Path) -> tuple[ConversionResult, LossReport]:
            calls.append(path)
            return (
                ConversionResult(
                    trajectory={"schema_version": "ATIF-v1.7", "session_id": "s", "steps": []},
                    validation_errors=(),
                    edges_lines=('{"uuid":"u-1"}',),
                ),
                LossReport(),
            )

        def _never(*_args: object, **_kwargs: object) -> tuple[ConversionResult, LossReport]:
            msg = "the Claude Code use case must not run for --agent codex"
            raise AssertionError(msg)

        monkeypatch.setattr("atif_cli.converter_adapter.convert_codex_and_audit", _stub)
        monkeypatch.setattr("atif_cli.converter_adapter.convert_and_audit", _never)
        converter: ConverterPort = RealConverter(agent=AgentSource.CODEX)
        output = converter.convert(Path("rollout-x.jsonl"))
        assert calls == [Path("rollout-x.jsonl")]
        assert output.edges_lines == ['{"uuid":"u-1"}']

    def test_the_corpus_side_twin_member_routes_the_same_way(self) -> None:
        """The regression this guards: two enum classes, equal values, ``is`` false.

        ``materialize`` resolves the agent from ``CorpusSettings``, whose enum is
        atif-corpus's twin. An identity comparison against atif-converter's enum
        is always False for it, which routed every Codex pass into the Claude
        Code converter and materialized nothing. Constructing from the twin
        member here is what keeps that from coming back.
        """
        from atif_corpus.domain.agents import AgentSource as CorpusAgentSource

        assert RealConverter(agent=CorpusAgentSource.CODEX).agent is AgentSource.CODEX
        assert RealConverter(agent="codex").agent is AgentSource.CODEX
        assert RealConverter(agent=CorpusAgentSource.CLAUDE_CODE).agent is AgentSource.CLAUDE_CODE

    def test_claude_code_adapter_calls_the_claude_code_use_case(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def _stub(path: Path, *, include_subagents: bool) -> tuple[ConversionResult, LossReport]:
            seen["path"] = path
            seen["include_subagents"] = include_subagents
            return (
                ConversionResult(
                    trajectory={"schema_version": "ATIF-v1.7", "session_id": "s", "steps": []},
                    validation_errors=(),
                ),
                LossReport(),
            )

        monkeypatch.setattr("atif_cli.converter_adapter.convert_and_audit", _stub)
        RealConverter(include_subagents=False).convert(Path("s.jsonl"))
        assert seen == {"path": Path("s.jsonl"), "include_subagents": False}


class TestPrivateApiProbe:
    """A moved upstream method must fail the PASS, not every session in it.

    ``materialize`` records a session's exception and continues, so an absent
    ``Codex._convert_events_to_trajectory`` used to produce N identical failures
    under exit 0 — and the cron lane logged "materialize ok" every ten minutes.
    Probing when the adapter is BUILT puts the failure before the pass.
    """

    def test_building_the_codex_adapter_probes_the_private_method(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from harbor.agents.installed.codex import Codex

        from atif_converter.domain.errors import HarborPrivateApiMissing

        monkeypatch.delattr(Codex, "_convert_events_to_trajectory")
        with pytest.raises(HarborPrivateApiMissing):
            RealConverter(agent="codex")

    def test_building_the_claude_code_adapter_probes_its_own_private_method(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from harbor.agents.installed.claude_code import ClaudeCode

        from atif_converter.domain.errors import HarborPrivateApiMissing

        monkeypatch.delattr(ClaudeCode, "_convert_events_to_trajectory")
        with pytest.raises(HarborPrivateApiMissing):
            RealConverter()

    def test_materialize_exits_127_when_the_private_method_is_gone(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from harbor.agents.installed.codex import Codex

        monkeypatch.delattr(Codex, "_convert_events_to_trajectory")
        monkeypatch.delenv("ATIF_SQL_CORPUS_ROOT", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        (tmp_path / ".codex" / "sessions").mkdir(parents=True)

        with pytest.raises(SystemExit) as excinfo:
            materialize(agent="codex", fmt=OutputFormat.JSON)

        assert excinfo.value.code == EXIT_CODES["harbor_missing"]
        envelope = capsys.readouterr()
        assert "harbor" in (envelope.out + envelope.err)


class TestConvertCommand:
    def test_convert_codex_writes_trajectory_and_edges(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rollout = write_rollout(tmp_path / "sessions")
        out = tmp_path / "out" / "trajectory.json"
        convert(rollout, agent="codex", trajectory_out=out)
        trajectory = json.loads(out.read_text())
        assert trajectory["agent"]["name"] == "codex"
        assert trajectory["session_id"] == _SESSION_ID
        edges = (out.parent / "edges.jsonl").read_text().splitlines()
        assert len(edges) == 5, "one edge line per raw rollout record"
        captured = capsys.readouterr().out
        assert "loss_report" in captured
        assert "codex_" in captured, "the loss report must name the Codex gaps"

    def test_convert_honors_the_atif_sql_agent_setting(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ATIF_SQL_AGENT`` is documented as a setting, so every command reads it.

        ``convert`` takes no corpus settings, so it used to ignore the variable
        and run the Claude Code converter over a rollout — which exits 2 with
        "no convertible events", blaming the transcript for the wrong adapter.
        """
        monkeypatch.setenv("ATIF_SQL_AGENT", "codex")
        rollout = write_rollout(tmp_path / "sessions")
        out = tmp_path / "out" / "trajectory.json"
        convert(rollout, trajectory_out=out)
        capsys.readouterr()
        assert json.loads(out.read_text())["agent"]["name"] == "codex"

    def test_an_explicit_agent_flag_beats_the_setting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATIF_SQL_AGENT", "codex")
        rollout = write_rollout(tmp_path / "sessions")
        with pytest.raises(SystemExit) as excinfo:
            convert(rollout, agent="claude-code")
        assert excinfo.value.code == EXIT_CODES["empty_session"]

    def test_convert_codex_rejects_a_claude_code_session(self, tmp_path: Path) -> None:
        """A Claude Code transcript has no rollout records, so harbor converts nothing."""
        session = tmp_path / "11111111-1111-1111-1111-111111111111.jsonl"
        session.write_text(json.dumps({"type": "user", "uuid": "u-1"}) + "\n")
        with pytest.raises(SystemExit) as excinfo:
            convert(session, agent="codex")
        assert excinfo.value.code == EXIT_CODES["empty_session"]


@pytest.mark.integration
class TestCodexEndToEnd:
    def test_materialize_then_query_a_codex_corpus(
        self,
        codex_corpus: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source_root, corpus_root = codex_corpus
        monkeypatch.delenv("ATIF_SQL_CORPUS_ROOT", raising=False)

        materialize(
            agent="codex",
            source_root=source_root,
            corpus_root=corpus_root,
            fmt=OutputFormat.JSON,
        )
        report = json.loads(capsys.readouterr().out)
        assert report["materialized"] == 1
        assert report["failed"] == 0

        meta = json.loads((corpus_root / "sessions" / _SESSION_ID / "meta.json").read_text())
        assert meta["agent"] == "codex"
        assert meta["harbor_version"]

        query(
            "SELECT agent, agent_version, cwd, git_branch FROM sessions",
            corpus_root=corpus_root,
            fmt=OutputFormat.JSON,
        )
        rows = json.loads(capsys.readouterr().out)
        assert rows == [
            {
                "agent": "codex",
                "agent_version": "0.154.0",
                "cwd": "/home/alice/proj",
                "git_branch": "main",
            }
        ]

        query(
            "SELECT sum(cache_creation) AS created FROM steps",
            corpus_root=corpus_root,
            fmt=OutputFormat.JSON,
        )
        assert json.loads(capsys.readouterr().out) == [{"created": 90}]

        status(
            agent="codex",
            source_root=source_root,
            corpus_root=corpus_root,
            fmt=OutputFormat.JSON,
        )
        reported = json.loads(capsys.readouterr().out)
        assert reported["agent"] == "codex"
        assert reported["source_sessions"] == 1
        assert reported["materialized_sessions"] == 1
        assert reported["staleness"] == {"stale": 0, "up_to_date": 1, "live": 0}

    def test_source_uuids_join_the_steps_to_the_edges(
        self,
        codex_corpus: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The enrichment's whole point: a step resolves back to raw records in SQL."""
        source_root, corpus_root = codex_corpus
        monkeypatch.delenv("ATIF_SQL_CORPUS_ROOT", raising=False)
        materialize(
            agent="codex",
            source_root=source_root,
            corpus_root=corpus_root,
            fmt=OutputFormat.JSON,
        )
        capsys.readouterr()

        query(
            """
            SELECT count(*) AS joined
            FROM steps s,
                 UNNEST(json_extract(s.source_uuids, '$[*]')) AS u(source_uuid)
            JOIN messages m
              ON m.uuid = json_extract_string(source_uuid, '$')
            """,
            corpus_root=corpus_root,
            fmt=OutputFormat.JSON,
        )
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["joined"] >= 2, "every attributed id must exist in edges.jsonl"

    def test_the_two_agents_get_separate_corpora_by_default(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One corpus root per agent — the guard against one overwriting the other."""
        monkeypatch.delenv("ATIF_SQL_CORPUS_ROOT", raising=False)
        monkeypatch.delenv("ATIF_SQL_SOURCE_ROOT", raising=False)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
        (tmp_path / "claude" / "projects").mkdir(parents=True)
        (tmp_path / "codex" / "sessions").mkdir(parents=True)

        status(fmt=OutputFormat.JSON)
        claude = json.loads(capsys.readouterr().out)
        status(agent="codex", fmt=OutputFormat.JSON)
        codex = json.loads(capsys.readouterr().out)

        assert claude["agent"] == "claude-code"
        assert codex["agent"] == "codex"
        assert claude["source_root"] != codex["source_root"]
        assert claude["corpus_root"] != codex["corpus_root"]

    def test_query_reaches_the_codex_corpus_by_agent(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--agent codex`` must select the Codex corpus without spelling its path."""
        monkeypatch.delenv("ATIF_SQL_CORPUS_ROOT", raising=False)
        monkeypatch.delenv("ATIF_SQL_SOURCE_ROOT", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        (tmp_path / ".codex" / "sessions").mkdir(parents=True)

        # No corpus is materialized under that root, so the failure names the
        # path — which is exactly the assertion: the ROUTE, not the result. The
        # reserved ``codex`` slug applies because the source root IS
        # ``~/.codex/sessions`` under the patched HOME; a re-pointed CODEX_HOME
        # hashes instead, exactly as a re-pointed CLAUDE_CONFIG_DIR does.
        with pytest.raises(SystemExit):
            query("SELECT count(*) FROM sessions", agent="codex", fmt=OutputFormat.JSON)

        captured = capsys.readouterr()
        assert str(tmp_path / ".atif-sql" / "corpus" / "codex") in captured.out + captured.err

    def test_agent_awareness_stops_at_the_four_documented_commands(self) -> None:
        """The ``--agent`` surface is a contract: four commands carry it, two do not.

        ``embed`` and ``search`` resolve one corpus root and spend money against
        it; routing them by agent would need the Codex corpus threaded through
        the Lance store too, which is a separate change. This test is what keeps
        the flag from being added to them by accident.
        """
        import inspect

        for command in (convert, materialize, status, query):
            assert "agent" in inspect.signature(command).parameters, command.__name__
        for command in (embed, search):
            assert "agent" not in inspect.signature(command).parameters, command.__name__
