# SPDX-License-Identifier: Apache-2.0

"""Domain error taxonomy for atif-embed.

Every failure that can reach the CLI must be a :class:`DomainError`: the
``embed`` command catches only that base class and turns it into a
classified ``{kind, exit_code, hint}`` envelope. Anything escaping the
taxonomy surfaces as a raw traceback instead.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for atif-embed domain errors.

    ``terminal`` distinguishes conditions no retry can clear (the store or
    its config needs an operator) from transient runs the next tick may
    succeed at. The CLI maps terminal errors to their own exit code so an
    unattended lane can stop retrying instead of burning identical ticks —
    the 2026-08-24 schema-stale condition retried 400+ times over 3 days
    with zero escalation because every failure exited alike.
    """

    #: True when retrying without operator action cannot succeed.
    terminal: bool = False


class EmbeddingProviderMismatch(DomainError):  # noqa: N818 — names a store state, not an "*Error"
    """Raised on a stamped-vs-active ``(model, dim)`` mismatch.

    The Lance store records the identity of the embedder that wrote it, and it
    differs from the identity of the embedder now asking to read or extend it.

    Different embedding models produce vectors in incompatible spaces, so
    appending or querying across a provider switch yields numerically valid
    but semantically garbage cosine scores. This error is terminal: the store
    must be dropped and re-embedded under the new provider.
    """

    terminal = True


class EmbeddingStoreSchemaStale(DomainError):  # noqa: N818 — names a store state, not an "*Error"
    """A Lance store's schema requires operator action before embeds can run.

    ADDITIVE drift no longer raises this: a store missing ``text_hash``
    migrates online via schema evolution + the sentinel staleness path (see
    ``infrastructure.lance_store``). The class stays in the taxonomy for
    schema states that genuinely cannot self-heal, and as the terminal
    exemplar the refresh lane's retry suppression keys on.
    """

    terminal = True


class EmbeddingProviderUnavailable(DomainError):  # noqa: N818 — names a provider state, not an "*Error"
    """The embedding backend could not be reached or refused the request.

    Covers what escapes tenacity's retry budget plus the non-retryable
    botocore failures (expired or denied credentials, a rejected request
    shape). Terminal for this run; the next run's staleness anti-join
    re-picks whatever never got written.
    """


class EmbeddingResponseInvalid(DomainError):  # noqa: N818 — names a response state, not an "*Error"
    """The backend answered, but not in the shape the adapter can read.

    Distinct from :class:`EmbeddingProviderUnavailable` because it points at
    a wire-format change rather than at credentials or capacity.
    """


__all__ = [
    "DomainError",
    "EmbeddingProviderMismatch",
    "EmbeddingProviderUnavailable",
    "EmbeddingResponseInvalid",
    "EmbeddingStoreSchemaStale",
]
