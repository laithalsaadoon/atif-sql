# SPDX-License-Identifier: Apache-2.0
"""Planted defects for ``run_fuzzers.py``: each way the task could pass on nothing turns it red.

The harnesses here are stand-ins that print what libFuzzer prints, so these run
without atheris, in ``mise run check``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import run_fuzzers
from run_fuzzers import FUZZ_DIR, Outcome, Target, executed_units, plan, run_target
from transcript_targets import TARGETS

_STATS = "stat::number_of_executed_units: {runs}\nstat::average_exec_per_sec: 1\n"


def _tree(root: Path, names: list[str]) -> Path:
    for name in names:
        (root / f"fuzz_{name}.py").write_text("")
        (root / f"{name}.dict").write_text('"x"\n')
        (root / "corpus" / name).mkdir(parents=True)
        (root / "corpus" / name / "seed").write_bytes(b"{}\n")
    return root


def _stand_in(tmp_path: Path, body: str) -> Target:
    harness = tmp_path / "fuzz_stand_in.py"
    harness.write_text(body)
    seeds = tmp_path / "seeds"
    seeds.mkdir()
    (seeds / "seed").write_bytes(b"{}\n")
    dictionary = tmp_path / "stand_in.dict"
    dictionary.write_text('"x"\n')
    return Target(name="stand_in", harness=harness, seeds=seeds, dictionary=dictionary)


def test_the_committed_tree_plans_every_target() -> None:
    """The repository's own fuzz/ directory is complete for every registered target."""
    assert [target.name for target in plan(TARGETS, [])] == sorted(TARGETS)


def test_missing_harness_fails(tmp_path: Path) -> None:
    """Deleting one harness fails the plan rather than fuzzing the other target alone."""
    root = _tree(tmp_path, ["one", "two"])
    (root / "fuzz_two.py").unlink()
    with pytest.raises(SystemExit, match=re.escape("target two: no harness fuzz_two.py")):
        plan(["one", "two"], [], root)


def test_empty_seed_directory_fails(tmp_path: Path) -> None:
    """A target whose seed directory is empty fails the plan."""
    root = _tree(tmp_path, ["one"])
    (root / "corpus" / "one" / "seed").unlink()
    with pytest.raises(SystemExit, match="no seed inputs"):
        plan(["one"], [], root)


def test_unregistered_harness_fails(tmp_path: Path) -> None:
    """A harness file no target names fails the plan, so it can't sit there unrun."""
    root = _tree(tmp_path, ["one"])
    (root / "fuzz_stray.py").write_text("")
    with pytest.raises(
        SystemExit, match=re.escape("harness fuzz_stray.py names no registered target")
    ):
        plan(["one"], [], root)


def test_zero_targets_fails(tmp_path: Path) -> None:
    """With nothing registered, nothing runs, and that is a failure."""
    with pytest.raises(SystemExit, match="no fuzz target selected"):
        plan([], [], tmp_path)


def test_unknown_request_fails(tmp_path: Path) -> None:
    """Asking for a target that does not exist fails rather than running nothing."""
    root = _tree(tmp_path, ["one"])
    with pytest.raises(SystemExit, match="unknown target nope"):
        plan(["one"], ["nope"], root)


def test_executed_units_reads_the_last_count() -> None:
    """The count is libFuzzer's final stats line; no line is ``None``."""
    assert executed_units(_STATS.format(runs=7) + _STATS.format(runs=12345)) == 12345
    assert executed_units("Done 3 runs in 1 second(s)\n") is None


def test_a_crash_fails(tmp_path: Path) -> None:
    """A harness that exits non-zero and writes an artifact fails, naming the artifact."""
    body = (
        "import sys\n"
        "prefix = next(a for a in sys.argv if a.startswith('-artifact_prefix='))\n"
        "open(prefix.split('=', 1)[1] + 'crash-0', 'wb').write(b'[]')\n"
        f"print({_STATS.format(runs=3)!r})\n"
        "sys.exit(1)\n"
    )
    outcome = run_target(_stand_in(tmp_path, body), 1, tmp_path / "out")
    assert outcome.returncode == 1
    assert [path.name for path in outcome.artifacts] == ["crash-0"]
    assert outcome.failure == "harness exited 1 with 1 artifact(s)"
    assert "artifact" in run_fuzzers.report(outcome)


def test_an_artifact_fails_even_on_exit_zero(tmp_path: Path) -> None:
    """An artifact left behind fails the run whatever the exit code says."""
    outcome = Outcome("t", 0, 10, 1.0, (tmp_path / "timeout-0",), tmp_path / "log")
    assert outcome.failure is not None


def test_a_run_that_executed_nothing_fails(tmp_path: Path) -> None:
    """Exit 0 with no executed input (or no stats at all) is not a pass."""
    zero = run_target(
        _stand_in(tmp_path, f"print({_STATS.format(runs=0)!r})\n"), 1, tmp_path / "out"
    )
    assert zero.failure == "libFuzzer executed no input"
    silent = Outcome("t", 0, None, 1.0, (), tmp_path / "log")
    assert silent.failure == "libFuzzer executed no input"


def test_a_clean_run_passes(tmp_path: Path) -> None:
    """A harness that executed inputs, exited 0 and left no artifact passes."""
    outcome = run_target(
        _stand_in(tmp_path, f"print({_STATS.format(runs=42)!r})\n"), 1, tmp_path / "out"
    )
    assert (outcome.returncode, outcome.runs, outcome.failure) == (0, 42, None)
    assert run_fuzzers.report(outcome).startswith("fuzz stand_in: 42 runs in ")


def test_stale_artifacts_are_cleared(tmp_path: Path) -> None:
    """A previous run's crash file does not fail, or pass, the next run."""
    stale = tmp_path / "out" / "stand_in" / "artifacts"
    stale.mkdir(parents=True)
    (stale / "crash-old").write_bytes(b"")
    outcome = run_target(
        _stand_in(tmp_path, f"print({_STATS.format(runs=5)!r})\n"), 1, tmp_path / "out"
    )
    assert outcome.artifacts == ()


@pytest.mark.parametrize("value", ["0", "-5", "ten", "1.5"])
def test_bad_seconds_fail(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """``ATIF_SQL_FUZZ_SECONDS`` must be a positive whole number."""
    monkeypatch.setenv(run_fuzzers.SECONDS_ENV, value)
    with pytest.raises(SystemExit, match=run_fuzzers.SECONDS_ENV):
        run_fuzzers.seconds_from_env()


def test_seconds_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset means the default; a positive integer is taken as given."""
    monkeypatch.delenv(run_fuzzers.SECONDS_ENV, raising=False)
    assert run_fuzzers.seconds_from_env() == run_fuzzers.DEFAULT_SECONDS
    monkeypatch.setenv(run_fuzzers.SECONDS_ENV, "5")
    assert run_fuzzers.seconds_from_env() == 5


def test_missing_atheris_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without atheris the task refuses rather than reporting a pass for no fuzzing."""

    def no_spec(_name: str) -> None:
        return None

    monkeypatch.setattr(run_fuzzers.importlib.util, "find_spec", no_spec)
    with pytest.raises(SystemExit, match="atheris is not installed"):
        run_fuzzers.main([])


def test_the_harnesses_import_atheris() -> None:
    """Each harness file carries the import Scorecard's Fuzzing check looks for."""
    for name in TARGETS:
        assert re.search(
            r"^import atheris\b", (FUZZ_DIR / f"fuzz_{name}.py").read_text(), re.MULTILINE
        )
