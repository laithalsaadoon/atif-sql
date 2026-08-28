# SPDX-License-Identifier: Apache-2.0

"""Runtime configuration for atif-embed.

Pydantic v2 ``BaseSettings`` populated from env vars prefixed with
``ATIF_SQL_`` (workspace convention). Defaults: Cohere Embed v4 on Bedrock
via the global CRIS profile, 1024-dim Matryoshka, int8 documents, batch 96,
concurrency 8.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbedSettings(BaseSettings):
    """Env-driven settings for the embedding pipeline."""

    model_config = SettingsConfigDict(
        env_prefix="ATIF_SQL_",
        env_file=".env",
        extra="ignore",
    )

    #: Cohere Embed v4 global CRIS profile on bedrock-runtime.
    embed_model_id: str = "global.cohere.embed-v4:0"
    #: Matryoshka output width.
    output_dimension: Literal[256, 512, 1024, 1536] = 1024
    #: Document storage type. int8 documents + float queries is the adapter's
    #: asymmetry: docs are float-widened on the way out for the FLOAT[dim]
    #: Lance column, queries always embed as float for the distance math.
    embedding_type: Literal["int8", "float", "uint8", "binary", "ubinary"] = "int8"
    #: Max texts per invoke_model call (Cohere Embed v4 batch ceiling).
    batch_size: int = 96
    #: Concurrent Bedrock batches. 8 × batch 96 ran without throttling in
    #: testing — Cohere's TPM bucket is the binding constraint.
    embed_concurrency: int = 8
    #: AWS region for the bedrock-runtime client.
    region: str = "us-east-1"
    #: Local LanceDB dataset URI. ``None`` (the default) resolves per-corpus
    #: to ``<corpus_root>/embeddings_lance`` via :meth:`resolve_lance_uri`
    #: — one vector store per corpus, so re-pointing the source root never
    #: mixes two corpora's vectors (the corpus-scoping rule).
    lance_uri: Path | None = None
    #: Distance metric for the IVF_HNSW_SQ index.
    hnsw_metric: Literal["cosine", "l2", "dot"] = "cosine"

    def resolve_lance_uri(self, corpus_root: Path) -> Path:
        """Effective Lance dataset directory for ``corpus_root``."""
        if self.lance_uri is not None:
            return self.lance_uri
        return corpus_root / "embeddings_lance"

    def expected_embedding_identity(self) -> tuple[str, int]:
        """Return ``(model_id, dim)`` without building the embedder.

        Dependency-free identity for the fail-loud store guard: callers that
        only need to BIND the store (e.g. the search path) can enforce the
        provider stamp without importing boto3.
        """
        return (self.embed_model_id, int(self.output_dimension))


__all__ = ["EmbedSettings"]
