# SPDX-License-Identifier: Apache-2.0

"""The raw source archive rides the verifying re-read and holds exactly the parsed bytes.

Three properties, for both agents: every source file lands in the archive and
decompresses to the bytes on disk; archiving adds no open, parse or hash pass
over any transcript (the single-read rule in CLAUDE.md, same counters as
``test_snapshot_and_drift.TestSinglePass``); and a source that moves during the
conversion still fails it, so a half-true archive never looks complete.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest
import zstandard

from atif_converter.application.convert_and_audit import convert_and_audit
from atif_converter.application.convert_codex import convert_codex_and_audit
from atif_converter.domain.errors import SourceMutatedDuringConversion
from atif_converter.infrastructure import harbor_adapter, raw_records as raw_records_module
from atif_converter.infrastructure.harbor_adapter import ConversionResult
from atif_converter.infrastructure.raw_records import LoadedSession, discover_session_files
from atif_converter.infrastructure.source_archive import SourceArchiveWriter


def _archived(archive_dir: Path) -> dict[str, bytes]:
    return {
        path.relative_to(archive_dir).as_posix().removesuffix(".zst"): (
            zstandard.ZstdDecompressor().decompressobj().decompress(path.read_bytes())
        )
        for path in sorted(archive_dir.rglob("*.zst"))
    }


def _originals(session: Path) -> dict[str, bytes]:
    base = session.parent
    paths = [session]
    side_dir = base / session.stem
    if side_dir.is_dir():
        paths.extend(p for p in sorted(side_dir.rglob("*")) if p.is_file())
    return {p.relative_to(base).as_posix(): p.read_bytes() for p in paths}


class TestArchiveContents:
    def test_claude_code_archive_holds_every_source_file(
        self, synthetic_session: Path, tmp_path: Path
    ) -> None:
        # A sidecar the converter never parses must be archived too.
        sidecar = synthetic_session.parent / synthetic_session.stem / "subagents"
        (sidecar / "agent-abc.meta.json").write_text('{"agentType":"general"}\n')
        (synthetic_session.parent / synthetic_session.stem / "tool-results").mkdir()
        spill = synthetic_session.parent / synthetic_session.stem / "tool-results" / "t1.txt"
        spill.write_text("a large tool result\n")
        writer = SourceArchiveWriter(tmp_path / "archive", base=synthetic_session.parent)

        convert_and_audit(synthetic_session, archive=writer)

        assert _archived(tmp_path / "archive") == _originals(synthetic_session)
        assert {f.relative_path: (f.size, f.sha256) for f in writer.files} == {
            path: (len(data), hashlib.sha256(data).hexdigest())
            for path, data in _originals(synthetic_session).items()
        }

    def test_codex_archive_holds_the_rollout(self, codex_rollout: Path, tmp_path: Path) -> None:
        writer = SourceArchiveWriter(tmp_path / "archive", base=codex_rollout.parent)
        convert_codex_and_audit(codex_rollout, archive=writer)
        assert _archived(tmp_path / "archive") == {codex_rollout.name: codex_rollout.read_bytes()}
        assert [f.relative_path for f in writer.files] == [codex_rollout.name]

    def test_archive_files_are_owner_only(self, synthetic_session: Path, tmp_path: Path) -> None:
        writer = SourceArchiveWriter(tmp_path / "archive", base=synthetic_session.parent)
        convert_and_audit(synthetic_session, archive=writer)
        modes = {p.stat().st_mode & 0o777 for p in (tmp_path / "archive").rglob("*.zst")}
        assert modes == {0o600}

    def test_symlinks_in_the_side_dir_are_not_followed(
        self, synthetic_session: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("not part of the session\n")
        side = synthetic_session.parent / synthetic_session.stem
        (side / "link.txt").symlink_to(outside)
        writer = SourceArchiveWriter(tmp_path / "archive", base=synthetic_session.parent)
        convert_and_audit(synthetic_session, archive=writer)
        assert f"{synthetic_session.stem}/link.txt" not in _archived(tmp_path / "archive")


class _ReadCounters:
    def __init__(self) -> None:
        self.opens: Counter[Path] = Counter()
        self.parses: Counter[Path] = Counter()
        self.digests = 0


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> _ReadCounters:
    """The same three seams ``TestSinglePass`` spies on."""
    counters = _ReadCounters()
    real_open = Path.open

    def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        counters.opens[self] += 1
        return real_open(self, *args, **kwargs)

    real_parse = raw_records_module.parse_jsonl_records

    def spy_parse(lines: Iterable[str], path: Path) -> list[Any]:
        counters.parses[path] += 1
        return real_parse(lines, path)

    real_digest = raw_records_module._new_digest

    def spy_digest() -> Any:
        counters.digests += 1
        return real_digest()

    monkeypatch.setattr(Path, "open", spy_open)
    monkeypatch.setattr(raw_records_module, "parse_jsonl_records", spy_parse)
    monkeypatch.setattr(raw_records_module, "_new_digest", spy_digest)
    return counters


class TestArchivingCostsNoRead:
    @pytest.mark.parametrize(
        ("fixture_name", "use_case"),
        [
            ("synthetic_session", convert_and_audit),
            ("codex_rollout", convert_codex_and_audit),
        ],
    )
    def test_each_transcript_is_still_opened_twice_parsed_once_hashed_twice(
        self,
        request: pytest.FixtureRequest,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fixture_name: str,
        use_case: Callable[..., Any],
    ) -> None:
        session = Path(request.getfixturevalue(fixture_name))
        files = discover_session_files(session)
        writer = SourceArchiveWriter(tmp_path / "archive", base=session.parent)
        counters = _count_reads(monkeypatch)

        use_case(session, archive=writer)

        assert {path: counters.opens[path] for path in files} == dict.fromkeys(files, 2)
        assert {path: counters.parses[path] for path in files} == dict.fromkeys(files, 1)
        assert counters.digests == 2 * len(files)
        assert len(writer.files) == len(files)


class TestArchiveUnderMutation:
    def test_an_append_during_conversion_still_fails(
        self, synthetic_session: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        real_convert = harbor_adapter.convert_loaded_session

        def resume_mid_convert(loaded: LoadedSession, **kwargs: Any) -> ConversionResult:
            result = real_convert(loaded, **kwargs)
            with loaded.snapshot.session_jsonl.open("a") as handle:
                handle.write(json.dumps({"type": "user", "uuid": "resumed-1"}) + "\n")
            return result

        monkeypatch.setattr(
            "atif_converter.application.convert_and_audit.convert_loaded_session",
            resume_mid_convert,
        )
        writer = SourceArchiveWriter(tmp_path / "archive", base=synthetic_session.parent)
        with pytest.raises(SourceMutatedDuringConversion):
            convert_and_audit(synthetic_session, archive=writer)
