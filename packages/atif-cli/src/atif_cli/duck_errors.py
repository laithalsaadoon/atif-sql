# SPDX-License-Identifier: Apache-2.0

"""DuckDB error classification for the ``query`` and ``search`` commands.

The one piece of the exit-code taxonomy that needs ``import duckdb`` — kept
out of :mod:`atif_cli.errors` (pure) and :mod:`atif_cli.output` (rendering)
so the lean import path never touches the driver. Import this module ONLY
inside command bodies that already hold a DuckDB connection.

The mapping: ``ParserException`` -> parse_error (64), ``CatalogException`` ->
catalog_error (65), everything else under ``duckdb.Error`` -> runtime_error
(70). The distinction agents act on is 64 vs 65 — a 64 means rewrite the SQL,
a 65 means the object does not exist, so run ``atif-sql schema`` first.

:data:`REGISTRATION_ERRORS` widens the caught set on the registration path.
``EmbeddingProviderMismatch`` derives from ``Exception``, not
``duckdb.Error``, so a bare ``except duckdb.Error`` lets it escape as an
unhandled traceback with exit 1 — a code outside :data:`EXIT_CODES`
entirely.
"""

from __future__ import annotations

import duckdb

from atif_cli.errors import EXIT_CODES, ClassifiedError
from atif_duck.domain.embedding_guard import EmbeddingProviderMismatch

#: What ``register`` can raise: DuckDB DDL failures plus the embedding-store
#: identity guard. Catch this tuple, then hand the exception to
#: :func:`classify_registration_error`.
REGISTRATION_ERRORS: tuple[type[Exception], ...] = (duckdb.Error, EmbeddingProviderMismatch)


def classify_duckdb_error(exc: duckdb.Error) -> ClassifiedError:
    """Classify a ``duckdb.Error`` into a stable kind + exit code."""
    message = str(exc)
    if isinstance(exc, duckdb.ParserException):
        return ClassifiedError(
            kind="parse_error",
            exit_code=EXIT_CODES["parse_error"],
            message=message,
            hint="check SQL syntax; try `atif-sql schema --format json` for view/macro names",
        )
    if isinstance(exc, duckdb.CatalogException):
        return ClassifiedError(
            kind="catalog_error",
            exit_code=EXIT_CODES["catalog_error"],
            message=message,
            hint="unknown view or column; run `atif-sql schema --format json` for the catalog",
        )
    if isinstance(exc, duckdb.IOException):
        return ClassifiedError(
            kind="runtime_error",
            exit_code=EXIT_CODES["runtime_error"],
            message=message,
            hint="is the corpus materialized? run `atif-sql materialize` then `atif-sql status`",
        )
    return ClassifiedError(
        kind="runtime_error",
        exit_code=EXIT_CODES["runtime_error"],
        message=message,
        hint=None,
    )


def classify_registration_error(exc: Exception) -> ClassifiedError:
    """Classify anything :data:`REGISTRATION_ERRORS` catches.

    ``EmbeddingProviderMismatch`` maps to ``embedding_mismatch`` at exit 65
    (the catalog-class code: the store's stamped identity does not match the
    active embedder, so the requested object cannot be bound). Everything
    else falls through to :func:`classify_duckdb_error`.
    """
    if isinstance(exc, EmbeddingProviderMismatch):
        return ClassifiedError(
            kind="embedding_mismatch",
            exit_code=EXIT_CODES["embedding_mismatch"],
            message=str(exc),
            hint="delete the Lance store and re-run `atif-sql embed --all --no-dry-run`, "
            "or point ATIF_SQL_EMBED_MODEL_ID at the model that wrote it",
        )
    if isinstance(exc, duckdb.Error):
        return classify_duckdb_error(exc)
    return ClassifiedError(
        kind="runtime_error",
        exit_code=EXIT_CODES["runtime_error"],
        message=str(exc),
        hint=None,
    )


__all__ = [
    "REGISTRATION_ERRORS",
    "classify_duckdb_error",
    "classify_registration_error",
]
