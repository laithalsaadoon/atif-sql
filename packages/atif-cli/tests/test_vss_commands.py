# SPDX-License-Identifier: Apache-2.0

"""CLI tests for the VSS commands: embed (dry-run + fake provider) and search.

NO live Bedrock calls: the real-embed path swaps a FakeEmbedder in for the
Cohere adapter via monkeypatch (the deferred in-body import resolves the
attribute at call time), and search's query embedding is patched the same
way. Lance runs for real over tmp dirs — it's a local library.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from atif_cli.app import embed, search
from atif_cli.errors import EXIT_CODES
from atif_cli.output import OutputFormat

MODEL = "global.cohere.embed-v4:0"
DIM = 1024

STEP_TEXT = "A qualifying step text with clearly more than thirty-two characters."


def _write_contract_corpus(root: Path) -> Path:
    """One contract-shaped session with a single embeddable step."""
    sid = "11111111-1111-1111-1111-111111111111"
    sdir = root / "sessions" / sid
    sdir.mkdir(parents=True)
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": sid,
        "agent": {"name": "claude-code", "version": "2.1.218", "model_name": "m"},
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-20T10:00:00.000Z",
                "source": "user",
                "message": STEP_TEXT,
                "extra": {"is_sidechain": False, "source_uuids": ["u-1"]},
            }
        ],
        "final_metrics": {"total_steps": 1},
    }
    edge: dict[str, Any] = {
        "uuid": "u-1",
        "parent_uuid": None,
        "message_id": None,
        "type": "user",
        "ts": "2026-08-20T10:00:00.000Z",
        "is_sidechain": False,
        "is_compact_summary": False,
        "source_file": "transcript.jsonl",
        "tool_use_ids": [],
    }
    loss: dict[str, Any] = {
        "record_counts": {"user": 1},
        "records_total": 1,
        "records_converted": 1,
        "records_dropped": 0,
        "gaps_observed": [],
        "subagent_files_found": 0,
        "subagent_files_convertible": 0,
        "workflow_subagent_files_found": 0,
    }
    meta = {
        "session_id": sid,
        "source_mtime_ns": 1,
        "source_files": ["transcript.jsonl"],
        "harbor_version": "0.22.0",
        "converter_version": "0.1.0",
        "materialized_at": "2026-08-22T00:00:00Z",
    }
    (sdir / "trajectory.json").write_text(json.dumps(trajectory, separators=(",", ":")))
    (sdir / "edges.jsonl").write_text(json.dumps(edge) + "\n")
    (sdir / "loss_report.json").write_text(json.dumps(loss, separators=(",", ":")))
    (sdir / "meta.json").write_text(json.dumps(meta, separators=(",", ":")))
    return root


class _FakeCohere:
    """Stands in for CohereBedrockEmbedder — identity matches EmbedSettings."""

    def __init__(self, settings: object) -> None:
        del settings

    @property
    def model_id(self) -> str:
        return MODEL

    @property
    def dimension(self) -> int:
        return DIM

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        del text
        return [1.0] + [0.0] * (DIM - 1)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    return _write_contract_corpus(tmp_path / "corpus")


# Requested by name through `@pytest.mark.usefixtures`, never by reference, so
# `reportUnusedFunction` is suppressed here rather than answered by a rename —
# the name IS the fixture's identity.
@pytest.fixture
def _fake_cohere(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.setattr(
        "atif_embed.infrastructure.cohere_bedrock.CohereBedrockEmbedder", _FakeCohere
    )


class TestEmbedCommand:
    def test_dry_run_emits_plan_without_bedrock(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        embed(dry_run=True, corpus_root=corpus, fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert payload["pipeline"] == "embed"
        assert payload["candidates"] == 1
        assert payload["dry_run"] is True
        assert payload["model"] == MODEL

    @pytest.mark.usefixtures("_fake_cohere")
    def test_real_run_writes_rows(self, corpus: Path, capsys: pytest.CaptureFixture[str]) -> None:
        embed(all_steps=True, corpus_root=corpus, fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"pipeline": "embed", "rows_processed": 1, "dry_run": False}
        assert (corpus / "embeddings_lance").is_dir()

    @pytest.mark.usefixtures("_fake_cohere")
    def test_limit_flag_caps_run(self, corpus: Path, capsys: pytest.CaptureFixture[str]) -> None:
        embed(limit=0, corpus_root=corpus, fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert payload["rows_processed"] == 0

    def test_bare_real_run_is_refused_with_hint(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No --limit and no --all: exit 64 + hint, no Bedrock call, no store."""
        with pytest.raises(SystemExit) as excinfo:
            embed(corpus_root=corpus, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["invalid_input"]
        err = json.loads(capsys.readouterr().err)
        assert err["error"]["kind"] == "invalid_input"
        assert "--limit" in err["error"]["hint"]
        assert "--all" in err["error"]["hint"]
        assert not (corpus / "embeddings_lance").is_dir()

    def test_dry_run_needs_no_scope(self, corpus: Path, capsys: pytest.CaptureFixture[str]) -> None:
        embed(dry_run=True, corpus_root=corpus, fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True

    @pytest.mark.usefixtures("_fake_cohere")
    def test_real_run_installs_the_lance_extension_first(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The network is already a deliberate act here, so this is where the download belongs."""
        from atif_duck.infrastructure import registry as registry_mod

        calls: list[object] = []

        def recording_install(con: object) -> str:
            calls.append(con)
            return "/ext/lance"

        monkeypatch.setattr(registry_mod, "install_lance_extension", recording_install)
        embed(all_steps=True, corpus_root=corpus, fmt=OutputFormat.JSON)
        assert json.loads(capsys.readouterr().out)["rows_processed"] == 1
        assert len(calls) == 1

    def test_dry_run_installs_nothing(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from atif_duck.infrastructure import registry as registry_mod

        def forbidden_install(con: object) -> str:
            del con
            pytest.fail("a dry run must not reach the extension repository")

        monkeypatch.setattr(registry_mod, "install_lance_extension", forbidden_install)
        embed(dry_run=True, corpus_root=corpus, fmt=OutputFormat.JSON)
        assert json.loads(capsys.readouterr().out)["dry_run"] is True

    @pytest.mark.usefixtures("_fake_cohere")
    def test_a_failed_install_only_warns_and_the_backfill_still_runs(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import duckdb
        from loguru import logger

        from atif_duck.infrastructure import registry as registry_mod

        def failing(con: object) -> str:
            del con
            problem = "Failed to download extension"
            raise duckdb.IOException(problem)

        monkeypatch.setattr(registry_mod, "install_lance_extension", failing)
        warnings: list[str] = []
        sink_id = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
        try:
            embed(all_steps=True, corpus_root=corpus, fmt=OutputFormat.JSON)
        finally:
            logger.remove(sink_id)
        assert json.loads(capsys.readouterr().out)["rows_processed"] == 1
        assert any("--install-extension" in w for w in warnings)

    def test_install_extension_flag_exits_70_when_the_download_fails(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import duckdb

        from atif_duck.infrastructure import registry as registry_mod

        def failing(con: object) -> str:
            del con
            problem = "Failed to download extension"
            raise duckdb.IOException(problem)

        monkeypatch.setattr(registry_mod, "install_lance_extension", failing)
        with pytest.raises(SystemExit) as excinfo:
            embed(install_extension=True, corpus_root=corpus, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["runtime_error"]
        assert json.loads(capsys.readouterr().err)["error"]["kind"] == "runtime_error"


class TestSearchCommand:
    def test_empty_store_exits_2(self, corpus: Path, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            search("anything", corpus_root=corpus, fmt=OutputFormat.JSON)
        assert excinfo.value.code == EXIT_CODES["no_embeddings"]
        err = json.loads(capsys.readouterr().err)
        assert err["error"]["kind"] == "no_embeddings"

    @pytest.mark.usefixtures("_fake_cohere")
    def test_search_returns_hit_with_snippet(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        embed(all_steps=True, corpus_root=corpus, fmt=OutputFormat.JSON)
        capsys.readouterr()  # discard embed output
        search("what was the qualifying step?", k=5, corpus_root=corpus, fmt=OutputFormat.JSON)
        hits = json.loads(capsys.readouterr().out)
        assert len(hits) == 1
        hit = hits[0]
        assert hit["uuid"] == "u-1"
        assert hit["session_id"] == "11111111-1111-1111-1111-111111111111"
        assert hit["snippet"].startswith(STEP_TEXT[:50])
        assert hit["sim"] == pytest.approx(1.0)

    @pytest.mark.usefixtures("_fake_cohere")
    def test_session_filter_excludes_other_sessions(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        embed(all_steps=True, corpus_root=corpus, fmt=OutputFormat.JSON)
        capsys.readouterr()
        search("anything", session_id="nonexistent", corpus_root=corpus, fmt=OutputFormat.JSON)
        hits = json.loads(capsys.readouterr().out)
        assert hits == []
