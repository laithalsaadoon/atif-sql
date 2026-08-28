# SPDX-License-Identifier: Apache-2.0

"""Experiment 001 runner — convert one Claude Code session and audit the loss.

Single-session mode (the only mode): the orchestrating shell wraps each
invocation in ``timeout`` so a pathological session cannot stall the sweep.

    uv run python experiments/001-harbor-converter-fidelity/run.py \
        <corpus-root> <session-id> <out-dir>

Writes ``<out-dir>/<session-id>/trajectory.json``, ``loss_report.json`` and
``meta.json`` (wall-clock, sizes, validation errors verbatim, failure
traceback head). Exit code 0 on conversion success (even if validation
failed — that is a finding, not a crash), 1 on conversion failure.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from loguru import logger

from atif_converter.application.convert_and_audit import convert_and_audit
from atif_converter.domain.fidelity import LossReport


def _loss_report_dict(report: LossReport) -> dict[str, Any]:
    return {
        "record_counts": {
            rt.value: n
            for rt, n in sorted(report.record_counts.items(), key=lambda kv: kv[0].value)
        },
        "records_total": report.records_total,
        "records_converted": report.records_converted,
        "records_dropped": report.records_dropped,
        "gaps_observed": sorted(gap.value for gap in report.gaps_observed),
        "subagent_files_found": report.subagent_files_found,
        "subagent_files_convertible": report.subagent_files_convertible,
        "workflow_subagent_files_found": report.workflow_subagent_files_found,
    }


def main(argv: list[str]) -> int:
    """Convert one session and write its fidelity report; return the exit code."""
    if len(argv) != 4:  # noqa: PLR2004 — the argv arity of the usage line below
        logger.error("usage: run.py <corpus-root> <session-id> <out-dir>")
        return 2

    corpus_root, session_id, out_root = Path(argv[1]), argv[2], Path(argv[3])
    session_jsonl = corpus_root / f"{session_id}.jsonl"
    out_dir = out_root / session_id
    out_dir.mkdir(parents=True, exist_ok=True)

    meta: dict[str, Any] = {
        "session_id": session_id,
        "raw_bytes": session_jsonl.stat().st_size if session_jsonl.exists() else None,
    }
    started = time.monotonic()
    try:
        result, report = convert_and_audit(session_jsonl)
    except Exception:  # noqa: BLE001 — a failed conversion is a finding to record, not a crash
        meta["status"] = "failed"
        meta["wall_seconds"] = round(time.monotonic() - started, 2)
        meta["traceback_head"] = traceback.format_exc(limit=20).splitlines()[:30]
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        logger.error("conversion FAILED for {}", session_id)
        return 1

    meta["wall_seconds"] = round(time.monotonic() - started, 2)

    trajectory_path = out_dir / "trajectory.json"
    trajectory_path.write_text(json.dumps(result.trajectory, indent=2))
    (out_dir / "loss_report.json").write_text(json.dumps(_loss_report_dict(report), indent=2))

    meta["status"] = "ok"
    meta["output_bytes"] = trajectory_path.stat().st_size
    meta["converted_steps"] = len(result.trajectory.get("steps", []))
    meta["validation_passed"] = result.is_valid
    meta["validation_errors"] = list(result.validation_errors)
    meta["final_metrics"] = result.trajectory.get("final_metrics")
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    logger.info(
        "{}: {} steps, valid={}, {}s",
        session_id,
        meta["converted_steps"],
        result.is_valid,
        meta["wall_seconds"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
