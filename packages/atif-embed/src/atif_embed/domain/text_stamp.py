# SPDX-License-Identifier: Apache-2.0

"""Per-text invariants the store must record: the content stamp and the cap.

Two rules, both about not losing information silently.

**The stamp.** A step's uuid is stable across re-conversions but its flattened
text is not: a harbor or converter fix changes the text under the same uuid.
Keying the store on uuid alone therefore makes the first vector permanent —
the anti-join sees the uuid, calls it embedded, and semantic search keeps
ranking against pre-fix text with nothing in the store to reveal it. Every
row also stamps :func:`text_hash` of the exact text it was built from, so a
mismatch means STALE and the row is replaced.

**The cap.** Texts over :data:`MAX_EMBEDDABLE_CHARS` are sent head-only, so
their vector cannot match content past the cap. Every row records whether it
was capped, which turns an otherwise unexplainable search miss into an
attributable one.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b

#: 16 bytes (128 bit) of blake2b, hex-encoded. Collision risk is negligible
#: at corpus scale and the digest is a third the width of sha256 in a column
#: read in full on every discovery pass.
_DIGEST_BYTES = 16

#: Characters of a text that reach the embedder. Bedrock's body ceiling is
#: 20 MB and a full batch is 96 texts, so 50K each leaves headroom. Both the
#: row's ``truncated`` stamp and the adapter's wire-level clip read this one
#: constant, so the flag can never disagree with what was actually sent.
MAX_EMBEDDABLE_CHARS = 50_000


def text_hash(text: str) -> str:
    """Return the stable content stamp for one embeddable text."""
    return blake2b(text.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()


def clip_text(text: str) -> tuple[str, bool]:
    """Return ``(text_to_send, was_truncated)`` for one embeddable text."""
    if len(text) <= MAX_EMBEDDABLE_CHARS:
        return text, False
    return text[:MAX_EMBEDDABLE_CHARS], True


@dataclass(frozen=True, slots=True)
class PendingText:
    """One text the backfill must embed, with its staleness verdict.

    ``replaces_existing`` is True when the store already holds a row for
    this uuid under a DIFFERENT hash: that row must be deleted before the
    new vector is appended, or the uuid would fan out to two vectors and the
    kNN join would rank against both.
    """

    uuid: str
    text: str
    text_hash: str
    replaces_existing: bool

    @property
    def truncated(self) -> bool:
        """True when this text exceeds the cap and embeds head-only."""
        return len(self.text) > MAX_EMBEDDABLE_CHARS


__all__ = ["MAX_EMBEDDABLE_CHARS", "PendingText", "clip_text", "text_hash"]
