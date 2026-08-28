# SPDX-License-Identifier: Apache-2.0

"""Every port in :mod:`atif_embed.domain.ports` has a proven implementation.

A declared `Protocol` with nothing asserted against it is worse than no port
at all: it reads as a contract, the type checker never sees a candidate to
compare it to, and an adapter can lose a method without a single gate going
red. The three ports here are satisfied STRUCTURALLY, with no import in either
direction, so nothing else in the workspace connects them to their adapters.

Two assertions per port, because they fail on different mistakes:

* the annotated binding, which pyright and ty check statically — it catches a
  changed signature, a wrong return type, a property that became a method;
* the runtime member sweep, which catches an adapter that dropped a method
  while someone deleted the annotation along with it.

Nothing here reaches the network. Constructing each adapter is cheap and lazy:
the embedder holds settings, the store binds a path until first use, and the
text reader is stateless.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, get_protocol_members, is_protocol

from atif_embed.domain.ports import EmbeddingProvider, TextRowsPort, VectorStorePort
from atif_embed.infrastructure.cohere_bedrock import CohereBedrockEmbedder
from atif_embed.infrastructure.corpus_text_rows import DuckDbTextRows
from atif_embed.infrastructure.lance_store import LanceVectorStore
from atif_embed.infrastructure.settings import EmbedSettings

if TYPE_CHECKING:
    from pathlib import Path


def _protocol_members(protocol: type) -> frozenset[str]:
    """The member names a ``Protocol`` requires, via :mod:`typing`'s own reader.

    Asserting it is a protocol first means the sweep below cannot pass by
    reading zero members off a class that stopped being one.
    """
    assert is_protocol(protocol), f"{protocol.__name__} is not a Protocol"
    members = get_protocol_members(protocol)
    assert members, f"{protocol.__name__} declares no members"
    return members


def test_cohere_embedder_satisfies_embedding_provider() -> None:
    provider: EmbeddingProvider = CohereBedrockEmbedder(EmbedSettings())
    for member in _protocol_members(EmbeddingProvider):
        assert hasattr(provider, member), f"CohereBedrockEmbedder is missing {member}"


def test_lance_store_satisfies_vector_store_port(tmp_path: Path) -> None:
    store: VectorStorePort = LanceVectorStore(tmp_path / "vectors.lance", dim=1024)
    for member in _protocol_members(VectorStorePort):
        assert hasattr(store, member), f"LanceVectorStore is missing {member}"


def test_duckdb_text_rows_satisfies_text_rows_port() -> None:
    rows: TextRowsPort = DuckDbTextRows()
    for member in _protocol_members(TextRowsPort):
        assert hasattr(rows, member), f"DuckDbTextRows is missing {member}"
