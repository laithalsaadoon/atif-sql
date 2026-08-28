# SPDX-License-Identifier: Apache-2.0

"""Ports for atif-embed: what the embed use case NEEDS from the world.

Three seams:

* :class:`EmbeddingProvider` — async document embedding (batched,
  float-widened) + sync query embedding, stamped with ``model_id`` /
  ``dimension``.
* :class:`VectorStorePort` — the Lance-backed store surface.
* :class:`TextRowsPort` — the corpus seam. atif-embed may never import
  atif-duck (import-linter independence contract), so the embeddable text
  rows come through this port; the duckdb implementation in atif-embed's OWN
  infrastructure reads the CONTRACT corpus layout directly (the ConverterPort
  precedent from atif-corpus).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    import polars as pl

    from atif_embed.domain.text_stamp import PendingText


class EmbeddingProvider(Protocol):
    """Anything that can embed documents and queries into one vector space."""

    @property
    def model_id(self) -> str:
        """Globally unique model identity stamped on every stored row."""
        ...

    @property
    def dimension(self) -> int:
        """Fixed output vector width."""
        ...

    async def embed_documents(self, texts: list[str]) -> list[list[float] | None]:
        """Embed corpus documents; one slot per input text, in the same order.

        A slot is ``None`` when that text could not be embedded. Returning
        per-text outcomes rather than raising is what makes loss bounded: a
        batch that fails terminally must not discard the sibling batches
        whose embeddings were already billed.
        """
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed one query string as a float vector of length :attr:`dimension`."""
        ...


class VectorStorePort(Protocol):
    """The embeddings store surface the backfill writes through."""

    def table_identity(self) -> tuple[str, int] | None:
        """Return the store's stamped ``(model, dim)``, or ``None`` when empty."""
        ...

    def get_embedded_hashes(self) -> dict[str, str]:
        """Return ``{uuid: text_hash}`` for every row currently embedded."""
        ...

    def delete_uuids(self, uuids: Iterable[str]) -> int:
        """Remove the rows for ``uuids``; return how many uuids were named."""
        ...

    def add_chunk(self, df: pl.DataFrame) -> None:
        """Append one chunk of embedding rows."""
        ...

    def optimize(self) -> None:
        """Compact accumulated fragments (best-effort)."""
        ...

    def ensure_index(self, *, metric: str = "cosine") -> None:
        """Create the vector index if missing (best-effort)."""
        ...


class TextRowsPort(Protocol):
    """Anything that can yield the corpus's embeddable text rows.

    Contract semantics (CONTRACT-V2 §VSS): the embeddable unit is a step's
    flattened text — main chain AND sidechain — of at least 32 characters,
    keyed by the step's PRIMARY uuid (the first ``extra.source_uuids`` entry;
    steps without source uuids are skipped, they cannot join back to the
    ``messages``/edges surface).
    """

    def iter_unembedded(
        self,
        corpus_root: Path,
        *,
        embedded: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> Iterator[PendingText]:
        """Yield the texts needing embedding, lazily.

        ``embedded`` is the store's ``{uuid: text_hash}`` map. A uuid absent
        from it is NEW; a uuid present under a different hash is STALE (its
        text changed since it was embedded) and is yielded with
        ``replaces_existing=True``.

        Yielding lazily bounds what the CALLER holds to one write-chunk. It
        says nothing about what an implementation holds internally: whether
        peak resident text is a fraction of the corpus is a property each
        adapter must establish for itself.
        """
        ...


__all__ = ["EmbeddingProvider", "TextRowsPort", "VectorStorePort"]
