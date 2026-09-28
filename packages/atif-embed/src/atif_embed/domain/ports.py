# SPDX-License-Identifier: Apache-2.0

"""Ports for atif-embed: what the embed use case NEEDS from the world.

Four seams:

* :class:`EmbeddingProvider` — async document embedding (batched,
  float-widened) + sync query embedding, stamped with ``model_id`` /
  ``dimension``.
* :class:`VectorStorePort` — the Lance-backed store surface.
* :class:`TextRowsPort` — the corpus seam. atif-embed may never import
  atif-duck (import-linter independence contract), so the embeddable text
  rows come through this port; the duckdb implementation in atif-embed's OWN
  infrastructure reads the CONTRACT corpus layout directly (the ConverterPort
  precedent from atif-corpus).
* :class:`LakeStepsPort` — the corpus's steps as the DuckLake holds them.
  atif-duck owns the lake, so atif-cli implements this port over it and
  hands it to :class:`~atif_embed.infrastructure.lake_text_rows.LakeTextRows`,
  which reads only what changed since the last complete pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path
    from types import TracebackType

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

    @property
    def discovery(self) -> str:
        """Which path the last :meth:`iter_unembedded` read (``corpus``, ``lake-full``, ...)."""
        ...

    def commit(self, *, stored_rows: int) -> None:
        """Record that every row the last :meth:`iter_unembedded` yielded is now stored.

        The use case calls it after a run that consumed the whole discovery
        and embedded every row of it, with the store's row count afterwards.
        An adapter with a watermark advances it here (and only when that
        discovery ran to its end, not to a ``--limit``); one without does
        nothing.
        """
        ...


@dataclass(frozen=True, slots=True)
class LakePosition:
    """Where one corpus's lake history stands: which history, and how far along it.

    ``lineage`` changes whenever the corpus's rows are loaded from scratch (a
    ``lake rebuild``, or the first load of a corpus), because snapshot ids
    from one history mean nothing in the next.
    """

    lineage: str
    snapshot_id: int


class LakeStepSnapshot(Protocol):
    """One corpus's steps at one lake snapshot, open until closed."""

    @property
    def position(self) -> LakePosition:
        """The snapshot being read."""
        ...

    def can_read_changes_since(self, since: LakePosition) -> bool:
        """True when the lake still holds every snapshot after ``since`` in this lineage."""
        ...

    def step_texts(
        self, *, min_chars: int, changed_since: LakePosition | None
    ) -> Iterator[tuple[str, str]]:
        """``(primary uuid, flattened text)`` for every qualifying step, in corpus order.

        Corpus order is session id, then step id. A qualifying step has a
        primary uuid, a non-NULL text and at least ``min_chars`` characters
        of it. With ``changed_since``, only rows whose uuid appears in a step
        row inserted or deleted after that position are read; those rows
        still come back in corpus order, every occurrence of each uuid, so
        the first-wins rule picks the same text a full read would.
        """
        ...

    def step_keys(self) -> frozenset[str]:
        """Every step's primary uuid in the corpus, whatever its text."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...

    def __enter__(self) -> Self:
        """Open for a ``with`` block."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close at the end of the ``with`` block."""
        ...


class LakeStepsPort(Protocol):
    """Open the lake's view of one corpus's steps."""

    def open_steps(self, corpus_root: Path) -> LakeStepSnapshot | None:
        """The corpus's steps at the lake's current snapshot, or ``None`` to fall back.

        ``None`` means no lake the caller should read: none exists, its
        schema is stale, it doesn't hold this corpus, or it holds another
        directory under the same name. The caller then reads the per-session
        artifacts, as it did before the lake existed.
        """
        ...


__all__ = [
    "EmbeddingProvider",
    "LakePosition",
    "LakeStepSnapshot",
    "LakeStepsPort",
    "TextRowsPort",
    "VectorStorePort",
]
