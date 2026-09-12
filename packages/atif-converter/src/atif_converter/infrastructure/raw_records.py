# SPDX-License-Identifier: Apache-2.0

"""Read the raw records of a session, main JSONL plus every side-file, in one pass.

The single raw-record reader the converter, the census, the edges emitter and
the enrichment pass share. Discovery mirrors the contract: rglob over the
session's sibling dir filtered to ``*.jsonl`` (``*.meta.json`` excluded by the
suffix filter), which includes workflow-nested
``subagents/workflows/wf_*/agent-*.jsonl``. File reads only: no harbor, no
env, no network.

ONE READ, ONE PARSE. :func:`load_session` opens each discovered file once and
streams its bytes through a digest on the way to the JSON parser, so the
fingerprint it records is the fingerprint of exactly the bytes that were
parsed. The parsed records are handed to the converter AND to the audit
(census, edges, enrichment), so the three artifacts cannot describe different
bytes: they come from one list. Before this pass existed the converter and the
audit each parsed the files and the snapshot hashed them separately, three
hash passes and two parses of a 135 MB session.

A :class:`SessionSnapshot` names a fixed set of source files and carries the
change evidence for each, an mtime/size/content-digest fingerprint, and
NOTHING else. Claude Code appends to a live session at any moment, so the
snapshot is re-checked with :func:`mutated_files` once the artifacts are
built; an unchanged fingerprint set is the evidence that the session held
still for the whole window, including while it was being read. The digest is
what catches a same-length rewrite inside one mtime tick, so the re-check
hashes the bytes again rather than trusting the stat pair.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, override

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Buffer, Iterable


def discover_session_files(session_jsonl: Path) -> list[Path]:
    """The main session JSONL plus every ``*.jsonl`` under its sibling dir.

    Deterministic order: main file first, then sorted side-files.
    """
    files = [session_jsonl]
    side_dir = session_jsonl.parent / session_jsonl.stem
    if side_dir.is_dir():
        files.extend(sorted(side_dir.rglob("*.jsonl")))
    return files


class FileFingerprint(NamedTuple):
    """Change evidence for one source file: mtime, size, and content digest.

    Each field covers a case the others miss.

    Size catches the append that mtime misses: two appends inside one
    filesystem timestamp tick share an ``st_mtime_ns``, and a session resuming
    mid-conversion is exactly that fast, but an append always changes size.

    mtime catches most in-place edits that leave the length alone.

    The digest catches what neither does: a same-length rewrite landing inside
    one mtime tick. That is not hypothetical here: on the xfs volume this runs
    on, 45 consecutive same-size rewrites were absorbed into a single
    ``st_mtime_ns``, so ``{"uuid": "aaa"}`` becoming ``{"uuid": "bbb"}`` is
    invisible to the stat pair. Different bytes behind an identical stat is
    precisely the state this snapshot exists to refuse, so the bytes are read.
    """

    mtime_ns: int
    size: int
    content_hash: bytes


class _Digest(Protocol):
    """The two methods of a ``hashlib`` object this module uses."""

    def update(self, data: Buffer, /) -> None: ...

    def digest(self) -> bytes: ...


def _new_digest() -> _Digest:
    """A fresh content digest.

    sha256 rather than blake2b: the digest is never persisted or compared
    across hosts, so only its speed matters, and sha256 has hardware
    instructions on current x86 (SHA-NI) and arm64 (SHA2) while blake2b does
    not. Measured on the 76 MB benchmark session: 1.6 GB/s against 0.9 GB/s.
    """
    return hashlib.sha256()


#: Digest chunk size; the transcripts are streamed rather than read whole.
_HASH_CHUNK_BYTES = 1024 * 1024


class _DigestingReader(io.RawIOBase):
    """A raw byte stream that folds every byte it hands out into a digest.

    Sits between the file and the text decoder so the one read that feeds the
    JSON parser also produces the content fingerprint. The digest is complete
    once the reader has been drained, which iterating the wrapping text handle
    to its end guarantees.
    """

    def __init__(self, raw: io.RawIOBase, digest: _Digest) -> None:
        super().__init__()
        self._raw = raw
        self._digest = digest

    @override
    def readable(self) -> bool:
        return True

    @override
    def readinto(self, buffer: Buffer, /) -> int | None:
        count = self._raw.readinto(buffer)
        if count is None:
            return None
        if count:
            self._digest.update(memoryview(buffer)[:count])
        return int(count)

    @override
    def close(self) -> None:
        self._raw.close()
        super().close()


def _content_hash(path: Path) -> bytes:
    digest = _new_digest()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.digest()


def _fingerprint(path: Path) -> FileFingerprint:
    stat = path.stat()
    return FileFingerprint(
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        content_hash=_content_hash(path),
    )


def parse_jsonl_records(lines: Iterable[str], path: Path) -> list[Any]:
    """Parse JSONL lines the way harbor's reading loop does.

    Blank lines are skipped; a line that is not JSON is logged at debug and
    skipped rather than failing the session. A line that IS JSON but not an
    object is kept as-is, as harbor keeps it (the converter fails on it later,
    in the same place harbor fails); the audit readers filter to objects.

    Iterating a text handle splits on newlines only, where ``str.splitlines``
    would also split on U+2028 / U+2029 / form feed, which are legal unescaped
    inside a JSON string. Callers pass the handle, never ``read().splitlines()``.
    """
    records: list[Any] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            records.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            logger.debug("Skipping malformed JSONL line in {}: {}", path, exc)
    return records


def _read_and_fingerprint(path: Path) -> tuple[FileFingerprint, list[Any]]:
    """One pass over ``path``: stat, then hash and parse the same bytes.

    The stat is taken BEFORE the read so an append that lands between the two
    still shows as movement when the post-conversion re-check compares stats;
    the digest covers the bytes the parser actually saw.
    """
    stat = path.stat()
    digest = _new_digest()
    with (
        path.open("rb", buffering=0) as raw,
        io.TextIOWrapper(
            io.BufferedReader(_DigestingReader(raw, digest), _HASH_CHUNK_BYTES),
            encoding="utf-8",
        ) as handle,
    ):
        records = parse_jsonl_records(handle, path)
    fingerprint = FileFingerprint(
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        content_hash=digest.digest(),
    )
    return fingerprint, records


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """The fixed file list of one session plus each file's change evidence.

    Holds no records. A :class:`LoadedSession` pairs one with the records
    parsed from those same bytes; :func:`read_snapshot_records` re-parses
    exactly the snapshot's file list for a caller that holds only the snapshot.
    """

    session_jsonl: Path

    #: Fingerprint per discovered file, sampled by the read that parsed it (or
    #: by :func:`take_session_snapshot`, which reads for the digest alone).
    fingerprints: dict[Path, FileFingerprint]

    @property
    def files(self) -> tuple[Path, ...]:
        """Every discovered file, main first then sorted side-files."""
        return tuple(self.fingerprints)


@dataclass(frozen=True, slots=True)
class LoadedSession:
    """One session's snapshot plus every record parsed from those same bytes.

    ``records_by_file`` keeps each file's records in file order under the
    file's path, main first then sorted side-files, so the converter can
    address the main transcript and the staged side-files by name while the
    audit walks every record of every file. Values are whatever JSON the lines
    held, objects or not, as harbor keeps them; :meth:`record_pairs` is the
    objects-only view the census, edges and enrichment consume.
    """

    snapshot: SessionSnapshot
    records_by_file: dict[Path, list[Any]]

    @property
    def main_records(self) -> list[Any]:
        """The main transcript's records, in file order."""
        return self.records_by_file[self.snapshot.session_jsonl]

    def record_pairs(self) -> list[tuple[dict[str, Any], str]]:
        """Every OBJECT record with its source file, in file order.

        ``source_file`` is relative to the session JSONL's parent directory,
        the contract's edge field. Non-object JSON values are dropped here, as
        the audit never counted them.
        """
        base = self.snapshot.session_jsonl.parent
        pairs: list[tuple[dict[str, Any], str]] = []
        for path, records in self.records_by_file.items():
            source_file = path.relative_to(base).as_posix()
            pairs.extend((record, source_file) for record in records if isinstance(record, dict))
        return pairs


def load_session(session_jsonl: Path) -> LoadedSession:
    """Read every discovered file once: fingerprint and parse in a single pass.

    Raises
    ------
        OSError: a discovered file cannot be opened or read.
        UnicodeDecodeError: a file is not UTF-8.
    """
    fingerprints: dict[Path, FileFingerprint] = {}
    records_by_file: dict[Path, list[Any]] = {}
    for path in discover_session_files(session_jsonl):
        fingerprints[path], records_by_file[path] = _read_and_fingerprint(path)
    return LoadedSession(
        snapshot=SessionSnapshot(session_jsonl=session_jsonl, fingerprints=fingerprints),
        records_by_file=records_by_file,
    )


def take_session_snapshot(session_jsonl: Path) -> SessionSnapshot:
    """Fingerprint every discovered file; holds digests, never parsed records.

    The use cases go through :func:`load_session`, which fingerprints as a
    by-product of the read. This is for a caller that wants the change
    evidence alone.
    """
    return SessionSnapshot(
        session_jsonl=session_jsonl,
        fingerprints={path: _fingerprint(path) for path in discover_session_files(session_jsonl)},
    )


def read_snapshot_records(snapshot: SessionSnapshot) -> list[tuple[dict[str, Any], str]]:
    """Parse every object record of exactly the snapshot's file list.

    Returns ``(record, source_file)`` pairs in file order, ``source_file``
    relative to the session JSONL's parent directory (the contract's edge
    field). Malformed lines are skipped (same policy as harbor itself). Files
    added since the snapshot are NOT read: the snapshot's list is the whole
    input, and :func:`mutated_files` reports the newcomer afterwards.
    """
    base = snapshot.session_jsonl.parent
    pairs: list[tuple[dict[str, Any], str]] = []
    for path in snapshot.files:
        source_file = path.relative_to(base).as_posix()
        with path.open(encoding="utf-8") as handle:
            records = parse_jsonl_records(handle, path)
        pairs.extend((record, source_file) for record in records if isinstance(record, dict))
    return pairs


def mutated_files(snapshot: SessionSnapshot) -> tuple[Path, ...]:
    """Files that changed, vanished, or appeared since the snapshot was taken.

    Re-stats AND re-hashes every file: the digest is the half of the evidence
    that catches a same-length rewrite inside one mtime tick. A newly APPEARED
    side-file counts: it carries records the snapshot never saw, so artifacts
    derived from the snapshot are already incomplete.
    """
    moved: list[Path] = []
    for path, fingerprint in snapshot.fingerprints.items():
        try:
            if _fingerprint(path) != fingerprint:
                moved.append(path)
        except OSError:
            moved.append(path)
    moved.extend(
        path
        for path in discover_session_files(snapshot.session_jsonl)
        if path not in snapshot.fingerprints
    )
    return tuple(moved)
