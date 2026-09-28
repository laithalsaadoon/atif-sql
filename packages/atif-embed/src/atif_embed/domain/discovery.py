# SPDX-License-Identifier: Apache-2.0

"""Turn ordered ``(uuid, text)`` step rows into the texts that need embedding.

Every discovery source (the per-session trajectory reader and the lake reader)
yields the same thing: the corpus's embeddable step rows in corpus order,
``(primary uuid, flattened text)``. What happens next is one rule set, kept
here so the two sources can't drift apart:

* the first row for a uuid wins, so the store never gets two vectors for one
  key (a resumed session can repeat an earlier session's records);
* a uuid whose stored ``text_hash`` equals the current text's hash is already
  embedded and is skipped;
* a uuid stored under a different hash is STALE and comes back with
  ``replaces_existing=True``;
* ``limit`` caps the rows yielded, and it's applied AFTER the store
  comparison, so ``--limit N`` always makes N rows of progress.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atif_embed.domain.text_stamp import PendingText, text_hash

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


class PendingSelection:
    """One pass of the selection rules over one stream of step rows.

    :attr:`exhausted` turns true only when the stream ran out before the
    limit did. A caller that keeps a watermark reads it to tell a complete
    pass from one ``--limit`` cut short.
    """

    def __init__(self, *, embedded: dict[str, str] | None, limit: int | None) -> None:
        self._embedded = embedded or {}
        self._limit = limit
        self.exhausted = False
        self.yielded = 0

    def select(self, rows: Iterable[tuple[str, str]]) -> Iterator[PendingText]:
        """Yield the rows that need embedding, lazily."""
        limit = self._limit
        if limit is not None and limit <= 0:
            return
        seen: set[str] = set()
        for uuid, text in rows:
            if uuid in seen:
                continue
            seen.add(uuid)
            stamp = text_hash(text)
            stored = self._embedded.get(uuid)
            if stored == stamp:
                continue
            self.yielded += 1
            yield PendingText(
                uuid=uuid,
                text=text,
                text_hash=stamp,
                replaces_existing=stored is not None,
            )
            if limit is not None and self.yielded >= limit:
                return
        self.exhausted = True


__all__ = ["PendingSelection"]
