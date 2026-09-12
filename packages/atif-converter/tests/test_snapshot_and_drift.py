# SPDX-License-Identifier: Apache-2.0

"""Guards for the converter invariants that are not about harbor's output.

1. SNAPSHOT CONSISTENCY: census, trajectory, and edges.jsonl describe the
   SAME bytes, and a source that moves during conversion fails the session.
   All three fingerprint fields are pinned: an append inside one mtime tick
   (size catches it), a same-length rewrite (mtime catches it), and a
   same-length rewrite inside one tick (only the digest catches it).
   ONE READ: the use case opens each transcript file once to parse and hash it
   together, and once more for the post-conversion re-check; nothing parses a
   file twice.
2. SNAPSHOT PURITY: the census reads the snapshot's file list, never current
   disk, so counts and classification cannot disagree.
3. CENSUS SCOPE: every discovered ``*.jsonl`` is counted, matching the file
   set the adapter stages and edges.jsonl is built from.
4. (retired with the port: the private-API drift alarm now lives in harbor_oracle)
   raises its own loud error, never the generic per-session ConversionError.
5. IN-PLACE ENRICHMENT: ``copy_input=False`` skips the deep copy while the
   default still protects the caller's dict.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from atif_converter.application import convert_and_audit as convert_and_audit_module
from atif_converter.application.convert_and_audit import convert_and_audit
from atif_converter.application.convert_codex import convert_codex_and_audit
from atif_converter.domain.enrichment import enrich_trajectory
from atif_converter.domain.errors import (
    SourceMutatedDuringConversion,
)
from atif_converter.domain.fidelity import RecordType
from atif_converter.infrastructure import harbor_adapter, raw_records as raw_records_module
from atif_converter.infrastructure.census import census_from_snapshot
from atif_converter.infrastructure.harbor_adapter import ConversionResult
from atif_converter.infrastructure.raw_records import (
    FileFingerprint,
    LoadedSession,
    SessionSnapshot,
    discover_session_files,
    load_session,
    mutated_files,
    read_snapshot_records,
    take_session_snapshot,
)

#: Typed stand-in for a step's absent ``extra``. A bare ``{}`` literal infers as
#: ``dict[Unknown, Unknown]``, which leaks Unknown into every ``.get`` chained
#: onto the ``or`` result.
_NO_EXTRA: Mapping[str, Any] = {}

#: One parsed raw record plus the session-relative path of the file it came from,
#: as ``read_snapshot_records`` yields them.
RawRecords = list[tuple[dict[str, Any], str]]


class TestSnapshotIsReadOnce:
    def test_census_and_edges_agree_on_the_same_records(self, synthetic_session: Path) -> None:
        snapshot = take_session_snapshot(synthetic_session)
        records = read_snapshot_records(snapshot)
        census = census_from_snapshot(snapshot, records)
        assert sum(census.record_counts.values()) == len(records)

    def test_snapshot_holds_no_records(self, synthetic_session: Path) -> None:
        """The snapshot is fingerprints only; holding one across harbor's peak
        must not pin a whole transcript's parsed records in memory."""
        snapshot = take_session_snapshot(synthetic_session)
        assert not hasattr(snapshot, "records")
        assert set(SessionSnapshot.__slots__) == {"session_jsonl", "fingerprints"}

    def test_reading_the_snapshot_ignores_files_added_afterwards(
        self, synthetic_session: Path
    ) -> None:
        """The snapshot's file list is the whole input, so a side-file that
        appears later cannot leak records into artifacts derived from it."""
        snapshot = take_session_snapshot(synthetic_session)
        before = len(read_snapshot_records(snapshot))
        new_side = (
            synthetic_session.parent / synthetic_session.stem / "subagents" / "agent-late.jsonl"
        )
        new_side.write_text(json.dumps({"type": "user", "uuid": "late-1"}) + "\n")
        assert len(read_snapshot_records(snapshot)) == before
        assert new_side in mutated_files(snapshot)

    def test_fingerprints_captured_for_every_discovered_file(self, synthetic_session: Path) -> None:
        snapshot = take_session_snapshot(synthetic_session)
        assert len(snapshot.fingerprints) == 3  # main + flat subagent + workflow-nested
        assert mutated_files(snapshot) == ()


class TestCensusReadsOnlyTheSnapshot:
    def test_classification_ignores_disk_changes_after_the_snapshot(
        self, synthetic_session: Path
    ) -> None:
        """Counts come from the snapshot, so classification must too: moving a
        side-file afterwards cannot empty one list while the counts stand."""
        snapshot = take_session_snapshot(synthetic_session)
        records = read_snapshot_records(snapshot)
        flat_side = (
            synthetic_session.parent / synthetic_session.stem / "subagents" / "agent-abc.jsonl"
        )
        moved = flat_side.with_name("agent-abc.moved.jsonl")
        flat_side.rename(moved)
        census = census_from_snapshot(snapshot, records)
        assert census.subagent_files == (flat_side,)
        assert sum(census.record_counts.values()) == len(records)

    def test_every_discovered_jsonl_is_counted(self, synthetic_session: Path) -> None:
        """A side-file outside ``subagents/`` is staged into harbor and lands in
        edges.jsonl, so the census must count it rather than skip it."""
        stray = synthetic_session.parent / synthetic_session.stem / "stray.jsonl"
        stray.write_text(json.dumps({"type": "result", "uuid": "stray-1"}) + "\n")
        snapshot = take_session_snapshot(synthetic_session)
        records = read_snapshot_records(snapshot)
        census = census_from_snapshot(snapshot, records)
        assert stray in snapshot.files
        assert census.record_counts.get(RecordType.RESULT) == 1
        assert stray in census.workflow_subagent_files

    def test_records_total_equals_the_edges_line_count(self, synthetic_session: Path) -> None:
        stray = synthetic_session.parent / synthetic_session.stem / "stray.jsonl"
        stray.write_text(json.dumps({"type": "result", "uuid": "stray-1"}) + "\n")
        result, report = convert_and_audit(synthetic_session)
        assert report.records_total == len(result.edges_lines)


class TestMutationDetection:
    def test_append_within_one_mtime_tick_is_still_reported(self, synthetic_session: Path) -> None:
        """Two appends inside one filesystem timestamp tick share an mtime, so
        size is what catches a session resuming that fast. The mtime is pinned
        back to its snapshot value to hold that case steady on every run."""
        snapshot = take_session_snapshot(synthetic_session)
        before = snapshot.fingerprints[synthetic_session]
        with synthetic_session.open("a") as handle:
            handle.write(json.dumps({"type": "user", "uuid": "late-1"}) + "\n")
        os.utime(
            synthetic_session,
            ns=(synthetic_session.stat().st_atime_ns, before.mtime_ns),
        )
        assert synthetic_session.stat().st_mtime_ns == before.mtime_ns
        assert synthetic_session.stat().st_size != before.size
        assert mutated_files(snapshot) == (synthetic_session,)

    def test_mtime_bump_at_identical_size_is_still_reported(self, synthetic_session: Path) -> None:
        """The mirror of the append case: an in-place rewrite of equal length
        leaves ``st_size`` untouched, so mtime is the half that catches it. The
        size is asserted unchanged so the assertion cannot pass via size."""
        snapshot = take_session_snapshot(synthetic_session)
        before = snapshot.fingerprints[synthetic_session]
        os.utime(
            synthetic_session,
            ns=(synthetic_session.stat().st_atime_ns, before.mtime_ns + 1_000_000_000),
        )
        assert synthetic_session.stat().st_size == before.size
        assert synthetic_session.stat().st_mtime_ns != before.mtime_ns
        assert mutated_files(snapshot) == (synthetic_session,)

    def test_same_size_rewrite_inside_one_mtime_tick_is_reported(
        self, synthetic_session: Path
    ) -> None:
        """The case neither stat field catches: a same-length rewrite whose
        mtime is ALSO unchanged. Both stat halves are pinned to their snapshot
        values, so only the content digest can fail this."""
        side = synthetic_session.parent / synthetic_session.stem / "subagents" / "agent-abc.jsonl"
        snapshot = take_session_snapshot(synthetic_session)
        before = snapshot.fingerprints[side]
        side.write_text(side.read_text().replace('"su-1"', '"su-9"'))
        os.utime(side, ns=(side.stat().st_atime_ns, before.mtime_ns))
        assert side.stat().st_size == before.size
        assert side.stat().st_mtime_ns == before.mtime_ns
        assert mutated_files(snapshot) == (side,)

    def test_new_side_file_is_reported(self, synthetic_session: Path) -> None:
        """An APPEARING side-file carries records the snapshot never saw, so
        the derived artifacts are already incomplete."""
        snapshot = take_session_snapshot(synthetic_session)
        new_side = (
            synthetic_session.parent / synthetic_session.stem / "subagents" / "agent-new.jsonl"
        )
        new_side.write_text(json.dumps({"type": "user", "uuid": "new-1"}) + "\n")
        assert mutated_files(snapshot) == (new_side,)

    def test_deleted_file_is_reported(self, synthetic_session: Path) -> None:
        snapshot = take_session_snapshot(synthetic_session)
        side = synthetic_session.parent / synthetic_session.stem / "subagents" / "agent-abc.jsonl"
        side.unlink()
        assert mutated_files(snapshot) == (side,)


class TestConvertRefusesOnMidConversionWrite:
    def test_resumed_session_fails_instead_of_desyncing(
        self,
        synthetic_session: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A session that resumes writing between the read and the end of the
        audit must FAIL, not publish artifacts describing bytes the session no
        longer holds."""
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
        with pytest.raises(SourceMutatedDuringConversion) as excinfo:
            convert_and_audit(synthetic_session)
        assert synthetic_session in excinfo.value.mutated

    def test_same_size_rewrite_mid_conversion_fails_too(
        self,
        synthetic_session: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A resume that rewrites bytes without changing the file's length or
        its mtime must fail the session exactly like an append does: only the
        digest half of the re-check can see it, which is why the re-check
        hashes rather than trusting the stat pair."""
        real_convert = harbor_adapter.convert_loaded_session

        def rewrite_mid_convert(loaded: LoadedSession, **kwargs: Any) -> ConversionResult:
            result = real_convert(loaded, **kwargs)
            session_jsonl = loaded.snapshot.session_jsonl
            before = session_jsonl.stat()
            session_jsonl.write_text(session_jsonl.read_text().replace('"u-1"', '"u-9"'))
            os.utime(session_jsonl, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert session_jsonl.stat().st_size == before.st_size
            assert session_jsonl.stat().st_mtime_ns == before.st_mtime_ns
            return result

        monkeypatch.setattr(
            "atif_converter.application.convert_and_audit.convert_loaded_session",
            rewrite_mid_convert,
        )
        with pytest.raises(SourceMutatedDuringConversion) as excinfo:
            convert_and_audit(synthetic_session)
        assert synthetic_session in excinfo.value.mutated

    def test_quiescent_session_still_converts(self, synthetic_session: Path) -> None:
        result, report = convert_and_audit(synthetic_session)
        assert result.is_valid
        assert len(result.edges_lines) == report.records_total

    def test_a_write_during_the_read_is_still_refused(
        self,
        synthetic_session: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The read is the one window left, so a resume landing while the
        side-files are still being read must fail, not publish. The main
        transcript is appended right after ITS read completes, before the
        side-files are read."""
        real_read = raw_records_module._read_and_fingerprint

        def resume_after_main(path: Path) -> tuple[FileFingerprint, list[Any]]:
            fingerprint, records = real_read(path)
            if path == synthetic_session:
                with path.open("a") as handle:
                    handle.write(json.dumps({"type": "user", "uuid": "resumed-2"}) + "\n")
            return fingerprint, records

        monkeypatch.setattr(raw_records_module, "_read_and_fingerprint", resume_after_main)
        with pytest.raises(SourceMutatedDuringConversion) as excinfo:
            convert_and_audit(synthetic_session)
        assert synthetic_session in excinfo.value.mutated


class _ReadCounters:
    """How often each transcript file was opened, parsed and hashed."""

    def __init__(self) -> None:
        self.opens: Counter[Path] = Counter()
        self.parses: Counter[Path] = Counter()
        self.digests = 0


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> _ReadCounters:
    """Spy on every seam a transcript's bytes can enter through.

    ``Path.open`` counts the opens (both the parse read and the hash read go
    through it), ``parse_jsonl_records`` counts the parses, and ``_new_digest``
    counts the hash passes: one digest object is one pass over one file.
    """
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


class TestSinglePass:
    """The use cases read each transcript file once for everything.

    One open parses and hashes the file together; the only other open is the
    post-conversion re-check's hash. Two opens, one parse, two hash passes per
    file, for both agents. The previous flow opened each file five times,
    parsed it twice and hashed it three times.
    """

    @pytest.mark.parametrize(
        ("fixture_name", "use_case"),
        [
            ("synthetic_session", convert_and_audit),
            ("codex_rollout", convert_codex_and_audit),
        ],
    )
    def test_each_file_is_opened_twice_parsed_once_hashed_twice(
        self,
        request: pytest.FixtureRequest,
        monkeypatch: pytest.MonkeyPatch,
        fixture_name: str,
        use_case: Callable[[Path], Any],
    ) -> None:
        session = Path(request.getfixturevalue(fixture_name))
        files = discover_session_files(session)
        counters = _count_reads(monkeypatch)

        use_case(session)

        assert {path: counters.opens[path] for path in files} == dict.fromkeys(files, 2)
        assert {path: counters.parses[path] for path in files} == dict.fromkeys(files, 1)
        assert counters.digests == 2 * len(files)

    def test_streamed_digest_equals_the_plain_hash(self, synthetic_session: Path) -> None:
        """The fingerprint taken while parsing is the fingerprint of the bytes
        on disk: same digest as hashing the file alone, same stat pair."""
        loaded = load_session(synthetic_session)
        assert loaded.snapshot == take_session_snapshot(synthetic_session)
        assert mutated_files(loaded.snapshot) == ()

    def test_loaded_records_equal_the_snapshot_reparse(self, synthetic_session: Path) -> None:
        """The audit view of one read is the audit view the separate parse
        gave: same object records, same source-file labels, same order."""
        loaded = load_session(synthetic_session)
        assert loaded.record_pairs() == read_snapshot_records(loaded.snapshot)
        assert len(loaded.record_pairs()) == 9

    def test_converter_and_audit_see_the_same_records(
        self,
        synthetic_session: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The converter is handed the loaded records themselves, so the
        trajectory and the census cannot come from different bytes."""
        seen: list[LoadedSession] = []
        real_convert = harbor_adapter.convert_loaded_session

        def capture(loaded: LoadedSession, **kwargs: Any) -> ConversionResult:
            seen.append(loaded)
            return real_convert(loaded, **kwargs)

        monkeypatch.setattr(convert_and_audit_module, "convert_loaded_session", capture)
        result, report = convert_and_audit(synthetic_session)
        assert len(seen) == 1
        assert report.records_total == len(seen[0].record_pairs()) == len(result.edges_lines)


class TestEnrichInPlace:
    def test_copy_input_false_mutates_the_given_dict(self, synthetic_session: Path) -> None:
        result = harbor_adapter.convert_session(synthetic_session)
        records = [
            record
            for record, _src in read_snapshot_records(take_session_snapshot(synthetic_session))
        ]
        enriched = enrich_trajectory(result.trajectory, records, copy_input=False)
        assert enriched is result.trajectory

    def test_default_still_leaves_the_input_untouched(self, synthetic_session: Path) -> None:
        result = harbor_adapter.convert_session(synthetic_session)
        records = [
            record
            for record, _src in read_snapshot_records(take_session_snapshot(synthetic_session))
        ]
        enriched = enrich_trajectory(result.trajectory, records)
        assert enriched is not result.trajectory
        assert not any(
            (step.get("extra") or _NO_EXTRA).get("source_uuids")
            for step in result.trajectory["steps"]
        )

    def test_both_modes_produce_the_same_enrichment(self, synthetic_session: Path) -> None:
        records = [
            record
            for record, _src in read_snapshot_records(take_session_snapshot(synthetic_session))
        ]
        copied = enrich_trajectory(
            harbor_adapter.convert_session(synthetic_session).trajectory, records
        )
        in_place = enrich_trajectory(
            harbor_adapter.convert_session(synthetic_session).trajectory,
            records,
            copy_input=False,
        )
        assert copied == in_place
