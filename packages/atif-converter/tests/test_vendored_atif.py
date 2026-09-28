# SPDX-License-Identifier: Apache-2.0

"""The vendored ATIF models are harbor's, and agree with harbor on real trajectories.

``atif_converter.domain.atif`` is a copy of harbor's ``harbor.models.trajectories``
and ``harbor.utils.trajectory_validator`` (Apache-2.0), so atif-sql installs
without harbor. harbor stays a DEV dependency for this file and the parity
oracle. Three layers of drift guard, each failing on its own:

1. SOURCE. Each vendored file, minus its attribution header and with the import
   path mapped back, is byte for byte the installed harbor's file. A harbor bump
   that touches the models fails here first, naming the file to re-vendor.
2. SCHEMA. Both sets of models publish the same JSON Schema.
3. BEHAVIOR. The frozen goldens, and the converter's own enriched output for
   both synthetic fixtures, round-trip through both sets of models to the same
   dict, and both validators return the same verdict and the same error text,
   on valid trajectories and on broken ones.

The module is skipped where harbor isn't installed; the conversion suite still
runs on the vendored models there.
"""

from __future__ import annotations

import copy
import importlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from atif_converter.application.convert_and_audit import convert_and_audit
from atif_converter.application.convert_codex import convert_codex_and_audit
from atif_converter.domain import atif as vendored
from atif_converter.domain.atif import trajectory_validator as vendored_validator

harbor_trajectories = pytest.importorskip("harbor.models.trajectories")
harbor_validator = pytest.importorskip("harbor.utils.trajectory_validator")

GOLDENS_DIR = Path(__file__).parent / "goldens"
VENDORED_DIR = Path(vendored.__file__).parent

#: Vendored module -> the upstream module it copies.
VENDORED_MODULES: dict[str, str] = {
    **{
        name: f"harbor.models.trajectories.{name}"
        for name in (
            "agent",
            "content",
            "final_metrics",
            "metrics",
            "observation",
            "observation_result",
            "step",
            "subagent_trajectory_ref",
            "tool_call",
            "trajectory",
        )
    },
    "trajectory_validator": "harbor.utils.trajectory_validator",
}

#: The attribution lines each vendored module carries above upstream's first line.
HEADER_LINES = 5


def _upstream_equivalent(vendored_source: str) -> str:
    """A vendored file as upstream spells it: header dropped, import path mapped back."""
    body = "".join(vendored_source.splitlines(keepends=True)[HEADER_LINES:])
    return body.replace("atif_converter.domain.atif", "harbor.models.trajectories")


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _both_validators(trajectory: Any) -> tuple[tuple[bool, list[str]], tuple[bool, list[str]]]:
    ours = vendored_validator.TrajectoryValidator()
    theirs = harbor_validator.TrajectoryValidator()
    ours_ok = ours.validate(copy.deepcopy(trajectory))
    theirs_ok = bool(theirs.validate(copy.deepcopy(trajectory)))
    theirs_errors = [str(error) for error in theirs.errors]
    return (ours_ok, ours.errors), (theirs_ok, theirs_errors)


def test_the_recorded_upstream_version_is_the_installed_one() -> None:
    import importlib.metadata

    assert vendored.UPSTREAM_DISTRIBUTION == "harbor"
    assert importlib.metadata.version("harbor") == vendored.UPSTREAM_VERSION, (
        "harbor moved; compare the vendored files (test_source_is_upstream) and "
        "move UPSTREAM_VERSION once they match"
    )


@pytest.mark.parametrize(("name", "upstream"), sorted(VENDORED_MODULES.items()))
def test_source_is_upstream(name: str, upstream: str) -> None:
    vendored_source = (VENDORED_DIR / f"{name}.py").read_text(encoding="utf-8")
    header = vendored_source.splitlines()[:HEADER_LINES]
    assert header[0] == "# SPDX-License-Identifier: Apache-2.0"
    assert header[1].startswith(f"# Vendored from harbor {vendored.UPSTREAM_VERSION}, ")
    upstream_source = inspect.getsource(importlib.import_module(upstream))
    assert _upstream_equivalent(vendored_source) == upstream_source, (
        f"{name}.py differs from {upstream}; re-vendor it"
    )


def test_the_package_exports_upstreams_names() -> None:
    ours = set(vendored.__all__) - {"UPSTREAM_DISTRIBUTION", "UPSTREAM_VERSION"}
    assert ours == set(harbor_trajectories.__all__)
    for name in ours:
        assert getattr(vendored, name).__name__ == getattr(harbor_trajectories, name).__name__


def test_both_models_publish_the_same_schema() -> None:
    assert (
        vendored.Trajectory.model_json_schema()
        == harbor_trajectories.Trajectory.model_json_schema()
    )


def _golden_documents() -> list[tuple[str, dict[str, Any]]]:
    return [
        (path.name, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(GOLDENS_DIR.glob("*.trajectory.json"))
    ]


def _converted_documents(synthetic_session: Path, codex_rollout: Path) -> list[dict[str, Any]]:
    claude, _ = convert_and_audit(synthetic_session)
    codex, _ = convert_codex_and_audit(codex_rollout)
    return [claude.trajectory, codex.trajectory]


def test_goldens_exist() -> None:
    assert len(_golden_documents()) >= 2


@pytest.mark.parametrize(("name", "document"), _golden_documents())
def test_golden_round_trips_through_both_models(name: str, document: dict[str, Any]) -> None:
    ours = _dump(vendored.Trajectory.model_validate(document))
    theirs = _dump(harbor_trajectories.Trajectory.model_validate(document))
    assert ours == theirs == document, name
    assert _both_validators(document) == ((True, []), (True, [])), name


def test_converted_output_round_trips_through_both_models(
    synthetic_session: Path, codex_rollout: Path
) -> None:
    """The ENRICHED output (extra fields, gap reports) is what the corpus stores."""
    for document in _converted_documents(synthetic_session, codex_rollout):
        ours = _dump(vendored.Trajectory.model_validate(document))
        theirs = _dump(harbor_trajectories.Trajectory.model_validate(document))
        assert ours == theirs == document
        assert _both_validators(document) == ((True, []), (True, []))


def _broken_variants(document: dict[str, Any]) -> list[tuple[str, Any]]:
    """One trajectory broken each way the validator has a message for."""
    missing_agent = copy.deepcopy(document)
    del missing_agent["agent"]
    extra_field = copy.deepcopy(document)
    extra_field["not_atif"] = 1
    bad_step_id = copy.deepcopy(document)
    bad_step_id["steps"][0]["step_id"] = 7
    wrong_type = copy.deepcopy(document)
    wrong_type["steps"][0]["step_id"] = "one"
    bad_literal = copy.deepcopy(document)
    bad_literal["schema_version"] = "ATIF-v9"
    no_steps = copy.deepcopy(document)
    no_steps["steps"] = []
    return [
        ("missing agent", missing_agent),
        ("extra field", extra_field),
        ("step id out of sequence", bad_step_id),
        ("wrong type", wrong_type),
        ("bad literal", bad_literal),
        ("no steps", no_steps),
        ("not a dict", [document]),
        ("not JSON", "{not json"),
    ]


@pytest.mark.parametrize(("name", "document"), _golden_documents())
def test_both_validators_reject_alike(name: str, document: dict[str, Any]) -> None:
    for label, broken in _broken_variants(document):
        (ours_ok, ours_errors), (theirs_ok, theirs_errors) = _both_validators(broken)
        assert ours_ok is theirs_ok is False, (name, label)
        assert ours_errors == theirs_errors, (name, label)
        assert ours_errors, (name, label)


def test_conversion_runs_with_harbor_and_litellm_unimportable(synthetic_session: Path) -> None:
    """The production path needs neither dev dependency: block both, convert, validate."""
    code = (
        "import sys\n"
        "sys.modules['harbor'] = None\n"
        "sys.modules['litellm'] = None\n"
        "from pathlib import Path\n"
        "from atif_converter.application.convert_and_audit import convert_and_audit\n"
        f"result, _ = convert_and_audit(Path({str(synthetic_session)!r}))\n"
        "import json\n"
        "print(result.is_valid, 'harbor' in str(sys.modules.get('harbor')))\n"
        "print(json.dumps(result.trajectory, sort_keys=True))\n"
    )
    out = subprocess.run(  # noqa: S603, fixed interpreter and code string
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    )
    verdict, dumped = out.stdout.split("\n", 1)
    assert verdict == "True False"
    in_process, _ = convert_and_audit(synthetic_session)
    assert json.loads(dumped) == in_process.trajectory
