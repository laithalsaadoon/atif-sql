# SPDX-License-Identifier: Apache-2.0

"""CLI error taxonomy: stable exit codes + the classified-error shape.

:data:`EXIT_CODES` is the workspace's ONE exit-code table. The numbers follow
the BSD ``sysexits.h`` range so a shell reading them gets the conventional
meaning: 64 for malformed input or SQL, 65 for a catalog miss (unknown
view/macro/column), 70 for a runtime error. No other package owns a mapping —
atif-converter raises typed :class:`~atif_converter.domain.errors.DomainError`
subclasses and deliberately carries no codes, so ``convert`` translates them
here and every command exits from the same dict.

A number in this dict is a WIRE CONTRACT. An agent branches on it, so
reassigning an existing key to a different number is a breaking change even
though nothing in this repo fails.

Pure module: no duckdb, no atif_* imports — the concrete ``duckdb.Error``
classifier lives in :mod:`atif_cli.duck_errors` so this module stays on the
lean import path (pinned by the fresh-interpreter lean-import test).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Exit codes agents can rely on. Add keys freely; never renumber one.
EXIT_CODES: dict[str, int] = {
    "ok": 0,
    "empty_session": 2,  # convert: session parsed but harbor produced no trajectory
    "no_embeddings": 2,  # search: store empty — run `atif-sql embed --all --no-dry-run`
    "invalid_input": 64,  # malformed user-supplied flags / paths
    "parse_error": 64,  # malformed SQL
    "catalog_error": 65,  # unknown view/macro/column
    "validation_error": 65,  # convert: trajectory failed TrajectoryValidator
    "embedding_mismatch": 65,  # query/search: Lance store written by another provider
    "runtime_error": 70,  # everything else duckdb (or the adapter) raises
    "terminal_state": 78,  # embed: store/config state requires OPERATOR action (EX_CONFIG);
    # retrying without intervention cannot succeed — unattended lanes suppress
    # retries on this code instead of burning identical ticks
    "suspicious_scan": 78,  # materialize: the source scan found 0 sessions over a
    # non-empty corpus, so ghost removal was refused. Same EX_CONFIG contract as
    # terminal_state — a wrong source_root needs an operator, and a retry over the
    # same wrong root cannot succeed — but a distinct `kind` string, because the
    # remedy is a path, not a store. A separate code matters most for what it is
    # NOT: exit 1 here is an uncaught traceback, indistinguishable from a crash.
    "harbor_missing": 127,  # RETIRED, kept so this table never renumbers. It named
    # the loss of the private harbor method the converter used to be built on; the
    # conversion is ours now (atif_converter.domain.*_conversion), so no code path
    # raises it. EX_NOTFOUND by convention: a surface, not a datum.
}


@dataclass(frozen=True, slots=True)
class ClassifiedError:
    """The structured shape of a CLI error after classification."""

    kind: str  # "invalid_input" | "parse_error" | "catalog_error" | "runtime_error"
    exit_code: int
    message: str
    hint: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """The JSON error envelope emitted on non-TTY stderr."""
        return {
            "error": {
                "kind": self.kind,
                "message": self.message,
                "hint": self.hint,
            }
        }


__all__ = ["EXIT_CODES", "ClassifiedError"]
