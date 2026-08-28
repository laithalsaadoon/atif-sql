# SPDX-License-Identifier: Apache-2.0

"""Fail-loud embedding provider/dimension guard for the VSS bind path.

DELIBERATE TWIN of ``atif_embed.domain.embedding_guard``: atif-duck may never
import atif-embed (import-linter independence contract — only atif-cli
composes), and this is a tiny pure rule (string/int comparison, no I/O), so
each package carries its own copy rather than growing a shared-kernel package
for one function. ``test_guard_twin_pin.py`` is what keeps the copies honest.
The two copies are coupled through the CONTRACT's store-stamp semantics; a
behavioral change in one must be ported to the other.

The read/bind path (``register_vss``) reads the Lance store's stamped
``(model, dim)`` back and calls :func:`ensure_store_matches` BEFORE binding
the ``message_embeddings`` view — a store written by a different provider
raises rather than serving numerically valid but garbage cosine scores.
"""

from __future__ import annotations

#: Recovery instruction appended to the mismatch message. The store lives at
#: ``<corpus_root>/embeddings_lance`` by default, NOT under a fixed home
#: directory — naming a path the store isn't at makes the operator delete
#: nothing and stay broken. Kept identical to atif-embed's twin copy.
RECOVERY_HINT = (
    "Re-embed under the new provider: rm -rf the Lance store directory "
    "(ATIF_SQL_LANCE_URI, or <corpus-root>/embeddings_lance by default) "
    "and re-run `atif-sql embed --all --no-dry-run` (a bare `atif-sql embed` "
    "exits 64: a real run needs an explicit scope)."
)


class EmbeddingProviderMismatch(Exception):  # noqa: N818 — names a store state, not an "*Error"
    """Raised on a stamped-vs-active ``(model, dim)`` mismatch.

    The Lance store records the identity of the embedder that wrote it, and it
    differs from the identity of the embedder now asking to read or extend it.

    The name carries no ``Error`` suffix because it names a STATE OF THE STORE
    that is true before anything is attempted, not a fault that occurred while
    doing something. Nothing has gone wrong at runtime: two consistent stores
    simply cannot be the same store.

    Different embedding models produce vectors in incompatible spaces, so
    querying across a provider switch yields numerically valid but
    semantically garbage cosine scores. This error is terminal: the store
    must be dropped and re-embedded under the new provider.
    """


def ensure_store_matches(
    *,
    stored_model: str | None,
    stored_dim: int | None,
    expected_model: str,
    expected_dim: int | None,
) -> None:
    """Fail loud if a store's stamped identity differs from the active embedder.

    ``None`` for either stored value means the store is empty (fresh
    install) and any provider may claim it, so the check is a no-op.
    ``model_id`` is the primary identity; ``expected_dim`` is also checked
    when supplied (Cohere's single model id can emit different Matryoshka
    widths). ``expected_dim=None`` trusts ``model_id`` alone.
    """
    if stored_model is None or stored_dim is None:
        return
    model_ok = stored_model == expected_model
    dim_ok = expected_dim is None or stored_dim == expected_dim
    if model_ok and dim_ok:
        return
    # The message stays INSIDE the raise: `test_guard_twin_pin.py` reads both
    # twin modules as source text and requires `{RECOVERY_HINT}` to appear after
    # the `raise EmbeddingProviderMismatch` keyword, which is what proves the
    # shared constant reaches the operator rather than only sitting in the
    # module. Hoisting the message to a local satisfies EM102 / TRY003 and
    # defeats that pin, so both are suppressed here instead.
    raise EmbeddingProviderMismatch(  # noqa: TRY003
        "Embedding store was written by a different provider/model. "  # noqa: EM102
        f"stored=(model={stored_model!r}, dim={stored_dim}) "
        f"active=(model={expected_model!r}, dim={expected_dim}). "
        "Vectors from different models live in incompatible spaces (even at "
        "matching dimensions), so mixing them silently corrupts kNN search. "
        f"{RECOVERY_HINT}"
    )


__all__ = ["RECOVERY_HINT", "EmbeddingProviderMismatch", "ensure_store_matches"]
