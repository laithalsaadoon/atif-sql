# SPDX-License-Identifier: Apache-2.0

"""Atomic-write guarantees: a mid-write failure leaves no partial artifact.

The guard is proven by breaking it deliberately: ``json.dump`` streams, so
an unserializable object buried deep in an otherwise-serializable payload
raises AFTER bytes have already hit the tmp file — exactly the torn-write
scenario the tmp+rename discipline exists for.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from atif_corpus.infrastructure.atomic import (
    replace_dir_atomic,
    write_json_atomic,
    write_text_atomic,
)


class _Unserializable:
    """A value json.dump chokes on only after emitting earlier keys."""


def _payload_that_fails_late() -> dict[str, object]:
    # 'aaa…' sorts (and insertion-orders) before the poison pill, so the tmp
    # file demonstrably received partial output before the failure.
    return {"aaa_written_first": "x" * 4096, "zzz_poison": _Unserializable()}


class TestWriteJsonAtomic:
    def test_mid_write_failure_leaves_no_file(self, tmp_path: Path) -> None:
        target = tmp_path / "trajectory.json"
        with pytest.raises(TypeError):
            write_json_atomic(target, _payload_that_fails_late(), compact=True)
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []  # tmp sibling cleaned up too

    def test_mid_write_failure_preserves_previous_version(self, tmp_path: Path) -> None:
        target = tmp_path / "trajectory.json"
        write_json_atomic(target, {"version": 1}, compact=True)
        with pytest.raises(TypeError):
            write_json_atomic(target, _payload_that_fails_late(), compact=True)
        assert json.loads(target.read_text()) == {"version": 1}

    def test_compact_uses_contract_separators(self, tmp_path: Path) -> None:
        target = tmp_path / "trajectory.json"
        write_json_atomic(target, {"a": 1, "b": [1, 2]}, compact=True)
        assert target.read_text() == '{"a":1,"b":[1,2]}\n'

    def test_success_replaces_previous_version(self, tmp_path: Path) -> None:
        target = tmp_path / "meta.json"
        write_json_atomic(target, {"version": 1})
        write_json_atomic(target, {"version": 2})
        assert json.loads(target.read_text()) == {"version": 2}
        assert list(tmp_path.iterdir()) == [target]


class TestWriteTextAtomic:
    def test_round_trip(self, tmp_path: Path) -> None:
        target = tmp_path / "edges.jsonl"
        write_text_atomic(target, '{"uuid":"u-1"}\n')
        assert target.read_text() == '{"uuid":"u-1"}\n'
        assert list(tmp_path.iterdir()) == [target]


class TestReplaceDirAtomic:
    def _staged(self, tmp_path: Path, version: int) -> Path:
        staging = tmp_path / ".staging" / f"s.tmp-{version}"
        staging.mkdir(parents=True)
        (staging / "meta.json").write_text(json.dumps({"version": version}))
        return staging

    def test_fresh_swap_installs_new_dir(self, tmp_path: Path) -> None:
        dst = tmp_path / "sessions" / "s"
        dst.parent.mkdir(parents=True)
        replace_dir_atomic(self._staged(tmp_path, 1), dst)
        assert json.loads((dst / "meta.json").read_text()) == {"version": 1}

    def test_swap_replaces_previous_generation_wholesale(self, tmp_path: Path) -> None:
        dst = tmp_path / "sessions" / "s"
        dst.parent.mkdir(parents=True)
        replace_dir_atomic(self._staged(tmp_path, 1), dst)
        (dst / "stale_extra.json").write_text("{}")  # must not survive the swap
        replace_dir_atomic(self._staged(tmp_path, 2), dst)
        assert json.loads((dst / "meta.json").read_text()) == {"version": 2}
        assert not (dst / "stale_extra.json").exists()
        # no aside/tmp litter anywhere readers glob
        assert [p.name for p in (tmp_path / "sessions").iterdir()] == ["s"]


class TestDurability:
    """Write ORDER is not persistence order across a kernel crash or power
    loss: a journalling filesystem can replay the renames while the data
    blocks are still dirty, publishing meta.json (atif-duck's
    "session complete" gate) next to a zero-length trajectory.json. Every
    writer must therefore fsync the tmp file BEFORE the rename that
    publishes it, and the directory after."""

    def _recorded_order(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Interleaved ``fsync``/``replace`` trace, so ORDER is assertable.

        A test that only asserts "some fsync happened" passes just as
        happily when the fsync moves AFTER the rename, which is the exact
        ordering that makes the fsync useless.
        """
        order: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def _fsync(fd: int) -> None:
            order.append("fsync")
            real_fsync(fd)

        def _replace(
            src: str | os.PathLike[str],
            dst: str | os.PathLike[str],
            **kwargs: int | None,
        ) -> None:
            order.append("replace")
            real_replace(src, dst)

        monkeypatch.setattr(os, "fsync", _fsync)
        monkeypatch.setattr(os, "replace", _replace)
        return order

    def test_json_write_fsyncs_data_before_publishing_the_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order = self._recorded_order(monkeypatch)
        write_json_atomic(tmp_path / "trajectory.json", {"a": 1}, compact=True)

        assert "replace" in order
        assert order[: order.index("replace")].count("fsync") >= 1

    def test_text_write_fsyncs_data_before_publishing_the_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order = self._recorded_order(monkeypatch)
        write_text_atomic(tmp_path / "edges.jsonl", '{"uuid":"u-1"}\n')

        assert "replace" in order
        assert order[: order.index("replace")].count("fsync") >= 1

    def test_writers_fsync_the_parent_directory_after_the_rename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rename is metadata: the entry can be lost even though the file it
        names was fsynced. watermark.json is written outside any staging swap
        and governs every future retry decision, so its rename needs the
        parent-directory fsync too."""
        synced_paths: list[str] = []
        opened: dict[int, str] = {}
        real_fsync, real_open, real_close = os.fsync, os.open, os.close

        def _open(
            path: str | os.PathLike[str], flags: int, *args: int, **kwargs: int | None
        ) -> int:
            fd = real_open(path, flags, *args, **kwargs)
            opened[fd] = str(path)
            return fd

        def _close(fd: int) -> None:
            # Fd numbers are recycled: a closed dir fd left in `opened` gets
            # reused by the next data write, whose fsync would then be
            # miscredited to the directory and pass this test for free.
            opened.pop(fd, None)
            real_close(fd)

        def _fsync(fd: int) -> None:
            if fd in opened:
                synced_paths.append(opened[fd])
            real_fsync(fd)

        monkeypatch.setattr(os, "open", _open)
        monkeypatch.setattr(os, "close", _close)
        monkeypatch.setattr(os, "fsync", _fsync)

        json_dir = tmp_path / "json_dir"
        json_dir.mkdir()
        write_json_atomic(json_dir / "watermark.json", {"/a.jsonl": 1})
        assert str(json_dir) in synced_paths

        text_dir = tmp_path / "text_dir"
        text_dir.mkdir()
        synced_paths.clear()
        write_text_atomic(text_dir / "edges.jsonl", '{"uuid":"u-1"}\n')
        assert str(text_dir) in synced_paths

    def test_dir_swap_fsyncs_staging_and_the_published_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dst = tmp_path / "sessions" / "s"
        dst.parent.mkdir(parents=True)
        staging = tmp_path / ".staging" / "s.tmp-1"
        staging.mkdir(parents=True)
        (staging / "meta.json").write_text("{}")

        synced_paths: list[str] = []
        real_fsync, real_open = os.fsync, os.open
        opened: dict[int, str] = {}

        def _open(
            path: str | os.PathLike[str], flags: int, *args: int, **kwargs: int | None
        ) -> int:
            fd = real_open(path, flags, *args, **kwargs)
            opened[fd] = str(path)
            return fd

        def _fsync(fd: int) -> None:
            if fd in opened:
                synced_paths.append(opened[fd])
            real_fsync(fd)

        monkeypatch.setattr(os, "open", _open)
        monkeypatch.setattr(os, "fsync", _fsync)
        replace_dir_atomic(staging, dst)

        assert str(staging) in synced_paths
        assert str(dst.parent) in synced_paths
