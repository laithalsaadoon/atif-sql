# SPDX-License-Identifier: Apache-2.0

"""Read the raw records of a session — main JSONL plus every side-file.

The single raw-record reader the census, edges emitter, and enrichment pass
share. Discovery mirrors the contract: rglob over the session's sibling dir
filtered to ``*.jsonl`` (``*.meta.json`` excluded by the suffix filter),
which includes workflow-nested ``subagents/workflows/wf_*/agent-*.jsonl``.
File reads only — no harbor, no env, no network.

A :class:`SessionSnapshot` names a fixed set of source files and carries the
change evidence for each — an mtime/size/content-digest fingerprint — and
NOTHING else. Claude Code appends to a live session at any moment, so the
snapshot is taken before any reader touches the session and re-checked with
:func:`mutated_files` afterwards; an unchanged fingerprint set across the
whole window is the evidence that every reader in that window saw the same
bytes. :func:`read_snapshot_records` parses exactly the snapshot's file list,
so callers choose WHEN to hold parsed records rather than being forced to
hold them for the snapshot's whole lifetime.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from loguru import logger


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
    mid-conversion is exactly that fast — but an append always changes size.

    mtime catches most in-place edits that leave the length alone.

    The digest catches what neither does: a same-length rewrite landing inside
    one mtime tick. That is not hypothetical here — on the xfs volume this runs
    on, 45 consecutive same-size rewrites were absorbed into a single
    ``st_mtime_ns``, so ``{"uuid": "aaa"}`` becoming ``{"uuid": "bbb"}`` is
    invisible to the stat pair. Different bytes behind an identical stat is
    precisely the state this snapshot exists to refuse, so the bytes are read.
    """

    mtime_ns: int
    size: int
    content_hash: bytes


#: Digest chunk size; the transcripts are streamed rather than read whole.
_HASH_CHUNK_BYTES = 1024 * 1024


def _content_hash(path: Path) -> bytes:
    digest = hashlib.blake2b(digest_size=16)
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


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """The fixed file list of one session plus each file's change evidence.

    Holds no records. Parsing is :func:`read_snapshot_records`, so a caller
    that needs the records only briefly can drop them and still re-check the
    snapshot afterwards.
    """

    session_jsonl: Path

    #: Fingerprint per discovered file, sampled before any reader ran.
    fingerprints: dict[Path, FileFingerprint]

    @property
    def files(self) -> tuple[Path, ...]:
        """Every discovered file, main first then sorted side-files."""
        return tuple(self.fingerprints)


def take_session_snapshot(session_jsonl: Path) -> SessionSnapshot:
    """Fingerprint every discovered file; holds digests, never parsed records."""
    return SessionSnapshot(
        session_jsonl=session_jsonl,
        fingerprints={path: _fingerprint(path) for path in discover_session_files(session_jsonl)},
    )


def read_snapshot_records(snapshot: SessionSnapshot) -> list[tuple[dict[str, Any], str]]:
    """Parse every record of exactly the snapshot's file list.

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
        with path.open() as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.debug("raw_records: skipping malformed JSONL line in {}", path)
                    continue
                if isinstance(record, dict):
                    pairs.append((record, source_file))
    return pairs


def mutated_files(snapshot: SessionSnapshot) -> tuple[Path, ...]:
    """Files that changed, vanished, or appeared since the snapshot was taken.

    A newly APPEARED side-file counts: it carries records the snapshot never
    saw, so artifacts derived from the snapshot are already incomplete.
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
