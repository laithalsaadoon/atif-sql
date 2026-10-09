# SPDX-License-Identifier: Apache-2.0

"""Build the embedder ``ATIF_SQL_EMBED_PROVIDER`` selects.

The one place that names a concrete :class:`~atif_embed.domain.ports.EmbeddingProvider`.
Each adapter is imported only when selected, so the Cohere path never loads
torch and the Gemma path never loads boto3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atif_embed.domain.ports import EmbeddingProvider
    from atif_embed.infrastructure.settings import EmbedSettings


def build_embedder(settings: EmbedSettings) -> EmbeddingProvider:
    """The selected provider's embedder, constructed without loading a model or a client."""
    if settings.embed_provider == "gemma":
        from atif_embed.infrastructure.gemma_local import EmbeddingGemmaLocalEmbedder

        return EmbeddingGemmaLocalEmbedder(settings)
    from atif_embed.infrastructure.cohere_bedrock import CohereBedrockEmbedder

    return CohereBedrockEmbedder(settings)


__all__ = ["build_embedder"]
