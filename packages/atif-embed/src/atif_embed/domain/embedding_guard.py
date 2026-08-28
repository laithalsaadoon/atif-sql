# SPDX-License-Identifier: Apache-2.0

"""The fail-loud embedding provider/dimension guard (pure domain rule).

Every embeddings-store row is stamped with the embedder's ``model_id`` and
``dimension``, and both the write path
(:func:`atif_embed.application.embed.run_backfill`) and the read/bind path
(``atif_duck.infrastructure.registry.register_vss``, via the caller-supplied
``expected_model`` / ``expected_dim``) read that stamp back and call
:func:`ensure_store_matches` before touching vectors. A provider switch is
destructive (different models live in incompatible vector spaces even at
matching widths), so the guard fails loud rather than silently corrupting
the kNN index.

DELIBERATE TWIN of ``atif_duck.domain.embedding_guard``: atif-duck may never
import atif-embed (independence contract) and this is a tiny pure rule, so
each package carries its own copy. A behavioral change in one — including
the recovery message — must be ported to the other.
"""

from __future__ import annotations

from atif_embed.domain.errors import EmbeddingProviderMismatch

#: Recovery instruction appended to the mismatch message. The store lives at
#: ``<corpus_root>/embeddings_lance`` by default (see
#: ``EmbedSettings.resolve_lance_uri``), NOT under a fixed home directory —
#: naming a path the store isn't at makes the operator delete nothing and
#: stay broken. Kept identical to atif-duck's twin copy.
RECOVERY_HINT = (
    "Re-embed under the new provider: rm -rf the Lance store directory "
    "(ATIF_SQL_LANCE_URI, or <corpus-root>/embeddings_lance by default) "
    "and re-run `atif-sql embed --all --no-dry-run` (a bare `atif-sql embed` "
    "exits 64: a real run needs an explicit scope)."
)


def ensure_store_matches(
    *,
    stored_model: str | None,
    stored_dim: int | None,
    expected_model: str,
    expected_dim: int | None,
) -> None:
    """Fail loud if a store's stamped identity differs from the active embedder.

    ``stored_model`` / ``stored_dim`` come from the Lance ``model`` / ``dim``
    columns (both stamped on every row). ``None`` for either means the store
    is empty (fresh install) and any provider may claim it, so the check is a
    no-op.

    ``model_id`` is the primary identity: it is globally unique and encodes
    the provider + model, so a match guarantees a compatible vector space.
    Cohere is the one provider whose single ``model_id`` can emit different
    Matryoshka widths, so ``expected_dim`` is also checked when supplied;
    ``expected_dim=None`` trusts ``model_id`` alone.

    On a genuine mismatch this raises :class:`EmbeddingProviderMismatch`
    naming both sides and the exact recovery command.
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


__all__ = ["RECOVERY_HINT", "ensure_store_matches"]
