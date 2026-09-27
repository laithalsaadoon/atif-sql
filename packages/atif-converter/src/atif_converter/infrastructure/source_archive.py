# SPDX-License-Identifier: Apache-2.0

"""Write a zstd-compressed copy of a session's raw source files while they are verified.

The corpus keeps every session's artifacts after the source transcript is gone,
and a future converter can only re-run on a session whose raw bytes survived.
So each successful conversion also leaves an ARCHIVE: one ``<relative path>.zst``
per source file, relative to the main transcript's parent directory, in a
directory the caller names.

WHERE THE BYTES COME FROM. The transcripts are read once for parsing (see
:mod:`atif_converter.infrastructure.raw_records`), and the second, verifying
read re-hashes every byte to prove nothing moved. The archive rides that second
read: :func:`~atif_converter.infrastructure.raw_records.mutated_files` hands each
chunk it hashes to :meth:`SourceArchiveWriter.member` as well. No file is opened
a third time, and the digest that vouches for the archived bytes is the one the
snapshot check compares against the parse's digest, so an archived transcript is
byte-for-byte the input the artifacts were built from. If anything moved, the
conversion raises and the caller throws the half-written archive away.

The other files under the session's side directory (``agent-*.meta.json``, the
``tool-results/`` spill files, workflow state) are not parsed by any converter
today, so :meth:`SourceArchiveWriter.archive_side_files` reads each of them once,
here. They are archived anyway because a future converter may want them and the
originals expire with the transcript.

Level 3 is deliberate: measured on a 76 MB transcript, level 3 compresses at
about 300 MB/s to 2.8x and level 19 reaches 2.9x at 5 MB/s. The archive sits on
the conversion hot path, so the extra 3% is not worth sixty times the CPU.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO

import zstandard

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Generator
    from pathlib import Path

#: zstd level for every archived file; see the module docstring.
ARCHIVE_ZSTD_LEVEL = 3

#: Suffix appended to each archived file's relative path.
ARCHIVE_SUFFIX = ".zst"

#: Archive files hold raw transcript bytes, so they get the transcripts' own
#: owner-only mode rather than whatever the umask allows.
_ARCHIVE_FILE_MODE = 0o600

#: Read size for side files, the same 1 MiB chunk the transcript reader uses.
_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ArchivedSourceFile:
    """One archived file: where it came from, and what its uncompressed bytes were."""

    #: POSIX path relative to the main transcript's parent directory.
    relative_path: str
    #: Uncompressed size in bytes.
    size: int
    #: Hex sha256 of the uncompressed bytes.
    sha256: str


class SourceArchiveWriter:
    """Collects one session's archive under ``archive_dir``.

    ``base`` is the main transcript's parent directory; every member's name is
    its path relative to ``base``, so the archive restores to the same shape
    (``<session>.jsonl`` beside ``<session>/subagents/...``) the converter
    discovers side files from.
    """

    def __init__(self, archive_dir: Path, *, base: Path) -> None:
        self.archive_dir = archive_dir
        self.base = base
        self._files: dict[str, ArchivedSourceFile] = {}

    @property
    def files(self) -> tuple[ArchivedSourceFile, ...]:
        """Every archived file, sorted by relative path."""
        return tuple(self._files[name] for name in sorted(self._files))

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.base).as_posix()

    def _open_member(self, relative_path: str) -> BinaryIO:
        target = self.archive_dir / f"{relative_path}{ARCHIVE_SUFFIX}"
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _ARCHIVE_FILE_MODE)
        return os.fdopen(fd, "wb")

    @contextmanager
    def member(self, path: Path) -> Generator[Callable[[bytes], object]]:
        """Stream one source file into the archive; yields the chunk sink.

        The caller hashes the same chunks itself and reports the digest through
        :meth:`record`, which is how the verifying read's digest, not a second
        one, vouches for the archived bytes.
        """
        with (
            self._open_member(self._relative(path)) as raw,
            zstandard.ZstdCompressor(level=ARCHIVE_ZSTD_LEVEL).stream_writer(
                raw, closefd=False
            ) as compressed,
        ):
            yield compressed.write

    def record(self, path: Path, *, size: int, sha256: bytes) -> None:
        """Note the uncompressed size and digest of a member written through :meth:`member`."""
        relative = self._relative(path)
        self._files[relative] = ArchivedSourceFile(
            relative_path=relative, size=size, sha256=sha256.hex()
        )

    def archive_side_files(self, session_jsonl: Path, *, already: Collection[Path]) -> None:
        """Archive every other regular file under the session's side directory.

        ``already`` holds the transcripts the verifying read archived; they are
        skipped. Symlinks are skipped too: following one would copy bytes from
        outside the session into its archive.
        """
        side_dir = session_jsonl.parent / session_jsonl.stem
        if not side_dir.is_dir() or side_dir.is_symlink():
            return
        skip = set(already)
        for path in sorted(side_dir.rglob("*")):
            if path in skip or path.is_symlink() or not path.is_file():
                continue
            digest = hashlib.sha256()
            size = 0
            with self.member(path) as sink, path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                    digest.update(chunk)
                    sink(chunk)
                    size += len(chunk)
            self.record(path, size=size, sha256=digest.digest())


__all__ = [
    "ARCHIVE_SUFFIX",
    "ARCHIVE_ZSTD_LEVEL",
    "ArchivedSourceFile",
    "SourceArchiveWriter",
]
