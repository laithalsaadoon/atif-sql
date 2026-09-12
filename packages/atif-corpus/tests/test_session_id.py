# SPDX-License-Identifier: Apache-2.0

"""The session id boundary: the rule itself, its twin in atif-duck, and the scan that applies it.

A session id is the one piece of outside text that becomes part of a corpus
path, so the rule is pinned three ways here: the pure function over every
adversarial shape the task named, the atif-duck copy read as SOURCE TEXT (the
packages may not import each other), and the scanner plus a materialize pass
over transcripts carrying those names, which must skip them, count them, and
leave every existing fixture's behavior alone.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from corpus_fixtures import SESSION_A, STALE_NS, write_session
from loguru import logger

from atif_corpus.application.materialize import MaterializationReport, materialize
from atif_corpus.domain.layout import CorpusLayout
from atif_corpus.domain.session_id import (
    SESSION_ID_MAX_CHARS,
    SESSION_ID_PATTERN,
    is_valid_session_id,
    session_id_rejection,
)
from atif_corpus.domain.source_layout import CODEX_LAYOUT
from atif_corpus.infrastructure.fake_converter import FakeConverter
from atif_corpus.infrastructure.scanner import scan_sources

#: packages/atif-corpus/tests/ -> packages/
_PACKAGES_DIR = Path(__file__).resolve().parents[2]
_DUCK_SESSION_ID = _PACKAGES_DIR / "atif-duck" / "src" / "atif_duck" / "domain" / "session_id.py"

#: Every shape the boundary must refuse. The 256-character name is the one
#: shape a filesystem refuses too (ext4 caps a name at 255 bytes), so it is
#: exercised on the pure function only; the others are also written to disk.
REJECTED_NAMES: dict[str, str] = {
    "single quote": "o'brien",
    "double quote": 'say "hi"',
    "semicolon": "a;b",
    "sql comment": "--comment",
    "positional parameter": "$1",
    "space": "two words",
    "unicode": "sessión",
    "256 chars": "a" * 256,
}

#: The ids the two supported agents really produce.
CLAUDE_CODE_ID = "4b108d1c-0915-4e3d-93a0-45f661b01ae2"
CODEX_ID = "019e9e38-39a6-7170-86fb-46e6f1cb1931"


class TestRule:
    def test_real_ids_from_both_agents_pass(self) -> None:
        assert is_valid_session_id(CLAUDE_CODE_ID)
        assert is_valid_session_id(CODEX_ID)
        assert is_valid_session_id(SESSION_A)

    @pytest.mark.parametrize("name", list(REJECTED_NAMES.values()), ids=list(REJECTED_NAMES))
    def test_each_adversarial_shape_is_rejected_with_a_reason(self, name: str) -> None:
        reason = session_id_rejection(name)
        assert reason is not None
        assert not is_valid_session_id(name)

    def test_the_length_limit_is_exact(self) -> None:
        assert is_valid_session_id("a" * SESSION_ID_MAX_CHARS)
        assert (
            session_id_rejection("a" * (SESSION_ID_MAX_CHARS + 1)) == "longer than 255 characters"
        )

    @pytest.mark.parametrize("name", ["", ".", "..", "-x", "_x", ".hidden", "a/b", "a\\b", "a\nb"])
    def test_a_name_that_does_not_start_with_a_letter_or_digit_is_rejected(self, name: str) -> None:
        assert not is_valid_session_id(name)

    @pytest.mark.parametrize("name", ["a", "A1", "a.b", "a_b", "a-b", "a..b", "abc--def", "x" * 36])
    def test_the_allowed_alphabet_passes(self, name: str) -> None:
        """``--`` inside a name is legal: a name is never statement text, only a path component."""
        assert is_valid_session_id(name)

    def test_a_trailing_newline_is_not_forgiven_by_the_dollar_anchor(self) -> None:
        """``re.match`` with ``$`` accepts ``"abc\\n"``; the boundary must not."""
        assert not is_valid_session_id("abc\n")


class TestTwinPin:
    """atif-duck carries the same rule; the two copies may not drift."""

    @staticmethod
    def _module_constants(source: str) -> dict[str, object]:
        constants: dict[str, object] = {}
        for node in ast.parse(source).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if isinstance(node.value, ast.Constant):
                    constants[node.target.id] = node.value.value
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                        constants[target.id] = node.value.value
        return constants

    def test_twin_is_present(self) -> None:
        assert _DUCK_SESSION_ID.is_file(), f"twin module missing at {_DUCK_SESSION_ID}"

    def test_pattern_and_limit_match_exactly(self) -> None:
        twin = self._module_constants(_DUCK_SESSION_ID.read_text(encoding="utf-8"))
        assert twin["SESSION_ID_PATTERN"] == SESSION_ID_PATTERN
        assert twin["SESSION_ID_MAX_CHARS"] == SESSION_ID_MAX_CHARS

    def test_pattern_is_the_documented_one(self) -> None:
        assert SESSION_ID_PATTERN == r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
        assert SESSION_ID_MAX_CHARS == 255


#: The rejected names a Linux filesystem will accept as a filename.
ON_DISK_REJECTED = {label: name for label, name in REJECTED_NAMES.items() if label != "256 chars"}


def _materialize(source_root: Path, corpus_root: Path) -> MaterializationReport:
    return materialize(
        source_root=source_root,
        corpus_root=corpus_root,
        converter=FakeConverter(),
        quiesce_seconds=300,
        now_ns=STALE_NS + 3_600 * 1_000_000_000,
        materialized_at="2026-01-02T00:00:00+00:00",
        harbor_version="0.22.0",
        converter_version="0.1.0",
    )


class TestScanBoundary:
    def test_each_bad_name_is_skipped_logged_and_counted(self, source_root: Path) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        for name in ON_DISK_REJECTED.values():
            write_session(source_root, name, mtime_ns=STALE_NS)
        warnings: list[str] = []
        sink_id = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
        try:
            scan = scan_sources(source_root)
        finally:
            logger.remove(sink_id)
        assert [s.session_id for s in scan.sessions] == [SESSION_A]
        assert scan.rejected_session_ids == tuple(sorted(ON_DISK_REJECTED.values()))
        assert not scan.unreadable
        rejecting = [w for w in warnings if "rejecting session name" in w]
        assert len(rejecting) == len(ON_DISK_REJECTED)
        for name in ON_DISK_REJECTED.values():
            assert any(repr(name) in w for w in rejecting), name

    def test_a_long_but_legal_name_passes(self, source_root: Path) -> None:
        # 249 + ".jsonl" is the longest filename ext4 stores.
        name = "b" * 249
        write_session(source_root, name, mtime_ns=STALE_NS)
        scan = scan_sources(source_root)
        assert [s.session_id for s in scan.sessions] == [name]
        assert scan.rejected_session_ids == ()

    def test_codex_ids_pass_by_construction(self, tmp_path: Path) -> None:
        day = tmp_path / "2026" / "09" / "11"
        day.mkdir(parents=True)
        (day / f"rollout-2026-09-11T00-00-00-{CODEX_ID}.jsonl").write_text("{}\n")
        scan = scan_sources(tmp_path, CODEX_LAYOUT)
        assert [s.session_id for s in scan.sessions] == [CODEX_ID]
        assert scan.rejected_session_ids == ()

    def test_with_unreadable_keeps_the_rejected_names(self, source_root: Path) -> None:
        write_session(source_root, "o'brien", mtime_ns=STALE_NS)
        scan = scan_sources(source_root).with_unreadable({"zzz": "/nowhere"})
        assert scan.rejected_session_ids == ("o'brien",)


class TestMaterializeBoundary:
    def test_rejected_sessions_are_reported_and_never_written(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        for name in ON_DISK_REJECTED.values():
            write_session(source_root, name, mtime_ns=STALE_NS)
        report = _materialize(source_root, corpus_root)
        assert report.materialized_count == 1
        assert report.rejected_count == len(ON_DISK_REJECTED)
        assert report.rejected_session_ids == tuple(sorted(ON_DISK_REJECTED.values()))
        # The other counters stay exactly what the good session earns.
        assert report.failed_count == 0
        assert report.unreadable_count == 0
        assert report.sessions_removed == 0
        layout = CorpusLayout(corpus_root=corpus_root)
        assert sorted(p.name for p in layout.sessions_dir.iterdir()) == [SESSION_A]
        watermark = json.loads(layout.watermark_path.read_text())
        assert all(SESSION_A in path for path in watermark)

    def test_a_corpus_dir_an_older_version_wrote_under_a_bad_name_is_kept(
        self, source_root: Path, corpus_root: Path
    ) -> None:
        """Rejected is not deleted: a rule change must not remove artifacts by itself."""
        bad = "o'brien"
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        write_session(source_root, bad, mtime_ns=STALE_NS)
        stale_dir = CorpusLayout(corpus_root=corpus_root).sessions_dir / bad
        stale_dir.mkdir(parents=True)
        (stale_dir / "meta.json").write_text("{}")
        warnings: list[str] = []
        sink_id = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
        try:
            report = _materialize(source_root, corpus_root)
        finally:
            logger.remove(sink_id)
        assert report.rejected_session_ids == (bad,)
        assert report.removed_session_ids == ()
        assert stale_dir.is_dir()
        assert any("keeping session dir" in w and repr(bad) in w for w in warnings)

    def test_a_clean_pass_reports_no_rejections(self, source_root: Path, corpus_root: Path) -> None:
        write_session(source_root, SESSION_A, mtime_ns=STALE_NS)
        report = _materialize(source_root, corpus_root)
        assert report.rejected_session_ids == ()
        assert report.rejected_count == 0
