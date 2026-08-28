# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for atif-embed tests.

* ``corpus_root`` — a tiny contract-shaped corpus (two sessions) exercising
  the TextRowsPort selection rules: >=32-char floor, ARRAY message
  flattening, sidechain inclusion, missing-source_uuids skip, and the
  meta.json torn-set gate. NO dependency on atif-corpus / atif-duck
  (import-linter independence).
* ``FakeEmbedder`` — a deterministic in-process :class:`EmbeddingProvider`.
* ``fake_bedrock_client`` — a stub boto3 client recording ``invoke_model``
  bodies and returning right-shaped vectors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

SESSION_IDS = [
    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
]

#: Long enough to clear the 32-char floor.
LONG_USER_TEXT = "Please investigate the flaky integration test in the corpus writer."
LONG_AGENT_TEXT = "The flaky test stems from a race between the watermark writer and the scanner."
LONG_SIDECHAIN_TEXT = (
    "Subagent findings: the scanner holds a stale directory listing across retries."
)
ARRAY_PART_ONE = "Compact summary part one carries enough characters to qualify."
ARRAY_PART_TWO = "Part two adds more."

#: uuids the fixture corpus should yield as embeddable, in scan order.
EXPECTED_EMBEDDABLE_UUIDS = ["ua-1", "aa-1", "sa-1", "uc-1", "ub-1"]


def _session_one() -> dict[str, Any]:
    """Rich session: qualifying, short, sidechain, keyless, and ARRAY steps."""
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": SESSION_IDS[0],
        "agent": {"name": "claude-code", "version": "2.1.218", "model_name": "m"},
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-20T10:00:00.000Z",
                "source": "user",
                "message": LONG_USER_TEXT,
                "extra": {"is_sidechain": False, "source_uuids": ["ua-1", "ua-1b"]},
            },
            {
                # Short text (< 32 chars) — must be skipped.
                "step_id": 2,
                "timestamp": "2026-08-20T10:00:05.000Z",
                "source": "agent",
                "message": "On it.",
                "extra": {"is_sidechain": False, "source_uuids": ["short-1"]},
            },
            {
                "step_id": 3,
                "timestamp": "2026-08-20T10:00:10.000Z",
                "source": "agent",
                "message": LONG_AGENT_TEXT,
                "extra": {"is_sidechain": False, "source_uuids": ["aa-1"]},
            },
            {
                # Sidechain step — MUST be included (main+sidechain).
                "step_id": 4,
                "timestamp": "2026-08-20T10:00:20.000Z",
                "source": "agent",
                "message": LONG_SIDECHAIN_TEXT,
                "extra": {"is_sidechain": True, "source_uuids": ["sa-1"]},
            },
            {
                # No source_uuids — cannot be keyed; must be skipped.
                "step_id": 5,
                "timestamp": "2026-08-20T10:00:30.000Z",
                "source": "agent",
                "message": "This step has plenty of text but no source uuids at all.",
                "extra": {"is_sidechain": False},
            },
            {
                # ARRAY message — parts joined with blank lines.
                "step_id": 6,
                "timestamp": "2026-08-20T10:00:40.000Z",
                "source": "user",
                "message": [
                    {"type": "text", "text": ARRAY_PART_ONE},
                    {"type": "text", "text": ARRAY_PART_TWO},
                ],
                "extra": {"is_sidechain": False, "source_uuids": ["uc-1"]},
            },
        ],
        "final_metrics": {"total_steps": 6},
    }


def _session_two() -> dict[str, Any]:
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": SESSION_IDS[1],
        "agent": {"name": "claude-code", "version": "2.1.218", "model_name": "m"},
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-08-21T09:00:00.000Z",
                "source": "user",
                "message": "Another qualifying user message with enough characters.",
                "extra": {"is_sidechain": False, "source_uuids": ["ub-1"]},
            },
        ],
        "final_metrics": {"total_steps": 1},
    }


def write_corpus(root: Path, *, torn_session: bool = False) -> Path:
    """Write the two-session corpus; optionally add a torn dir (no meta)."""
    for session_id, trajectory in (
        (SESSION_IDS[0], _session_one()),
        (SESSION_IDS[1], _session_two()),
    ):
        sdir = root / "sessions" / session_id
        sdir.mkdir(parents=True)
        (sdir / "trajectory.json").write_text(json.dumps(trajectory, separators=(",", ":")))
        (sdir / "edges.jsonl").write_text("")
        (sdir / "loss_report.json").write_text("{}")
        (sdir / "meta.json").write_text(json.dumps({"session_id": session_id}))
    if torn_session:
        torn = root / "sessions" / "cccccccc-cccc-cccc-cccc-cccccccccccc"
        torn.mkdir(parents=True)
        torn_trajectory: dict[str, Any] = _session_two() | {"session_id": torn.name}
        torn_trajectory["steps"][0]["extra"]["source_uuids"] = ["torn-1"]
        (torn / "trajectory.json").write_text(json.dumps(torn_trajectory, separators=(",", ":")))
        # NO meta.json — the torn-set guard must exclude this dir.
    return root


def rewrite_step_text(corpus_root: Path, uuid: str, new_text: str) -> None:
    """Rewrite the flattened text of the step keyed by ``uuid``, in place.

    Stands in for a re-conversion after a harbor/converter fix: the uuid is
    unchanged, the text is not — exactly the case a uuid-only anti-join
    cannot see.
    """
    for trajectory_path in sorted((corpus_root / "sessions").glob("*/trajectory.json")):
        trajectory: dict[str, Any] = json.loads(trajectory_path.read_text())
        changed = False
        for step in trajectory["steps"]:
            extra: dict[str, Any] = step.get("extra") or {}
            if extra.get("source_uuids", [None])[0] == uuid:
                step["message"] = new_text
                changed = True
        if changed:
            trajectory_path.write_text(json.dumps(trajectory, separators=(",", ":")))
            return
    msg = f"no step keyed by {uuid!r} in {corpus_root}"
    raise AssertionError(msg)


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    """Contract-shaped corpus with one torn session dir."""
    return write_corpus(tmp_path / "corpus", torn_session=True)


class FakeEmbedder:
    """Deterministic :class:`EmbeddingProvider` for use-case tests.

    ``fail_texts`` names texts whose slot comes back ``None``, standing in for
    a batch that exhausted its retry budget while its siblings succeeded.
    """

    def __init__(
        self,
        *,
        model_id: str = "fake-model:1",
        dim: int = 8,
        fail_texts: set[str] | None = None,
    ) -> None:
        self._model_id = model_id
        self._dim = dim
        self._fail_texts = fail_texts or set()
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dim

    def _vector(self, text: str) -> list[float]:
        # Deterministic, text-dependent, non-degenerate.
        seed = sum(ord(c) for c in text) or 1
        return [((seed * (i + 1)) % 97) / 97.0 + 0.01 for i in range(self._dim)]

    async def embed_documents(self, texts: list[str]) -> list[list[float] | None]:
        self.document_calls.append(list(texts))
        return [None if t in self._fail_texts else self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return self._vector(text)
