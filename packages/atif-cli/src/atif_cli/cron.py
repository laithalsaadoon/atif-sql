# SPDX-License-Identifier: Apache-2.0

"""The ``atif-sql cron`` subcommand group: install (print-only) + status.

Companion surface for ``scripts/atif-sql-refresh.sh``. Two commands:

* ``cron install`` prints the crontab block for the three lanes and NEVER
  writes the crontab (CONTRACT-V2 §Cron: "no silent crontab writes") — the
  human pastes it after checking ``crontab -l``.
* ``cron status`` reports, per lane: whether the flock is currently held
  (and by which pid), the last completed run parsed from the refresh log,
  and the last skip. Plus the log tail.

Lean by construction: stdlib + cyclopts only, no atif_* imports beyond
:mod:`atif_cli.output` — this module loads eagerly with ``atif_cli.app``
(sub-apps must exist at registration time) and therefore sits on the fast
path pinned by the lean-import test.

The log/lock parsing is pure (:func:`parse_last_runs`, :func:`lock_state`
takes injected probe results) so the unit tests never need a live cron.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import cyclopts

from atif_cli.errors import EXIT_CODES
from atif_cli.output import OutputFormat, emit_json, resolve_format

if TYPE_CHECKING:
    from collections.abc import Iterable

cron_app = cyclopts.App(
    name="cron",
    help="Inspect and (manually) install the atif-sql refresh cron lanes.",
)

#: The three lanes and their crontab schedules — the single source the
#: ``install`` block is rendered from. Cadence rationale lives in the
#: refresh script's header (materialize is the cheap incremental lane;
#: structural is hourly zero-cost analytics; llm is the one that spends).
LANES: tuple[tuple[str, str], ...] = (
    ("materialize", "*/10 * * * *"),
    ("structural", "17 * * * *"),
    ("llm", "20 10 * * *"),
)

#: ``date -Is`` prefix on every refresh-log line (fixed-offset ISO-8601).
_TS = r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})"
_COMPLETE_RE = re.compile(_TS + r" refresh complete \(mode=(?P<mode>[a-z]+), exit=(?P<exit>\d+)\)")
_SKIP_RE = re.compile(_TS + r" skip\[(?P<mode>[a-z]+)\]:")


@dataclass(frozen=True, slots=True)
class LaneRun:
    """One parsed lane event: the run's ISO timestamp plus its exit code."""

    timestamp: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class LaneStatus:
    """Everything ``cron status`` reports for one lane."""

    lane: str
    schedule: str
    lock_held: bool
    holder_pid: int | None
    last_complete: LaneRun | None
    last_skip: str | None


def crontab_block(script: Path, log: Path) -> str:
    """Render the three-lane crontab block for ``script`` logging to ``log``.

    Pure. One line per lane, schedules from :data:`LANES`, stdout+stderr
    appended to the cron-side log (the script keeps its own structured log
    under ``scripts/.run/`` regardless).
    """
    width = max(len(schedule) for _, schedule in LANES)
    lines = [f"{schedule:<{width}} {script} {lane} >> {log} 2>&1" for lane, schedule in LANES]
    return "\n".join(lines)


def parse_last_runs(lines: Iterable[str]) -> dict[str, dict[str, LaneRun | str]]:
    """Extract per-lane last-completion and last-skip events from log lines.

    Pure. Later lines win (the log is append-only, so iteration order IS
    chronological order). Returns ``{lane: {"complete": LaneRun, "skip": ts}}``
    with keys present only when the event was seen.
    """
    out: dict[str, dict[str, LaneRun | str]] = {}
    for line in lines:
        if match := _COMPLETE_RE.match(line):
            out.setdefault(match["mode"], {})["complete"] = LaneRun(
                timestamp=match["ts"], exit_code=int(match["exit"])
            )
        elif match := _SKIP_RE.match(line):
            out.setdefault(match["mode"], {})["skip"] = match["ts"]
    return out


def lock_state(*, probe_acquired: bool, pidfile_text: str | None) -> tuple[bool, int | None]:
    """Classify one lane's lock: ``(held, holder_pid)``.

    Pure — the caller injects the nonblocking-flock probe result and the
    pidfile's text. ``probe_acquired=True`` means WE got the lock, so no run
    holds it. The pid is reported only for a held lock; a stale pidfile next
    to a free lock is normal (the last run's pid, not a holder).
    """
    if probe_acquired:
        return (False, None)
    pid: int | None = None
    if pidfile_text is not None:
        stripped = pidfile_text.strip()
        if stripped.isdigit():
            pid = int(stripped)
    return (True, pid)


def _probe_lock(lock_path: Path) -> bool:
    """Try to acquire ``lock_path`` nonblocking; True = acquired (lane idle).

    The kernel drops the flock when the fd closes, so a successful probe
    perturbs nothing. An absent lock file means no run ever started — also
    "acquired" for classification purposes.
    """
    import fcntl

    if not lock_path.exists():
        return True
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True


def _scripts_dir() -> Path | None:
    """Locate ``scripts/atif-sql-refresh.sh`` by walking up from this file.

    Works for the editable install this workspace runs (site-packages paths
    resolve back into ``packages/atif-cli/src``). Returns ``None`` when the
    tree isn't reachable — e.g. a future ``uv tool install`` — in which case
    the commands require the explicit ``--script`` flag.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scripts" / "atif-sql-refresh.sh"
        if candidate.is_file():
            return candidate.parent
    return None


def _resolve_script(script: Path | None) -> Path:
    """Resolve the refresh-script path from the flag or by discovery."""
    if script is not None:
        return script
    scripts_dir = _scripts_dir()
    if scripts_dir is None:
        import sys

        print(
            "error: cannot locate scripts/atif-sql-refresh.sh from this install; "
            "pass --script explicitly",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CODES["invalid_input"])
    return scripts_dir / "atif-sql-refresh.sh"


@cron_app.command
def install(*, script: Path | None = None) -> None:
    """Print the crontab block for the three refresh lanes — never write it.

    CONTRACT-V2 §Cron: no silent crontab writes. Check ``crontab -l`` for an
    existing block, then paste this one via ``crontab -e``.

    Parameters
    ----------
    script
        Path to ``atif-sql-refresh.sh``; default: discovered from the repo
        this CLI is (editably) installed from.
    """
    resolved = _resolve_script(script)
    log = resolved.parent / ".run" / "atif-sql-refresh.cron.log"
    print("# atif-sql refresh lanes — paste into `crontab -e` (check `crontab -l` first)")
    print(crontab_block(resolved, log))


@cron_app.command
def status(
    *,
    script: Path | None = None,
    tail: int = 10,
    fmt: Annotated[OutputFormat, cyclopts.Parameter(name="--format")] = OutputFormat.AUTO,
) -> None:
    """Report each lane's lock holder + last run/skip parsed from the log.

    Read-only: the lock probe acquires-and-releases nonblocking, perturbing
    no running lane.

    Parameters
    ----------
    script
        Path to ``atif-sql-refresh.sh`` (its ``.run/`` sibling holds the
        locks and log); default: discovered from the repo.
    tail
        How many trailing log lines to include (0 disables).
    fmt
        ``auto`` = human lines on a TTY, JSON on a pipe.
    """
    resolved = _resolve_script(script)
    run_dir = resolved.parent / ".run"
    log_path = run_dir / "atif-sql-refresh.log"
    log_lines = log_path.read_text().splitlines() if log_path.is_file() else []
    runs = parse_last_runs(log_lines)

    lanes: list[LaneStatus] = []
    for lane, schedule in LANES:
        pidfile = run_dir / f"atif-sql-refresh-{lane}.pid"
        held, pid = lock_state(
            probe_acquired=_probe_lock(run_dir / f"atif-sql-refresh-{lane}.lock"),
            pidfile_text=pidfile.read_text() if pidfile.is_file() else None,
        )
        lane_runs = runs.get(lane, {})
        complete = lane_runs.get("complete")
        skip = lane_runs.get("skip")
        lanes.append(
            LaneStatus(
                lane=lane,
                schedule=schedule,
                lock_held=held,
                holder_pid=pid,
                last_complete=complete if isinstance(complete, LaneRun) else None,
                last_skip=skip if isinstance(skip, str) else None,
            )
        )

    tail_lines = log_lines[-tail:] if tail > 0 else []
    if resolve_format(fmt) is OutputFormat.TABLE:
        for lane_status in lanes:
            lock = (
                f"RUNNING (pid {lane_status.holder_pid or '?'})"
                if lane_status.lock_held
                else "idle"
            )
            last = (
                f"{lane_status.last_complete.timestamp} exit={lane_status.last_complete.exit_code}"
                if lane_status.last_complete
                else "never"
            )
            print(f"{lane_status.lane:<12} [{lane_status.schedule:<12}] {lock:<20} last: {last}")
            if lane_status.last_skip:
                print(f"{'':<12} last skip: {lane_status.last_skip}")
        if tail_lines:
            print(f"\n--- last {len(tail_lines)} log lines ({log_path}) ---")
            for line in tail_lines:
                print(line)
        return
    emit_json(
        {
            "log": str(log_path),
            "lanes": [
                {
                    "lane": s.lane,
                    "schedule": s.schedule,
                    "lock_held": s.lock_held,
                    "holder_pid": s.holder_pid,
                    "last_complete": (
                        {"timestamp": s.last_complete.timestamp, "exit": s.last_complete.exit_code}
                        if s.last_complete
                        else None
                    ),
                    "last_skip": s.last_skip,
                }
                for s in lanes
            ],
            "tail": tail_lines,
        },
        fmt,
    )


__all__ = [
    "LANES",
    "LaneRun",
    "LaneStatus",
    "cron_app",
    "crontab_block",
    "install",
    "lock_state",
    "parse_last_runs",
    "status",
]
