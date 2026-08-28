# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ``atif-sql cron`` subcommand group (atif_cli.cron).

The parse functions are pure, so these tests feed them synthetic log lines /
probe results and never touch a live cron, crontab, or lock. The command-level
tests run against a tmp scripts/ tree via ``--script``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atif_cli.app import app
from atif_cli.cron import (
    LANES,
    LaneRun,
    crontab_block,
    install,
    lock_state,
    parse_last_runs,
    status,
)
from atif_cli.errors import EXIT_CODES
from atif_cli.output import OutputFormat


class TestSurface:
    def test_cron_group_registered_on_app(self) -> None:
        assert app["cron"] is not None

    def test_install_and_status_registered(self) -> None:
        cron = app["cron"]
        assert cron["install"] is not None
        assert cron["status"] is not None


class TestCrontabBlock:
    def test_one_line_per_lane_with_schedule_script_and_log(self) -> None:
        block = crontab_block(Path("/repo/scripts/atif-sql-refresh.sh"), Path("/repo/log"))
        lines = block.splitlines()
        assert len(lines) == len(LANES) == 3
        for line, (lane, schedule) in zip(lines, LANES, strict=True):
            assert line.startswith(schedule)
            assert f"/repo/scripts/atif-sql-refresh.sh {lane} " in line
            assert line.endswith(">> /repo/log 2>&1")

    def test_schedules_match_contract(self) -> None:
        # CONTRACT-V2 §Cron: materialize */10, structural :17, llm nightly 10:20Z.
        assert dict(LANES) == {
            "materialize": "*/10 * * * *",
            "structural": "17 * * * *",
            "llm": "20 10 * * *",
        }

    def test_block_never_contains_crontab_write(self) -> None:
        block = crontab_block(Path("/s.sh"), Path("/l"))
        assert "crontab" not in block  # print-only; the human pastes it


class TestParseLastRuns:
    # A tuple: immutable class attribute (RUF012-clean by construction).
    _LOG = (
        "2026-08-23T10:00:01+00:00 interactive: materialize ok",
        "2026-08-23T10:00:02+00:00 refresh complete (mode=materialize, exit=0)",
        "2026-08-23T10:10:02+00:00 refresh complete (mode=materialize, exit=1)",
        "2026-08-23T10:17:00+00:00 [structural] analytics not yet installed, skipping",
        "2026-08-23T10:20:00+00:00 skip[llm]: a llm run is already going (pid 4242)",
        "2026-08-23T11:20:00+00:00 refresh complete (mode=llm, exit=0)",
    )

    def test_last_completion_wins_per_lane(self) -> None:
        runs = parse_last_runs(self._LOG)
        assert runs["materialize"]["complete"] == LaneRun(
            timestamp="2026-08-23T10:10:02+00:00", exit_code=1
        )
        assert runs["llm"]["complete"] == LaneRun(
            timestamp="2026-08-23T11:20:00+00:00", exit_code=0
        )

    def test_skip_lines_parse_separately_from_completions(self) -> None:
        runs = parse_last_runs(self._LOG)
        assert runs["llm"]["skip"] == "2026-08-23T10:20:00+00:00"
        assert "skip" not in runs.get("materialize", {})

    def test_lane_with_no_events_is_absent(self) -> None:
        runs = parse_last_runs(self._LOG)
        assert "structural" not in runs  # the guard line is not a completion

    def test_empty_log(self) -> None:
        assert parse_last_runs([]) == {}


class TestLockState:
    def test_acquired_probe_means_idle(self) -> None:
        assert lock_state(probe_acquired=True, pidfile_text="123\n") == (False, None)

    def test_held_lock_names_holder_pid(self) -> None:
        assert lock_state(probe_acquired=False, pidfile_text="4242\n") == (True, 4242)

    def test_held_lock_with_missing_pidfile(self) -> None:
        assert lock_state(probe_acquired=False, pidfile_text=None) == (True, None)

    def test_held_lock_with_garbage_pidfile(self) -> None:
        assert lock_state(probe_acquired=False, pidfile_text="not-a-pid") == (True, None)


@pytest.fixture
def scripts_tree(tmp_path: Path) -> Path:
    """A tmp scripts/ tree with a refresh script and a populated .run/ dir."""
    scripts = tmp_path / "scripts"
    run_dir = scripts / ".run"
    run_dir.mkdir(parents=True)
    script = scripts / "atif-sql-refresh.sh"
    script.write_text("#!/usr/bin/env bash\n")
    (run_dir / "atif-sql-refresh.log").write_text(
        "2026-08-23T10:10:02+00:00 refresh complete (mode=materialize, exit=0)\n"
        "2026-08-23T10:20:00+00:00 skip[llm]: a llm run is already going (pid 4242)\n"
    )
    return script


class TestInstallCommand:
    def test_prints_block_for_explicit_script(
        self, scripts_tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        install(script=scripts_tree)
        out = capsys.readouterr().out
        assert str(scripts_tree) in out
        assert "*/10 * * * *" in out
        assert "20 10 * * *" in out
        assert "crontab -l" in out  # points the human at the check-first step

    def test_discovers_repo_script_by_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The editable install resolves back into this worktree, whose
        # scripts/atif-sql-refresh.sh this task ships.
        install()
        out = capsys.readouterr().out
        assert "scripts/atif-sql-refresh.sh materialize" in out


class TestStatusCommand:
    def test_json_payload_reports_lanes_locks_and_log(
        self, scripts_tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status(script=scripts_tree, fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        lanes = {lane["lane"]: lane for lane in payload["lanes"]}
        assert set(lanes) == {"materialize", "structural", "llm"}
        # No lock files exist in the tmp tree -> every lane idle.
        assert all(not lane["lock_held"] for lane in lanes.values())
        assert lanes["materialize"]["last_complete"] == {
            "timestamp": "2026-08-23T10:10:02+00:00",
            "exit": 0,
        }
        assert lanes["structural"]["last_complete"] is None
        assert lanes["llm"]["last_skip"] == "2026-08-23T10:20:00+00:00"
        assert payload["tail"], "tail should carry the trailing log lines"

    def test_table_output_on_explicit_table_format(
        self, scripts_tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status(script=scripts_tree, fmt=OutputFormat.TABLE)
        out = capsys.readouterr().out
        assert "materialize" in out
        assert "idle" in out
        assert "never" in out  # structural + llm have no completion yet

    def test_missing_log_reports_all_lanes_never_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        script = tmp_path / "scripts" / "atif-sql-refresh.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env bash\n")
        status(script=script, fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        assert all(lane["last_complete"] is None for lane in payload["lanes"])
        assert payload["tail"] == []

    def test_held_lock_reported_with_holder_pid(
        self, scripts_tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import fcntl

        run_dir = scripts_tree.parent / ".run"
        (run_dir / "atif-sql-refresh-llm.pid").write_text("4242\n")
        with (run_dir / "atif-sql-refresh-llm.lock").open("w") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            status(script=scripts_tree, fmt=OutputFormat.JSON)
        payload = json.loads(capsys.readouterr().out)
        llm = next(lane for lane in payload["lanes"] if lane["lane"] == "llm")
        assert llm["lock_held"] is True
        assert llm["holder_pid"] == 4242


class TestResolveScriptError:
    def test_undiscoverable_script_exits_invalid_input(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import atif_cli.cron as cron_mod

        monkeypatch.setattr(cron_mod, "_scripts_dir", lambda: None)
        with pytest.raises(SystemExit) as excinfo:
            install()
        assert excinfo.value.code == EXIT_CODES["invalid_input"]
        assert "pass --script" in capsys.readouterr().err
