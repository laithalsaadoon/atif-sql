# SPDX-License-Identifier: Apache-2.0

"""The parity oracle: harbor's PRIVATE converters, reachable from tests only.

atif-converter used to call ``ClaudeCode._convert_events_to_trajectory`` and
``Codex._convert_events_to_trajectory`` in production. Those are not public API
(harbor documents the ``harbor.models.trajectories`` data classes and the
validator, and nothing that converts a native session log), and the two files
that hold them took 26 and 18 upstream commits between June and September 2026.
The converters now live in this package, built on the public models; harbor's
private ones survive HERE, as the oracle a port is measured against.

Two ways to consult the oracle:

* LIVE, through :func:`harbor_claude_code_trajectory` and
  :func:`harbor_codex_trajectory`, while harbor still ships the private method.
  A test that needs it calls :func:`require_harbor_private_api` first and is
  SKIPPED, loudly, the day the method is gone — a skip here is the signal that
  the frozen goldens are now the only oracle, never a failure.
* FROZEN, through the ``goldens/`` directory beside this module: harbor's
  output for each synthetic fixture, captured while the private method
  existed, so parity stays checkable after it is removed. ``test_goldens``
  asserts the live oracle still agrees with the frozen one, which is how an
  upstream behavior change becomes a failing test instead of silent drift.

Staging is replicated here rather than imported from the adapter, because the
adapter no longer stages anything: the private methods take a DIRECTORY and
discover the transcript inside it, which is the shape this module builds.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest

GOLDENS_DIR = Path(__file__).parent / "goldens"

_CONVERT_METHOD = "_convert_events_to_trajectory"

#: Where the port departs from harbor ON PURPOSE, one pattern per decision,
#: matched against :func:`diff_paths` lines. Anything not named here is still
#: a parity failure. Keep each entry narrow (a path AND a direction), so a
#: divergence the decision did not cover still fails.
#:
#: Empty since harbor 0.24.0: harbor #3434 reads ``agentId``, so the
#: ``steps[i].extra.agent_id`` divergence this list named is gone and every
#: step's subagent id must agree with harbor's.
DELIBERATE_DIVERGENCES: tuple[re.Pattern[str], ...] = ()

#: The blob placeholder :class:`atif_converter.domain.blobs.BlobRef` writes for an
#: image, as ``_image_content_forgiven`` reads it.
_IMAGE_PLACEHOLDER = re.compile(r"\[image sha256:(?P<sha>[0-9a-f]{64}) (?P<media>\S+) \d+ bytes\]")
#: harbor's path for a Codex tool output image it saved (harbor #3467).
_HARBOR_IMAGE_PATH = re.compile(r"images/codex_(?P<sha16>[0-9a-f]{16})\.(?:png|jpg|gif|webp)")

#: ``agent.extra`` lists harbor builds from a Python ``set``, so its order
#: follows the process hash seed; ours keep first-seen order (see
#: ``_agent_extra`` in ``domain/claude_code_conversion.py``). Their element
#: paths are forgiven only when the two lists hold the same values, so a
#: missing, extra or changed value still fails parity.
SET_ORDER_DIVERGENCE_KEYS: tuple[str, ...] = ("cwds", "git_branches", "agent_ids")


def harbor_has_private_api(agent: str) -> bool:
    """Whether harbor still exposes the private converter for ``agent``."""
    if agent == "claude-code":
        from harbor.agents.installed.claude_code import ClaudeCode  # type: ignore[import-untyped]

        return callable(getattr(ClaudeCode, _CONVERT_METHOD, None))
    if agent == "codex":
        from harbor.agents.installed.codex import Codex  # type: ignore[import-untyped]

        return callable(getattr(Codex, _CONVERT_METHOD, None))
    msg = f"unknown agent {agent!r}"
    raise ValueError(msg)


def require_harbor_private_api(agent: str) -> None:
    """Skip the calling test when the live oracle is gone; never fail on it."""
    if not harbor_has_private_api(agent):
        pytest.skip(
            f"harbor no longer exposes {agent}'s private converter; the frozen "
            f"goldens under {GOLDENS_DIR} are now the only parity oracle"
        )


def _stage_claude_code_session(
    session_jsonl: Path, staging: Path, *, include_subagents: bool
) -> Path:
    """Symlink one session into the ``<projects>/<slug>/`` layout harbor discovers.

    Per-FILE symlinks under REAL directories (Python 3.13's ``rglob`` does not
    descend symlinked directories), and every side-file — including the
    workflow-nested ones harbor's own discovery cannot see — staged FLAT with
    ``__``-joined names. This is the staging the production adapter used to do.
    """
    session_dir = staging / "sessions" / "projects" / (session_jsonl.parent.name or "-unknown")
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / session_jsonl.name).symlink_to(session_jsonl.resolve())
    side_dir = session_jsonl.parent / session_jsonl.stem
    if include_subagents and side_dir.is_dir():
        staged_subagents = session_dir / side_dir.name / "subagents"
        for side_file in sorted(side_dir.rglob("*.jsonl")):
            rel_parts = side_file.relative_to(side_dir).parts
            if rel_parts and rel_parts[0] == "subagents":
                rel_parts = rel_parts[1:]
            staged = staged_subagents / "__".join(rel_parts)
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.symlink_to(side_file.resolve())
    return session_dir


def harbor_claude_code_trajectory(
    session_jsonl: Path, *, include_subagents: bool = True
) -> dict[str, Any] | None:
    """harbor's own conversion of one Claude Code session, as ``to_json_dict()``."""
    from harbor.agents.installed.claude_code import ClaudeCode  # type: ignore[import-untyped]

    with tempfile.TemporaryDirectory(prefix="atif-oracle-") as scratch:
        staging = Path(scratch)
        session_dir = _stage_claude_code_session(
            session_jsonl, staging, include_subagents=include_subagents
        )
        logs_dir = staging / "logs"
        logs_dir.mkdir()
        trajectory = getattr(ClaudeCode(logs_dir=logs_dir), _CONVERT_METHOD)(session_dir)
    return None if trajectory is None else trajectory.to_json_dict()


def harbor_codex_trajectory(rollout_jsonl: Path) -> dict[str, Any] | None:
    """harbor's own conversion of one Codex rollout, staged alone, as ``to_json_dict()``."""
    from harbor.agents.installed.codex import Codex  # type: ignore[import-untyped]

    with tempfile.TemporaryDirectory(prefix="atif-oracle-") as scratch:
        staging = Path(scratch)
        session_dir = staging / "sessions"
        session_dir.mkdir()
        (session_dir / rollout_jsonl.name).symlink_to(rollout_jsonl.resolve())
        logs_dir = staging / "logs"
        logs_dir.mkdir()
        trajectory = getattr(Codex(logs_dir=logs_dir), _CONVERT_METHOD)(session_dir)
    return None if trajectory is None else trajectory.to_json_dict()


def golden_path(name: str) -> Path:
    """Where the frozen oracle output for one named fixture lives."""
    return GOLDENS_DIR / f"{name}.trajectory.json"


def load_golden(name: str) -> dict[str, Any]:
    return json.loads(golden_path(name).read_text(encoding="utf-8"))


def write_golden(name: str, trajectory: dict[str, Any]) -> None:
    golden_path(name).write_text(
        json.dumps(trajectory, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def diff_paths(expected: Any, actual: Any, path: str = "$") -> list[str]:
    """Every JSON path where two trajectories disagree, with both values.

    A flat list rather than an assertion, so a parity failure names ALL the
    divergent fields at once instead of one per test run.
    """
    if isinstance(expected, dict) and isinstance(actual, dict):
        out: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                out.append(f"{path}.{key}: only in ours = {actual[key]!r}")
            elif key not in actual:
                out.append(f"{path}.{key}: only in harbor = {expected[key]!r}")
            else:
                out.extend(diff_paths(expected[key], actual[key], f"{path}.{key}"))
        return out
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: length harbor={len(expected)} ours={len(actual)}"]
        out = []
        for index, (e, a) in enumerate(zip(expected, actual, strict=True)):
            out.extend(diff_paths(e, a, f"{path}[{index}]"))
        return out
    if expected != actual:
        return [f"{path}: harbor={expected!r} ours={actual!r}"]
    return []


#: JSON paths where our converter departs from harbor ON PURPOSE when a
#: session holds a model harbor prices at a fabricated $0 (a rate-less litellm
#: entry) or one we price from a local override (``pricing.override_models()``). harbor sums
#: litellm's ``(0.0, 0.0)``; we report no estimate, or the override's price.
PRICING_DIVERGENCE_PATHS: frozenset[str] = frozenset(
    {"$.final_metrics.total_cost_usd", "$.final_metrics.extra.cost_source"}
)


#: The same policy at the step level (harbor 0.23.0 prices every step): a step
#: whose model harbor prices at a fabricated $0 carries no ``cost_usd`` and no
#: label in ours, and a step priced from a local override carries the override's
#: price and label. ``{i}`` is the step index.
STEP_PRICING_DIVERGENCE_PATHS: tuple[str, ...] = (
    "$.steps[{i}].metrics.cost_usd",
    "$.steps[{i}].metrics.extra.cost_source",
)


def _diff_path(diff: str) -> str:
    return diff.split(":", 1)[0]


def _policy_prices_differently(model: object) -> bool:
    """Whether the pricing policy prices ``model`` differently from harbor's litellm.

    True for a model priced from a local override, and for one the vendored
    table can't price (harbor writes litellm's $0 there, we write nothing).
    """
    from atif_converter.domain import pricing

    if not isinstance(model, str):
        return False
    if pricing.is_local_override(model):
        return True
    try:
        priced = pricing.priced_cost_per_token(
            model=model,
            prompt_tokens=1,
            completion_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
    except Exception:  # noqa: BLE001 - a model nobody prices is not a policy divergence
        return False
    return priced is None


def _step_pricing_forgiven(expected: dict[str, Any], ours: dict[str, Any]) -> frozenset[str]:
    """The step-level :data:`STEP_PRICING_DIVERGENCE_PATHS` the pricing policy explains.

    Checked per step against that step's own model, so a priced step that
    regressed still fails parity even in a session holding an unpriced one.
    """
    forgiven: set[str] = set()
    harbor_steps = expected.get("steps") or []
    for index, step in enumerate(ours.get("steps") or []):
        if not _policy_prices_differently(step.get("model_name")):
            continue
        forgiven.update(path.format(i=index) for path in STEP_PRICING_DIVERGENCE_PATHS)
        # When the label was harbor's only metrics.extra key, ours has no extra
        # at all, and the diff names the parent path instead.
        harbor_step = harbor_steps[index] if index < len(harbor_steps) else {}
        harbor_extra = (harbor_step.get("metrics") or {}).get("extra")
        if isinstance(harbor_extra, dict) and set(harbor_extra) == {"cost_source"}:
            forgiven.add(f"$.steps[{index}].metrics.extra")
    return frozenset(forgiven)


def _pricing_divergence_expected(ours: dict[str, Any]) -> bool:
    """Whether ``ours`` is a trajectory the pricing policy SHOULD price differently from harbor.

    Two cases, each checked against the policy itself so a priced session that
    regressed to ``None`` still fails parity: an estimate labeled as using the
    local overrides, or no estimate while some agent step's model has no price.
    """
    from atif_converter.domain import pricing

    final_metrics = ours.get("final_metrics") or {}
    extra = final_metrics.get("extra") or {}
    if extra.get("cost_source") == pricing.COST_SOURCE_WITH_OVERRIDES:
        return True
    if final_metrics.get("total_cost_usd") is not None:
        return False
    models = {
        step.get("model_name")
        for step in ours.get("steps") or []
        if step.get("source") == "agent" and isinstance(step.get("model_name"), str)
    }
    for model in models:
        try:
            priced = pricing.priced_cost_per_token(
                model=model,
                prompt_tokens=1,
                completion_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )
        except Exception:  # noqa: BLE001, S112 - a model nobody prices is not a policy divergence
            continue
        if priced is None:
            return True
    return False


def _set_order_forgiven(expected: dict[str, Any], ours: dict[str, Any]) -> tuple[str, ...]:
    """Path prefixes of the :data:`SET_ORDER_DIVERGENCE_KEYS` lists that differ only in order."""
    harbor_extra = (expected.get("agent") or {}).get("extra") or {}
    our_extra = (ours.get("agent") or {}).get("extra") or {}
    forgiven: list[str] = []
    for key in SET_ORDER_DIVERGENCE_KEYS:
        theirs, mine = harbor_extra.get(key), our_extra.get(key)
        if isinstance(theirs, list) and isinstance(mine, list) and sorted(theirs) == sorted(mine):
            forgiven.append(f"$.agent.extra.{key}[")
    return tuple(forgiven)


def _image_content_matches(theirs: Any, ours: Any) -> bool:
    """Whether ``ours`` is harbor's image-bearing tool output, with each image as its placeholder.

    harbor 0.24.0 saves a Codex tool output's inline image to a file and returns
    the content as parts; our pure converter writes the blob placeholder text
    instead and joins the parts with newlines, as harbor joins a text-only list
    (see ``_tool_output_content`` in ``domain/codex_conversion.py``). Each
    placeholder must name the same bytes harbor saved (its SHA-256 starts with
    the 16 hex digits of harbor's file name) and the same media type, and every
    text part must match exactly.
    """
    if not isinstance(theirs, list) or not isinstance(ours, str):
        return False
    if not any(isinstance(part, dict) and part.get("type") == "image" for part in theirs):
        return False
    texts: list[str] = []
    for part in theirs:
        if not isinstance(part, dict):
            return False
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
            continue
        source = part.get("source") or {}
        path = _HARBOR_IMAGE_PATH.fullmatch(str(source.get("path", "")))
        if part.get("type") != "image" or path is None:
            return False
        texts.append(f"\0{path['sha16']}\0{source.get('media_type')}\0")
    # Rebuild ours with each placeholder in the same marker form, then compare whole.
    rebuilt = _IMAGE_PLACEHOLDER.sub(lambda m: f"\0{m['sha'][:16]}\0{m['media']}\0", ours)
    return rebuilt == "\n".join(texts)


def _image_content_forgiven(expected: dict[str, Any], ours: dict[str, Any]) -> frozenset[str]:
    """The ``observation.results[j].content`` paths :func:`_image_content_matches` explains."""
    forgiven: set[str] = set()
    harbor_steps = expected.get("steps") or []
    for index, step in enumerate(ours.get("steps") or []):
        if index >= len(harbor_steps):
            break
        our_results = (step.get("observation") or {}).get("results") or []
        harbor_results = (harbor_steps[index].get("observation") or {}).get("results") or []
        for position, (theirs, mine) in enumerate(zip(harbor_results, our_results, strict=False)):
            if _image_content_matches(theirs.get("content"), mine.get("content")):
                forgiven.add(f"$.steps[{index}].observation.results[{position}].content")
    return frozenset(forgiven)


def parity_diffs(expected: dict[str, Any], ours: dict[str, Any]) -> list[str]:
    """:func:`diff_paths` minus the documented deliberate divergences.

    Five kinds are forgiven. Every line a :data:`DELIBERATE_DIVERGENCES`
    pattern matches, always; a Codex tool output's content where harbor saved
    an image and ours holds its blob placeholder, only when
    :func:`_image_content_matches` holds; the element lines of a
    :data:`SET_ORDER_DIVERGENCE_KEYS` list, only when both sides hold the same
    values; the :data:`PRICING_DIVERGENCE_PATHS`, only when
    :func:`_pricing_divergence_expected` says the pricing policy explains them;
    and a step's :data:`STEP_PRICING_DIVERGENCE_PATHS`, only when that step's
    model is one the policy prices differently.
    """
    step_forgiven = _step_pricing_forgiven(expected, ours) | _image_content_forgiven(expected, ours)
    order_forgiven = _set_order_forgiven(expected, ours)
    diffs = [
        line
        for line in diff_paths(expected, ours)
        if not any(pattern.match(line) for pattern in DELIBERATE_DIVERGENCES)
        and _diff_path(line) not in step_forgiven
        and not (order_forgiven and _diff_path(line).startswith(order_forgiven))
    ]
    if not _pricing_divergence_expected(ours):
        return diffs
    # When the label was harbor's only final_metrics.extra key, ours has no
    # extra at all, and the diff names the parent path instead.
    harbor_extra = (expected.get("final_metrics") or {}).get("extra")
    label_only = isinstance(harbor_extra, dict) and set(harbor_extra) == {"cost_source"}
    forgiven = PRICING_DIVERGENCE_PATHS | ({"$.final_metrics.extra"} if label_only else frozenset())
    return [diff for diff in diffs if _diff_path(diff) not in forgiven]
