# SPDX-License-Identifier: Apache-2.0

"""Fresh-interpreter lean-import gate.

A bare ``import atif_cli.app`` must not pull duckdb or harbor: the fast path
(``schema`` / ``--help`` / ``--version``) needs neither, and each costs
hundreds of ms of import time. Heavy imports belong inside the command
bodies that use them.
"""

from __future__ import annotations

import subprocess
import sys

#: Modules that must NOT load on a bare ``import atif_cli.app``.
_FORBIDDEN_EAGER_IMPORTS = (
    "duckdb",
    "harbor",
    "atif_converter",
    "atif_duck.infrastructure",
    # atif-analytics drags umap/boto3-adjacent subtrees; the analyze command
    # defers it into its body.
    "atif_analytics",
    "umap",
    # VSS stack: lancedb costs ~2.6s of import, boto3 hundreds of ms; both
    # belong inside the embed/search command bodies.
    "atif_embed",
    "lancedb",
    "boto3",
    "polars",
)


def test_cli_import_is_lean() -> None:
    probe = (
        "import sys; import atif_cli.app; "
        f"forbidden = {_FORBIDDEN_EAGER_IMPORTS!r}; "
        "leaked = sorted(m for m in sys.modules "
        "if m in forbidden or any(m.startswith(f + '.') for f in forbidden)); "
        "print('|'.join(leaked))"
    )
    result = subprocess.run(  # noqa: S603 — argv is this interpreter plus a literal probe
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"import probe failed: {result.stderr[:500]}"
    leaked = [m for m in result.stdout.strip().split("|") if m]
    assert not leaked, (
        f"atif_cli.app eagerly imported heavy modules ({leaked}); "
        "defer the import into the command body that uses it"
    )
