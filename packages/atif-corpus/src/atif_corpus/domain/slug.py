# SPDX-License-Identifier: Apache-2.0

"""Map one corpus source root to a stable, human-legible directory key.

The slug is an ON-DISK NAME, not an internal identifier: it is the
``<slug>`` in ``~/.atif-sql/corpus/<slug>/``, so this function's output is
part of every installed user's filesystem layout.

Changing the algorithm therefore ORPHANS existing corpora rather than
migrating them. ``materialize`` starts writing a fresh tree, ``status``
reports an empty corpus, ``query`` binds zero sessions, and the watermarks
and LanceDB embedding store at the no-longer-computed path are stranded,
re-earned only by a full re-materialize and re-embed. ``tests/test_slug.py``
freezes concrete outputs so such a change fails a test, not a user's install.

If a change here is genuinely wanted, it needs all three of: the frozen
values in ``tests/test_slug.py`` updated, a migration for corpora already on
disk, and the two call sites that compose the path
(``_default_corpus_root`` in :mod:`atif_corpus.infrastructure.settings` and
the ``--corpus-root`` fallback in ``atif_cli.app``) reviewed together.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

#: Reserved slug for the interactive corpus at ``~/.claude`` — the root nearly
#: every user has. It is reserved rather than hashed so the common case has a
#: dirname a human can recognize in ``ls``.
DEFAULT_CORPUS_KEY = "default"

#: Reserved slug for the Codex rollout corpus at ``~/.codex/sessions``. Same
#: reasoning as :data:`DEFAULT_CORPUS_KEY`, and safe to add rather than a
#: breaking change: this workspace could not read a Codex rollout before
#: 2026-09-11, so no corpus can already occupy the hashed name this replaces
#: (``sessions-<8 hex>``). Reserved on the SESSIONS dir rather than on
#: ``~/.codex`` because the sessions dir is the source root a scan walks.
CODEX_CORPUS_KEY = "codex"

#: Any run of characters outside ``[a-z0-9]`` collapses to a single ``-``.
_SLUG_SANITIZE_RE = re.compile(r"[^a-z0-9]+")

#: Cap on the legible dirname component; the hash suffix carries uniqueness.
_SLUG_MAX_NAME_LEN = 32


def corpus_slug(corpus_root: Path | str) -> str:
    """Map one corpus root to a stable, human-legible directory key.

    Pure: no I/O beyond path resolution. The interactive corpus
    (``~/.claude``, however it is spelled) maps to the reserved key
    :data:`DEFAULT_CORPUS_KEY`. Every other root maps to
    ``<sanitized-dirname>-<8-hex sha256 of the resolved path>`` — legible
    enough to eyeball in ``ls``, hashed enough that two roots sharing a
    dirname (``…/alice/.claude`` vs ``…/bob/.claude``) never collide.

    The Codex rollout root (``~/.codex/sessions``) maps to
    :data:`CODEX_CORPUS_KEY` for the same legibility reason.

    The root is ``expanduser().resolve()``-normalized first so symlinked
    spellings of the same corpus agree on one key.
    """
    resolved = Path(corpus_root).expanduser().resolve()
    if resolved == Path("~/.claude").expanduser().resolve():
        return DEFAULT_CORPUS_KEY
    if resolved == Path("~/.codex/sessions").expanduser().resolve():
        return CODEX_CORPUS_KEY
    name = _SLUG_SANITIZE_RE.sub("-", resolved.name.lower()).strip("-")[:_SLUG_MAX_NAME_LEN]
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}" if name else digest
