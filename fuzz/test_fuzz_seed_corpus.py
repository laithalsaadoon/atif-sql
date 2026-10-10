# SPDX-License-Identifier: Apache-2.0
"""Replay the committed seed corpus through the fuzz targets, with no fuzzing engine.

This is the half of the fuzzing that runs in ``mise run check``: every seed
under ``corpus/<target>/`` goes through the same function the Atheris harness
drives, so a converter change that makes a seed raise anything but a
``DomainError`` fails here before a fuzzer ever runs. The seeds must also reach
a successful conversion (a corpus the converter rejects outright would start
the fuzzer at the front door), and a planted unexpected exception must escape
the target, which is what makes the harness report a crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import transcript_targets
from transcript_targets import TARGETS

from atif_converter.domain.errors import ConversionError

CORPUS = Path(__file__).resolve().parent / "corpus"


def _seeds() -> list[tuple[str, Path]]:
    return [
        (name, seed)
        for name in sorted(TARGETS)
        for seed in sorted((CORPUS / name).iterdir())
        if seed.is_file()
    ]


def test_every_target_has_seeds() -> None:
    """Each registered target ships at least two seeds, and no seed directory is unregistered."""
    by_target = {name: [seed for target, seed in _seeds() if target == name] for name in TARGETS}
    assert all(len(seeds) >= 2 for seeds in by_target.values()), by_target
    assert sorted(path.name for path in CORPUS.iterdir() if path.is_dir()) == sorted(TARGETS)


@pytest.mark.parametrize(
    ("name", "seed"), _seeds(), ids=lambda value: getattr(value, "name", value)
)
def test_seed_replays_without_unexpected_exception(name: str, seed: Path, tmp_path: Path) -> None:
    """A seed raises nothing but the converter's own rejection."""
    TARGETS[name](seed.read_bytes(), tmp_path)


@pytest.mark.parametrize(
    ("name", "seed"), _seeds(), ids=lambda value: getattr(value, "name", value)
)
def test_seed_converts(
    name: str, seed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every seed converts: none is rejected, so each starts the fuzzer past the reader."""
    monkeypatch.setattr(transcript_targets, "EXPECTED_REJECTIONS", ())
    TARGETS[name](seed.read_bytes(), tmp_path)


@pytest.mark.parametrize("name", sorted(TARGETS))
@pytest.mark.parametrize(
    "data", [b"", b"\xff\xfe not utf-8\n", b'[]\n1\n"x"\nnull\n{\n', b"\x00\x00\x00"]
)
def test_malformed_input_is_rejected_or_converted(name: str, data: bytes, tmp_path: Path) -> None:
    """Empty, non-UTF-8, non-object and truncated JSONL never escape as a crash."""
    TARGETS[name](data, tmp_path)


def test_empty_session_is_an_expected_rejection(tmp_path: Path) -> None:
    """An empty transcript is the converter's ``EmptySessionError``, a ``DomainError``."""
    with pytest.raises(transcript_targets.EXPECTED_REJECTIONS):
        transcript_targets.convert_and_audit(
            transcript_targets.write_claude_code_session(b"", tmp_path)
        )


@pytest.mark.parametrize(
    ("name", "use_case"),
    [("claude_code", "convert_and_audit"), ("codex", "convert_codex_and_audit")],
)
def test_unexpected_exception_escapes(
    name: str, use_case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planted ``KeyError`` in the use case reaches the caller: the harness reports it."""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise KeyError(use_case)

    monkeypatch.setattr(transcript_targets, use_case, explode)
    with pytest.raises(KeyError, match=use_case):
        TARGETS[name](b"{}\n", tmp_path)


def test_domain_error_is_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``DomainError`` from the use case is the documented rejection, not a finding."""

    def reject(*_args: object, **_kwargs: object) -> None:
        msg = "refused"
        raise ConversionError(msg)

    monkeypatch.setattr(transcript_targets, "convert_and_audit", reject)
    TARGETS["claude_code"](b"{}\n", tmp_path)


def test_claude_code_layout_splits_on_nul(tmp_path: Path) -> None:
    """Main transcript, then subagent transcript, then its sidecar, split on NUL."""
    main = transcript_targets.write_claude_code_session(b"a\x00b\x00c", tmp_path)
    side = tmp_path / transcript_targets.CLAUDE_SESSION_ID / "subagents"
    assert main.read_bytes() == b"a"
    assert (side / "agent-fuzz.jsonl").read_bytes() == b"b"
    assert (side / "agent-fuzz.meta.json").read_bytes() == b"c"
