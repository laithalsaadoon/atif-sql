# SPDX-License-Identifier: Apache-2.0

"""`scripts/verify_single_yaml_document.py` rejects what GitHub's dependency graph cannot read.

pnpm 12 can write its own pinned version into `site/pnpm-lock.yaml` as a separate first YAML
document, and GitHub reads only that one. The guard is a `check` leg, so it needs the same
anti-vacuity proof every gate here carries: a planted second document turns it red, an empty
or package-less file turns it red, and the real lockfile is green.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

#: Repository root: `packages/atif-cli/tests/` -> three parents up.
ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "verify_single_yaml_document.py"
LOCKFILE = ROOT / "site" / "pnpm-lock.yaml"

_ENV_DOCUMENT = """---
lockfileVersion: '9.0'

importers:

  .:
    configDependencies: {}
    packageManagerDependencies:
      pnpm:
        specifier: 12.10.1
        version: 12.10.1

packages:

  pnpm@12.10.1:
    hasBin: true

"""

_PROJECT_DOCUMENT = """---
lockfileVersion: '9.0'

importers:

  .:
    dependencies:
      astro:
        specifier: 7.3.5
        version: 7.3.5

packages:

  astro@7.3.5:
    resolution: {integrity: sha512-x}

snapshots:

  astro@7.3.5: {}
"""


def _run(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_real_lockfile_is_one_document() -> None:
    assert LOCKFILE.is_file(), f"{LOCKFILE} is missing"
    result = _run(LOCKFILE)
    assert result.returncode == 0, result.stderr


def test_second_document_is_rejected(tmp_path: Path) -> None:
    lock = tmp_path / "pnpm-lock.yaml"
    lock.write_text(_ENV_DOCUMENT + _PROJECT_DOCUMENT, encoding="utf-8")
    result = _run(lock)
    assert result.returncode == 1
    assert "2 YAML documents" in result.stderr


def test_single_document_with_leading_marker_passes(tmp_path: Path) -> None:
    lock = tmp_path / "pnpm-lock.yaml"
    lock.write_text(_PROJECT_DOCUMENT, encoding="utf-8")
    assert _run(lock).returncode == 0


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("", "no content"),
        ("# only a comment\n", "no content"),
        ("lockfileVersion: '9.0'\npackages:\n", "no top-level importers:"),
        ("lockfileVersion: '9.0'\nimporters:\n  .: {}\n", "no top-level packages:"),
        ("lockfileVersion: '9.0'\nimporters:\n  .: {}\npackages:\n\n", "lists no package"),
    ],
)
def test_vacuous_lockfile_is_rejected(tmp_path: Path, body: str, reason: str) -> None:
    lock = tmp_path / "pnpm-lock.yaml"
    lock.write_text(body, encoding="utf-8")
    result = _run(lock)
    assert result.returncode == 1
    assert reason in result.stderr


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    result = _run(tmp_path / "absent.yaml")
    assert result.returncode == 1
    assert "unreadable" in result.stderr


def test_wrong_arguments_exit_two() -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
