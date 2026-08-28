# SPDX-License-Identifier: Apache-2.0

"""Atomic file writes: tmp-in-same-dir + fsync + rename, never a partial artifact.

atif-duck reads the corpus with no coordination — no locks, no journal — so
the ONLY correctness lever the writer has is that every artifact path either
holds a complete previous version or a complete new version. ``os.replace``
of a sibling tmp file gives exactly that on POSIX (same filesystem, atomic
rename); a failure mid-serialization leaves the destination untouched and
the tmp file removed.

Durability, not just visibility: write ORDER is not persistence order across
a kernel crash or power loss — a journalling filesystem can replay the
renames while the data blocks are still dirty, publishing a ``meta.json``
(which atif-duck treats as "this session is complete") next to a
zero-length ``trajectory.json``. Every writer here therefore fsyncs the tmp
file BEFORE the rename, and the directory holding the renamed entries after,
so persistence order matches write order. The corpus is regenerable, so this
is cheap insurance rather than a hot-path cost.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from pathlib import Path

#: Compact separators for trajectory.json, fixed by CONTRACT.md.
COMPACT_SEPARATORS: tuple[str, str] = (",", ":")


def _tmp_sibling(path: Path) -> Path:
    """A tmp path in the SAME directory (rename across filesystems isn't atomic)."""
    return path.with_name(f".{path.name}.tmp-{os.getpid()}")


def fsync_dir(path: Path) -> None:
    """Flush ``path``'s directory entries so renames into it survive power loss.

    A rename is metadata: without this the entry can be lost even though the
    file it names was fsynced. Best-effort — a filesystem that refuses
    ``O_RDONLY`` fsync on a directory must not fail a materialization pass
    over a durability upgrade.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as error:
        logger.debug("fsync_dir: cannot open {} ({}); skipping", path, error)
        return
    try:
        os.fsync(fd)
    except OSError as error:
        logger.debug("fsync_dir: fsync of {} failed ({}); skipping", path, error)
    finally:
        os.close(fd)


def write_json_atomic(
    path: Path,
    obj: Any,
    *,
    compact: bool = False,
    indent: int | None = None,
) -> None:
    """Serialize ``obj`` as JSON to ``path`` atomically and durably.

    Streams ``json.dump`` into the tmp file — an object that fails to
    serialize partway (the mid-write failure the tests pin) leaves a partial
    TMP file, which is unlinked, and never a partial destination. The tmp
    file is fsynced before the rename so its bytes are on stable storage
    before the name that publishes them, and the parent directory after so
    the rename itself survives power loss.
    """
    tmp = _tmp_sibling(path)
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            if compact:
                json.dump(obj, handle, separators=COMPACT_SEPARATORS)
            else:
                json.dump(obj, handle, indent=indent)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        logger.debug("atomic write failed; removing tmp {}", tmp)
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def replace_dir_atomic(tmp_dir: Path, dst_dir: Path) -> None:
    """Swap a fully-written ``tmp_dir`` into place at ``dst_dir``.

    The directory rename is the atomicity boundary for an artifact SET: a
    reader either sees the complete previous generation at ``dst_dir`` or
    the complete new one, never a mix. ``tmp_dir`` must be on the same
    filesystem AND must live OUTSIDE any directory readers glob (DuckDB's
    ``read_json`` glob matches dot-dirs, verified on 1.5) — atif-corpus
    stages under ``<corpus_root>/.staging/``, which no reader glob touches.

    ``os.replace`` cannot overwrite a non-empty directory, so an existing
    ``dst_dir`` is first renamed aside NEXT TO ``tmp_dir`` (outside the
    globbed tree) and removed after the swap; an EXCEPTION between the two
    renames triggers the restore below.

    What this does NOT survive: a SIGKILL or power loss in the window
    between the aside-rename and the swap leaves NO generation at
    ``dst_dir`` — no in-process handler can run there, and POSIX offers no
    atomic directory exchange (``renameat2(RENAME_EXCHANGE)`` is
    Linux-specific and unexposed). Recovery is owned by the next pass, not
    by this function:
    :func:`atif_corpus.application.materialize.materialize` sweeps
    ``.staging/`` at pass start and force-replans any session the watermark
    claims is current but whose dir is absent. A reader that catches the
    window sees a missing session, never a torn one.
    """
    old = tmp_dir.with_name(f"{tmp_dir.name}.old-{os.getpid()}")
    shutil.rmtree(old, ignore_errors=True)
    fsync_dir(tmp_dir)
    if dst_dir.is_dir():
        dst_dir.rename(old)
    try:
        tmp_dir.replace(dst_dir)
    except BaseException:
        logger.debug("dir swap failed; restoring previous generation at {}", dst_dir)
        if old.is_dir() and not dst_dir.exists():
            old.rename(dst_dir)
        raise
    fsync_dir(dst_dir.parent)
    shutil.rmtree(old, ignore_errors=True)


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically and durably.

    Same discipline as :func:`write_json_atomic`: tmp sibling, fsync the data
    before the rename that publishes it, fsync the parent directory after so
    the rename survives power loss.
    """
    tmp = _tmp_sibling(path)
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        logger.debug("atomic write failed; removing tmp {}", tmp)
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)
