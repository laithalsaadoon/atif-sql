# SPDX-License-Identifier: Apache-2.0

"""Load DuckDB's lance extension, installing it first when the caller allows.

atif-embed reads and writes its vector store through the extension, and it
may never import atif-duck (the import-linter independence contract), so it
carries this small helper instead of reusing atif-duck's twin in
``atif_duck.infrastructure.registry``.

Installing reaches the network (the extension is a download of a few hundred
megabytes from DuckDB's repository), so only a caller that already reaches
the network may ask for it: a real embed run, which calls Bedrock anyway. A
read-only caller gets :class:`~atif_embed.domain.errors.LanceExtensionMissing`
instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from atif_embed.domain.errors import LanceExtensionInstallFailed, LanceExtensionMissing

if TYPE_CHECKING:
    import duckdb

#: DuckDB core extension that reads and writes Lance datasets.
LANCE_EXTENSION = "lance"


def lance_extension_installed(con: duckdb.DuckDBPyConnection) -> bool:
    """True when the lance extension is in this connection's extension directory.

    Read from ``duckdb_extensions()``, which lists the local directory and
    never reaches the network.
    """
    rows = con.execute(
        "SELECT installed FROM duckdb_extensions() WHERE extension_name = ?", [LANCE_EXTENSION]
    ).fetchall()
    return bool(rows) and bool(rows[0][0])


def load_lance_extension(con: duckdb.DuckDBPyConnection, *, install: bool) -> None:
    """``LOAD`` the lance extension, ``INSTALL``-ing it first when absent and ``install``.

    Raises
    ------
    LanceExtensionMissing
        The extension is absent and ``install`` is false.
    LanceExtensionInstallFailed
        The extension is absent, ``install`` is true, and the download failed.
    """
    import duckdb

    if not lance_extension_installed(con):
        if not install:
            msg = (
                f"the {LANCE_EXTENSION} DuckDB extension is not installed, so the embeddings "
                "store cannot be opened; run `atif-sql embed --install-extension` "
                "(a one-time download)"
            )
            raise LanceExtensionMissing(msg)
        logger.info("Installing the {} DuckDB extension", LANCE_EXTENSION)
        try:
            con.execute(f"INSTALL {LANCE_EXTENSION};")
        except duckdb.Error as exc:
            first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            msg = f"could not install the {LANCE_EXTENSION} DuckDB extension: {first_line}"
            raise LanceExtensionInstallFailed(msg) from exc
    con.execute(f"LOAD {LANCE_EXTENSION};")


__all__ = ["LANCE_EXTENSION", "lance_extension_installed", "load_lance_extension"]
