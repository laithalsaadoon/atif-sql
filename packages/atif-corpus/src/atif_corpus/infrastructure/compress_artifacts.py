# SPDX-License-Identifier: Apache-2.0

"""Compress an existing session's plain JSON artifacts in place (``atif-sql corpus slim``).

A session written before materialize stored its JSON artifacts compressed
holds ``trajectory.json``, ``edges.jsonl`` and ``session_events.jsonl`` as
plain files. :func:`compress_in_place` turns one of them into ``<name>.zst``
without a moment where the session lacks a readable copy:

1. stream the plain file through zstd into a pid-suffixed tmp sibling, the
   frame recording the plain size as its content size (what the materialize
   writer records too), then fsync it;
2. read the tmp file back, decompressing, and compare its sha256 with the
   plain file's; a difference unlinks the tmp file and raises, leaving the
   plain file as it was;
3. give the tmp file the plain file's mtime, then rename it to
   ``<name>.zst`` and fsync the directory;
4. unlink the plain file and fsync the directory again.

Between 3 and 4 the session holds both spellings with the same content, and
every reader prefers the compressed one. The mtime is carried over because
analyze's checkpoints bound each session by its trajectory's mtime: a
compressed copy with a new mtime would make every session look rewritten and
re-run every pipeline on it.

The same compressor settings as the materialize writer
(:data:`atif_corpus.infrastructure.atomic.ZSTD_LEVEL`, single-threaded), so a
slimmed session and a freshly converted one store the same bytes for the same
document.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from atif_corpus.domain.layout import COMPRESSED_ARTIFACT_FILENAMES, COMPRESSED_SUFFIX
from atif_corpus.infrastructure.atomic import ZSTD_LEVEL, fsync_dir

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: Read and write chunk size.
_CHUNK_BYTES: int = 1024 * 1024


class CompressionMismatchError(RuntimeError):
    """The compressed copy did not decompress to the plain file's bytes; nothing was replaced."""


@dataclass(frozen=True, slots=True)
class Compressed:
    """One artifact compressed (or, on a dry run, measured)."""

    #: The plain file (removed unless this was a dry run).
    plain: Path
    #: Its size.
    plain_bytes: int
    #: The compressed file's size (on a dry run, what it would be).
    compressed_bytes: int


def plain_artifacts(session_dir: Path) -> list[Path]:
    """The session's artifacts still stored plain, in :data:`COMPRESSED_ARTIFACT_FILENAMES` order."""
    return [
        session_dir / name
        for name in COMPRESSED_ARTIFACT_FILENAMES
        if (session_dir / name).is_file()
    ]


def _compressed_chunks(plain: Path, size: int) -> Iterator[bytes]:
    """``plain``'s bytes as one zstd frame, in pieces (a dry run counts them, a real run writes them)."""
    import zstandard

    compressor = zstandard.ZstdCompressor(level=ZSTD_LEVEL, write_content_size=True, threads=0)
    stream = compressor.compressobj(size=size)
    with plain.open("rb") as source:
        while chunk := source.read(_CHUNK_BYTES):
            if out := stream.compress(chunk):
                yield out
    yield stream.flush()


def _sha256_plain(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_decompressed(path: Path) -> str:
    import zstandard

    digest = hashlib.sha256()
    with path.open("rb") as handle, zstandard.ZstdDecompressor().stream_reader(handle) as reader:
        while chunk := reader.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _check_round_trip(compressed: Path, plain: Path) -> None:
    if _sha256_decompressed(compressed) != _sha256_plain(plain):
        msg = f"{compressed} does not decompress to {plain}; left {plain} as it was"
        raise CompressionMismatchError(msg)


def measure_compressed(plain: Path) -> Compressed:
    """What :func:`compress_in_place` would store for ``plain``, without writing anything."""
    size = plain.stat().st_size
    compressed = sum(len(chunk) for chunk in _compressed_chunks(plain, size))
    return Compressed(plain=plain, plain_bytes=size, compressed_bytes=compressed)


def compress_in_place(plain: Path) -> Compressed:
    """Replace ``plain`` with ``<plain>.zst`` holding the same bytes (see the module docstring).

    Raises
    ------
    CompressionMismatchError
        The compressed copy read back differently; ``plain`` is untouched.
    OSError
        The file or its directory went away (a materialize pass swapped the
        session's directory meanwhile) or could not be written.
    """
    stat = plain.stat()
    target = plain.with_name(f"{plain.name}{COMPRESSED_SUFFIX}")
    tmp = plain.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("wb") as handle:
            for chunk in _compressed_chunks(plain, stat.st_size):
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        _check_round_trip(tmp, plain)
        os.utime(tmp, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        compressed_bytes = tmp.stat().st_size
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(plain.parent)
    plain.unlink()
    fsync_dir(plain.parent)
    logger.debug("slim: {} -> {} ({} -> {} bytes)", plain, target, stat.st_size, compressed_bytes)
    return Compressed(plain=plain, plain_bytes=stat.st_size, compressed_bytes=compressed_bytes)


__all__ = [
    "Compressed",
    "CompressionMismatchError",
    "compress_in_place",
    "measure_compressed",
    "plain_artifacts",
]
