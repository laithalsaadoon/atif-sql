# SPDX-License-Identifier: Apache-2.0

"""`security:vex:check` holds dependency review's `allow-ghsas` to the OpenVEX ledger.

actions/dependency-review-action 5.0.0 has no VEX input, so its `allow-ghsas` list is rendered
from `security/atif-sql.openvex.json` by `scripts/vex_to_osv_config.py --workflow`. That list
matches on the GHSA id alone, so the script also holds every `pkg:npm` PURL in the ledger to
the version `site/pnpm-lock.yaml` locks. The gate is a `check` leg and needs the anti-vacuity
proof every gate here carries: each planted defect turns it red, and the real tree is green.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: Repository root: `packages/atif-cli/tests/` -> three parents up.
ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "vex_to_osv_config.py"
#: Relative to ROOT, where `_run` starts the script: the renderer writes the ledger's path into
#: the osv-scanner.toml header, so the committed file only matches a render that names it the
#: way `mise run security:vex` does.
LEDGER = Path("security/atif-sql.openvex.json")
OSV_CONFIG = Path("osv-scanner.toml")
UV_LOCK = Path("uv.lock")
PNPM_LOCK = Path("site/pnpm-lock.yaml")
WORKFLOW = Path(".github/workflows/dependency-review.yml")

_ALLOW_LINE = re.compile(r"^(\s*allow-ghsas:[ \t]*)(.*)$", re.MULTILINE)


def _run(
    *,
    ledger: Path = LEDGER,
    output: Path = OSV_CONFIG,
    pnpm_lock: Path = PNPM_LOCK,
    workflow: Path = WORKFLOW,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(SCRIPT),
        str(ledger),
        str(output),
        "--lock",
        str(UV_LOCK),
        "--pnpm-lock",
        str(pnpm_lock),
        "--workflow",
        str(workflow),
    ]
    if check:
        argv.append("--check")
    return subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        argv, capture_output=True, text=True, check=False, cwd=ROOT
    )


def _ledger_ghsas() -> list[str]:
    document = json.loads((ROOT / LEDGER).read_text(encoding="utf-8"))
    return sorted(
        statement["vulnerability"]["name"]
        for statement in document["statements"]
        if statement["status"] in {"not_affected", "fixed"}
    )


def _workflow_with(tmp_path: Path, value: str) -> Path:
    text = (ROOT / WORKFLOW).read_text(encoding="utf-8")
    path = tmp_path / "dependency-review.yml"
    path.write_text(_ALLOW_LINE.sub(lambda m: m.group(1) + value, text, count=1), "utf-8")
    return path


def _lock_with(tmp_path: Path, old: str, new: str) -> Path:
    text = (ROOT / PNPM_LOCK).read_text(encoding="utf-8")
    assert old in text, f"{old!r} is not in {PNPM_LOCK}"
    path = tmp_path / "pnpm-lock.yaml"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def test_real_tree_is_green() -> None:
    result = _run()
    assert result.returncode == 0, result.stderr
    assert f"allow-ghsas matches {LEDGER}" in result.stdout


def test_ledger_suppresses_at_least_one_npm_ghsa() -> None:
    # Anti-vacuity: with an empty ledger every comparison below would pass on nothing.
    ghsas = _ledger_ghsas()
    assert ghsas, "the live ledger carries no suppressing GHSA statement"
    assert all(ghsa.startswith("GHSA-") for ghsa in ghsas)


def test_ledger_ghsa_missing_from_workflow_is_red(tmp_path: Path) -> None:
    dropped, *kept = _ledger_ghsas()
    workflow = _workflow_with(tmp_path, ", ".join(kept) or "''")
    result = _run(workflow=workflow)
    assert result.returncode == 1
    assert f"allow-ghsas is missing {dropped}" in result.stderr


def test_workflow_ghsa_without_ledger_statement_is_red(tmp_path: Path) -> None:
    extra = "GHSA-aaaa-bbbb-cccc"
    workflow = _workflow_with(tmp_path, ", ".join([*_ledger_ghsas(), extra]))
    result = _run(workflow=workflow)
    assert result.returncode == 1
    assert f"allow-ghsas lists {extra} with no not_affected or fixed" in result.stderr


def test_non_suppressing_statement_leaves_the_allow_list(tmp_path: Path) -> None:
    document = json.loads((ROOT / LEDGER).read_text(encoding="utf-8"))
    demoted = document["statements"][0]
    demoted["status"] = "under_investigation"
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps(document), encoding="utf-8")
    result = _run(ledger=ledger)
    assert result.returncode == 1
    assert f"allow-ghsas lists {demoted['vulnerability']['name']}" in result.stderr


def test_npm_version_moved_in_lock_is_red(tmp_path: Path) -> None:
    lock = _lock_with(tmp_path, "\n  braces@3.0.3:", "\n  braces@3.0.4:")
    result = _run(pnpm_lock=lock)
    assert result.returncode == 1
    assert "names pkg:npm/braces@3.0.3 but" in result.stderr
    assert "locks braces 3.0.4" in result.stderr


def test_second_locked_copy_the_statement_does_not_name_is_red(tmp_path: Path) -> None:
    lock = _lock_with(
        tmp_path, "\n  braces@3.0.3:", "\n  braces@2.3.2:\n    resolution: {}\n\n  braces@3.0.3:"
    )
    result = _run(pnpm_lock=lock)
    assert result.returncode == 1
    assert "also locks braces 2.3.2" in result.stderr


def test_npm_package_gone_from_lock_is_red(tmp_path: Path) -> None:
    lock = _lock_with(tmp_path, "\n  braces@3.0.3:", "\n  brace-gone@3.0.3:")
    result = _run(pnpm_lock=lock)
    assert result.returncode == 1
    assert "locks no package 'braces'" in result.stderr


def test_scoped_npm_purl_is_decoded(tmp_path: Path) -> None:
    document = json.loads((ROOT / LEDGER).read_text(encoding="utf-8"))
    purl = "pkg:npm/%40types/braces@3.0.5"
    document["statements"][0]["products"] = [{"@id": purl, "identifiers": {"purl": purl}}]
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps(document), encoding="utf-8")
    workflow = tmp_path / "dependency-review.yml"
    shutil.copyfile(ROOT / WORKFLOW, workflow)
    result = _run(ledger=ledger, output=tmp_path / "osv.toml", workflow=workflow, check=False)
    assert result.returncode == 0, result.stderr


def test_workflow_without_allow_line_is_red(tmp_path: Path) -> None:
    text = _ALLOW_LINE.sub("", (ROOT / WORKFLOW).read_text(encoding="utf-8"), count=1)
    workflow = tmp_path / "dependency-review.yml"
    workflow.write_text(text, encoding="utf-8")
    result = _run(workflow=workflow)
    assert result.returncode == 1
    assert "exactly one `allow-ghsas:` input" in result.stderr


def test_render_rewrites_the_allow_line_from_the_ledger(tmp_path: Path) -> None:
    workflow = _workflow_with(tmp_path, "GHSA-stale-0000-0000")
    result = _run(output=tmp_path / "osv.toml", workflow=workflow, check=False)
    assert result.returncode == 0, result.stderr
    assert workflow.read_text(encoding="utf-8") == (ROOT / WORKFLOW).read_text(encoding="utf-8")
    assert (tmp_path / "osv.toml").read_text(encoding="utf-8") == (ROOT / OSV_CONFIG).read_text(
        encoding="utf-8"
    )
