# SPDX-License-Identifier: Apache-2.0

"""Find and read a session's stored JSON artifacts, compressed or plain.

:mod:`atif_duck.domain.artifacts` says which artifacts may be stored as
``<name>.zst``; this module does the filesystem half. DuckDB's JSON readers
decompress a ``.zst`` file on their own (the compression is detected per file
from its extension), so a SQL reader only needs the right path. The Python
readers (the columnar producer's events input, the lake loader's trajectory
decode) go through :func:`read_bytes` and :func:`iter_lines`.

A zstd frame written by atif-sql records its decompressed size in the frame
header, which :func:`decoded_size` reads (18 bytes, no decompression): DuckDB's
``maximum_object_size`` has to be sized from the document, not from the far
smaller compressed file. A frame without that field (written by another tool)
is measured by decompressing it once. A newline-delimited artifact's objects
are its lines, so :func:`longest_line` measures the longest one, decompressing
the file once.
"""

from __future__ import annotations

import contextlib
import io
from typing import TYPE_CHECKING

from atif_duck.domain.artifacts import COMPRESSED_SUFFIX, stored_names

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: The largest zstd frame header (magic, descriptor, window, dictionary id,
#: content size): enough bytes to read the content size of any frame.
_FRAME_HEADER_MAX_BYTES: int = 18

#: Chunk size for the fallback that measures a frame by decompressing it.
_MEASURE_CHUNK_BYTES: int = 1024 * 1024


def is_compressed(path: Path) -> bool:
    """True when ``path`` is a compressed artifact (by its suffix, as DuckDB decides)."""
    return path.name.endswith(COMPRESSED_SUFFIX)


def stored_artifact(session_dir: Path, name: str) -> Path | None:
    """The file ``name`` is stored under in ``session_dir``, or ``None`` if neither exists."""
    for candidate in stored_names(name):
        path = session_dir / candidate
        if path.is_file():
            return path
    return None


def _measure(path: Path) -> int:
    import zstandard

    if not is_compressed(path):
        return path.stat().st_size
    with path.open("rb") as handle:
        header = handle.read(_FRAME_HEADER_MAX_BYTES)
    size = zstandard.frame_content_size(header) if header else 0
    if size >= 0:
        return int(size)
    total = 0
    with path.open("rb") as handle, zstandard.ZstdDecompressor().stream_reader(handle) as reader:
        while chunk := reader.read(_MEASURE_CHUNK_BYTES):
            total += len(chunk)
    return total


def decoded_size(path: Path) -> int:
    """The artifact's size once decompressed (a plain file's own size); 0 if unreadable."""
    import zstandard

    try:
        return _measure(path)
    except (OSError, zstandard.ZstdError):
        return 0


def _longest_line(path: Path) -> int:
    import zstandard

    longest = run = 0
    with path.open("rb") as raw, contextlib.ExitStack() as stack:
        stream = (
            stack.enter_context(zstandard.ZstdDecompressor().stream_reader(raw))
            if is_compressed(path)
            else raw
        )
        while chunk := stream.read(_MEASURE_CHUNK_BYTES):
            pieces = chunk.split(b"\n")
            if len(pieces) == 1:
                run += len(chunk)
                continue
            inner = max(map(len, pieces[1:-1]), default=0)
            longest = max(longest, run + len(pieces[0]), inner)
            run = len(pieces[-1])
    return max(longest, run)


def longest_line(path: Path) -> int:
    """The artifact's longest line in bytes once decompressed, newline excluded; 0 if unreadable.

    Read a chunk at a time, so a file with no newline in it costs one chunk
    of memory rather than the whole file.
    """
    import zstandard

    try:
        return _longest_line(path)
    except (OSError, zstandard.ZstdError):
        return 0


def read_bytes(path: Path) -> bytes:
    """The artifact's decompressed bytes."""
    if not is_compressed(path):
        return path.read_bytes()
    import zstandard

    with path.open("rb") as handle, zstandard.ZstdDecompressor().stream_reader(handle) as reader:
        return reader.readall()


def iter_lines(path: Path) -> Iterator[str]:
    """The artifact's decompressed text, line by line (a missing file yields nothing)."""
    try:
        raw = path.open("rb")
    except FileNotFoundError:
        return
    with raw:
        if is_compressed(path):
            import zstandard

            with (
                zstandard.ZstdDecompressor().stream_reader(raw) as reader,
                io.TextIOWrapper(reader, encoding="utf-8") as text,
            ):
                yield from text
        else:
            with io.TextIOWrapper(raw, encoding="utf-8") as text:
                yield from text


__all__ = [
    "decoded_size",
    "is_compressed",
    "iter_lines",
    "longest_line",
    "read_bytes",
    "stored_artifact",
]
