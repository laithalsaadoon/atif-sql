# SPDX-License-Identifier: Apache-2.0

"""Runtime configuration for atif-embed.

Pydantic v2 ``BaseSettings`` populated from env vars prefixed with
``ATIF_SQL_`` (workspace convention). Two providers share one vector-store
contract, selected by ``ATIF_SQL_EMBED_PROVIDER``:

* ``cohere`` (the default): Cohere Embed v4 on Bedrock via the global CRIS
  profile, 1024-dim Matryoshka, int8 documents, batch 96, concurrency 8.
* ``gemma``: EmbeddingGemma 2 on this machine, text-only, 768-dim (MRL
  widths 512/256/128 too), through the optional ``local`` extra.

Each provider keeps its own default store under the corpus root, so switching
providers never meets the fail-loud ``(model, dim)`` guard on the other's
vectors, and both stores can live side by side.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Matryoshka widths each provider can emit. Cohere Embed v4 takes these four
#: as ``output_dimension``; EmbeddingGemma 2 is trained for 768 natively and
#: for truncation to the other three (re-normalized after truncating).
PROVIDER_DIMENSIONS: dict[str, tuple[int, ...]] = {
    "cohere": (256, 512, 1024, 1536),
    "gemma": (768, 512, 256, 128),
}

#: Width a provider emits when ``output_dimension`` is unset.
DEFAULT_DIMENSION: dict[str, int] = {"cohere": 1024, "gemma": 768}

#: Store directory under the corpus root, per provider. Cohere keeps the name
#: every existing store already has.
STORE_DIRNAME: dict[str, str] = {
    "cohere": "embeddings_lance",
    "gemma": "embeddings_lance_gemma",
}


class EmbedSettings(BaseSettings):
    """Env-driven settings for the embedding pipeline."""

    model_config = SettingsConfigDict(
        env_prefix="ATIF_SQL_",
        env_file=".env",
        extra="ignore",
    )

    #: Which embedder writes and queries the store: Cohere on Bedrock, or
    #: EmbeddingGemma 2 on this machine.
    embed_provider: Literal["cohere", "gemma"] = "cohere"
    #: Cohere Embed v4 global CRIS profile on bedrock-runtime.
    embed_model_id: str = "global.cohere.embed-v4:0"
    #: Matryoshka output width; ``None`` takes the provider's default (1024
    #: for Cohere, 768 for Gemma). An ``int`` checked against the provider's
    #: widths rather than a ``Literal``, because pydantic refuses the string
    #: an environment variable carries for an integer ``Literal``.
    output_dimension: int | None = None
    #: Cohere document storage type. int8 documents + float queries is the
    #: adapter's asymmetry: docs are float-widened on the way out for the
    #: FLOAT[dim] Lance column, queries always embed as float for the distance
    #: math.
    embedding_type: Literal["int8", "float", "uint8", "binary", "ubinary"] = "int8"
    #: Max texts per invoke_model call (Cohere Embed v4 batch ceiling).
    batch_size: int = 96
    #: Concurrent Bedrock batches. 8 × batch 96 ran without throttling in
    #: testing — Cohere's TPM bucket is the binding constraint.
    embed_concurrency: int = 8
    #: AWS region for the bedrock-runtime client.
    region: str = "us-east-1"
    #: Hugging Face repository of the local model, also the identity stamped
    #: on every row it writes.
    gemma_model_id: str = "google/embeddinggemma-2"
    #: Repository commit the local model loads, pinned so a later upload under
    #: the same name cannot change the vectors a store already holds.
    gemma_revision: str = "914f7f89142e33e77833254d9c9b90c3cef7303b"
    #: Where the local model runs; ``auto`` takes CUDA, then Apple MPS, then CPU.
    gemma_device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    #: Texts per local forward pass. Peak memory grows with it times the
    #: longest text in the batch.
    gemma_batch_size: int = Field(default=32, ge=1)
    #: Local LanceDB dataset URI. ``None`` (the default) resolves per corpus
    #: and per provider via :meth:`resolve_lance_uri` — one vector store per
    #: corpus, so re-pointing the source root never mixes two corpora's
    #: vectors (the corpus-scoping rule).
    lance_uri: Path | None = None
    #: Distance metric for the IVF_HNSW_SQ index.
    hnsw_metric: Literal["cosine", "l2", "dot"] = "cosine"

    @model_validator(mode="after")
    def _width_fits_provider(self) -> Self:
        """Refuse a width the selected provider cannot emit."""
        allowed = PROVIDER_DIMENSIONS[self.embed_provider]
        if self.output_dimension is not None and self.output_dimension not in allowed:
            msg = (
                f"ATIF_SQL_OUTPUT_DIMENSION={self.output_dimension} is not a width the "
                f"{self.embed_provider} provider emits; use one of {list(allowed)} "
                f"or leave it unset for {DEFAULT_DIMENSION[self.embed_provider]}"
            )
            raise ValueError(msg)
        return self

    @property
    def embedding_dim(self) -> int:
        """The vector width the selected provider writes and queries."""
        if self.output_dimension is None:
            return DEFAULT_DIMENSION[self.embed_provider]
        return int(self.output_dimension)

    @property
    def active_model_id(self) -> str:
        """The model identity the selected provider stamps on every row."""
        if self.embed_provider == "gemma":
            return self.gemma_model_id
        return self.embed_model_id

    @property
    def active_batch_size(self) -> int:
        """Texts per provider call: one Bedrock request, or one local forward pass."""
        return self.gemma_batch_size if self.embed_provider == "gemma" else self.batch_size

    @property
    def active_concurrency(self) -> int:
        """Batches in flight at once; the local model runs one at a time."""
        return 1 if self.embed_provider == "gemma" else self.embed_concurrency

    def resolve_lance_uri(self, corpus_root: Path) -> Path:
        """Effective Lance dataset directory for ``corpus_root``."""
        if self.lance_uri is not None:
            return self.lance_uri
        return corpus_root / STORE_DIRNAME[self.embed_provider]

    def expected_embedding_identity(self) -> tuple[str, int]:
        """Return ``(model_id, dim)`` without building the embedder.

        Dependency-free identity for the fail-loud store guard: callers that
        only need to BIND the store (e.g. the search path) can enforce the
        provider stamp without importing boto3 or torch.
        """
        return (self.active_model_id, self.embedding_dim)


__all__ = [
    "DEFAULT_DIMENSION",
    "PROVIDER_DIMENSIONS",
    "STORE_DIRNAME",
    "EmbedSettings",
]
