# SPDX-License-Identifier: Apache-2.0

"""How the per-session JSON artifacts are stored: their names and their compression.

Three of the artifacts a session directory holds are the bulk of a corpus's
bytes: ``trajectory.json`` (the ATIF document), ``edges.jsonl`` and
``session_events.jsonl``. materialize stores each of them zstd-compressed,
as ``<name>.zst``, and a corpus written before that keeps the plain file until
``atif-sql corpus slim`` compresses it. Either spelling holds the same bytes
once decompressed, so every reader resolves the stored file per session
(:func:`stored_names`, the compressed one first) and nothing else about it
changes. ``meta.json`` and ``loss_report.json`` stay plain: they're small, and
``meta.json`` is what the corpus writer and every reader check first.

A path a view reports (``sessions.trajectory_path``) names the logical
artifact, ``<session>/trajectory.json``, whichever spelling is on disk, so a
row reads the same before and after a corpus is slimmed.

atif-corpus writes these names and carries the twin constants; the cross-package
pin lives in atif-cli's tests, the one package that may import both.

Pure constants: no filesystem access.
"""

from __future__ import annotations

#: The ATIF document.
TRAJECTORY_JSON: str = "trajectory.json"
#: One line per raw transcript record.
EDGES_JSONL: str = "edges.jsonl"
#: One line per kept non-message record.
SESSION_EVENTS_JSONL: str = "session_events.jsonl"
#: The completion marker every reader gates on (always plain).
META_JSON: str = "meta.json"
#: The fidelity report (always plain).
LOSS_REPORT_JSON: str = "loss_report.json"

#: The suffix a compressed artifact carries after its logical name.
COMPRESSED_SUFFIX: str = ".zst"

#: The artifacts materialize stores compressed.
COMPRESSED_ARTIFACTS: tuple[str, ...] = (TRAJECTORY_JSON, EDGES_JSONL, SESSION_EVENTS_JSONL)


def stored_names(name: str) -> tuple[str, ...]:
    """The file names ``name`` may be stored under, in the order a reader tries them.

    The compressed spelling comes first: a session caught between ``corpus
    slim`` writing ``<name>.zst`` and removing ``<name>`` holds both, with the
    same content, and either answer is right.
    """
    if name in COMPRESSED_ARTIFACTS:
        return (f"{name}{COMPRESSED_SUFFIX}", name)
    return (name,)


__all__ = [
    "COMPRESSED_ARTIFACTS",
    "COMPRESSED_SUFFIX",
    "EDGES_JSONL",
    "LOSS_REPORT_JSON",
    "META_JSON",
    "SESSION_EVENTS_JSONL",
    "TRAJECTORY_JSON",
    "stored_names",
]
