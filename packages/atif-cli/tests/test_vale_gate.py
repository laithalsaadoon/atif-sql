# SPDX-License-Identifier: Apache-2.0

"""`scripts/vale_gate.py` (`mise run docs:prose`) fails on planted prose defects and on nothing checked.

The prose gate needs the same anti-vacuity proof every gate here carries: a planted banned
phrase and a planted misspelling each turn it red on the rule that must catch them, a clean
fixture stays green, a scope of zero files is a failure rather than an empty pass, and on the
real tree Vale checks exactly the files an independent walk of the scope finds.

The fixtures live under `.vale/fixtures/`, outside the gate's scope, so the real tree stays
clean while the plants stay committed. Vale is the mise-pinned binary: run this through
`mise run test` or `mise run check`, which put it on PATH.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

#: Repository root: `packages/atif-cli/tests/` -> three parents up.
ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "vale_gate.py"
FIXTURES = ROOT / ".vale" / "fixtures"

_CHECKED = re.compile(r"vale checked (\d+) of (\d+) files: (\d+) errors?")


@pytest.fixture(autouse=True)
def _vale_on_path() -> None:
    # A missing binary must fail, not skip: a skipped plant is a gate nobody proved.
    if shutil.which("vale") is None:
        pytest.fail("vale is not on PATH: run the suite through `mise run test`")


def _run(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(SCRIPT), "--label", "selftest", *(str(p) for p in paths)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


def _checked(stdout: str) -> tuple[int, int, int]:
    match = _CHECKED.search(stdout)
    assert match is not None, stdout
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vale_gate", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_clean_fixture_passes() -> None:
    result = _run(FIXTURES / "clean.md")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _checked(result.stdout) == (1, 1, 0)


def test_banned_phrase_fails_on_its_rule() -> None:
    result = _run(FIXTURES / "planted-phrase.md")
    assert result.returncode == 1, result.stdout + result.stderr
    assert ".vale/fixtures/planted-phrase.md:3:" in result.stdout
    assert "proselint.Cliches: 'for free' is a cliche." in result.stdout
    assert _checked(result.stdout) == (1, 1, 1)


def test_misspelling_fails_on_spelling() -> None:
    result = _run(FIXTURES / "planted-spelling.md")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Vale.Spelling: Did you really mean 'recieves'?" in result.stdout


def test_clean_file_beside_a_plant_is_counted() -> None:
    # Vale's JSON names only files with alerts; the count must still include the clean one.
    result = _run(FIXTURES / "planted-phrase.md", FIXTURES / "clean.md")
    assert result.returncode == 1, result.stdout + result.stderr
    assert _checked(result.stdout) == (2, 2, 1)


def test_missing_file_is_a_usage_error() -> None:
    result = _run(FIXTURES / "absent.md")
    assert result.returncode == 2
    assert "no such file" in result.stderr


def test_empty_scope_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _load_gate()
    monkeypatch.setattr(gate, "scope_files", list)
    assert gate.main([]) == 1
    assert "checked 0 files" in capsys.readouterr().err


def _independent_scope() -> set[Path]:
    """The scope as git lists it (tracked, plus untracked files git does not ignore), not by
    the gate's own globs."""
    listed = subprocess.run(
        [  # noqa: S607 - git from PATH with fixed flags
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "docs",
            "site/authored",
            "README.md",
            "AGENTS.md",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    paths = {ROOT / name for name in listed.stdout.split("\0") if name.endswith(".md")}
    return {p for p in paths if p.is_file()}


def test_real_tree_is_clean_and_fully_checked() -> None:
    expected = _independent_scope()
    for name in ("README.md", "AGENTS.md", "site/authored/index.md", "site/authored/agents.md"):
        assert ROOT / name in expected, name
    assert set(_load_gate().scope_files()) == expected

    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _checked(result.stdout) == (len(expected), len(expected), 0)
