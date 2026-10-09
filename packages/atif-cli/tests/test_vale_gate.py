# SPDX-License-Identifier: Apache-2.0

"""`scripts/vale_gate.py` (`mise run docs:prose`) fails on planted prose defects and on nothing checked.

The prose gate needs the same anti-vacuity proof every gate here carries: a planted banned
phrase and a planted misspelling each turn it red on the rule that must catch them, a clean
fixture stays green, a scope of zero files is a failure rather than an empty pass, a glob that
matches fewer files than its floor fails naming that glob, and on the real tree Vale checks
exactly the files an independent walk of the scope finds.

The warning ratchet carries the same proof: a planted em dash beside the real tree turns it red
naming Google.EmDash, a rule deleted from the baseline is red naming that rule, one warning
fewer than the baseline is green and asks for the baseline to come down, and a baseline that is
missing, empty, unparsable or not counts fails before Vale runs.

The fixtures live under `.vale/fixtures/`, outside the gate's scope, so the real tree stays
clean while the plants stay committed. Vale is the mise-pinned binary: run this through
`mise run test` or `mise run check`, which put it on PATH.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

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
    # With every floor at zero only the zero-files guard stands between nothing and a pass.
    gate = _load_gate()
    monkeypatch.setattr(gate, "SCOPE", dict.fromkeys(gate.SCOPE, 0))
    monkeypatch.setattr(gate, "scope_by_glob", dict)
    assert gate.main([]) == 1
    assert "checked 0 files" in capsys.readouterr().err


# --- floors: a shrunken scope is red, naming the glob that came up short ---------------------


def test_every_glob_has_a_floor_of_at_least_one() -> None:
    scope = _load_gate().SCOPE
    assert set(scope) == {
        "docs/**/*.md",
        "site/authored/**/*.md",
        "README.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
    }
    assert all(floor >= 1 for floor in scope.values()), scope
    assert scope["docs/**/*.md"] == 15
    assert scope["site/authored/**/*.md"] == 2


def _real_scope_without(gate: ModuleType, pattern: str, keep: int) -> dict[str, list[Path]]:
    """The real tree's scope by glob, with `pattern` cut to its first `keep` files."""
    by_glob = cast("dict[str, list[Path]]", gate.scope_by_glob())
    by_glob[pattern] = by_glob[pattern][:keep]
    return by_glob


def _main_with(
    monkeypatch: pytest.MonkeyPatch, gate: ModuleType, by_glob: dict[str, list[Path]]
) -> int:
    monkeypatch.setattr(gate, "scope_by_glob", lambda: by_glob)
    return cast("int", gate.main(["--label", "selftest"]))


def test_docs_moved_away_fails_naming_its_glob(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The defect the floor exists for: without docs/ the other globs still match 5 files, and
    # "0 errors in 5 files" passed the gate before it had floors.
    gate = _load_gate()
    by_glob = _real_scope_without(gate, "docs/**/*.md", 0)
    assert sum(len(v) for v in by_glob.values()) > 0
    assert _main_with(monkeypatch, gate, by_glob) == 1
    err = capsys.readouterr().err
    assert "selftest: docs/**/*.md matched 0 files, below its floor of 15" in err
    assert "site/authored" not in err


def test_docs_one_below_its_floor_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _load_gate()
    assert _main_with(monkeypatch, gate, _real_scope_without(gate, "docs/**/*.md", 14)) == 1
    assert "docs/**/*.md matched 14 files, below its floor of 15" in capsys.readouterr().err


def test_docs_at_its_floor_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The floor is a minimum, not an exact count: 15 docs pages pass and Vale checks them.
    gate = _load_gate()
    by_glob = _real_scope_without(gate, "docs/**/*.md", 15)
    expected = len(set().union(*by_glob.values()))
    assert _main_with(monkeypatch, gate, by_glob) == 0
    assert _checked(capsys.readouterr().out) == (expected, expected, 0)


def test_site_authored_emptied_fails_naming_its_glob(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _load_gate()
    assert _main_with(monkeypatch, gate, _real_scope_without(gate, "site/authored/**/*.md", 0)) == 1
    err = capsys.readouterr().err
    assert "site/authored/**/*.md matched 0 files, below its floor of 2" in err
    assert "docs/**/*.md" not in err


def test_site_authored_one_page_short_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _load_gate()
    assert _main_with(monkeypatch, gate, _real_scope_without(gate, "site/authored/**/*.md", 1)) == 1
    assert "site/authored/**/*.md matched 1 file, below its floor of 2" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["README.md", "AGENTS.md", "CONTRIBUTING.md"])
def test_missing_root_manual_fails_naming_it(
    name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _load_gate()
    assert _main_with(monkeypatch, gate, _real_scope_without(gate, name, 0)) == 1
    assert f"selftest: {name} matched 0 files, below its floor of 1" in capsys.readouterr().err


def test_every_short_glob_is_named() -> None:
    gate = _load_gate()
    short = gate.shortfalls({"README.md": [ROOT / "README.md"]})
    assert [line.split(" matched ")[0] for line in short] == [
        "docs/**/*.md",
        "site/authored/**/*.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
    ]


def test_floors_count_files_git_does_not_ignore(tmp_path: Path) -> None:
    # A tree laid out like the repository, outside it: ignored pages do not count toward the
    # floor, so a laptop's untracked drafts cannot hold up a scope CI sees shrunken.
    gate = _load_gate()
    subprocess.run(  # noqa: S603 - git from PATH with fixed flags into our tmp_path
        ["git", "init", "-q", str(tmp_path)],  # noqa: S607
        check=True,
    )
    (tmp_path / ".gitignore").write_text("docs/parity/\n")
    for rel in ("docs/a.md", "docs/sub/b.md", "docs/parity/c.md", "site/authored/index.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("# Page\n")
    by_glob = gate.scope_by_glob(tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in by_glob["docs/**/*.md"]] == [
        "docs/a.md",
        "docs/sub/b.md",
    ]
    floors = {"docs/**/*.md": 2, "site/authored/**/*.md": 2, "README.md": 1}
    assert [line.split(" matched ")[0] for line in gate.shortfalls(by_glob, floors)] == [
        "site/authored/**/*.md",
        "README.md",
    ]
    assert gate.shortfalls(by_glob, {"docs/**/*.md": 2}) == []


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
            "CONTRIBUTING.md",
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
    for name in (
        "README.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "site/authored/index.md",
        "site/authored/agents.md",
    ):
        assert ROOT / name in expected, name
    gate = _load_gate()
    assert set(gate.scope_files()) == expected
    # The real tree meets every floor, so the floors bind only on a shrunken scope.
    assert gate.shortfalls(gate.scope_by_glob()) == []

    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _checked(result.stdout) == (len(expected), len(expected), 0)
    assert "0 rules above the baseline" in result.stdout, result.stdout


# --- the warning ratchet: a rule above its baseline is red, naming the rule -----------------


@pytest.fixture(scope="module")
def real_counts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, int]:
    """The real tree's warnings per rule, as the gate's own writer measures them."""
    gate = _load_gate()
    path = tmp_path_factory.mktemp("baseline") / ".vale-baseline.json"
    setattr(gate, "BASELINE", path)  # noqa: B010 - a fresh module instance, not the shared one
    assert gate.main(["--label", "selftest", "--write-baseline"]) == 0
    counts = cast("dict[str, int]", json.loads(path.read_text()))
    assert sum(counts.values()) > 0, counts
    return counts


def _gate_with_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, baseline: object
) -> ModuleType:
    gate = _load_gate()
    path = tmp_path / ".vale-baseline.json"
    path.write_text(baseline if isinstance(baseline, str) else json.dumps(baseline))
    monkeypatch.setattr(gate, "BASELINE", path)
    return gate


def test_committed_baseline_is_usable() -> None:
    gate = _load_gate()
    baseline = gate.load_baseline(gate.BASELINE)
    assert baseline, baseline
    assert all(rule.split(".")[0] in {"Vale", "Google", "proselint"} for rule in baseline)


def test_planted_em_dash_fails_naming_its_rule(
    real_counts: dict[str, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # One more spaced em dash on a docs page than the baseline allows: the real tree plus a
    # page carrying exactly one, under the real tree's own counts.
    gate = _gate_with_baseline(monkeypatch, tmp_path, real_counts)
    by_glob = cast("dict[str, list[Path]]", gate.scope_by_glob())
    by_glob["docs/**/*.md"] = [*by_glob["docs/**/*.md"], FIXTURES / "planted-emdash.md"]
    assert _main_with(monkeypatch, gate, by_glob) == 1
    out, err = capsys.readouterr()
    base = real_counts["Google.EmDash"]
    assert err.splitlines() == [f"selftest: Google.EmDash: {base + 1} warnings, baseline {base}"]
    assert "1 rule above the baseline" in out
    assert gate.LOWER_HINT not in out


def test_rule_deleted_from_baseline_fails_naming_it(
    real_counts: dict[str, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rule = max(real_counts, key=real_counts.__getitem__)
    rest = {k: v for k, v in real_counts.items() if k != rule}
    gate = _gate_with_baseline(monkeypatch, tmp_path, rest)
    assert gate.main(["--label", "selftest"]) == 1
    err = capsys.readouterr().err
    assert err.splitlines() == [
        f"selftest: {rule}: {real_counts[rule]} warnings, not in the baseline"
    ]


def test_one_warning_fewer_passes_and_asks_to_lower(
    real_counts: dict[str, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The tree one Google.EmDash below its baseline: green, and the gate says to ratchet down.
    base = real_counts["Google.EmDash"]
    gate = _gate_with_baseline(monkeypatch, tmp_path, {**real_counts, "Google.EmDash": base + 1})
    assert gate.main(["--label", "selftest"]) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert f"selftest: Google.EmDash: {base} warnings, baseline {base + 1}" in out
    assert f"selftest: {gate.LOWER_HINT}" in out
    assert "0 rules above the baseline" in out


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (None, "is missing"),
        ("", "does not parse"),
        ("{", "does not parse"),
        ("null", "must be a JSON object"),
        ("[]", "must be a JSON object"),
        ("{}", "is empty"),
        ('{"EmDash": 3}', "is not a rule name"),
        ('{"Google.EmDash": -1}', "not an integer of 0 or more"),
        ('{"Google.EmDash": "449"}', "not an integer of 0 or more"),
        ('{"Google.EmDash": true}', "not an integer of 0 or more"),
        ('{"Google.EmDash": 1.5}', "not an integer of 0 or more"),
    ],
)
def test_unusable_baseline_fails_before_vale_runs(
    text: str | None,
    reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gate = _load_gate()
    path = tmp_path / ".vale-baseline.json"
    if text is not None:
        path.write_text(text)
    monkeypatch.setattr(gate, "BASELINE", path)

    def _no_vale(*_args: object) -> None:
        pytest.fail("vale ran with an unusable baseline")

    monkeypatch.setattr(gate, "_vale", _no_vale)
    assert gate.main(["--label", "selftest"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("selftest: .vale-baseline.json"), err
    assert reason in err


def test_path_mode_ignores_the_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The selftest's PATH mode stays error-only: no baseline is read, so a missing one is fine.
    gate = _load_gate()
    monkeypatch.setattr(gate, "BASELINE", tmp_path / "absent.json")
    assert gate.main(["--label", "selftest", str(FIXTURES / "planted-emdash.md")]) == 0
    assert "1 warning (reported, not gated)" in capsys.readouterr().out


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Vale 3.24.0 anchors an alert by searching for its matched text, so the dash of a list "
        "item that wraps onto a second line lands on a later ' \u2014 ' that carries its own "
        "alert, and the two report as one (vale-cli/vale#1147 is the same anchoring). A plant "
        "appended after such an item can lower the Google.EmDash count instead of raising it."
    ),
)
def test_vale_reports_every_spaced_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two spaced dashes, so two warnings. Strict: the day Vale reports both, this XPASS fails,
    # and the xfail and the caveat in scripts/vale_gate.py come out together.
    gate = _load_gate()
    monkeypatch.setattr(gate, "BASELINE", tmp_path / "absent.json")
    assert gate.main(["--label", "selftest", str(FIXTURES / "merged-emdash.md")]) == 0
    assert "2 warnings (reported, not gated)" in capsys.readouterr().out


def test_ratchet_names_rises_new_rules_and_falls() -> None:
    over, under = _load_gate().ratchet(
        {"A.x": 3, "B.y": 1, "D.w": 2}, {"A.x": 2, "C.z": 4, "D.w": 2}
    )
    assert over == ["A.x: 3 warnings, baseline 2", "B.y: 1 warning, not in the baseline"]
    assert under == ["C.z: 0 warnings, baseline 4"]


def test_write_baseline_rewrites_from_the_tree(
    real_counts: dict[str, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A stale baseline: one rule too high, one too low, one retired, one missing.
    missing = min(real_counts)
    stale = {k: v for k, v in real_counts.items() if k != missing}
    stale["Google.EmDash"] += 5
    stale["Google.WordListCase"] -= 1
    stale["Retired.Rule"] = 2
    gate = _gate_with_baseline(monkeypatch, tmp_path, stale)
    assert gate.main(["--label", "selftest", "--write-baseline"]) == 0
    out = capsys.readouterr().out
    written = json.loads(gate.BASELINE.read_text())
    assert written == {**real_counts, "Retired.Rule": 0}
    assert list(written) == sorted(written)
    em, wl = real_counts["Google.EmDash"], real_counts["Google.WordListCase"]
    assert f"selftest: Google.EmDash: lowered {em + 5} -> {em}" in out
    assert (
        f"selftest: Google.WordListCase: raised {wl - 1} -> {wl}: a baseline only goes down" in out
    )
    assert "selftest: Retired.Rule: lowered 2 -> 0" in out
    assert f"selftest: {missing}: added at {real_counts[missing]}" in out
    # The tree now sits exactly at its baseline: green with nothing to lower.
    assert gate.main(["--label", "selftest"]) == 0
    out = capsys.readouterr().out
    assert gate.LOWER_HINT not in out
    assert "0 rules above the baseline" in out


def test_write_baseline_takes_no_path() -> None:
    with pytest.raises(SystemExit) as exit_info:
        _load_gate().main(["--write-baseline", str(FIXTURES / "clean.md")])
    assert exit_info.value.code == 2


def test_write_baseline_refuses_to_write_nothing(tmp_path: Path) -> None:
    gate = _load_gate()
    with pytest.raises(gate.BaselineError, match="nothing to write"):
        gate.write_baseline(tmp_path / ".vale-baseline.json", {})
    assert not (tmp_path / ".vale-baseline.json").exists()
