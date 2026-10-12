# SPDX-License-Identifier: Apache-2.0

"""`scripts/docs_citations.py` (`mise run docs:citations`) fails on planted citation drift.

Each test builds a two-commit repository in `tmp_path`: a base where `docs/page.md` cites
`src/a.py` correctly, then a working-tree change. A clean tree passes; each way a citation goes
wrong without its doc line changing turns the gate red on its own: lines inserted above the
cited code (moved, and `--fix` re-points it), the cited code deleted (changed), a range past
the end of the file, a shorthand under a fresh heading, a path git does not track, and an empty
scope. An edit in place passes with a note, and a citation whose doc line the change edits is
left to whoever edited it. The real tree's own citations pass the static check.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

#: Repository root: `packages/atif-cli/tests/` -> three parents up.
ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "docs_citations.py"

CODE = "".join(f"line {n}\n" for n in range(1, 10)).replace("line 3\n", "def target():\n")
PAGE = (
    "# Page\n\n"
    "The target is `src/a.py:3`, and the block after it is `:4-5`.\n\n"
    "| Name | Where |\n| --- | --- |\n| target | `src/a.py:3` |\n"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("docs_citations", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["docs_citations"] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - git from PATH with fixed flags inside tmp_path
        [  # noqa: S607
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository whose base commit cites `src/a.py` correctly from `docs/page.md`."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "ATIF_SQL_CITATIONS_BASE"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "a.py").write_text(CODE)
    (tmp_path / "docs" / "page.md").write_text(PAGE)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _run(repo: Path, *extra: str) -> tuple[int, str]:
    base = _git(repo, "rev-parse", "HEAD")
    result = subprocess.run(  # noqa: S603 - this interpreter and the repository's own script
        [sys.executable, str(SCRIPT), "--root", str(repo), "--base", base, *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def test_clean_tree_passes(repo: Path) -> None:
    code, out = _run(repo)
    assert code == 0, out
    assert "3 citations on 1 pages, 0 problems" in out


def test_lines_inserted_above_the_cited_code_fail_as_moved(repo: Path) -> None:
    (repo / "src" / "a.py").write_text("new 1\nnew 2\n" + CODE)
    code, out = _run(repo)
    assert code == 1
    assert "moved    docs/page.md:3: `src/a.py:3` now sits at `src/a.py:5`" in out
    assert "moved    docs/page.md:3: `:4-5` now sits at `:6-7`" in out
    assert "moved    docs/page.md:7: `src/a.py:3` now sits at `src/a.py:5`" in out


def test_fix_rewrites_moved_citations_and_then_passes(repo: Path) -> None:
    (repo / "src" / "a.py").write_text("new 1\nnew 2\n" + CODE)
    code, out = _run(repo, "--fix")
    assert code == 0, out
    assert "rewrote 3 moved citations" in out
    page = (repo / "docs" / "page.md").read_text()
    assert "The target is `src/a.py:5`, and the block after it is `:6-7`." in page
    assert "| target | `src/a.py:5` |" in page
    assert (repo / "src" / "a.py").read_text().splitlines()[4] == "def target():"


def test_a_block_moved_past_a_rewrite_is_found_by_its_text(repo: Path) -> None:
    lines = CODE.splitlines(keepends=True)
    # The target moves to the end while the lines around its old place are rewritten.
    (repo / "src" / "a.py").write_text("".join([*lines[:2], "x\n", "y\n", *lines[5:], lines[2]]))
    code, out = _run(repo)
    assert code == 1
    assert "`src/a.py:3` now sits at `src/a.py:9`" in out


def test_deleted_cited_code_fails_as_changed(repo: Path) -> None:
    (repo / "src" / "a.py").write_text(CODE.replace("def target():\n", ""))
    code, out = _run(repo)
    assert code == 1
    assert "changed  docs/page.md:3: `src/a.py:3`: the cited lines in src/a.py were removed" in out


def test_an_edit_in_place_passes_with_a_note(repo: Path) -> None:
    (repo / "src" / "a.py").write_text(CODE.replace("def target():", "def target(x):"))
    code, out = _run(repo)
    assert code == 0, out
    assert "edited   docs/page.md:3: `src/a.py:3`" in out


def test_a_doc_line_the_change_edits_is_left_to_its_editor(repo: Path) -> None:
    (repo / "src" / "a.py").write_text("new 1\nnew 2\n" + CODE)
    page = repo / "docs" / "page.md"
    page.write_text(page.read_text().replace("The target is", "The function is"))
    code, out = _run(repo)
    assert code == 1
    assert "docs/page.md:3" not in out
    assert "docs/page.md:7" in out


def test_a_range_past_the_end_of_the_file_fails(repo: Path) -> None:
    page = repo / "docs" / "page.md"
    page.write_text(page.read_text() + "\nThe tail is `src/a.py:8-12`.\n")
    code, out = _run(repo)
    assert code == 1
    assert (
        "range    docs/page.md:9: `src/a.py:8-12` names lines 8-12 of src/a.py, which has 9" in out
    )


def test_a_shorthand_under_a_fresh_heading_fails(repo: Path) -> None:
    page = repo / "docs" / "page.md"
    page.write_text(page.read_text() + "\n## Next\n\nAnd `:4`.\n")
    code, out = _run(repo)
    assert code == 1
    assert "orphan   docs/page.md:11: `:4` is a shorthand with no antecedent" in out


def test_a_shorthand_in_a_table_row_does_not_inherit_the_row_above(repo: Path) -> None:
    page = repo / "docs" / "page.md"
    page.write_text(page.read_text() + "| again | `:4` |\n")
    code, out = _run(repo)
    assert code == 1
    assert "orphan   docs/page.md:8: `:4`" in out


def test_an_untracked_path_fails(repo: Path) -> None:
    (repo / "src" / "b.py").write_text("x\n")
    page = repo / "docs" / "page.md"
    page.write_text(page.read_text() + "\nSee `src/b.py:1`.\n")
    code, out = _run(repo)
    assert code == 1
    assert "missing  docs/page.md:9: `src/b.py:1` names src/b.py, which git does not track" in out


def test_a_scope_with_no_citation_fails(repo: Path) -> None:
    (repo / "docs" / "page.md").write_text("# Page\n\nNo citations here.\n")
    code, out = _run(repo)
    assert code == 1
    assert "found 0 citations: the gate checked nothing" in out


def test_a_base_that_does_not_resolve_is_a_usage_error(repo: Path) -> None:
    result = subprocess.run(  # noqa: S603 - this interpreter and the repository's own script
        [sys.executable, str(SCRIPT), "--root", str(repo), "--base", "no-such-rev"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "does not name a commit" in result.stderr


def test_the_real_tree_passes_the_static_check() -> None:
    report = gate.check(ROOT, None)
    assert report.problems == []
    # Never a vacuous pass: the docs tree carries thousands of citations.
    assert report.citations > 1000
    assert report.pages >= 15
