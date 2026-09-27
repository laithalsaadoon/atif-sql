# SPDX-License-Identifier: Apache-2.0

"""A session's raw source archive: its ``meta.json`` manifest, and restoring it.

Every session materialized from a live source carries ``source/`` in its corpus
dir: one ``<relative path>.zst`` per source file (the main transcript, every
side-file transcript, and every other file under the session's side directory),
written by the converter from the exact bytes it parsed. ``meta.json`` describes
it under ``source_archive``::

    {
        "codec": "zstd",
        "source_dir": "-home-me-proj",  # the main transcript's parent, relative
        # to the source root it was scanned under
        "main": "<session>.jsonl",  # the main transcript, relative to source_dir
        "files": [{"path": "<session>.jsonl", "size": 123, "sha256": "..."}, ...],
    }

The archive is what lets a future converter re-run a session after Claude Code or
Codex deleted the original. :func:`restore_session_sources` rebuilds the source
tree under a directory the caller owns and returns the main transcript's path
there; materialize uses it to re-convert a source-removed session whose recorded
generation is stale, and any caller can use it to point a converter at the
restored copy. ``source_dir`` is restored too because the converters read the
transcript's parent directory name (the fallback session id).

Every path in the manifest becomes a filesystem path, so each one is checked
before use: relative, no empty, ``.`` or ``..`` part, no backslash or NUL.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import zstandard

from atif_corpus.domain.layout import META_FILENAME, SOURCE_ARCHIVE_DIRNAME

if TYPE_CHECKING:
    from collections.abc import Sequence

    from atif_corpus.domain.ports import ArchivedSource

#: The meta.json key that holds the manifest.
META_SOURCE_ARCHIVE_KEY = "source_archive"

#: The only codec written so far.
ARCHIVE_CODEC = "zstd"

#: Suffix of every archived file.
ARCHIVE_SUFFIX = ".zst"

#: Decompression read size.
_CHUNK_BYTES = 1024 * 1024


class SourceArchiveError(RuntimeError):
    """The archive is missing, malformed, or does not decompress to what it claims."""


def archive_path_rejection(path: str) -> str | None:
    """Why ``path`` may not become a path under the archive, or ``None`` if it may."""
    if not path:
        return "empty path"
    if "\\" in path or "\x00" in path:
        return "backslash or NUL in path"
    pure = PurePosixPath(path)
    if pure.is_absolute():
        return "absolute path"
    if any(part in {"", ".", ".."} for part in path.split("/")):
        return "empty, '.' or '..' component"
    return None


def _require_safe(path: str, what: str) -> str:
    rejection = archive_path_rejection(path)
    if rejection is not None:
        msg = f"source archive {what} {path!r} rejected: {rejection}"
        raise SourceArchiveError(msg)
    return path


def archive_manifest(
    files: Sequence[ArchivedSource],
    *,
    source_dir: str,
    main: str,
) -> dict[str, Any]:
    """The ``meta.json`` ``source_archive`` value for one freshly archived session.

    Raises :class:`SourceArchiveError` when a path is unsafe or the main
    transcript is not among the files, so a converter that archived the wrong
    thing fails the session rather than publishing an archive nothing can
    restore.
    """
    if source_dir != ".":
        _require_safe(source_dir, "source_dir")
    _require_safe(main, "main")
    entries = [
        {"path": _require_safe(f.relative_path, "file"), "size": f.size, "sha256": f.sha256}
        for f in sorted(files, key=lambda f: f.relative_path)
    ]
    if main not in {entry["path"] for entry in entries}:
        msg = f"source archive does not contain its main transcript {main!r}"
        raise SourceArchiveError(msg)
    return {"codec": ARCHIVE_CODEC, "source_dir": source_dir, "main": main, "files": entries}


def verify_archive_on_disk(archive_dir: Path, files: Sequence[ArchivedSource]) -> None:
    """Every file the converter claims to have archived exists under ``archive_dir``."""
    for archived in files:
        _require_safe(archived.relative_path, "file")
        member = archive_dir / f"{archived.relative_path}{ARCHIVE_SUFFIX}"
        if not member.is_file():
            msg = f"converter listed {archived.relative_path!r} but wrote no {member}"
            raise SourceArchiveError(msg)


def read_manifest(session_dir: Path) -> dict[str, Any] | None:
    """The ``source_archive`` manifest of one corpus session dir, or ``None`` without one."""
    try:
        meta = json.loads((session_dir / META_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        msg = f"cannot read {session_dir / META_FILENAME}: {error}"
        raise SourceArchiveError(msg) from error
    if not isinstance(meta, dict):
        msg = f"{session_dir / META_FILENAME} is not a JSON object"
        raise SourceArchiveError(msg)
    manifest = meta.get(META_SOURCE_ARCHIVE_KEY)
    if manifest is None:
        return None
    if not isinstance(manifest, dict):
        msg = f"{session_dir / META_FILENAME} has a malformed {META_SOURCE_ARCHIVE_KEY}"
        raise SourceArchiveError(msg)
    return manifest


def _restore_one(member: Path, target: Path, *, size: int, sha256: str) -> None:
    digest = hashlib.sha256()
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with member.open("rb") as compressed, target.open("wb") as out:
        reader = zstandard.ZstdDecompressor().stream_reader(compressed)
        for chunk in iter(lambda: reader.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
            out.write(chunk)
            written += len(chunk)
    if written != size or digest.hexdigest() != sha256:
        msg = (
            f"{member} decompressed to {written} bytes with sha256 {digest.hexdigest()}, "
            f"expected {size} bytes with sha256 {sha256}"
        )
        raise SourceArchiveError(msg)


def restore_session_sources(session_dir: Path, dest_root: Path) -> Path:
    """Rebuild one session's raw source tree under ``dest_root``; return the main transcript.

    ``session_dir`` is a corpus session dir (``<corpus>/sessions/<id>``). Files
    land at ``dest_root/<source_dir>/<path>`` with their original bytes, each
    verified against its recorded size and sha256, so ``dest_root`` stands in
    for the source root the session was scanned under and the returned path
    can be handed to the same converter the live source would have been.

    ``dest_root`` must be a directory the caller owns and removes; nothing
    here cleans it up. Raises :class:`SourceArchiveError` when the session has
    no archive, the manifest is malformed, or a file fails verification.
    """
    manifest = read_manifest(session_dir)
    if manifest is None:
        msg = f"{session_dir} has no source archive"
        raise SourceArchiveError(msg)
    if manifest.get("codec") != ARCHIVE_CODEC:
        msg = f"{session_dir} source archive codec {manifest.get('codec')!r} is not supported"
        raise SourceArchiveError(msg)
    source_dir = str(manifest.get("source_dir", ""))
    main = _require_safe(str(manifest.get("main", "")), "main")
    base = dest_root if source_dir == "." else dest_root / _require_safe(source_dir, "source_dir")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        msg = f"{session_dir} source archive lists no files"
        raise SourceArchiveError(msg)
    archive_dir = session_dir / SOURCE_ARCHIVE_DIRNAME
    for entry in files:
        if not isinstance(entry, dict):
            msg = f"{session_dir} source archive has a malformed file entry"
            raise SourceArchiveError(msg)
        path = _require_safe(str(entry.get("path", "")), "file")
        _restore_one(
            archive_dir / f"{path}{ARCHIVE_SUFFIX}",
            base / path,
            size=int(entry.get("size", -1)),
            sha256=str(entry.get("sha256", "")),
        )
    restored_main = base / main
    if not restored_main.is_file():
        msg = f"{session_dir} source archive does not contain its main transcript {main!r}"
        raise SourceArchiveError(msg)
    return restored_main
